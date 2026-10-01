"""Regresi dari review adversarial akhir tahap 3 (setiap tes mereproduksi satu temuan terkonfirmasi).

- Tap langkah maju (chip variasi di sheet, baris/opsi/konfirmasi metode bayar, centang keranjang) tidak pernah
  mengenai dialog crash/ANR; konfirmasi sheet yang ditahan dialog tidak dihitung sebagai klik.
- Jendela asing berbentuk dialog (halaman Shopee tetap terbaca di bawahnya) setelah klik Beli -> VERIFICATION,
  tanpa klik ulang di atasnya.
- Jarak tap Beli >= 425 ms dihitung dari tap sebenarnya (latensi RPC sebelum tap tidak memperpendeknya); konfirmasi
  ulang sheet tidak setelah T+8 s dan dicek captcha (teks) tepat sebelumnya.
- Layar PIN ber-content-desc setelah "Buat Pesanan" = ORDER_PLACED_AWAIT_PIN (bukan "PIN saat polling").
- Jaring UNKNOWN/loading: dialog yang sudah hilang tidak ikut dihitung; loading berselang-seling tetap dibatasi.
- Dialog sistem dicari di luar package Shopee: judul produk/ulasan "tidak merespons" bukan dialog ANR.
- Halaman produk berisi pesan galat (tanpa tombol Beli) di-reload; dialog ANR menahan reload.
- Precheck: verifikasi di halaman saldo = stop; captcha content-desc saat arm menghentikan runner lain saat itu
  juga; `svc power stayon usb` selalu dikembalikan (Ctrl+C, bacaan ulang gagal, nilai > 9), gagal = alarm.
"""

from __future__ import annotations

import asyncio
import csv
import re
import threading

import pytest

from flashbuy import android_selectors
from flashbuy.android_driver import DriverError, FakeDriver, Node, Sel, U2Driver
from flashbuy.android_runner import LOADING_LIMIT_S, SYSTEM_DIALOG_MATCH
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus
from tests.android_harness import polling_actions, run_android
from tests.conftest import FakeClock
from tests.fake_android import AppScenario, FakeShopeeApp
from tests.test_android_spec_f import (
    BOTH,
    INCALL,
    RECAPTCHA,
    _front_app,
    _hard_rules,
    _log_text,
    _one_buy,
    _run,
    _step_t,
    _stop,
    _stopped,
    _t,
)

# Dialog ANR yang menutupi hampir seluruh layar: tap apa pun selama dialog tampil mengenai 'Tutup aplikasi'.
FULL_ANR = (("Shopee tidak merespons", (60, 60, 660, 120)), ("Tutup aplikasi", (0, 130, 720, 1612)))


class _AnrAfterStepApp(FakeShopeeApp):
    """Dialog ANR muncul `delay_s` setelah runner mencatat langkah `trigger` (waktu server langkah itu), selama
    `anr_s`. Elemen Shopee tetap terbaca di bawahnya; `dialog` = (teks, bounds) jendela dialog."""

    trigger = "click_buy"
    delay_s = 0.06
    anr_s = 0.6
    dialog = FULL_ANR

    def __init__(self, sc, clock):
        super().__init__(sc, clock)
        self.runner = None
        self.window: tuple[float, float] | None = None

    def anr_active(self) -> bool:
        if self.window is None and self.runner is not None:
            step = next((s for s in self.runner.log.steps if s.name == self.trigger), None)
            if step is not None:
                start = step.t_server_ms / 1000 + self.delay_s
                self.window = (start, start + self.anr_s)
        return self.window is not None and self.window[0] <= self.now() < self.window[1]

    def system_nodes(self) -> list[Node]:
        if not self.anr_active():
            return []
        return [Node(text=t, bounds=b, clickable=i > 0) for i, (t, b) in enumerate(self.dialog)]


def _anr_after(trigger: str, delay_s: float = 0.06, anr_s: float = 0.6, dialog=FULL_ANR):
    app_cls = type("App", (_AnrAfterStepApp,), {"trigger": trigger, "delay_s": delay_s, "anr_s": anr_s,
                                                "dialog": dialog})

    def setup(runner, app, driver):
        app.runner = runner

    return app_cls, setup


def _quiet_in(out, window: tuple[float, float]) -> None:
    start, end = (int(x * 1000) for x in window)
    assert [e for e in out.app.events if start <= e["t_server_ms"] < end] == [], "tidak ada aksi selama dialog"


def _ok(out, live: bool) -> None:
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message


# ------------------------------------------------------------------ tap langkah maju vs dialog ANR


@BOTH
def test_anr_over_sheet_chip_delays_variant_tap_until_gone(tmp_path, monkeypatch, live):
    """Dialog ANR muncul 0,15 s setelah klik Beli (setelah cek paksa pasca-klik, sebelum tap chip), menutupi baris
    chip variasi: chip tidak di-tap selama dialog tampil (tap akan mengenai 'Tutup aplikasi'), dipilih setelah
    hilang."""
    app_cls, setup = _anr_after("click_buy", delay_s=0.15)
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, setup=setup, live=live, variants=["Merah", "Biru"],
               cfg={"variant": "Biru"})
    _ok(out, live)
    _quiet_in(out, out.app.window)
    assert [e["detail"] for e in out.kind("variant")] == ["Biru"]
    assert out.kind("variant")[0]["t_server_ms"] >= int(out.app.window[1] * 1000)
    assert out.app.selected_variant == "Biru"
    _one_buy(out)
    _hard_rules(out, ordered=live)


