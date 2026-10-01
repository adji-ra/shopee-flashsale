"""Skenario stop/keamanan jalur Android (AndroidRunner + FakeDriver + FakeShopeeApp, waktu virtual).

- Captcha/verifikasi/OTP/"aktivitas tidak biasa" (pre-check, arm, polling, setelah Beli, langkah maju, ganti
  metode bayar, setelah "Buat Pesanan"): STOP semua runner (stop_event), alarm, tanpa retry, aplikasi dibiarkan
  apa adanya (tanpa back/tap/intent setelah terdeteksi; last_toast/clear_toast hanya operasi agent).
- Layar tak dikenal (WebView tanpa teks, layar asing, aplikasi lain, challenge yang hanya ada di content-desc):
  UNKNOWN_STATE dalam ~unknown_limit_s; diagnosa mahal hanya sekali saat eskalasi; tanpa klik ulang/reload.
- Habis (tombol, pesan Toast/overlay, banner "telah berakhir") / login / chip variasi "Habis" / banner
  "Flash Sale berakhir dalam ..", "Ingatkan Saya".
- Agent uiautomator2: AgentDead saat polling -> restart (maks 2); DriverError lain atau di luar polling ->
  hasil ERROR (precheck/arm tidak pernah melempar exception).
- Setelah "Buat Pesanan" diklik tanpa layar PIN: pesan wajib "Pesanan MUNGKIN sudah terbuat — ...".
Waktu di sini = waktu server (ms) relatif T (slot buka).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading

import pytest

from flashbuy import android_selectors
from flashbuy.android_driver import AgentDead, DriverError, Node
from flashbuy.android_runner import Screen
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus
from tests import android_harness
from tests.android_harness import assert_polling_rules, make_android, polling_actions, run_android
from tests.fake_android import FakeShopeeApp

BOTH = pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
VARIANTS = ["128GB Hitam", "256GB Biru"]
BANNER_LIVE = "Flash Sale berakhir dalam 01:59:59"  # hitung mundur saat sale BERJALAN (FakeShopeeApp._r_product)
MANDATORY = "Pesanan MUNGKIN sudah terbuat — cek status pesanan manual"

# Operasi driver yang mengubah layar aplikasi; selain ini hanya membaca/diagnosa.
ACTION_OPS = {"click", "start_url", "swipe_refresh", "press_back", "restart_agent"}
# last_toast/clear_toast: status Toast di agent uiautomator2 (tidak menyentuh aplikasi/Shopee).
READ_OPS = {"exists", "info", "find_all", "current_app", "webview", "screenshot", "dump", "agent_alive",
            "last_toast", "clear_toast"}
# Perintah shell yang menutup/mematikan aplikasi atau mengetik/menekan tombol (PIN tidak boleh diketik alat).
FORBIDDEN_SHELL = re.compile(r"force-stop|\bam\s+(kill|stop)|\bpm\s+clear|\binput\b|\bkill\b", re.I)


# ------------------------------------------------------------------ util


def _t(out) -> int:
    return int(out.open_at * 1000)


def _ops(out) -> list[str]:
    return [op for op, _ in out.driver.calls]


def _step_t(out, name: str) -> int:
    step = out.result.step(name)
    assert step is not None, f"langkah {name} tidak ada di {out.step_names()}"
    return step.t_server_ms


def _run(tmp_path, monkeypatch, *, app_cls=None, setup=None, **kw):
    """run_android dengan subkelas FakeShopeeApp dan/atau hook `setup(runner, app, driver)` sebelum run dimulai.

    Hook dipasang lewat make_android (sebelum event loop jalan) supaya tidak berlomba dengan thread precheck.
    """
    if app_cls is not None:
        monkeypatch.setattr(android_harness, "FakeShopeeApp", app_cls)
    if setup is not None:
        orig = android_harness.make_android

        def make(*a, **k):
            built = orig(*a, **k)
            runner, app, driver = built[:3]
            setup(runner, app, driver)
            return built

        monkeypatch.setattr(android_harness, "make_android", make)
    return run_android(tmp_path, **kw)


def _stop() -> dict:
    """runner_attrs dengan stop_event bersama (seolah ada runner lain yang ikut dihentikan)."""
    return {"stop_event": threading.Event()}


class _RecordingEvent(threading.Event):
    """stop_event bersama yang mencatat langkah RunLog runner saat pertama kali di-set (KAPAN runner lain
    dihentikan)."""

    def __init__(self):
        super().__init__()
        self.runner = None
        self.steps_at_set: list[str] | None = None

    def attach(self, runner) -> None:
        self.runner = runner

    def set(self) -> None:
        if self.steps_at_set is None and self.runner is not None:
            self.steps_at_set = [s.name for s in self.runner.log.steps]
        super().set()


def _no_order(out) -> None:
    assert out.kind("order") == [], "tidak boleh ada klik 'Buat Pesanan'"
    assert not out.runner.order_clicked
    assert "click_place_order" not in out.step_names()


def _one_buy(out) -> dict:
    """Tepat satu klik Beli (tanpa retry) - dikembalikan event-nya."""
    buys = out.kind("buy")
    assert len(buys) == 1, f"klik Beli harus tepat sekali, dapat {len(buys)}"
    assert out.kind("buy_disabled") == []
    assert out.step_names().count("click_buy") == 1
    return buys[0]


def _alarmed(out, status: RunStatus, message: str | None = None) -> None:
    assert out.events() == [str(status)], out.notifier.events
    assert out.notifier.events[0]["platform"] == "android"
    if message is not None:
        assert out.notifier.events[0]["message"] == message


def _left_as_is(out, since: dict, final_diag: bool = True) -> None:
    """Setelah terdeteksi (event `since`): tidak ada aksi apa pun ke aplikasi, hanya diagnosa baca.
    final_diag=False: run berhenti di pre-check (run_single tidak memanggil attempt -> tanpa diagnosa akhir)."""
    i = out.app.events.index(since)
    assert out.app.events[i + 1:] == [], f"aksi ke aplikasi setelah deteksi: {out.app.events[i + 1:]}"
    assert out.kind("back") == [], "tidak boleh menekan back"
    ops = _ops(out)
    assert "press_back" not in ops
    last_action = max(k for k, op in enumerate(ops) if op in ACTION_OPS)
    assert set(ops[last_action + 1:]) <= READ_OPS, set(ops[last_action + 1:]) - READ_OPS
    if final_diag:
        assert ops[-3:] == ["current_app", "screenshot", "dump"], ops[-6:]
    bad = [t for op, t in out.driver.calls if op == "shell" and FORBIDDEN_SHELL.search(t)]
    assert bad == [], f"perintah shell terlarang: {bad}"


def _result_json(out) -> dict:
    return json.loads((out.log_dir / "android-result.json").read_text(encoding="utf-8"))


def _log_text(out) -> str:
    return (out.log_dir / "android.log").read_text(encoding="utf-8")


# ------------------------------------------------------------------ subkelas aplikasi palsu


def _text_screen_app(label: str, *, as_desc: bool = False) -> type[FakeShopeeApp]:
    """Layar captcha/verifikasi dengan teks lain (atau hanya content-desc, mis. WebView beraksesibilitas)."""
    node = Node(desc=label, bounds=(40, 600, 680, 680)) if as_desc else Node(text=label, bounds=(40, 600, 680, 680))

    class App(FakeShopeeApp):
        def _r_captcha(self):
            return [("", node), ("", Node(cls="android.webkit.WebView", bounds=(0, 0, 720, 1612)))]

        def _r_verification(self):
            return [("", node)]

    return App


def _after_order_app(screen: str) -> type[FakeShopeeApp]:
    """Setelah 'Buat Pesanan' diklik aplikasi menampilkan `screen` (loading = spinner selamanya)."""

    class App(FakeShopeeApp):
        def _handle(self, key: str, node: Node) -> None:
            if key != "place_order":
                return super()._handle(key, node)
            self._event("order", self.payment_method)
            self.screen = screen
            self.loading_until = None  # spinner tidak pernah selesai

    return App


def _precheck_screen_app(screen: str, url_part: str) -> type[FakeShopeeApp]:
    """Intent pre-check (T-12 s di harness) ke URL yang memuat `url_part` berakhir di layar `screen`."""

    class App(FakeShopeeApp):
        def on_intent(self, url: str, package: str) -> None:
            super().on_intent(url, package)
            if url_part in url and self.now() < self.sc.open_at - 6:
                self.screen = screen

    return App


def _arm_screen_app(screen: str) -> type[FakeShopeeApp]:
    """Pre-check normal, tapi intent produk saat arm (menjelang T) berakhir di `screen`."""

    class App(FakeShopeeApp):
        def on_intent(self, url: str, package: str) -> None:
            super().on_intent(url, package)
            if self.screen == "product" and self.now() > self.sc.open_at - 10:
                self.screen = screen

    return App


class SlowSheetApp(FakeShopeeApp):
    """Bottom sheet baru tampil 150 ms setelah tap Beli (animasi); selama itu halaman produk masih terlihat."""

    SHEET_DELAY_S = 0.15

    def __init__(self, sc, clock):
        super().__init__(sc, clock)
        self.sheet_at: float | None = None

    def _tap_buy(self, node: Node) -> None:
        super()._tap_buy(node)
        if self.screen == "sheet":
            self.screen, self.sheet_at = "product", self.now() + self.SHEET_DELAY_S

    def _render(self):
        if self.sheet_at is not None and self.now() >= self.sheet_at:
            self.screen, self.sheet_at = "sheet", None
        return super()._render()


class OtpAtCheckoutApp(FakeShopeeApp):
    """Overlay OTP muncul di checkout 50 ms setelah halaman checkout dimuat."""

    def _r_checkout(self):
        out = super()._r_checkout()
        if self.now() >= self.checkout_at + 0.05:
            out.append(("", Node(text="Masukkan kode OTP yang dikirim ke +62 812****0000", bounds=(40, 700, 680, 760))))
        return out


def _challenge_on_tap_app(key: str, screen: str) -> type[FakeShopeeApp]:
    """Tap elemen `key` (confirm sheet / baris pembayaran / Checkout keranjang) berakhir di layar `screen`."""

    class App(FakeShopeeApp):
        def _handle(self, k: str, node: Node) -> None:
            if k != key:
                return super()._handle(k, node)
            self._event("challenge", f"{k}->{screen}")
            self.screen = screen

    return App


class OverlayCaptchaApp(FakeShopeeApp):
    """Dialog captcha di atas halaman produk saat slot buka (tombol Beli masih ada di hierarki)."""

    def _r_product(self):
        out = super()._r_product()
        if self.sale_active():
            out.append(("", Node(text="Geser untuk verifikasi", bounds=(100, 700, 620, 750))))
        return out


class DescCaptchaOverlayApp(FakeShopeeApp):
    """Dialog captcha di atas halaman produk saat slot buka yang HANYA ber-content-desc (mis. WebView)."""

    def _r_product(self):
        out = super()._r_product()
        if self.sale_active():
            out.append(("", Node(desc="Geser untuk verifikasi", bounds=(100, 700, 620, 750))))
        return out


class _AsyncToastApp(FakeShopeeApp):
    """Toast Android ASINKRON seperti di HP (tap -> handler aplikasi -> NotificationManager -> event aksesibilitas):
    baru tercatat di getLastToast `toast_delay_s` setelah tap (clear_toast runner terjadi sebelum klik).
    clearLastToast hanya menghapus toast yang sudah tercatat. toast_mode="node": overlay in-app."""

    toast_delay_s = 0.06

    def __init__(self, sc, clock):
        self._pending: tuple[str, float] | None = None
        self._recorded: str | None = None
        super().__init__(sc, clock)

    def _flush(self) -> None:
        if self._pending is not None and self.now() >= self._pending[1]:
            self._recorded, self._pending = self._pending[0], None

    def _show_toast(self, text: str) -> None:
        self.toast = (text, self.now() + self.toast_delay_s + 1.5)
        if self.sc.toast_mode == "toast":
            self._pending = (text, self.now() + self.toast_delay_s)

    @property
    def last_toast(self) -> str | None:
        self._flush()
        return self._recorded

    @last_toast.setter
    def last_toast(self, value: str | None) -> None:
        self._flush()
        self._recorded = value


class SoldOutMessageSheetApp(_AsyncToastApp):
    """Konfirmasi di sheet -> pesan 'Stok habis' tetapi sheet TETAP terbuka dan tidak ada tombol 'Habis': satu-
    satunya sinyal habis adalah pesan itu (Toast Android atau overlay node)."""

    def _confirm(self) -> None:
        if not self.sc.sold_out_on_confirm:
            return super()._confirm()
        self._event("sold_out_message")
        self._show_toast("Stok habis")


# deskripsi produk panjang (> MARKER_MAX_CHARS) yang kebetulan memuat kata penanda verifikasi ("kode OTP")
DESC_OTP = ("Deskripsi produk: garansi resmi 1 tahun. Demi keamanan akun, jangan pernah memberikan kode OTP kepada "
            "siapa pun termasuk penjual. Barang dikirim H+1 setelah pembayaran terverifikasi.")


def _with_description(base: type[FakeShopeeApp]) -> type[FakeShopeeApp]:
    """Halaman produk `base` + DESC_OTP di bawah nama produk (di urutan pohon SEBELUM overlay yang ditambahkan
    di akhir)."""

    class App(base):
        def _r_product(self):
            out = super()._r_product()
            out.insert(1, ("", Node(text=DESC_OTP, bounds=(20, 920, 700, 1100))))
            return out

    return App


class SaleEndedApp(FakeShopeeApp):
    """Banner halaman produk berganti 'Flash Sale telah berakhir' saat slot buka (kontrol positif)."""

    def _r_product(self):
        return [(k, Node(text="Flash Sale telah berakhir", bounds=n.bounds) if n.text == BANNER_LIVE else n)
                for k, n in super()._r_product()]


# ------------------------------------------------------------------ hook driver


def _phase(runner, name: str) -> bool:
    s = [st.name for st in runner.log.steps]
    return {
        "precheck": "precheck" in s and "arm" not in s,
        "arm": "page_open" in s and "armed" not in s,
        "polling": "poll_start" in s and "click_buy" not in s,
        "after_buy": "click_buy" in s and "buy_ok" not in s,
        "checkout": "checkout_loaded" in s and "place_order_gate" not in s,
        "place_order": "place_order_gate" in s and "result" not in s,
    }[name]


def _inject(phase: str, *, kill: bool, times: int = 1, ops: tuple[str, ...] = ("info", "find_all", "exists"),
            dead_error: bool | None = None):
    """Query berikutnya di fase `phase` gagal. kill=True: agent juga mati (HiOS).

    Jenis error mengikuti U2Driver: agent/transport mati -> AgentDead (HTTPError, UiAutomationNotConnectedError,
    socket ...); selain itu DriverError biasa. `dead_error` memaksa jenisnya (default: AgentDead bila kill)."""
    dead = kill if dead_error is None else dead_error

    def setup(runner, app, driver):
        orig = driver._rpc
        left = [times]
        driver.injected = []

        def rpc(op, target="", latency=None):
            if op in ops and left[0] > 0 and _phase(runner, phase):
                left[0] -= 1
                if kill:
                    driver.alive = False
                driver.injected.append((op, str(target)))
                cls = AgentDead if dead else DriverError
                driver.fail_next.append(cls(f"uiautomator2: koneksi ke agent putus ({op})"))
            return orig(op, target, latency)

        driver._rpc = rpc

    return setup


def _record_restarts(runner, driver) -> list[list[str]]:
    """Catat langkah RunLog pada setiap restart agent (untuk memastikan KAPAN restart terjadi)."""
    seen: list[list[str]] = []
    orig = driver.restart_agent

    def restart():
        seen.append([s.name for s in runner.log.steps])
        orig()

    driver.restart_agent = restart
    return seen


# ------------------------------------------------------------------ captcha / verifikasi setelah Beli


@BOTH
@pytest.mark.parametrize("flag, status, screen, evidence", [
    ("captcha_after_buy", RunStatus.CAPTCHA, "captcha", "Geser untuk verifikasi"),
    ("verification_after_buy", RunStatus.VERIFICATION, "verification", "aktivitas tidak biasa"),
], ids=["captcha", "verification"])
def test_challenge_after_buy_stops_all_without_retry(tmp_path, live, flag, status, screen, evidence):
    attrs = _stop()
    out = run_android(tmp_path, live=live, runner_attrs=attrs, **{flag: True})
    assert out.result.status == status, out.result.message
    assert evidence in out.result.message
    assert out.result.detail == ""
    buy = _one_buy(out)
    assert "no_response" not in out.step_names()
    # deteksi segera (bukan menunggu jaring pengaman UNKNOWN)
    assert _step_t(out, "result") - buy["t_server_ms"] < 300
    assert attrs["stop_event"].is_set(), f"{status} wajib menghentikan SEMUA runner"
    _alarmed(out, status)
    assert out.app.screen == screen, "layar captcha/verifikasi dibiarkan untuk diselesaikan manual"
    _left_as_is(out, buy)
    assert _ops(out).count("click") == 1
    _no_order(out)
    assert out.kind("confirm") == [] and out.kind("checkout") == []
    assert _result_json(out)["status"] == str(status)


@pytest.mark.parametrize("flag, label, status", [
    ("captcha_after_buy", "Geser untuk menyelesaikan puzzle", RunStatus.CAPTCHA),
    ("captcha_after_buy", "Verifikasi keamanan", RunStatus.CAPTCHA),
    ("captcha_after_buy", "Saya bukan robot", RunStatus.CAPTCHA),
    ("captcha_after_buy", "CAPTCHA", RunStatus.CAPTCHA),
    ("verification_after_buy", "Masukkan kode OTP", RunStatus.VERIFICATION),
    ("verification_after_buy", "Kode verifikasi telah dikirim ke +62 812****0000", RunStatus.VERIFICATION),
    ("verification_after_buy", "Verifikasi diperlukan", RunStatus.VERIFICATION),
    ("verification_after_buy", "Aktivitas mencurigakan terdeteksi", RunStatus.VERIFICATION),
    ("verification_after_buy", "Kami mendeteksi aktivitas yang tidak biasa", RunStatus.VERIFICATION),
])
@pytest.mark.parametrize("as_desc", [False, True], ids=["text", "content_desc"])
def test_challenge_texts_detected_in_text_or_content_desc(tmp_path, monkeypatch, flag, label, status, as_desc):
    """Teks challenge (text) -> CAPTCHA/VERIFICATION segera. Hanya content-desc (mis. WebView beraksesibilitas):
    dibaca di ujung rantai klasifikasi (sebelum layar dianggap UNKNOWN) -> status challenge yang sama, segera
    (bukan menunggu jaring UNKNOWN 1,5 s), stop semua + alarm + tanpa retry."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_text_screen_app(label, as_desc=as_desc), live=True,
               runner_attrs=attrs, **{flag: True})
    buy = _one_buy(out)
    dt = _step_t(out, "result") - buy["t_server_ms"]
    if as_desc:
        assert out.result.status == status, out.result.message
        assert "content-desc" in out.result.message and label[:40] in out.result.message
        assert dt < 600, f"challenge di content-desc harus terdeteksi tanpa menunggu jaring UNKNOWN, dapat {dt} ms"
        # tanpa diagnosa mahal (find_all content-desc) - cukup satu info descriptionMatches per klasifikasi
        assert not [t for op, t in out.driver.calls if op == "find_all" and t.startswith("descriptionMatches")]
        _alarmed(out, status)
    else:
        assert out.result.status == status, out.result.message
        assert label[:40] in out.result.message
        assert dt < 300, f"teks challenge harus terdeteksi segera, dapat {dt} ms"
        _alarmed(out, status)
    assert "no_response" not in out.step_names()
    assert attrs["stop_event"].is_set()
    _left_as_is(out, buy)
    _no_order(out)


