"""Skenario stop/keamanan jalur Android (AndroidRunner + FakeDriver + FakeShopeeApp, waktu virtual).

- Captcha/verifikasi/OTP/"aktivitas tidak biasa": STOP semua runner (stop_event), alarm, tanpa retry,
  aplikasi dibiarkan apa adanya (tanpa back/tap/intent setelah terdeteksi).
- Layar tak dikenal (WebView tanpa teks, layar asing, aplikasi lain): UNKNOWN_STATE dalam ~unknown_limit_s.
- Habis / login / chip variasi "Habis" / banner "Flash Sale berakhir dalam ..".
- Agent uiautomator2 dibunuh HiOS: dihidupkan ulang; DriverError lain -> hasil ERROR, bukan crash.
- Setelah "Buat Pesanan" diklik tanpa layar PIN: pesan wajib "Pesanan MUNGKIN sudah terbuat — ...".
Waktu di sini = waktu server (ms) relatif T (slot buka).
"""

from __future__ import annotations

import json
import re
import threading

import pytest

from flashbuy import android_selectors
from flashbuy.android_driver import DriverError, Node
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
READ_OPS = {"exists", "info", "find_all", "current_app", "webview", "screenshot", "dump", "agent_alive"}
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


def _left_as_is(out, since: dict) -> None:
    """Setelah terdeteksi (event `since`): tidak ada aksi apa pun ke aplikasi, hanya diagnosa baca."""
    i = out.app.events.index(since)
    assert out.app.events[i + 1:] == [], f"aksi ke aplikasi setelah deteksi: {out.app.events[i + 1:]}"
    assert out.kind("back") == [], "tidak boleh menekan back"
    ops = _ops(out)
    assert "press_back" not in ops
    last_action = max(k for k, op in enumerate(ops) if op in ACTION_OPS)
    assert set(ops[last_action + 1:]) <= READ_OPS, set(ops[last_action + 1:]) - READ_OPS
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


def _inject(phase: str, *, kill: bool, times: int = 1, ops: tuple[str, ...] = ("info", "find_all", "exists")):
    """Query berikutnya di fase `phase` gagal dengan DriverError; kill=True: agent juga mati (HiOS)."""

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
                driver.fail_next.append(DriverError(f"uiautomator2: koneksi ke agent putus ({op})"))
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
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_text_screen_app(label, as_desc=as_desc), live=True,
               runner_attrs=attrs, **{flag: True})
    assert out.result.status == status, out.result.message
    assert label[:40] in out.result.message
    if as_desc:
        assert "content-desc" in out.result.message
    buy = _one_buy(out)
    # content-desc baru diperiksa saat layar tak dikenali (> 0,3 s), tetap jauh sebelum jaring pengaman 1,5 s
    assert _step_t(out, "result") - buy["t_server_ms"] < (1000 if as_desc else 300)
    assert attrs["stop_event"].is_set()
    _alarmed(out, status)
    _left_as_is(out, buy)
    _no_order(out)


@pytest.mark.parametrize("screen, status", [
    ("captcha", RunStatus.CAPTCHA),
    ("verification", RunStatus.VERIFICATION),
    ("login", RunStatus.LOGIN_REQUIRED),
])
def test_challenge_when_opening_product_at_arm_stops_without_polling(tmp_path, monkeypatch, screen, status):
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_arm_screen_app(screen), live=True, runner_attrs=attrs)
    assert out.result.status == status, out.result.message
    assert "saat membuka produk" in out.result.message
    assert out.kind("buy") == [] and out.kind("tap") == []
    assert polling_actions(out) == [], "tanpa aksi polling sama sekali"
    assert "poll_start" not in out.step_names() and "click_buy" not in out.step_names()
    assert attrs["stop_event"].is_set() == (status != RunStatus.LOGIN_REQUIRED)
    _alarmed(out, status)
    assert out.app.screen == screen
    _left_as_is(out, out.kind("intent")[-1])
    _no_order(out)


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