@BOTH
def test_anr_during_payment_switch_delays_row_option_and_confirm_taps(tmp_path, monkeypatch, live):
    """Metode bawaan COD; dialog ANR muncul saat checkout terbuka: baris 'Metode Pembayaran', opsi ShopeePay, dan
    'Konfirmasi' tidak di-tap selama dialog tampil; ShopeePay tetap terpilih setelah dialog hilang."""
    app_cls, setup = _anr_after("checkout_loaded", delay_s=0.02)
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, setup=setup, live=live, payment_default="COD - Cek Dulu")
    _ok(out, live)
    _quiet_in(out, out.app.window)
    assert [e["detail"] for e in out.kind("payment")] == ["ShopeePay"]
    assert out.kind("payment")[0]["t_server_ms"] >= int(out.app.window[1] * 1000)
    assert out.app.payment_method == "ShopeePay"
    _hard_rules(out, ordered=live)


def test_anr_when_cart_opens_delays_uncheck_tap(tmp_path, monkeypatch):
    """Keranjang dengan item lain tercentang; dialog ANR muncul saat keranjang terbuka: centang item lain tidak
    di-tap selama dialog tampil (lapis 2 tetap jalan setelah dialog hilang)."""
    app_cls, setup = _anr_after("buy_ok", delay_s=0.0)
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, setup=setup, live=True, go_cart=True,
               cart_other_items=[("Casing Lain", 20_000, True)])
    _ok(out, True)
    _quiet_in(out, out.app.window)
    assert [e["detail"] for e in out.kind("cart_toggle")] == ["Casing Lain"]
    assert out.kind("cart_toggle")[0]["t_server_ms"] >= int(out.app.window[1] * 1000)
    _hard_rules(out, ordered=True)


class _SwallowFirstConfirmApp(FakeShopeeApp):
    """Konfirmasi sheet pertama tidak bereaksi (sheet tetap terbuka); konfirmasi berikutnya normal."""

    def _handle(self, key: str, node: Node) -> None:
        if key == "confirm" and not self.kind("confirm_swallowed"):
            self._event("confirm_swallowed")
            return
        super()._handle(key, node)


def test_retry_confirm_blocked_by_anr_is_not_counted_and_follows_right_after(tmp_path, monkeypatch):
    """Konfirmasi ulang (lewat gate) ditahan dialog ANR 0,2 s: tidak dihitung sebagai klik dan tidak ditunggu
    'tidak bereaksi' 1,5 s; dikonfirmasi pada slot gate berikutnya setelah dialog hilang."""
    base, setup = _anr_after("no_response", delay_s=0.0, anr_s=0.2)
    app_cls = type("App", (base, _SwallowFirstConfirmApp), {})
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, setup=setup, live=True)
    _ok(out, True)
    _quiet_in(out, out.app.window)
    confirms = [e["t_server_ms"] for e in out.app.events if e["kind"] in ("confirm", "confirm_swallowed")]
    assert len(confirms) == 2, confirms
    assert confirms[1] - int(out.app.window[1] * 1000) <= 600, "konfirmasi ulang segera setelah dialog hilang"
    assert out.step_names().count("no_response") == 1, "konfirmasi yang ditahan bukan 'tidak bereaksi'"
    _hard_rules(out, ordered=True)


# ------------------------------------------------------------------ jendela asing setelah klik Beli


def _dialog_front(front: str, texts: tuple[str, ...]):
    """Jendela asing berbentuk dialog setelah tap Beli: hanya current_app yang menunjukkan package lain; layar
    Shopee di belakangnya tetap terbaca oleh kueri com.shopee.id (beda dengan _front_app layar penuh)."""
    package, activity = front.split("/", 1)
    base = _front_app(package, activity, texts, None)

    class App(base):
        def _r_front(self):
            return getattr(self, f"_r_{self.front[0]}")()

    return App


@BOTH
@pytest.mark.parametrize("scenario", [{"no_response_clicks": 9}, {"loading_after_buy_ms": 20_000}],
                         ids=["halaman_produk_tetap", "spinner"])
def test_dialog_style_foreign_activity_after_buy_is_verification_without_reclick(tmp_path, monkeypatch, live,
                                                                                 scenario):
    """reCAPTCHA Play Services (dialog) di atas halaman produk / spinner: VERIFICATION < 1 s setelah klik, tanpa
    klik ulang yang mendarat di jendela itu (dulu: 5 klik ke jendela asing lalu NOT_STARTED_TIMEOUT, atau
    UNKNOWN_STATE baru setelah loading 10 s)."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_dialog_front(RECAPTCHA, ("Saya bukan robot", "Verifikasi")),
               live=live, runner_attrs=attrs, **scenario)
    _stopped(out, attrs, RunStatus.VERIFICATION)
    assert out.result.message == f"aplikasi/activity asing di depan: {RECAPTCHA}", out.result.message
    buy = _one_buy(out)
    assert _step_t(out, "result") - buy["t_server_ms"] <= 1000
    assert out.app.events[-1]["kind"] == "front", "tidak ada aksi setelah jendela asing muncul"
    _hard_rules(out)


def test_dialog_style_system_app_after_buy_is_unknown_state_without_reclick(tmp_path, monkeypatch):
    """Panggilan masuk (aplikasi sistem) berbentuk dialog di atas halaman produk yang tidak bereaksi: UNKNOWN
    (bukan VERIFICATION), tanpa klik ulang, UNKNOWN_STATE setelah 1,5 s."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_dialog_front(INCALL, ("Panggilan masuk", "Tolak")),
               runner_attrs=attrs, no_response_clicks=9)
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert out.result.message.startswith(f"layar tidak dikenali > 1.5 s (Shopee keluar dari foreground: {INCALL}"), \
        out.result.message
    _one_buy(out)
    assert out.app.events[-1]["kind"] == "front"
    _hard_rules(out)


# ------------------------------------------------------------------ jarak tap & jendela polling