@pytest.mark.parametrize("url_part", ["Ponsel-Uji-Coba", "/user/account/address", "/user/shopeepay"],
                         ids=["product", "address_page", "wallet_page"])
@pytest.mark.parametrize("screen, status", [("captcha", RunStatus.CAPTCHA), ("verification", RunStatus.VERIFICATION)])
def test_challenge_at_precheck_stops_all_without_raising(tmp_path, monkeypatch, url_part, screen, status):
    """precheck() tidak melempar: captcha/verifikasi di halaman produk/alamat/saldo saat pre-check -> hasil
    CAPTCHA/VERIFICATION, stop_event saat itu juga, tanpa intent berikutnya yang menimpa layar, tanpa arm/polling."""
    ev = _RecordingEvent()
    out = _run(tmp_path, monkeypatch, app_cls=_precheck_screen_app(screen, url_part),
               setup=lambda r, a, d: ev.attach(r), live=True, runner_attrs={"stop_event": ev})
    assert out.result.status == status, out.result.message
    assert out.result.message.startswith("pre-check: ") and str(status) in out.result.message
    assert out.step_names() == ["precheck"], "berhenti di pre-check: tanpa arm/polling"
    assert out.runner.open_at is None
    assert ev.is_set() and ev.steps_at_set == ["precheck"], "runner lain dihentikan saat pre-check"
    assert out.events() == ["precheck"], out.notifier.events
    assert url_part in out.kind("intent")[-1]["detail"], "layar challenge tidak ditimpa intent berikutnya"
    assert out.app.screen == screen
    _left_as_is(out, out.kind("intent")[-1], final_diag=False)
    assert _result_json(out)["status"] == str(status)
    assert polling_actions(out) == [] and out.kind("tap") == []
    _no_order(out)