@pytest.mark.xfail(strict=True, reason=(
    "BUG: AndroidRunner._ensure_shopeepay - loop menunggu opsi ShopeePay (setelah tap 'Metode Pembayaran') hanya "
    "memanggil _find('payment_shopeepay') tanpa klasifikasi layar; captcha/verifikasi di titik ini tidak terdeteksi "
    "-> setelah 3 s hasil ERROR 'opsi ShopeePay tidak ditemukan': tanpa stop_event (runner lain jalan terus) dan "
    "tanpa alarm (ERROR bukan status alarm)"))
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
    ("unknown_after_buy", "unknown", "teks tak dikenali"),
    ("other_app_after_buy", "other_app", "com.android.launcher3"),
], ids=["unknown", "other_app"])
def test_unknown_screen_after_buy_within_unknown_limit(tmp_path, live, flag, screen, evidence):
    attrs = {**_stop(), "unknown_limit_s": 0.5}
    out = run_android(tmp_path, live=live, runner_attrs=attrs, **{flag: True})
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert "> 0.5 s" in out.result.message and evidence in out.result.message
    buy = _one_buy(out)
    dt = _step_t(out, "result") - buy["t_server_ms"]
    assert 500 <= dt <= 900, f"UNKNOWN_STATE harus ~0,5 s setelah klik, dapat {dt} ms"
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
    if extra.get("live_update", True):  # jarak reload->Beli: bug terpisah, xfail di test_android_runner.py
        assert_polling_rules(out)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: AndroidRunner._classify_nodes - di konteks 'after_buy' SEMUA penanda 'Habis' dianggap SOLD_OUT "
    "(`context != 'product'`), termasuk chip variasi LAIN di halaman produk yang masih terlihat sesaat setelah "
    "tap Beli (animasi bottom sheet / klik belum direspons) -> run berhenti SOLD_OUT palsu ~20 ms setelah klik"))
@pytest.mark.parametrize("variant", ["slow_sheet", "no_response_click"])
def test_other_variant_chip_habis_right_after_buy_click_is_not_sold_out(tmp_path, monkeypatch, variant):
    if variant == "slow_sheet":
        out = _run(tmp_path, monkeypatch, app_cls=SlowSheetApp, variant_chip_sold_out=True)
    else:
        out = run_android(tmp_path, variant_chip_sold_out=True, no_response_clicks=1)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert len(out.kind("checkout")) == 1


def test_slow_sheet_without_habis_chip_is_ok(tmp_path, monkeypatch):
    """Kontrol untuk tes xfail di atas: sheet lambat saja tidak masalah (tanpa klik ulang)."""
    out = _run(tmp_path, monkeypatch, app_cls=SlowSheetApp)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _one_buy(out)
    assert "no_response" not in out.step_names()


@pytest.mark.xfail(strict=True, reason=(
    "BUG: AndroidRunner._classify_nodes - bila tombol Beli belum ada (sebelum slot buka tombolnya 'Ingatkan Saya'), "
    "cabang `buy is None` membuat chip variasi lain berlabel 'Habis' dianggap produk habis -> SOLD_OUT sebelum T"))
def test_other_variant_chip_habis_with_ingatkan_saya_button_before_open(tmp_path):
    out = run_android(tmp_path, variant_chip_sold_out=True, button_before_open="Ingatkan Saya")
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _one_buy(out)


def test_ingatkan_saya_button_before_open_on_time_is_ok(tmp_path):
    out = run_android(tmp_path, button_before_open="Ingatkan Saya", open_in_s=40,
                      runner_attrs={"open_timeout_s": 1.0})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert _one_buy(out)["t_server_ms"] >= _t(out)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: AndroidRunner._classify_nodes - penanda not_started ('Ingatkan Saya', 'Flash Sale dimulai dalam') hanya "
    "dikenali di konteks 'after_buy'; di konteks 'product' halaman produk yang belum dibuka (tombol 'Ingatkan Saya') "
    "jadi UNKNOWN -> slot telat > 1,5 s memicu UNKNOWN_STATE palsu (stop semua runner + alarm) di T+1,5 s"))
def test_ingatkan_saya_button_with_late_slot_is_not_unknown_state(tmp_path):
    out = run_android(tmp_path, button_before_open="Ingatkan Saya", sale_skew_ms=2500, open_in_s=40,
                      runner_attrs={"open_timeout_s": 1.0, **_stop()})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert _one_buy(out)["t_server_ms"] >= _t(out) + 2500
    assert not out.runner.stop_event.is_set()