class _ErrorFirstBuyApp(FakeShopeeApp):
    """Klik Beli pertama langsung dijawab toast 'Terjadi kesalahan, coba lagi nanti' (halaman tidak berubah)."""

    def _tap_buy(self, node: Node) -> None:
        if node.enabled and not self.kind("buy"):
            self._event("buy")
            self._show_toast("Terjadi kesalahan, coba lagi nanti")
            return
        super()._tap_buy(node)


@pytest.mark.parametrize("spike_s", [0.0, 0.15, 0.25])
def test_buy_taps_are_400ms_apart_even_with_latency_spike_before_first_tap(tmp_path, monkeypatch, spike_s):
    """RPC tepat sebelum tap Beli #1 (cek dialog) lambat; tap #1 langsung dijawab galat: tap #2 tetap >= 400 ms
    setelah tap #1 (slot berikutnya dihitung dari tap sebenarnya, bukan dari slot tap #1)."""

    def setup(runner, app, driver):
        orig = driver._rpc
        state = {"done": False}

        def rpc(op, target="", latency=None):
            if op == "info_any" and not state["done"] and driver.calls and driver.calls[-1][0] == "clear_toast":
                state["done"] = True
                latency = driver.latency_s + spike_s
            return orig(op, target, latency)

        driver._rpc = rpc

    out = _run(tmp_path, monkeypatch, app_cls=_ErrorFirstBuyApp, setup=setup)
    _ok(out, False)
    buys = [e["t_server_ms"] for e in out.kind("buy")]
    assert len(buys) == 2 and buys[1] - buys[0] >= 425, buys
    _hard_rules(out)


class _ChipStateHiddenApp(FakeShopeeApp):
    """Sheet tidak pernah bereaksi terhadap konfirmasi dan status terpilih chip variasi tidak terekspos (RN)."""

    def _handle(self, key: str, node: Node) -> None:
        if key == "confirm":
            self._event("confirm")
            return
        super()._handle(key, node)

    def _r_sheet(self):
        return [(k, Node(**{**n.__dict__, "selected": False})) if k.startswith("variant:") else (k, n)
                for k, n in super()._r_sheet()]


def test_retry_confirm_never_after_t_plus_8s(tmp_path, monkeypatch):
    """Konfirmasi ulang = aksi polling: tidak ada tap chip/konfirmasi setelah T+8 s walaupun pilih ulang chip
    (status tak terbaca) + tunggu verifikasi 0,6 s terjadi setelah slot gate."""
    out = _run(tmp_path, monkeypatch, app_cls=_ChipStateHiddenApp, variants=["Merah", "Biru"],
               cfg={"variant": "Biru"})
    assert out.result.status == RunStatus.NOT_STARTED_TIMEOUT, out.result.message
    taps = [e["t_server_ms"] - _t(out) for e in out.app.events if e["kind"] in ("confirm", "variant")]
    assert len(out.kind("confirm")) >= 3, "konfirmasi ulang lewat gate"
    assert max(taps) <= 8000, taps
    _hard_rules(out)


class _CaptchaOnRetryApp(_SwallowFirstConfirmApp):
    """Setelah runner mencatat 'no_response' (sheet tidak bereaksi), overlay captcha (pohon Shopee) menutupi area
    tombol konfirmasi sheet."""

    runner = None

    def _r_sheet(self):
        out = super()._r_sheet()
        if self.runner is not None and any(s.name == "no_response" for s in self.runner.log.steps):
            out.append(("captcha", Node(text="Geser untuk verifikasi", bounds=(0, 1400, 720, 1612))))
        return out


def test_captcha_over_sheet_before_retry_confirm_is_never_tapped(tmp_path, monkeypatch):
    attrs = _stop()

    def setup(runner, app, driver):
        app.runner = runner

    out = _run(tmp_path, monkeypatch, app_cls=_CaptchaOnRetryApp, setup=setup, runner_attrs=attrs)
    _stopped(out, attrs, RunStatus.CAPTCHA)
    assert len(out.kind("confirm_swallowed")) == 1 and out.kind("confirm") == []
    assert [e for e in out.kind("tap") if e["detail"] == "captcha"] == [], "tap mendarat di captcha"
    _hard_rules(out)


# ------------------------------------------------------------------ PIN & jaring UNKNOWN setelah "Buat Pesanan"


def _after_order(blank_s: float, *, loading: bool, pin_desc: bool):
    """Tap 'Buat Pesanan' -> spinner (loading) atau layar kosong selama `blank_s` -> layar PIN; judul PIN juga
    sebagai content-desc (accessibilityLabel RN) bila `pin_desc`."""

    class App(FakeShopeeApp):
        def _handle(self, key: str, node: Node) -> None:
            if key != "place_order":
                return super()._handle(key, node)
            self._event("order", self.payment_method)
            self.loading_until, self.after_loading = self.now() + blank_s, "pin"
            self.screen = "loading" if loading else "blank"

        def _render(self):
            if self.screen == "blank" and self.now() >= self.loading_until:
                self.screen = "pin"
            return super()._render()

        def _r_blank(self):
            return []

        def _r_pin(self):
            out = super()._r_pin()
            if pin_desc:
                out[0] = ("", Node(**{**out[0][1].__dict__, "desc": self.sc.pin_title}))
            return out

    return App


@pytest.mark.parametrize("delay_s", [0.4, 0.6, 2.0])
def test_pin_screen_with_content_desc_after_place_order_is_order_placed(tmp_path, monkeypatch, delay_s):
    """Layar PIN (judul juga content-desc) muncul setelah spinner: ORDER_PLACED_AWAIT_PIN, bukan UNKNOWN_STATE
    'layar PIN muncul saat polling' dari cek content-desc berkala."""
    out = _run(tmp_path, monkeypatch, app_cls=_after_order(delay_s, loading=True, pin_desc=True), live=True)
    assert out.result.message == "pesanan dibuat; masukkan PIN ShopeePay secara manual", out.result.message
    _hard_rules(out, ordered=True)