@pytest.mark.parametrize("screen, status", [
    ("captcha", RunStatus.CAPTCHA),
    ("verification", RunStatus.VERIFICATION),
    ("login", RunStatus.LOGIN_REQUIRED),
])
def test_challenge_when_opening_product_at_arm_stops_without_polling(tmp_path, monkeypatch, screen, status):
    """Captcha/verifikasi saat buka produk (T-60 s): alarm + stop_event SEGERA (saat arm, bukan menunggu T),
    alarm hanya sekali, tanpa polling/klik, layar dibiarkan. Status akhir dengan stop_event bersama: lihat
    test_challenge_at_arm_with_shared_stop_event_reports_challenge_status."""
    ev = _RecordingEvent()
    out = _run(tmp_path, monkeypatch, app_cls=_arm_screen_app(screen), setup=lambda r, a, d: ev.attach(r),
               live=True, runner_attrs={"stop_event": ev})
    assert out.kind("buy") == [] and out.kind("tap") == []
    assert polling_actions(out) == [], "tanpa aksi polling sama sekali"
    assert "poll_start" not in out.step_names() and "click_buy" not in out.step_names()
    if status == RunStatus.LOGIN_REQUIRED:
        assert out.result.status == status, out.result.message
        assert out.result.message.startswith("saat membuka produk")
        assert not ev.is_set(), "login bukan status stop-semua"
    else:
        assert ev.is_set(), f"{status} saat arm wajib menghentikan SEMUA runner"
        assert ev.steps_at_set[-2:] == ["arm", "page_open"], f"stop_event harus di-set saat arm: {ev.steps_at_set}"
    _alarmed(out, status)
    assert out.notifier.events[0]["message"].startswith("saat membuka produk (T-60 s): teks ")
    assert out.app.screen == screen
    _left_as_is(out, out.kind("intent")[-1])
    _no_order(out)