def test_flash_sale_banner_running_is_not_sold_out(tmp_path, monkeypatch):
    seen: set[str] = set()
    out = _run(tmp_path, monkeypatch, setup=_record_product_labels(seen))
    assert BANNER_LIVE in seen, "skenario default wajib menampilkan banner 'Flash Sale berakhir dalam ..'"
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _one_buy(out)
    assert "SOLD_OUT" not in _log_text(out)


def test_flash_sale_banner_classification_all_contexts(tmp_path):
    runner, app, *_, log, _notifier = make_android(tmp_path)
    log.close()
    app.screen = "product"
    app.clock.advance(13.0)  # lewat T: halaman produk live
    nodes = app.nodes()
    assert BANNER_LIVE in [n.text for n in nodes]
    for context in ("product", "after_buy", "any"):
        seen = runner._classify_nodes(nodes, context)
        assert seen.screen == Screen.PRODUCT, (context, seen)
    sold = android_selectors.defaults().marker_re("sold_out")
    for text in (BANNER_LIVE, "FLASH SALE BERAKHIR DALAM 00:10:00", "Flash Sale dimulai dalam 00:00:05"):
        assert sold.fullmatch(text) is None, text
    for text in ("Habis", "Stok habis", "Flash Sale telah berakhir", "Flash Sale sudah berakhir"):
        assert sold.fullmatch(text) is not None, text


def test_flash_sale_ended_banner_is_sold_out_without_click(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, app_cls=SaleEndedApp, live=True)
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    assert "berakhir" in out.result.message
    assert out.kind("buy") == [] and out.kind("tap") == []
    _no_order(out)


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
    assert "query gagal saat polling: uiautomator2: koneksi ke agent putus" in log
    assert "agent uiautomator2 mati saat polling (HiOS?); restart #1" in log
    buy = _one_buy(out)
    assert buy["t_server_ms"] >= _t(out)
    # restart ~2 s -> reload lalu Beli; jarak reload->Beli < 400 ms = bug terpisah (xfail di test_android_runner.py)
    acts = polling_actions(out)
    assert all(_t(out) - 1000 <= a <= _t(out) + 8000 for a in acts), acts
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


@pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
@pytest.mark.parametrize("phase", ["polling", "after_buy", "checkout"])
def test_driver_error_with_agent_alive_is_error_result(tmp_path, monkeypatch, phase, live):
    out = _run(tmp_path, monkeypatch, setup=_inject(phase, kill=False), live=live, runner_attrs=_stop())
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert out.result.message.startswith("driver: uiautomator2: koneksi ke agent putus")
    assert len(out.driver.injected) == 1
    assert out.driver.restarts == 0, "agent hidup: tidak di-restart"
    assert len(out.kind("buy")) == (0 if phase == "polling" else 1), "tanpa klik ulang Beli"
    assert not out.runner.stop_event.is_set()
    assert _result_json(out)["status"] == "ERROR"
    assert out.result.screenshots, "diagnosa akhir tetap jalan"
    _no_order(out)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: DriverError di precheck()/arm() (mis. _wait_screen -> find_all saat agent putus) tidak ditangkap: "
    "exception keluar dari API Runner, run_single crash tanpa RunResult ERROR, tanpa result.json/alarm"))
@pytest.mark.parametrize("phase", ["precheck", "arm"])
def test_driver_error_before_attempt_is_error_result_not_crash(tmp_path, monkeypatch, phase):
    runners = []

    def setup(runner, app, driver):
        runners.append(runner)
        _inject(phase, kill=False, ops=("find_all",))(runner, app, driver)

    try:
        out = _run(tmp_path, monkeypatch, setup=setup)
    except DriverError as e:
        runners[0].log.close()  # harness tidak menutup log bila run melempar exception
        pytest.fail(f"DriverError di {phase} keluar dari runner: {e}")
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert out.kind("buy") == []
    assert (out.log_dir / "android-result.json").exists()


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