def test_anr_gone_before_place_order_does_not_count_toward_unknown_net(tmp_path, monkeypatch):
    """Dialog ANR 0,3 s saat ongkir masih dihitung, lalu layar kosong 0,3 s setelah 'Buat Pesanan': jaring UNKNOWN
    dihitung dari layar kosong itu (bukan dari dialog yang sudah hilang ~1,3 s sebelumnya)."""
    base, setup = _anr_after("checkout_loaded", delay_s=0.05, anr_s=0.3)
    app_cls = type("App", (base, _after_order(0.3, loading=False, pin_desc=False)), {})
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, setup=setup, live=True, shipping_delay_ms=1200)
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    assert _step_t(out, "click_place_order") - int(out.app.window[0] * 1000) > 1500
    _hard_rules(out, ordered=True)


class _FlickerApp(FakeShopeeApp):
    """Setelah klik Beli layar berganti tiap 0,3 s antara spinner dan `other` ('blank' / halaman produk)."""

    other = "blank"

    def _tap_buy(self, node: Node) -> None:
        self._event("buy")
        self.t_buy = self.now()
        self.screen = "flicker"

    def _r_flicker(self):
        if int((self.now() - self.t_buy) / 0.3) % 2 == 0:
            return self._r_loading()
        return [] if self.other == "blank" else self._r_product()


@pytest.mark.parametrize("other, message", [
    ("blank", f"indikator loading > {LOADING_LIMIT_S:.0f} s"),
    ("product", f"indikator loading berselang > {LOADING_LIMIT_S:.0f} s setelah klik"),
], ids=["spinner_kosong", "spinner_tombol_beli"])
def test_loading_alternating_after_buy_is_bounded_by_loading_limit(tmp_path, monkeypatch, other, message):
    """Spinner berselang-seling dengan layar kosong / tombol Beli: UNKNOWN_STATE di batas loading (10 s) + alarm,
    bukan NOT_STARTED_TIMEOUT 'slot tidak terbuka' setelah batas alur 30 s."""
    attrs = _stop()
    app_cls = type("App", (_FlickerApp,), {"other": other})
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, runner_attrs=attrs)
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert out.result.message.startswith(message), out.result.message
    dt = _step_t(out, "result") - out.kind("buy")[0]["t_server_ms"]
    assert LOADING_LIMIT_S * 1000 <= dt <= LOADING_LIMIT_S * 1000 + 1000, dt
    _one_buy(out)
    _hard_rules(out)


# ------------------------------------------------------------------ dialog sistem vs teks Shopee


class _ReviewApp(FakeShopeeApp):
    def _r_product(self):
        return [*super()._r_product(), ("", Node(text="Barang oke, tapi penjual tidak merespons chat",
                                                  bounds=(20, 940, 700, 980)))]


@BOTH
def test_shopee_text_like_anr_phrase_is_not_a_system_dialog(tmp_path, monkeypatch, live):
    """Judul produk 'Anti Berhenti Bekerja' dan ulasan 'penjual tidak merespons' (node Shopee) tidak dianggap
    dialog crash/ANR: arm, klik Beli, dan alur berjalan normal."""
    name = "Jam Dinding Anti Berhenti Bekerja 30cm"
    out = _run(tmp_path, monkeypatch, app_cls=_ReviewApp, live=live, product_name=name,
               cfg={"expected_name": name})
    _ok(out, live)
    assert "dialog sistem" not in _log_text(out)
    _one_buy(out)
    _hard_rules(out, ordered=live)


def test_system_dialog_query_excludes_shopee_and_systemui_packages():
    """U2Driver: info_any dengan exclude -> packageNameMatches (regex Java) yang menolak package itu saja."""
    class Dev:
        def __call__(self, **kw):
            self.kw = kw
            return type("O", (), {"selector": kw})()

    drv = U2Driver.__new__(U2Driver)
    drv.package, drv.d = "com.shopee.id", Dev()
    kw = drv._selector(Sel("textMatches", SYSTEM_DIALOG_MATCH), any_package=True,
                       exclude=("com.shopee.id", "com.android.systemui"))
    assert "packageName" not in kw
    regex = kw["packageNameMatches"]
    for pkg, ok in [("android", True), ("com.transsion.phonemaster", True), ("com.shopee.id", False),
                    ("com.android.systemui", False), ("com.shopee.idx", True), ("com.shopee.id.uji", True)]:
        assert bool(re.fullmatch(regex, pkg)) is ok, pkg
    assert drv._selector(Sel("text", "x"))["packageName"] == "com.shopee.id"


def test_fake_driver_info_any_exclude_skips_own_package_nodes():
    class App(FakeShopeeApp):
        def system_nodes(self):
            return [Node(text="Shopee tidak merespons")]

    clock = FakeClock()
    app = App(AppScenario(open_at=clock.time() + 5, product_name="Anti Berhenti Bekerja"), clock)
    app.screen = "product"
    d = FakeDriver(app, clock)
    sel = Sel("textMatches", SYSTEM_DIALOG_MATCH)
    assert d.info_any(sel, exclude=("com.shopee.id",)).text == "Shopee tidak merespons"
    app.system_nodes = lambda: []
    assert d.info_any(sel, exclude=("com.shopee.id",)) is None
    assert d.info_any(sel).text == "Anti Berhenti Bekerja"


# ------------------------------------------------------------------ reload


class _ErrorPageApp(FakeShopeeApp):
    """Mulai T halaman produk hanya berisi 'Gagal memuat. Coba lagi nanti' (tanpa tombol Beli) sampai di-reload."""

    def _r_product(self):
        if self.sale_active() and not self.page_live():
            return [("", Node(text="Detail Produk", bounds=(100, 60, 500, 110))),
                    ("", Node(text="Gagal memuat. Coba lagi nanti", bounds=(100, 700, 620, 760)))]
        return super()._r_product()