@pytest.mark.parametrize("screen, status", [("captcha", RunStatus.CAPTCHA), ("verification", RunStatus.VERIFICATION)])
def test_challenge_at_arm_is_returned_by_attempt_and_alarms_once(tmp_path, monkeypatch, screen, status):
    """Tanpa runner lain (stop_event None): attempt() mengembalikan status dari arm; alarm dari arm tidak
    diulang oleh hasil akhir (dedup)."""
    out = _run(tmp_path, monkeypatch, app_cls=_arm_screen_app(screen), live=True)
    assert out.result.status == status, out.result.message
    assert out.result.message.startswith("saat membuka produk: teks ")
    _alarmed(out, status)
    assert polling_actions(out) == [] and out.kind("tap") == []
    assert _result_json(out)["status"] == str(status)
    _no_order(out)


@pytest.mark.parametrize("screen, status", [("captcha", RunStatus.CAPTCHA), ("verification", RunStatus.VERIFICATION)])
def test_challenge_at_arm_with_shared_stop_event_reports_challenge_status(tmp_path, monkeypatch, screen, status):
    out = _run(tmp_path, monkeypatch, app_cls=_arm_screen_app(screen), live=True, runner_attrs=_stop())
    assert out.result.status == status, f"{out.result.status}: {out.result.message}"
    assert "runner lain" not in out.result.message
    assert _result_json(out)["status"] == str(status)


def test_otp_overlay_at_checkout_stops_live_before_order(tmp_path, monkeypatch):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=OtpAtCheckoutApp, live=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.VERIFICATION, out.result.message
    assert "OTP" in out.result.message
    _one_buy(out)
    assert len(out.kind("checkout")) == 1
    _no_order(out)
    assert "place_order_gate" not in out.step_names()
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.VERIFICATION)
    assert out.kind("back") == [] and "press_back" not in _ops(out)
    assert out.app.screen == "checkout"


def test_captcha_overlay_on_product_page_stops_before_buy(tmp_path, monkeypatch):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=OverlayCaptchaApp, live=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.CAPTCHA, out.result.message
    assert out.kind("buy") == [] and out.kind("tap") == [], "tombol Beli di bawah dialog captcha tidak boleh diklik"
    assert polling_actions(out) == []
    assert 0 <= _step_t(out, "result") - _t(out) < 300
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.CAPTCHA)
    _left_as_is(out, out.kind("intent")[-1])
    _no_order(out)


@pytest.mark.parametrize("key, extra", [
    ("confirm", {}),
    ("cart_checkout", {"go_cart": True}),
], ids=["sheet_confirm", "cart_checkout"])
@pytest.mark.parametrize("screen, status", [
    ("captcha", RunStatus.CAPTCHA),
    ("verification", RunStatus.VERIFICATION),
])
def test_challenge_during_forward_steps_stops_all(tmp_path, monkeypatch, key, extra, screen, status):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_challenge_on_tap_app(key, screen), live=True, runner_attrs=attrs,
               **extra)
    assert out.result.status == status, out.result.message
    _one_buy(out)
    hit = out.kind("challenge")
    assert len(hit) == 1
    assert _step_t(out, "result") - hit[0]["t_server_ms"] < 300
    assert attrs["stop_event"].is_set()
    _alarmed(out, status)
    assert out.app.screen == screen
    _left_as_is(out, hit[0])
    _no_order(out)


@pytest.mark.parametrize("screen, status", [
    ("captcha", RunStatus.CAPTCHA),
    ("verification", RunStatus.VERIFICATION),
])
def test_challenge_when_switching_payment_stops_all(tmp_path, monkeypatch, screen, status):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_challenge_on_tap_app("payment_row", screen), live=True,
               payment_default="COD - Cek Dulu", runner_attrs=attrs)
    assert out.result.status == status, out.result.message
    assert attrs["stop_event"].is_set()
    _alarmed(out, status)
    hit = out.kind("challenge")
    assert len(hit) == 1 and _step_t(out, "result") - hit[0]["t_server_ms"] < 300
    assert "payment_change" in out.step_names() and "select_shopeepay" not in out.step_names()
    _one_buy(out)
    assert out.app.screen == screen
    _left_as_is(out, hit[0])
    _no_order(out)


# ------------------------------------------------------------------ captcha saat reload


@pytest.mark.parametrize("reload", ["swipe", "intent"])
def test_captcha_on_refresh_stops_right_after_reload(tmp_path, reload):
    attrs = _stop()
    out = run_android(tmp_path, live=True, live_update=False, captcha_on_refresh=True,
                      cfg={"android": {"reload": reload}}, runner_attrs=attrs)
    assert out.result.status == RunStatus.CAPTCHA, out.result.message
    acts = polling_actions(out)
    assert len(acts) == 1, f"hanya satu aksi polling (reload) yang boleh terjadi: {acts}"
    assert acts[0] >= _t(out) + 500, "reload pertama baru T+0,5 s"
    assert out.step_names().count("reload") == 1
    assert _step_t(out, "result") - acts[0] < 400, "captcha harus terdeteksi segera setelah reload"
    assert out.kind("buy") == [] and out.kind("tap") == []
    reload_event = [e for e in out.app.events if e["kind"] in ("refresh", "intent")][-1]
    assert reload_event["t_server_ms"] == acts[0]
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.CAPTCHA)
    assert out.app.screen == "captcha"
    _left_as_is(out, reload_event)
    _no_order(out)


# ------------------------------------------------------------------ layar tak dikenal


@BOTH
def test_webview_without_text_after_buy_is_unknown_state(tmp_path, live):
    attrs = _stop()
    out = run_android(tmp_path, live=live, webview_after_buy=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert "WebView" in out.result.message
    buy = _one_buy(out)
    dt = _step_t(out, "result") - buy["t_server_ms"]
    assert 1500 <= dt <= 2500, f"UNKNOWN_STATE setelah {dt} ms (batas default 1,5 s)"
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.UNKNOWN_STATE)
    assert out.app.screen == "webview"
    _left_as_is(out, buy)
    _no_order(out)


@BOTH
@pytest.mark.parametrize("flag, screen, evidence", [
    ("unknown_after_buy", "unknown", "tidak ada elemen yang dikenali, aplikasi aktif com.shopee.id/"),
    ("other_app_after_buy", "other_app", "aplikasi aktif com.android.launcher3/.Launcher, (aplikasi lain di depan)"),
], ids=["unknown", "other_app"])
def test_unknown_screen_after_buy_within_unknown_limit(tmp_path, live, flag, screen, evidence):
    attrs = {**_stop(), "unknown_limit_s": 0.5}
    out = run_android(tmp_path, live=live, runner_attrs=attrs, **{flag: True})
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.message.startswith("layar tidak dikenali > 0.5 s (")
    assert evidence in out.result.message
    buy = _one_buy(out)
    dt = _step_t(out, "result") - buy["t_server_ms"]
    # batas + paling lama ~2 iterasi klasifikasi UNKNOWN (10 query satu objek @20 ms) + diagnosa sekali
    assert 500 <= dt <= 1000, f"UNKNOWN_STATE harus ~0,5 s setelah klik, dapat {dt} ms"
    assert "no_response" not in out.step_names(), "layar tak dikenal bukan 'tidak ada reaksi' (tanpa klik ulang)"
    # diagnosa (aplikasi aktif, WebView, content-desc) hanya sekali saat eskalasi
    after = _ops(out)[_ops(out).index("click"):]
    assert after.count("webview") == 1 and after.count("current_app") == 2, after  # diagnosa + layar akhir
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.UNKNOWN_STATE)
    assert out.app.screen == screen, "aplikasi lain/layar asing dibiarkan (Shopee tidak dibuka ulang)"
    _left_as_is(out, buy)
    _no_order(out)


def test_unknown_screen_default_limit_is_1_5_s(tmp_path):
    out = run_android(tmp_path, unknown_after_buy=True, runner_attrs=_stop())
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert "> 1.5 s" in out.result.message
    buy = _one_buy(out)
    dt = _step_t(out, "result") - buy["t_server_ms"]
    assert 1500 <= dt <= 2200, dt
    _left_as_is(out, buy)


# ------------------------------------------------------------------ login & habis