@pytest.mark.parametrize("reload", ["swipe", "intent"])
def test_error_page_without_buy_button_is_reloaded(tmp_path, monkeypatch, reload):
    out = _run(tmp_path, monkeypatch, app_cls=_ErrorPageApp, live_update=False, cfg={"android": {"reload": reload}})
    _ok(out, False)
    acts = [a - _t(out) for a in polling_actions(out)]
    assert len(acts) == 2 and 500 <= acts[0] <= 1000, acts  # reload di iterasi yang melewati T+0,5 s, lalu Beli
    assert out.kind("buy")[0]["t_server_ms"] - _t(out) == acts[1]
    _hard_rules(out)


@pytest.mark.parametrize("reload", ["swipe", "intent"])
def test_anr_with_remind_button_blocks_reload_and_is_unknown_state(tmp_path, monkeypatch, reload):
    """Tombol 'Ingatkan Saya' (bukan tombol Beli) + dialog ANR menetap sejak T-0,5 s: tidak ada reload selama
    dialog tampil; jaring UNKNOWN (tidak direset oleh 'belum dimulai') -> UNKNOWN_STATE."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, live_update=False, button_before_open="Ingatkan Saya",
               anr_dialog=(-0.5, 20.0), runner_attrs=attrs, cfg={"android": {"reload": reload}})
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert polling_actions(out) == [] and out.kind("tap") == [], "tanpa reload/tap selama dialog"
    assert "dialog sistem 'Shopee tidak merespons'" in out.result.message
    _hard_rules(out)


# ------------------------------------------------------------------ precheck & arm


class _WalletVerifyApp(FakeShopeeApp):
    """Intent halaman ShopeePay mendarat di activity verifikasi Shopee (WebView tanpa teks)."""

    def on_intent(self, url: str, package: str) -> None:
        super().on_intent(url, package)
        if "/user/shopeepay" in url:
            self.sc.verify_activity_after_buy = "com.shopee.app.ui.auth.VerificationActivity"
            self.screen = "verify_activity"


def test_verification_activity_on_wallet_page_stops_precheck(tmp_path, monkeypatch):
    """Dulu: dianggap 'saldo tidak terbaca' (alarm saja), lalu intent produk ditembakkan di atas layar verifikasi
    dan Beli diklik. Sekarang: precheck berhenti VERIFICATION (alarm precheck + stop semua runner)."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_WalletVerifyApp, runner_attrs=attrs)
    assert out.result.status == RunStatus.VERIFICATION, out.result.message
    assert attrs["stop_event"].is_set() and out.events() == ["precheck"]
    assert "activity verifikasi com.shopee.id/com.shopee.app.ui.auth.VerificationActivity" in out.result.message
    intents = [e["detail"] for e in out.kind("intent")]
    assert intents[-1].endswith("/user/shopeepay"), "tidak ada intent lain di atas layar verifikasi"
    assert out.kind("buy") == [] and out.app.screen == "verify_activity"
    _hard_rules(out)


class _ArmDescCaptchaApp(FakeShopeeApp):
    """Sejak T-4,5 s (arm) halaman produk tertutup captcha WebView yang hanya ber-content-desc."""

    def _r_product(self):
        if self.now() < self.sc.open_at - 4.5:
            return super()._r_product()
        return [("", Node(desc="Geser untuk verifikasi", bounds=(0, 0, 720, 1612), cls="android.webkit.WebView"))]


def test_content_desc_captcha_at_arm_stops_other_runners_at_arm(tmp_path, monkeypatch):
    stopped_at: list[float] = []

    class Ev(threading.Event):
        def set(self):
            stopped_at.append(app_ref[0].now())
            super().set()

    app_ref: list = []
    attrs = {"stop_event": Ev()}

    def setup(runner, app, driver):
        app_ref.append(app)

    out = _run(tmp_path, monkeypatch, app_cls=_ArmDescCaptchaApp, setup=setup, runner_attrs=attrs)
    _stopped(out, attrs, RunStatus.CAPTCHA)
    assert stopped_at and stopped_at[0] < out.open_at - 1, "runner lain dihentikan saat arm, bukan saat T-lead"
    assert out.kind("buy") == []
    _hard_rules(out)


def _stay(out) -> list[str]:
    return [e["detail"] for e in out.kind("settings")]


@pytest.mark.parametrize("when", ["sebelum_svc", "setelah_svc"])
def test_ctrl_c_during_precheck_always_restores_stay_on(tmp_path, monkeypatch, when):
    """Ctrl+C (abort + close()) saat precheck: sebelum svc -> svc tidak pernah dikirim; setelah svc -> nilai semula
    dikembalikan oleh thread precheck walaupun close() sudah jalan. Tidak ada intent/aksi lain setelahnya."""

    def setup(runner, app, driver):
        orig = driver.shell

        def shell(cmd):
            line = " ".join(cmd)
            if when == "sebelum_svc" and line == "settings get system screen_off_timeout":
                runner._abort_reason = "dibatalkan"
                asyncio.run(runner.close())  # close() di thread utama sebelum thread precheck lanjut
            res = orig(cmd)
            if when == "setelah_svc" and line == "svc power stayon usb":
                runner._abort_reason = "dibatalkan"
            return res

        driver.shell = shell

    out = _run(tmp_path, monkeypatch, setup=setup, stay_on="0", runner_attrs={"hold_screen_on": True})
    assert out.result.status == RunStatus.ABORTED, out.result.message
    assert _stay(out) == ([] if when == "sebelum_svc" else ["svc power stayon usb", "svc power stayon false"])
    assert out.app.sc.stay_on == "0"
    assert out.kind("intent") == [] and out.kind("tap") == []