@BOTH
def test_login_required_stops_at_precheck(tmp_path, live):
    attrs = _stop()
    out = run_android(tmp_path, live=live, login_required=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.LOGIN_REQUIRED, out.result.message
    assert "login" in out.result.message.lower()
    assert out.step_names() == ["precheck"], "berhenti di pre-check: tanpa arm/polling"
    intents = out.kind("intent")
    assert len(intents) == 1, "tidak ada intent produk lagi setelah pre-check"
    assert intents[0]["t_server_ms"] < _t(out) - 10_000
    assert out.runner.open_at is None, "arm() tidak boleh dipanggil"
    assert out.kind("buy") == [] and out.kind("tap") == [] and out.kind("back") == []
    assert not {"click", "press_back", "swipe_refresh"} & set(_ops(out)), "tidak mencoba login otomatis"
    assert out.app.screen == "login"
    assert out.events() == ["precheck"]
    _no_order(out)


@BOTH
def test_sold_out_at_open_without_click(tmp_path, live):
    out = run_android(tmp_path, live=live, sold_out=True, runner_attrs=_stop())
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    assert "Habis" in out.result.message
    assert out.kind("buy") == [] and out.kind("buy_disabled") == [] and out.kind("tap") == []
    assert "click" not in _ops(out)
    assert 0 <= _step_t(out, "result") - _t(out) < 400, "habis terdeteksi segera setelah slot buka"
    assert not out.runner.stop_event.is_set(), "SOLD_OUT bukan status stop-semua"
    _no_order(out)


@BOTH
def test_sold_out_on_confirm(tmp_path, live):
    out = run_android(tmp_path, live=live, sold_out_on_confirm=True)
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    buy = _one_buy(out)
    assert len(out.kind("confirm")) == 1
    assert out.kind("checkout") == []
    i = out.app.events.index(out.kind("confirm")[0])
    assert out.app.events[i + 1:] == [], "tidak ada klik ulang setelah stok habis"
    assert _step_t(out, "result") - buy["t_server_ms"] < 500
    _no_order(out)


@BOTH
@pytest.mark.parametrize("toast_mode", ["toast", "node"], ids=["android_toast", "overlay_node"])
def test_sold_out_message_with_sheet_still_open_stops_without_reconfirm(tmp_path, monkeypatch, toast_mode, live):
    """'Stok habis' setelah konfirmasi sementara sheet TETAP terbuka (tanpa tombol 'Habis'): pesan diprioritaskan
    di atas SHEET, baik sebagai Toast Android (getLastToast, asinkron setelah tap) maupun overlay node.
    Tanpa konfirmasi ulang, tanpa checkout."""
    out = _run(tmp_path, monkeypatch, app_cls=SoldOutMessageSheetApp, live=live, sold_out_on_confirm=True,
               toast_mode=toast_mode, runner_attrs=_stop())
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    channel = "toast" if toast_mode == "toast" else "teks"
    assert out.result.message == f"{channel} 'Stok habis'"
    buy = _one_buy(out)
    assert len(out.kind("confirm")) == 1, "tanpa konfirmasi ulang setelah stok habis"
    assert out.kind("checkout") == [] and out.app.screen == "sheet"
    msg = out.kind("sold_out_message")[0]
    assert out.app.events[out.app.events.index(msg) + 1:] == [], "tidak ada aksi lagi setelah pesan habis"
    assert _step_t(out, "result") - buy["t_server_ms"] < 500
    if toast_mode == "toast":
        assert out.app.last_toast == "Stok habis", "pesan hanya ada di kanal getLastToast"
        assert "last_toast" in _ops(out)
    assert not out.runner.stop_event.is_set(), "SOLD_OUT bukan status stop-semua"
    assert "press_back" not in _ops(out)
    _no_order(out)


def test_stale_toast_before_buy_click_is_not_read_as_reaction(tmp_path, monkeypatch):
    """Toast lama ('Stok habis' dari sebelum T) masih tercatat di getLastToast: clear_toast tepat sebelum klik
    Beli membuangnya, jadi tidak dibaca sebagai reaksi klik (bukan SOLD_OUT palsu)."""
    stale = "Stok habis"
    out = _run(tmp_path, monkeypatch, setup=lambda r, a, d: setattr(a, "last_toast", stale))
    # kontrol: toast ini memang akan dianggap habis bila dibaca sebagai reaksi
    assert out.runner._message_screen(Node(text=stale), None, "after_buy").screen == Screen.SOLD_OUT
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _one_buy(out)
    ops = _ops(out)
    first_click = ops.index("click")
    assert "clear_toast" in ops[first_click - 2:first_click]  # sebelum klik (mungkin + cek ulang tombol)
    assert "last_toast" not in ops[:first_click], "toast tidak dibaca di hot path polling"
    assert "last_toast" in ops[first_click:], "reaksi klik dibaca dari kanal toast"
    assert out.app.last_toast is None
    _no_order(out)


# ------------------------------------------------------------------ label "Habis" lain & banner


def _record_product_labels(seen: set[str]):
    """Hook: catat semua teks yang tampil di halaman produk selama polling (sebelum klik Beli)."""

    def setup(runner, app, driver):
        orig = app.nodes

        def nodes():
            out = orig()
            if app.screen == "product" and _phase(runner, "polling"):
                seen.update(n.label for n in out)
            return out

        app.nodes = nodes

    return setup


@pytest.mark.parametrize("live, extra", [
    (False, {}),
    (True, {}),
    (False, {"variants": VARIANTS, "variants_on_page": True, "cfg": {"variant": "256GB Biru"}}),
    (False, {"live_update": False}),
    (False, {"sale_skew_ms": 1500}),
], ids=["dry", "live", "variant_on_page", "static_ui", "skew1500"])
def test_other_variant_chip_habis_is_not_sold_out(tmp_path, monkeypatch, live, extra):
    seen: set[str] = set()
    out = _run(tmp_path, monkeypatch, setup=_record_product_labels(seen), live=live, variant_chip_sold_out=True,
               **extra)
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message
    assert "Habis" in seen and "Beli Sekarang" in seen, "chip 'Habis' harus terlihat saat polling"
    _one_buy(out)
    assert len(out.kind("checkout")) == 1
    assert_polling_rules(out)  # termasuk reload -> Beli (static_ui): slot dihitung dari akhir reload


@pytest.mark.parametrize("variant", ["slow_sheet", "no_response_click"])
def test_other_variant_chip_habis_right_after_buy_click_is_not_sold_out(tmp_path, monkeypatch, variant):
    if variant == "slow_sheet":
        out = _run(tmp_path, monkeypatch, app_cls=SlowSheetApp, variant_chip_sold_out=True)
    else:
        out = run_android(tmp_path, variant_chip_sold_out=True, no_response_clicks=1)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert len(out.kind("checkout")) == 1
    assert "SOLD_OUT" not in _log_text(out)
    if variant == "slow_sheet":
        _one_buy(out)
        assert "no_response" not in out.step_names()
    else:
        assert len(out.kind("buy")) == 2 and out.step_names().count("no_response") == 1
    assert_polling_rules(out)


def test_slow_sheet_without_habis_chip_is_ok(tmp_path, monkeypatch):
    """Kontrol untuk tes di atas: sheet lambat saja tidak masalah (tanpa klik ulang)."""
    out = _run(tmp_path, monkeypatch, app_cls=SlowSheetApp)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _one_buy(out)
    assert "no_response" not in out.step_names()


def test_other_variant_chip_habis_with_ingatkan_saya_button_before_open(tmp_path):
    out = run_android(tmp_path, variant_chip_sold_out=True, button_before_open="Ingatkan Saya")
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert _one_buy(out)["t_server_ms"] >= _t(out)
    assert_polling_rules(out)


def test_ingatkan_saya_button_before_open_on_time_is_ok(tmp_path):
    out = run_android(tmp_path, button_before_open="Ingatkan Saya", open_in_s=40,
                      runner_attrs={"open_timeout_s": 1.0})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert _one_buy(out)["t_server_ms"] >= _t(out)


@pytest.mark.parametrize("flash_banner", [True, False], ids=["banner", "button_only"])
def test_ingatkan_saya_button_with_late_slot_is_not_unknown_state(tmp_path, flash_banner):
    """NOT_STARTED dikenali di semua konteks ('Ingatkan Saya' / 'dimulai dalam' tanpa tombol Beli): slot yang
    telat > 1,5 s bukan UNKNOWN_STATE palsu (stop semua + alarm)."""
    out = run_android(tmp_path, button_before_open="Ingatkan Saya", sale_skew_ms=2500, open_in_s=40,
                      flash_banner=flash_banner, runner_attrs={"open_timeout_s": 1.0, **_stop()})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert _one_buy(out)["t_server_ms"] >= _t(out) + 2500
    assert not out.runner.stop_event.is_set()
    assert out.events() == []
    assert_polling_rules(out)


def test_flash_sale_banner_running_is_not_sold_out(tmp_path, monkeypatch):
    seen: set[str] = set()
    out = _run(tmp_path, monkeypatch, setup=_record_product_labels(seen))
    assert BANNER_LIVE in seen, "skenario default wajib menampilkan banner 'Flash Sale berakhir dalam ..'"
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _one_buy(out)
    assert "SOLD_OUT" not in _log_text(out)


def test_flash_sale_banner_classification_all_contexts(tmp_path):
    runner, app, *_, log, _notifier = make_android(tmp_path)
    asyncio.run(runner.prepare())
    app.screen = "product"
    app.clock.advance(13.0)  # lewat T: halaman produk live
    nodes = app.nodes()
    assert BANNER_LIVE in [n.text for n in nodes]
    for context in ("product", "after_buy", "any"):
        # bacaan langkah maju (node hasil find_all) dan rantai info/exists hot path
        assert runner._classify_nodes(nodes, context).screen == Screen.PRODUCT, context
        assert runner._classify(context).screen == Screen.PRODUCT, context
    sold = android_selectors.defaults().marker_re("sold_out")
    for text in (BANNER_LIVE, "FLASH SALE BERAKHIR DALAM 00:10:00", "Flash Sale dimulai dalam 00:00:05"):
        assert sold.fullmatch(text) is None, text
    for text in ("Habis", "Stok habis", "Flash Sale telah berakhir", "Flash Sale sudah berakhir"):
        assert sold.fullmatch(text) is not None, text
    log.close()


def test_flash_sale_ended_banner_with_buy_button_stops_without_click(tmp_path, monkeypatch):
    """Query bahaya hot path ikut memuat penanda kuat 'Flash Sale telah/sudah berakhir': banner itu dengan tombol
    Beli aktif & harga lolos lapis 1 -> SOLD_OUT segera saat slot buka, TANPA klik Beli (bukan setelah klik)."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=SaleEndedApp, live=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    assert out.result.message == "teks 'Flash Sale telah berakhir'"
    assert out.kind("buy") == [] and out.kind("tap") == [] and "click" not in _ops(out)
    assert _step_t(out, "result") - _t(out) < 300, "terbaca di iterasi polling pertama setelah slot buka"
    assert polling_actions(out) == [], "tanpa reload/klik"
    assert not attrs["stop_event"].is_set()  # SOLD_OUT bukan STOP_ALL
    _no_order(out)


@pytest.mark.parametrize("reload", ["swipe", "intent"])
def test_flash_sale_ended_with_normal_price_is_sold_out_without_click(tmp_path, monkeypatch, reload):
    """Realistis: flash sale berakhir -> harga kembali normal (> max_item_price): lapis 1 tidak mengklik dan banner
    'telah berakhir' terbaca langsung di query bahaya -> SOLD_OUT tanpa klik Beli dan tanpa reload."""
    out = _run(tmp_path, monkeypatch, app_cls=SaleEndedApp, live=True, flash_price=150_000,
               cfg={"android": {"reload": reload}})
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    assert "berakhir" in out.result.message
    assert out.kind("buy") == [] and out.kind("tap") == []
    assert "click" not in _ops(out)
    assert "reload" not in out.step_names() and polling_actions(out) == []
    assert _step_t(out, "result") - _t(out) < 300
    _no_order(out)


def test_desc_only_captcha_overlay_on_product_page_stops_quickly(tmp_path, monkeypatch):
    """Captcha yang hanya ber-content-desc dicek pra-klik paling lama tiap 0,5 s (bukan tiap iterasi, demi
    kecepatan klik di T) dan SEGERA setelah klik: paling banyak SATU klik Beli, lalu CAPTCHA sebelum sheet
    dikonfirmasi - walau dialog tidak menahan tap - tanpa klik ulang/konfirmasi/checkout, stop semua + alarm."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=DescCaptchaOverlayApp, live=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.CAPTCHA, out.result.message
    assert out.result.message.startswith("content-desc teks 'Geser untuk verifikasi'")
    assert len(out.kind("buy")) <= 1 and out.kind("confirm") == [] and out.kind("checkout") == []
    assert "no_response" not in out.step_names()
    assert _step_t(out, "result") - _t(out) < 1000
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.CAPTCHA)
    _no_order(out)


class DescCaptchaOnSheetApp(FakeShopeeApp):
    """Captcha ber-content-desc saja yang muncul DI ATAS bottom sheet 50 ms setelah sheet terbuka (setelah cek
    paksa di awal reaksi klik Beli)."""

    sheet_t: float | None = None

    def _r_sheet(self):
        out = super()._r_sheet()
        self.sheet_t = self.sheet_t if self.sheet_t is not None else self.now()
        if self.now() >= self.sheet_t + 0.05:
            out.append(("", Node(desc="Geser untuk verifikasi", bounds=(100, 1000, 620, 1060))))
        return out