class _ReadbackFailsApp(FakeShopeeApp):
    """Bacaan `settings get global stay_on_while_plugged_in` sesudah svc gagal (adb putus sesaat)."""

    def shell(self, cmd: list[str]) -> str:
        if " ".join(cmd) == "settings get global stay_on_while_plugged_in" and self.kind("settings"):
            raise DriverError("adb: device offline")
        return super().shell(cmd)


def test_stay_on_restored_even_when_readback_fails(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, app_cls=_ReadbackFailsApp, stay_on="0", runner_attrs={"hold_screen_on": True})
    _ok(out, False)
    assert _stay(out) == ["svc power stayon usb", "svc power stayon false"] and out.app.sc.stay_on == "0"
    assert "nilai sesudahnya tidak terbaca; dikembalikan ke 0 setelah run" in _log_text(out)
    assert "tidak berefek" not in _log_text(out)


def test_stay_on_multi_digit_value_is_restored_exactly(tmp_path):
    out = run_android(tmp_path, stay_on="12", runner_attrs={"hold_screen_on": True})
    _ok(out, False)
    assert _stay(out) == ["svc power stayon usb", "settings put global stay_on_while_plugged_in 12"]
    assert out.app.sc.stay_on == "12"


class _RestoreFailsApp(FakeShopeeApp):
    def shell(self, cmd: list[str]) -> str:
        if " ".join(cmd) == "svc power stayon false":
            raise DriverError("adb: device offline")
        return super().shell(cmd)


def test_stay_on_restore_failure_is_alarmed_not_logged_as_restored(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, app_cls=_RestoreFailsApp, stay_on="0", runner_attrs={"hold_screen_on": True})
    _ok(out, False)
    assert out.app.sc.stay_on == "2"
    assert out.events() == ["stay_on_restore"]
    assert "kembalikan manual" in out.notifier.events[0]["message"]
    assert "layar tetap menyala dikembalikan" not in _log_text(out)


def test_legacy_calibration_without_app_version_is_strong_warning(tmp_path):
    """Kalibrasi lama (meta tanpa app_version) tetap dipakai runner: precheck tidak boleh bilang 'belum ada
    kalibrasi'; versi tidak bisa dibandingkan = PERINGATAN KERAS + alarm."""
    path = tmp_path / "selectors.json"
    android_selectors.save(path, {"buy_button": [{"resourceId": "com.shopee.id:id/btn_buy_now"}]},
                           {"at": "2026-09-30T10:00:00+0700", "device": "FAKE123", "skipped": []})
    out = run_android(tmp_path, selectors=android_selectors.load(path))
    _ok(out, False)
    assert ("precheck versi vs kalibrasi: PERINGATAN - PERINGATAN KERAS: kalibrasi tanpa versi Shopee tercatat "
            "(versi sekarang 3.40.21), tidak bisa dibandingkan") in _log_text(out)
    assert out.events() == ["precheck_versi"]


# ------------------------------------------------------------------ review putaran 2 (delta perbaikan)


class _SpinnerAroundSheetApp(FakeShopeeApp):
    """Beli -> spinner `loading_after_buy_ms` -> sheet -> konfirmasi -> spinner `post_s` -> checkout."""

    post_s = 7.0

    def _enter_checkout(self, items) -> None:
        super()._enter_checkout(items)
        self.loading_until, self.after_loading, self.screen = self.now() + self.post_s, "checkout", "loading"


@pytest.mark.parametrize("pre_ms, post_s", [(4000, 7.0), (300, 9.9)], ids=["4s_7s", "0.3s_9.9s"])
def test_spinners_before_and_after_sheet_each_get_their_own_loading_budget(tmp_path, monkeypatch, pre_ms, post_s):
    """Sheet = layar progres yang dikenali: spinner setelah konfirmasi tidak dihitung dari spinner sebelum sheet
    (masing-masing < 10 s, jumlahnya > 10 s) -> checkout normal, bukan UNKNOWN_STATE."""
    app_cls = type("App", (_SpinnerAroundSheetApp,), {"post_s": post_s})
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, loading_after_buy_ms=pre_ms)
    _ok(out, False)
    _one_buy(out)
    _hard_rules(out)


class _RejectUntilApp(FakeShopeeApp):
    """Tombol Beli aktif sejak T, tetapi server menolak tap sampai T+1,2 s (toast 'Flash sale belum dimulai')."""

    def _tap_buy(self, node: Node) -> None:
        if node.enabled and self.now() < self.sc.open_at + 1.2:
            self._event("buy")
            self._show_toast("Flash sale belum dimulai")
            return
        super()._tap_buy(node)


def test_retry_after_shopee_toast_reads_no_foreground_package(tmp_path, monkeypatch):
    """Klik ulang setelah toast Shopee (Shopee jelas di depan): tanpa adb dumpsys (current_app, mahal di HP) di
    jalur kritis antara klik; jarak klik ulang tetap dekat 425 ms."""

    def setup(runner, app, driver):
        orig = driver._rpc
        driver._rpc = lambda op, target="", latency=None: orig(op, target, 0.25 if op == "current_app" else latency)

    out = _run(tmp_path, monkeypatch, app_cls=_RejectUntilApp, setup=setup, button_enabled_before_open=True,
               flash_price=99_000, normal_price=99_000)
    _ok(out, False)
    buys = [e["t_server_ms"] for e in out.kind("buy")]
    assert len(buys) >= 3, buys
    assert all(b - a <= 650 for a, b in zip(buys, buys[1:], strict=False)), buys
    ops = [op for t, op, _ in _queries(out) if buys[0] <= t <= _step_t(out, "buy_ok")]
    assert "current_app" not in ops, "current_app di antara klik ulang"
    _hard_rules(out)


def _queries(out) -> list[tuple[int, str, str]]:
    with (out.log_dir / "android-queries-run.csv").open(encoding="utf-8", newline="") as f:
        return [(int(r[0]), r[1], r[2]) for r in list(csv.reader(f))[1:]]


class _BlankThenSheetApp(FakeShopeeApp):
    """Setelah Beli: layar kosong 0,3 s (transisi), lalu bottom sheet."""

    def _tap_buy(self, node: Node) -> None:
        self._event("buy")
        self.blank_until, self.screen = self.now() + 0.3, "blank"

    def _render(self):
        if self.screen == "blank" and self.now() >= self.blank_until:
            self.screen = "sheet"
        return super()._render()

    def _r_blank(self):
        return []


def test_transient_webview_query_error_on_unknown_screen_is_not_fatal(tmp_path, monkeypatch):
    """Galat sesaat pada cek WebView (layar transisi kosong) = layar tak terbaca (jaring UNKNOWN), bukan ERROR."""

    def setup(runner, app, driver):
        orig, state = driver.webview_present, {"n": 0}

        def webview():
            state["n"] += 1
            if state["n"] == 1 and app.screen == "blank":
                raise DriverError("JSONRPCError: transient")
            return orig()

        driver.webview_present = webview

    out = _run(tmp_path, monkeypatch, app_cls=_BlankThenSheetApp, setup=setup)
    _ok(out, False)
    _one_buy(out)
    _hard_rules(out)


@BOTH
def test_anr_appearing_as_arm_reads_variant_chip_waits_then_selects(tmp_path, monkeypatch, live):
    """Chip variasi di halaman produk, tanpa bottom sheet; dialog ANR muncul tepat saat arm membaca chip: chip
    tidak di-tap selama dialog, dipilih setelah dialog hilang (dulu: tidak pernah dipilih -> PRICE_GUARD)."""

    def setup(runner, app, driver):
        app.runner = runner
        orig = driver.info

        def info(sel):
            if app.window is None and sel == Sel("text", "Biru") and app.now() < app.sc.open_at:
                app.window = (app.now(), app.now() + 0.6)
            return orig(sel)

        driver.info = info

    app_cls = type("App", (_AnrAfterStepApp,), {"trigger": "__never__"})
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, setup=setup, live=live, variants=["Merah", "Biru"],
               variants_on_page=True, sheet=False, cfg={"variant": "Biru"})
    _ok(out, live)
    _quiet_in(out, out.app.window)
    variant = out.kind("variant")
    assert [e["detail"] for e in variant] == ["Biru"] and variant[0]["t_server_ms"] < _t(out)
    assert variant[0]["t_server_ms"] >= int(out.app.window[1] * 1000)
    _hard_rules(out, ordered=live)


class _DialogFrontAfterErrorApp(FakeShopeeApp):
    """Klik Beli -> halaman galat (tanpa tombol Beli); 0,2 s kemudian jendela verifikasi asing berbentuk dialog di
    depan (halaman galat tetap terbaca)."""

    def _tap_buy(self, node: Node) -> None:
        self._event("buy")
        self.t_front, self.screen = self.now() + 0.2, "errorpage"

    def _r_errorpage(self):
        return [("", Node(text="Detail Produk", bounds=(100, 60, 500, 110))),
                ("", Node(text="Gagal memuat. Coba lagi nanti", bounds=(100, 700, 620, 760)))]

    def _front(self) -> bool:
        return getattr(self, "t_front", None) is not None and self.now() >= self.t_front

    @property
    def package(self) -> str:
        return RECAPTCHA.split("/")[0] if self._front() else super().package

    def activity(self) -> str:
        return RECAPTCHA.split("/", 1)[1] if self._front() else super().activity()


@pytest.mark.parametrize("reload", ["swipe", "intent"])
def test_foreign_window_after_click_blocks_reload(tmp_path, monkeypatch, reload):
    """Reload setelah klik (halaman galat) didahului cek package di depan: jendela verifikasi asing -> VERIFICATION
    tanpa reload (intent reload akan menutupi layar verifikasi, swipe akan mengenai jendela itu)."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_DialogFrontAfterErrorApp, runner_attrs=attrs,
               cfg={"android": {"reload": reload}})
    _stopped(out, attrs, RunStatus.VERIFICATION)
    assert out.result.message == f"aplikasi/activity asing di depan: {RECAPTCHA}"
    buy = _one_buy(out)
    assert out.app.events[-1] == buy, "tidak ada reload/aksi setelah klik"
    _hard_rules(out)


class _PinOverlayApp(FakeShopeeApp):
    """'Buat Pesanan' -> PIN ShopeePay sebagai overlay di atas checkout; judulnya HANYA content-desc (tanpa teks,
    tanpa resource-id), node checkout tetap ada di pohon."""

    def _handle(self, key: str, node: Node) -> None:
        if key != "place_order":
            return super()._handle(key, node)
        self._event("order", self.payment_method)
        self.pin_overlay = True

    def _r_checkout(self):
        out = super()._r_checkout()
        if getattr(self, "pin_overlay", False):
            out.append(("", Node(desc="Masukkan PIN ShopeePay", bounds=(0, 600, 720, 1612))))
        return out


def test_content_desc_pin_overlay_over_checkout_is_order_placed(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, app_cls=_PinOverlayApp, live=True)
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    order = out.kind("order")
    assert len(order) == 1 and out.app.events[-1] == order[0], "tidak ada aksi setelah 'Buat Pesanan'"
    assert _step_t(out, "pin_screen") - order[0]["t_server_ms"] <= 700


class _PinInsteadOfListApp(FakeShopeeApp):
    """Tap baris 'Metode Pembayaran' memunculkan layar PIN (sebelum alat mengklik 'Buat Pesanan')."""

    def _handle(self, key: str, node: Node) -> None:
        if key == "payment_row":
            self._event("pin_shown")
            self.screen = "pin"
            return
        super()._handle(key, node)


@BOTH
def test_pin_screen_during_checkout_steps_is_maybe_ordered_stop(tmp_path, monkeypatch, live):
    """Layar PIN sebelum 'Buat Pesanan' dari alat (langkah maju): pesanan mungkin terbuat -> UNKNOWN_STATE + pesan
    wajib + alarm + stop semua (dulu: ERROR 'opsi ShopeePay tidak ditemukan' tanpa alarm)."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_PinInsteadOfListApp, live=live, runner_attrs=attrs,
               payment_default="COD - Cek Dulu")
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert out.result.message == MAYBE_ORDERED_MSG
    assert "layar PIN muncul sebelum alat mengklik 'Buat Pesanan'" in out.result.detail, out.result.detail
    assert out.kind("order") == [] and out.app.events[-1]["kind"] == "pin_shown", "tidak ada aksi di layar PIN"