def test_desc_only_captcha_over_sheet_blocks_confirm(tmp_path, monkeypatch):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=DescCaptchaOnSheetApp, live=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.CAPTCHA, out.result.message
    assert "content-desc" in out.result.message
    _one_buy(out)
    assert out.kind("confirm") == [] and out.kind("checkout") == [], "konfirmasi sheet tidak boleh di bawah captcha"
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.CAPTCHA)
    _no_order(out)


def test_long_description_with_marker_words_is_not_a_challenge(tmp_path, monkeypatch):
    """Kontrol: teks panjang (> 120 karakter, mis. deskripsi produk) yang menyebut 'kode OTP' bukan penanda
    verifikasi (tidak stop palsu)."""
    out = _run(tmp_path, monkeypatch, app_cls=_with_description(FakeShopeeApp), live=True, runner_attrs=_stop())
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    assert not out.runner.stop_event.is_set()


def test_long_description_does_not_mask_captcha_dialog_on_product_page(tmp_path, monkeypatch):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_with_description(OverlayCaptchaApp), live=True, runner_attrs=attrs)
    assert out.kind("buy") == [] and out.kind("tap") == [], "tombol Beli di bawah dialog captcha tidak boleh diklik"
    assert out.result.status == RunStatus.CAPTCHA, out.result.message
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.CAPTCHA)
    _no_order(out)


_MASKED_SOLD_OUT = pytest.mark.parametrize("scenario", [
    {"sold_out": True},
    {"sold_out_on_confirm": True},
    {"sold_out_on_confirm": True, "toast_mode": "node"},
], ids=["at_open", "on_confirm_toast", "on_confirm_node"])


@_MASKED_SOLD_OUT
def test_other_variant_chip_habis_does_not_mask_real_sold_out(tmp_path, scenario):
    out = run_android(tmp_path, variant_chip_sold_out=True, runner_attrs=_stop(), **scenario)
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    assert not out.runner.stop_event.is_set()


@_MASKED_SOLD_OUT
def test_other_variant_chip_habis_with_real_sold_out_never_retries_or_orders(tmp_path, scenario):
    """Live: chip variasi lain 'Habis' tidak menutupi sinyal habis -> SOLD_OUT tanpa klik ulang Beli, tanpa
    reload, tanpa konfirmasi ulang/checkout/pesanan, tanpa stop-semua."""
    out = run_android(tmp_path, live=True, variant_chip_sold_out=True, runner_attrs=_stop(), **scenario)
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    assert not out.runner.stop_event.is_set()
    assert len(out.kind("buy")) == (0 if scenario.get("sold_out") else 1) and out.kind("buy_disabled") == []
    assert len(out.kind("confirm")) == (0 if scenario.get("sold_out") else 1)
    assert out.kind("refresh") == [] and out.kind("checkout") == []
    assert _step_t(out, "result") <= _t(out) + 2500
    _no_order(out)


def test_other_variant_chip_habis_does_not_mask_ingatkan_saya_late_slot(tmp_path):
    out = run_android(tmp_path, button_before_open="Ingatkan Saya", variant_chip_sold_out=True, flash_banner=False,
                      sale_skew_ms=2500, open_in_s=40, runner_attrs={"open_timeout_s": 1.0, **_stop()})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert not out.runner.stop_event.is_set()


# ------------------------------------------------------------------ agent uiautomator2 (HiOS)


def test_precheck_logs_device_props_and_hios(tmp_path):
    out = run_android(tmp_path, driver_kw={"wm_size": "Physical size: 1080x2460\nOverride size: 720x1640"})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    log = _log_text(out)
    assert "device ro.tranos.version = hios13.6.0" in log
    assert "device ro.build.version.release = 13" in log
    assert "device wm size = Physical size: 1080x2460; Override size: 720x1640" in log, "resolusi dibaca dari device"
    assert "device wm density = Physical density: 320" in log
    assert "HiOS/Transsion terdeteksi" in log
    assert "precheck device: OK - TECNO TECNO BG6 Android 13 (SDK 33), Physical size: 1080x2460" in log
    assert "precheck agent uiautomator2: OK - hidup" in log
    shells = [t for op, t in out.driver.calls if op == "shell"]
    assert "getprop ro.tranos.version" in shells and "wm size" in shells
    assert out.runner.device_info["ro.tranos.version"] == "hios13.6.0"


def test_precheck_non_hios_device_not_flagged(tmp_path, monkeypatch):
    props = {"ro.product.brand": "samsung", "ro.product.manufacturer": "samsung", "ro.product.model": "SM-A155F",
             "ro.build.version.release": "14", "ro.build.version.sdk": "34"}
    out = _run(tmp_path, monkeypatch, setup=lambda r, a, d: setattr(d, "props", props))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    log = _log_text(out)
    assert "device ro.product.brand = samsung" in log
    assert "ro.tranos.version" not in log, "prop kosong tidak dicatat"
    assert "HiOS/Transsion terdeteksi" not in log
    assert "Android 14 (SDK 34), Physical size: 720x1612" in log


def test_agent_dead_at_precheck_is_restarted_with_warning(tmp_path):
    out = run_android(tmp_path, driver_kw={"alive": False})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert out.driver.restarts == 1
    log = _log_text(out)
    assert "precheck agent uiautomator2: PERINGATAN - mati lalu dihidupkan ulang - kemungkinan dibunuh HiOS" in log
    _one_buy(out)


def test_agent_killed_before_arm_is_restarted_and_run_ok(tmp_path, monkeypatch):
    def setup(runner, app, driver):
        orig = driver._rpc
        killed = []

        def rpc(op, target="", latency=None):
            if not killed and [s.name for s in runner.log.steps][-1:] == ["arm"]:
                killed.append(op)
                driver.alive = False  # HiOS membunuh agent antara pre-check dan arm (T-60 s)
            return orig(op, target, latency)

        driver._rpc = rpc
        driver._restart_log = _record_restarts(runner, driver)

    out = _run(tmp_path, monkeypatch, setup=setup, live=True)
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    assert out.driver.restarts == 1
    assert [steps[-1] for steps in out.driver._restart_log] == ["arm"], "restart terjadi saat arm, sebelum buka produk"
    log = _log_text(out)
    assert "precheck agent uiautomator2: OK - hidup" in log
    assert "arm: agent uiautomator2 mati (HiOS?), menghidupkan ulang" in log
    assert "arm: agent hidup lagi (True)" in log
    _one_buy(out)
    assert len(out.kind("order")) == 1
    assert_polling_rules(out)


def test_agent_killed_during_polling_is_restarted_and_continues(tmp_path, monkeypatch):
    def setup(runner, app, driver):
        _inject("polling", kill=True, ops=("info",))(runner, app, driver)
        driver._restart_log = _record_restarts(runner, driver)

    out = _run(tmp_path, monkeypatch, setup=setup)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert len(out.driver.injected) == 1
    assert out.driver.restarts == 1
    assert [steps[-1] for steps in out.driver._restart_log] == ["poll_start"]
    log = _log_text(out)
    assert "agent uiautomator2 tidak menjawab saat polling (HiOS?): uiautomator2: koneksi ke agent putus" in log
    assert "restart agent #1" in log
    buy = _one_buy(out)
    assert buy["t_server_ms"] >= _t(out)
    # restart ~2 s -> reload lalu Beli: RateLimiter.touch(akhir reload) menjaga jarak >= 400 ms di aplikasi
    assert_polling_rules(out)
    slots = out.runner.limiter.history
    assert all(b - a >= 0.4 for a, b in zip(slots, slots[1:], strict=False)), slots
    _no_order(out)