@pytest.mark.parametrize("skew_ms", [5000, 7900, 7930])
def test_variant_reselect_after_reload_never_after_t_plus_8s(tmp_path, monkeypatch, skew_ms):
    """Pilih ulang variasi setelah reload = aksi polling: tidak di-tap setelah T+8 s walaupun slot gate <= T+8 s
    (skew 7900/7930: slot T+7,94..7,99 s, dulu tap mendarat T+8,02..8,06 s). Kontrol 5000: dipilih ulang."""
    out = run_android(tmp_path, variants=["Merah", "Biru"], variants_on_page=True, sheet=False,
                      sale_skew_ms=skew_ms, cfg={"variant": "Biru", "android": {"reload": "intent"}})
    taps = [e["t_server_ms"] - _t(out) for e in out.kind("variant") if e["t_server_ms"] >= _t(out) - 1000]
    assert all(t <= 8000 for t in taps), taps
    if skew_ms == 5000:
        assert taps and out.result.status == RunStatus.DRYRUN_OK, (taps, out.result.message)
    _hard_rules(out)


@pytest.mark.parametrize("start_s", [0.70, 0.76, 0.80, 0.85])
def test_anr_appearing_while_waiting_for_reload_slot_blocks_reload(tmp_path, monkeypatch, start_s):
    """Klik Beli -> halaman galat -> keputusan reload -> dialog ANR muncul saat menunggu slot gate: gestur/intent
    reload tidak dikirim selama dialog tampil."""
    app_cls = type("App", (_DialogFrontAfterErrorApp,), {"_front": lambda self: False,
                                                         "on_refresh": FakeShopeeApp.on_refresh})

    def refresh(self):
        if self.screen == "errorpage":
            self._event("refresh")
            self.screen = "product"

    app_cls.on_refresh = refresh
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, anr_dialog=(start_s, start_s + 1.0), sale_skew_ms=300)
    window = (out.open_at + start_s, out.open_at + start_s + 1.0)
    acts = [a for a in polling_actions(out) if window[0] * 1000 <= a < window[1] * 1000]
    assert acts == [], "reload/klik selama dialog ANR"
    assert out.kind("refresh"), "reload dikirim setelah dialog hilang"
    _hard_rules(out)


class _AutoSheetApp(FakeShopeeApp):
    """Pada T+`at` bottom sheet tanpa teks 'Beli Sekarang' terbuka sendiri, bersamaan dengan dialog ANR menetap.
    Back saat dialog tampil tertelan dialog (ANR tidak bisa dibatalkan)."""

    at = 0.0

    def _render(self):
        if self.screen == "product" and self.now() >= self.sc.open_at + self.at and not self.kind("auto_sheet"):
            self._event("auto_sheet")
            self.screen = "autosheet"
        return super()._render()

    def _r_autosheet(self):
        return [("", Node(text="Detail Produk", bounds=(100, 60, 500, 110))),
                ("", Node(text="Jumlah", bounds=(20, 1300, 200, 1340))),
                ("", Node(text="Konfirmasi", bounds=(0, 1500, 720, 1612), clickable=True))]

    def on_back(self) -> None:
        if self.anr_active():
            self._event("back_on_anr")
            return
        super().on_back()
        if self.screen == "autosheet":
            self.screen = "product"


@pytest.mark.parametrize("at", [-0.04, -0.01, 0.02, 0.05])
def test_sheet_and_anr_together_before_first_buy_no_back_into_dialog(tmp_path, monkeypatch, at):
    """Sheet + dialog ANR menetap muncul bersamaan sebelum klik Beli pertama: tidak ada back ke dialog;
    UNKNOWN_STATE (jaring 1,5 s), bukan ERROR 'sheet tidak tertutup'."""
    attrs = _stop()
    app_cls = type("App", (_AutoSheetApp,), {"at": at})
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, anr_dialog=(at, at + 5.0), runner_attrs=attrs)
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert out.kind("back_on_anr") == [] and out.kind("back") == []
    assert out.kind("buy") == [] and out.kind("tap") == []
    _hard_rules(out)


class _StayReadFailsApp(FakeShopeeApp):
    def shell(self, cmd: list[str]) -> str:
        if " ".join(cmd) == "settings get global stay_on_while_plugged_in" and not self.kind("stay_read_failed"):
            self._event("stay_read_failed")
            raise DriverError("adb: device offline")
        return super().shell(cmd)


def test_unreadable_original_stay_on_is_never_changed(tmp_path, monkeypatch):
    """Nilai semula stay_on_while_plugged_in tidak terbaca: svc TIDAK dikirim (nilai pengguna, mis. 'Tetap aktif'
    = 7, tidak boleh diganti lalu 'dikembalikan' ke 0)."""
    out = _run(tmp_path, monkeypatch, app_cls=_StayReadFailsApp, stay_on="7", runner_attrs={"hold_screen_on": True})
    _ok(out, False)
    assert _stay(out) == [] and out.app.sc.stay_on == "7"
    assert "`svc power stayon usb` TIDAK dikirim" in _log_text(out)