def test_agent_restart_limit_during_polling_then_error(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, setup=_inject("polling", kill=True, times=3, ops=("info",)), live=True)
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert out.result.message.startswith("driver: ")
    assert len(out.driver.injected) == 3
    assert out.driver.restarts == 2, "restart agent maksimal 2 kali"
    assert out.kind("buy") == []
    assert _step_t(out, "result") <= _t(out) + 8000
    _no_order(out)


@pytest.mark.parametrize("phase", ["after_buy", "checkout"])
def test_agent_dead_after_buy_is_error_without_restart_or_reclick(tmp_path, monkeypatch, phase):
    """Agent mati SETELAH klik Beli (langkah maju): tidak di-restart di tengah alur dan Beli tidak diklik ulang;
    hasil ERROR (status layar tidak pasti -> manual)."""
    out = _run(tmp_path, monkeypatch, setup=_inject(phase, kill=True, ops=("info", "find_all")), live=True,
               runner_attrs=_stop())
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert out.result.message.startswith("driver: uiautomator2: koneksi ke agent putus")
    assert len(out.driver.injected) == 1
    assert out.driver.restarts == 0, "restart hanya saat polling"
    _one_buy(out)
    assert not out.runner.stop_event.is_set()
    _no_order(out)


@pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
@pytest.mark.parametrize("phase", ["polling", "after_buy", "checkout"])
def test_driver_error_with_agent_alive_is_error_result(tmp_path, monkeypatch, phase, live):
    out = _run(tmp_path, monkeypatch, setup=_inject(phase, kill=False), live=live, runner_attrs=_stop())
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert out.result.message.startswith("driver: uiautomator2: koneksi ke agent putus")
    assert len(out.driver.injected) == 1
    # bukan AgentDead (agent hidup): tidak di-restart, juga saat polling
    assert out.driver.restarts == 0 and "restart_agent" not in _ops(out), "agent hidup: tidak di-restart"
    assert "restart agent #" not in _log_text(out)
    assert len(out.kind("buy")) == (0 if phase == "polling" else 1), "tanpa klik ulang Beli"
    assert not out.runner.stop_event.is_set()
    assert _result_json(out)["status"] == "ERROR"
    assert out.result.screenshots, "diagnosa akhir tetap jalan"
    _no_order(out)


@pytest.mark.parametrize("kill", [False, True], ids=["driver_error", "agent_dead"])
@pytest.mark.parametrize("phase", ["precheck", "arm"])
def test_driver_error_before_attempt_is_error_result_not_crash(tmp_path, monkeypatch, phase, kill):
    """precheck()/arm() tidak pernah melempar: kegagalan query (info/exists di _wait_screen) jadi hasil ERROR
    dengan result.json & alarm; tanpa polling/klik, tanpa restart di luar polling, tanpa stop-semua."""
    runners = []

    def setup(runner, app, driver):
        runners.append(runner)
        _inject(phase, kill=kill, ops=("info", "exists", "find_all"))(runner, app, driver)

    try:
        out = _run(tmp_path, monkeypatch, setup=setup, live=True, runner_attrs=_stop())
    except DriverError as e:
        runners[0].log.close()  # harness tidak menutup log bila run melempar exception
        pytest.fail(f"DriverError di {phase} keluar dari runner: {e}")
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert "koneksi ke agent putus" in out.result.message
    assert len(out.driver.injected) == 1
    assert out.driver.restarts == 0
    assert out.kind("buy") == [] and out.kind("tap") == [] and polling_actions(out) == []
    assert "poll_start" not in out.step_names()
    assert not out.runner.stop_event.is_set(), "ERROR bukan status stop-semua"
    assert _result_json(out)["status"] == "ERROR"
    if phase == "precheck":
        assert out.step_names() == ["precheck"]
        assert out.result.message.startswith("pre-check: device: koneksi device/agent gagal")
        _alarmed(out, "precheck")
    else:
        assert out.result.message.startswith("saat membuka produk (T-60 s): ")
        _alarmed(out, RunStatus.ERROR)  # alarm sekali saat arm; hasil akhir tidak alarm ulang
    _no_order(out)


# ------------------------------------------------------------------ setelah "Buat Pesanan" diklik


def _after_order_common(out, attrs) -> dict:
    assert out.result.message == MANDATORY == MAYBE_ORDERED_MSG
    orders = out.kind("order")
    assert len(orders) == 1, "tidak boleh pesan dua kali"
    assert out.step_names().count("click_place_order") == 1
    assert attrs["stop_event"].is_set()
    _alarmed(out, out.result.status, MANDATORY)
    _left_as_is(out, orders[0])
    data = _result_json(out)
    assert data["message"] == MANDATORY and data["detail"] == out.result.detail
    return orders[0]


def test_spinner_forever_after_order_is_maybe_ordered(tmp_path, monkeypatch):
    attrs = {**_stop(), "flow_timeout_s": 2.0}
    out = _run(tmp_path, monkeypatch, app_cls=_after_order_app("loading"), live=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.detail.startswith("TIMEOUT: layar PIN tidak muncul"), out.result.detail
    order = _after_order_common(out, attrs)
    dt = _step_t(out, "result") - _step_t(out, "buy_ok")
    assert 1950 <= dt <= 2500, f"batas alur (flow_timeout_s=2 s) sejak Beli berhasil, dapat {dt} ms"
    assert _step_t(out, "result") > order["t_server_ms"]
    assert out.app.screen == "loading"


def test_spinner_forever_after_order_default_flow_timeout_30_s(tmp_path, monkeypatch):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_after_order_app("loading"), live=True, runner_attrs=attrs)
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.detail.startswith("TIMEOUT:")
    _after_order_common(out, attrs)
    dt = _step_t(out, "result") - _step_t(out, "buy_ok")
    assert 29_900 <= dt <= 30_500, dt


@pytest.mark.parametrize("screen, status, evidence", [
    ("captcha", RunStatus.CAPTCHA, "Geser untuk verifikasi"),
    ("verification", RunStatus.VERIFICATION, "aktivitas tidak biasa"),
])
def test_challenge_after_order_keeps_status_with_mandatory_message(tmp_path, monkeypatch, screen, status, evidence):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_after_order_app(screen), live=True, runner_attrs=attrs)
    assert out.result.status == status, out.result.message
    assert out.result.detail.startswith(f"{status}: ") and evidence in out.result.detail
    order = _after_order_common(out, attrs)
    assert _step_t(out, "result") - order["t_server_ms"] < 300, "terdeteksi segera, tanpa menunggu batas alur"
    assert out.app.screen == screen


def test_driver_error_on_place_order_click_is_maybe_ordered(tmp_path, monkeypatch):
    """Klik 'Buat Pesanan' gagal di tengah RPC: bisa saja sudah sampai ke HP -> tetap pesan wajib."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, setup=_inject("place_order", kill=False, ops=("click",)), live=True,
               runner_attrs=attrs)
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.message == MANDATORY
    assert out.result.detail.startswith("ERROR: driver: ")
    assert out.runner.order_clicked
    assert attrs["stop_event"].is_set()
    _alarmed(out, RunStatus.UNKNOWN_STATE, MANDATORY)
    assert out.step_names().count("place_order_gate") == 1
    assert len([op for op, t in out.driver.calls if op == "click" and t == "Buat Pesanan"]) == 1, "tanpa klik ulang"
    assert "press_back" not in _ops(out)
