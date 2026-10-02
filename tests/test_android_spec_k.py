"""Spesifikasi K (jaminan lintas skenario) jalur Android di atas FakeDriver + FakeShopeeApp (waktu virtual).

- "Stok habis" saat konfirmasi lalu halaman kembali ke harga normal dengan Beli aktif -> SOLD_OUT, tanpa beli ulang.
- Setiap status stop yang bisa dicapai mode LIVE, DRYRUN_OK, dan kalibrasi: 0 klik "Buat Pesanan".
- Alat tidak pernah mengetik: tidak ada API/RPC input teks (juga lewat jsonrpc mentah), tidak ada aksi ke aplikasi
  setelah layar PIN muncul (juga layar PIN tanpa teks).
- E.5 refresh: pertama T+0,5 s, lalu tiap 2 s (dihitung dari AWAL reload), lewat RateLimiter & jendela T-1..T+8 s;
  variasi dipilih ulang setelah reload (chip tak terbaca: intent & swipe, swipe yang mereset variasi, chip
  terlambat).
- Spesifikasi I: tanpa frida/jadx/apktool/root/su/hook/API privat (perintah adb shell: allowlist check_shell).
Waktu = waktu server (ms) relatif T (slot buka). Tes yang terkait dengan tes lain dirujuk di komentar satu baris.
"""

from __future__ import annotations

import ast
import io
import re
import threading
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console

from flashbuy import android_selectors, cli
from flashbuy.android_driver import AndroidDriver, FakeDriver, Node, Sel, TimedDriver, U2Driver
from flashbuy.android_screen import RP_SHORT_MATCH
from flashbuy.pricing import rupiah
from flashbuy.runner_base import ALARM_STATUSES, MAYBE_ORDERED_MSG, STOP_ALL_STATUSES, RunStatus
from tests import android_harness
from tests.android_harness import android_cfg, assert_polling_rules, polling_actions, run_android
from tests.conftest import FakeClock
from tests.fake_android import PACKAGE, AppScenario, FakeShopeeApp

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ["128GB Hitam", "256GB Biru"]
TARGET = "256GB Biru"
RECAPTCHA = "com.google.android.gms/.recaptcha.RecaptchaActivity"
RESEL = "(ulang setelah reload)"  # detail langkah variant_selected saat dipilih ulang setelah reload

# Operasi driver yang mengubah layar aplikasi; selain ini hanya membaca/diagnosa.
ACTION_OPS = {"click", "start_url", "swipe_refresh", "press_back", "restart_agent"}
# configure = waitForSelectorTimeout 0; click_miss = klik selector tanpa ketukan; click_sel = RPC klik gagal
READ_OPS = {"exists", "info", "info_any", "find_all", "current_app", "webview", "screenshot", "dump", "agent_alive",
            "last_toast", "clear_toast", "configure", "click_miss", "click_sel"}
ALLOWED_OPS = ACTION_OPS | READ_OPS | {"shell"}
# API driver yang sah (Protocol = U2Driver = FakeDriver = TimedDriver). Tidak ada set_text/send_keys/...
DRIVER_API = {"exists", "info", "info_any", "find_all", "click", "get_text", "current_app", "start_url",
              "swipe_refresh", "press_back", "webview_present", "screenshot", "last_toast", "clear_toast", "dump",
              "shell", "agent_alive", "restart_agent", "window_size", "click_sel", "configure_selector_click"}
TEXT_INPUT_API = {"set_text", "send_keys", "input_text", "clear_text", "type_text", "send_text", "set_value",
                  "press_key", "keyevent", "set_input_ime", "set_fastinput_ime", "send_action", "set_clipboard"}
# Nama RPC/metode uiautomator2 untuk mengetik/menekan tombol (camelCase server agent).
U2_TYPING = {"setText", "clearTextField", "injectInputEvent", "pressKeyCode", "sendKeys", "sendAction",
             "setClipboard"}
TYPING_RX = re.compile(r"\b(" + "|".join(sorted(TEXT_INPUT_API | U2_TYPING)) + r")\b")
# Pemanggilan uiautomator2 yang dipakai U2Driver: jsonrpc agent + utilitas device. Tanpa setText/injectInputEvent.
# getConfigurator/setConfigurator: hanya waitForSelectorTimeout = 0 untuk klik selector (bukan input)
U2_RPC = {"exist", "objInfo", "objInfoOfAllInstances", "click", "swipe", "pressKey", "getLastToast", "clearLastToast",
          "deviceInfo", "getConfigurator", "setConfigurator"}
U2_DEVICE_ATTRS = {"jsonrpc", "app_current", "window_size", "screenshot", "dump_hierarchy", "shell", "_check_alive",
                   "_dev", "_device_server_port", "stop_uiautomator", "start_uiautomator"}
# Perintah shell yang menutup/mematikan aplikasi atau mengetik/menekan tombol.
FORBIDDEN_SHELL = re.compile(r"force-stop|\bam\s+(kill|stop)|\bpm\s+clear|\binput\b|\bkill\b|monkey", re.I)


# ------------------------------------------------------------------ util


def _t(out) -> int:
    return int(out.open_at * 1000)


def _ops(out) -> list[str]:
    return [op for op, _ in out.driver.calls]


def _in_window(out, ms: int) -> bool:
    return _t(out) - 1000 <= ms <= _t(out) + 8000


def _reload_arrivals(out, method: str) -> list[int]:
    """Reload yang SAMPAI ke aplikasi (akhir gestur/intent) di sekitar jendela polling, ms server."""
    if method == "swipe":
        return [e["t_server_ms"] for e in out.kind("refresh")]
    return [e["t_server_ms"] for e in out.kind("intent") if e["t_server_ms"] >= _t(out) - 1000]


def _variant_taps(out) -> list[int]:
    """Tap chip variasi di jendela polling (pilih ulang setelah reload); tap saat arm (T-60 s) tidak ikut."""
    return [e["t_server_ms"] for e in out.kind("variant") if e["t_server_ms"] >= _t(out) - 1000]


def _gated(out) -> list[int]:
    """Aksi polling (klik Beli + reload) DAN pilih ulang variasi: semuanya lewat PollingGate."""
    acts = sorted(polling_actions(out) + _variant_taps(out))
    assert acts and all(_in_window(out, a) for a in acts), [a - _t(out) for a in acts]
    gaps = [b - a for a, b in zip(acts, acts[1:], strict=False)]
    assert all(g >= 425 for g in gaps), f"jarak antaraksi < 425 ms: {gaps}"
    return acts


def _hard_rules(out, *, ordered: bool = False) -> None:
    """Aturan keras di SEMUA skenario: "Buat Pesanan" hanya pada live sukses, tanpa input teks, tanpa tap di area
    tanpa elemen (kolom PIN), tanpa perintah shell penutup/pengetik, aksi polling patuh jendela & 400 ms."""
    orders = out.kind("order")
    if ordered:
        assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN and len(orders) == 1, out.result.message
        assert out.app.screen == "pin", "layar PIN dibiarkan (PIN diketik manual)"
        i = out.app.events.index(orders[0])
        after = [e for e in out.app.events[i + 1:] if e["kind"] != "settings"]  # pengaturan layar (close), bukan app
        assert after == [], f"aksi ke aplikasi setelah layar PIN muncul: {after}"
    else:
        assert orders == [], "tidak boleh ada klik 'Buat Pesanan'"
        assert "click_place_order" not in out.step_names()
        assert not [e for e in out.kind("tap") if e["detail"] == "place_order"]
    assert len([t for op, t in out.driver.calls if op == "click" and t == "Buat Pesanan"]) == len(orders)
    assert set(_ops(out)) <= ALLOWED_OPS, set(_ops(out)) - ALLOWED_OPS
    assert [e for e in out.kind("tap") if e["detail"] == "-"] == [], "tap di area tanpa elemen (mis. kolom PIN)"
    bad = [t for op, t in out.driver.calls if op == "shell" and FORBIDDEN_SHELL.search(t)]
    assert bad == [], f"perintah shell terlarang (menutup app / mengetik): {bad}"
    if polling_actions(out):
        assert_polling_rules(out)


def _left_open(out) -> None:
    """Aplikasi tidak ditutup: setelah hasil hanya diagnosa baca (aplikasi aktif, screenshot, dump)."""
    ops = _ops(out)
    assert ops[-3:] == ["current_app", "screenshot", "dump"], ops[-6:]
    t_result = out.result.step("result").t_server_ms
    assert all(e["t_server_ms"] < t_result for e in out.kind("back")), "press_back setelah hasil"


def _use_app(monkeypatch, app_cls: type[FakeShopeeApp]) -> None:
    monkeypatch.setattr(android_harness, "FakeShopeeApp", app_cls)


# ------------------------------------------------------------------ driver & aplikasi palsu tambahan


class _SpyDriver(FakeDriver):
    """FakeDriver yang juga mencatat waktu AWAL tiap reload (sebelum RPC) dan waktu tiap query info."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reload_starts: list[tuple[str, float]] = []
        self.infos: list[tuple[float, Sel]] = []

    def info(self, sel: Sel) -> Node | None:
        self.infos.append((self.clock.time(), sel))
        return super().info(sel)

    def swipe_refresh(self) -> None:
        self.reload_starts.append(("swipe", self.clock.time()))
        super().swipe_refresh()

    def start_url(self, url: str, package: str, wait: bool = True) -> None:
        if not wait:  # reload intent (tanpa -W); intent precheck/arm/login memakai -W
            self.reload_starts.append(("intent", self.clock.time()))
        super().start_url(url, package, wait)


@pytest.fixture(autouse=True)
def _spy(monkeypatch):
    monkeypatch.setattr(android_harness, "FakeDriver", _SpyDriver)


class _SoldOutThenNormalPriceApp(FakeShopeeApp):
    """Konfirmasi di sheet -> pesan "Stok habis" (Toast/overlay); halaman produk lalu kembali ke harga NORMAL dengan
    "Beli Sekarang" AKTIF (kuota flash sale habis, produk dijual biasa) - tanpa tombol "Habis" sama sekali."""

    def _r_product(self):
        if not self.sold_out:
            return super()._r_product()
        sc = self.sc
        return [("", Node(text="Detail Produk", bounds=(100, 60, 500, 110))),
                ("", Node(text=rupiah(sc.normal_price), bounds=(20, 610, 400, 690))),
                ("", Node(text=sc.product_name, bounds=(20, 700, 700, 740))),
                ("", Node(text="Masukkan Keranjang", bounds=(0, 1500, 360, 1612), clickable=True)),
                ("buy", Node(text="Beli Sekarang", bounds=(360, 1500, 720, 1612), clickable=True))]


class _PinAfterBuyApp(FakeShopeeApp):
    """Tap Beli berakhir di layar PIN ShopeePay (mis. sesi checkout lama): alat belum mengklik "Buat Pesanan"."""

    def _tap_buy(self, node: Node) -> None:
        super()._tap_buy(node)
        if node.enabled:
            self.screen = "pin"


class _StuckCartApp(FakeShopeeApp):
    """Tombol Checkout di keranjang tidak bereaksi (server macet): checkout tidak pernah muncul."""

    def _handle(self, key: str, node: Node) -> None:
        if key == "cart_checkout":
            self._event("cart_checkout_ignored")
            return
        super()._handle(key, node)


class _UnreadableChipApp(FakeShopeeApp):
    """Chip variasi di halaman produk tidak pernah melaporkan status terpilih (selected=False selalu)."""

    def _r_product(self):
        return [(k, replace(n, selected=False) if k.startswith("variant:") else n) for k, n in super()._r_product()]


class _SwipeResetsVariantApp(FakeShopeeApp):
    """Swipe-down memuat ulang halaman ke variasi BAWAAN (pilihan variasi hilang), seperti intent."""

    def on_refresh(self) -> None:
        was_product = self.screen == "product"
        super().on_refresh()
        if was_product:
            self.selected_variant = None


class _SwipeResetsUnreadableChipApp(_SwipeResetsVariantApp, _UnreadableChipApp):
    """Swipe mereset variasi DAN status terpilih chip tidak terbaca."""


class _LateChipApp(FakeShopeeApp):
    """Setelah reload di sekitar jendela polling, chip variasi baru tampil CHIP_DELAY_S kemudian (layout dimuat
    bertahap); harga & tombol Beli (variasi bawaan) sudah tampil lebih dulu."""

    CHIP_DELAY_S = 0.4

    def _r_product(self):
        out = super()._r_product()
        ra = self.refreshed_at
        if ra is not None and ra >= self.sc.open_at - 1 and self.now() < ra + self.CHIP_DELAY_S:
            out = [(k, n) for k, n in out if not k.startswith("variant:")]
        return out


class _RidApp(FakeShopeeApp):
    """Seperti aplikasi asli: konfirmasi sheet & "Buat Pesanan" punya resource-id (kalibrasi bisa menyimpannya)."""

    RIDS = {"confirm": "com.shopee.id:id/btn_sheet_confirm", "place_order": "com.shopee.id:id/btn_place_order"}

    def _render(self):
        return [(k, replace(n, rid=self.RIDS[k]) if k in self.RIDS else n) for k, n in super()._render()]


class _User:
    """Pengguna palsu kalibrasi: tiap prompt dicocokkan dengan awalan skrip, aksinya (pengguna men-tap HP SENDIRI)
    dijalankan, lalu dijawab."""

    def __init__(self, script: list[tuple[str, Callable[[], None] | None, str]]):
        self.script = list(script)

    def __call__(self, message: str) -> str:
        assert self.script, f"prompt tak terduga: {message!r}"
        prefix, action, answer = self.script.pop(0)
        assert message.lstrip().startswith(prefix), f"prompt {message!r}, diharapkan {prefix!r}"
        if action is not None:
            action()
        return answer


def _goto(app: FakeShopeeApp, screen: str) -> Callable[[], None]:
    def act() -> None:
        if screen == "checkout":
            app.checkout_items = [(app.sc.product_name, app.unit_price(), 1)]
            app.checkout_at = app.now()
        app.screen = screen

    return act


def _calibrate(tmp_path, monkeypatch) -> tuple[FakeShopeeApp, _SpyDriver, dict]:
    """calibrate --platform android lengkap (semua langkah, termasuk "Buat Pesanan" di layar checkout)."""
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), width=200))
    clock = FakeClock(tick=0.00005)
    app = _RidApp(AppScenario(open_at=clock.time() + 3600, variants=VARIANTS), clock)
    driver = _SpyDriver(app, clock)
    enter = ("Nomor elemen", None, "")  # Enter = kandidat no. 1
    user = _User([
        ("[buy_button]", _goto(app, "product"), ""), enter,
        ("[product_price]", None, "lewati"),
        ("[sheet_marker]", _goto(app, "sheet"), ""), enter,
        ("[variant_option]", None, ""), enter,
        ("[sheet_confirm]", None, ""), enter,
        ("[place_order]", _goto(app, "checkout"), ""), enter,
        ("[payment_change]", None, ""), enter,
        ("[payment_shopeepay]", _goto(app, "payment_list"), ""), enter,
        ("[payment_confirm]", None, ""), enter,
    ])
    path = tmp_path / "selectors.json"
    assert cli.calibrate_android(android_cfg(app.sc.open_at, variant=TARGET), driver, path, prompt=user) == 0
    assert user.script == [], "semua langkah kalibrasi ditanyakan"
    return app, driver, android_selectors.load(path).steps


# ------------------------------------------------------------------ K: stok habis lalu harga kembali normal


@pytest.mark.parametrize("toast_mode", ["toast", "node"], ids=["android_toast", "overlay_node"])
@pytest.mark.parametrize("normal_price", [150_000, 100_000], ids=["normal_di_atas_maks", "normal_dalam_maks"])
def test_sold_out_message_then_normal_price_with_active_buy_never_rebuys(tmp_path, monkeypatch, normal_price,
                                                                         toast_mode):
    """"Stok habis" setelah konfirmasi, lalu halaman produk menampilkan harga normal dengan "Beli Sekarang" aktif
    (tanpa tombol "Habis"): SOLD_OUT dari pesan itu saja; tidak ada klik Beli/konfirmasi ulang, juga bila harga
    normal masih <= max_item_price. Tanpa alarm, bukan stop-semua, aplikasi dibiarkan terbuka."""
    # terkait: test_android_safety.test_sold_out_on_confirm (fake jatuh ke tombol "Habis", tanpa harga normal)
    _use_app(monkeypatch, _SoldOutThenNormalPriceApp)
    stop = threading.Event()
    out = run_android(tmp_path, live=True, sold_out_on_confirm=True, normal_price=normal_price,
                      toast_mode=toast_mode, runner_attrs={"stop_event": stop})
    assert out.result.status == RunStatus.SOLD_OUT, out.result.message
    channel = "toast" if toast_mode == "toast" else "teks"
    assert out.result.message == f"{channel} 'Stok habis'"
    assert len(out.kind("buy")) == 1 and len(out.kind("confirm")) == 1, "tanpa beli/konfirmasi ulang"
    assert out.kind("checkout") == [] and out.kind("buy_disabled") == []
    confirm = out.kind("confirm")[0]
    assert out.app.events[out.app.events.index(confirm) + 1:] == [], "tidak ada aksi ke aplikasi setelah pesan habis"
    assert not stop.is_set() and out.events() == [], "SOLD_OUT: bukan stop-semua, tanpa alarm"
    _hard_rules(out)
    _left_open(out)
    # skenario memang nyata: saat hasil, halaman produk menampilkan harga normal & "Beli Sekarang" aktif
    assert out.app.screen == "product"
    labels = {n.label: n for n in out.app.nodes()}
    assert labels["Beli Sekarang"].enabled and rupiah(normal_price) in labels and "Habis" not in labels


# ------------------------------------------------------------------ K: 0 klik "Buat Pesanan" di semua status stop


STOP_CASES = [
    pytest.param(True, {"captcha_after_buy": True}, None, RunStatus.CAPTCHA, id="captcha"),
    pytest.param(True, {"verification_after_buy": True}, None, RunStatus.VERIFICATION, id="verification"),
    pytest.param(True, {"webview_after_buy": True}, None, RunStatus.VERIFICATION, id="webview_captcha"),
    pytest.param(True, {"foreign_after_buy": RECAPTCHA}, None, RunStatus.VERIFICATION, id="foreign_activity"),
    pytest.param(True, {"login_required": True}, None, RunStatus.LOGIN_REQUIRED, id="login_required"),
    pytest.param(True, {"sold_out": True}, None, RunStatus.SOLD_OUT, id="sold_out"),
    pytest.param(True, {"sold_out_on_confirm": True}, None, RunStatus.SOLD_OUT, id="sold_out_on_confirm"),
    pytest.param(True, {"flash_price": 120_000}, None, RunStatus.PRICE_GUARD, id="price_l1"),
    pytest.param(True, {"checkout_qty": 2}, None, RunStatus.PRICE_GUARD, id="qty_2"),
    pytest.param(True, {"checkout_name": "Casing Ponsel Lain"}, None, RunStatus.PRICE_GUARD, id="name_mismatch"),
    pytest.param(True, {"unknown_after_buy": True}, None, RunStatus.UNKNOWN_STATE, id="unknown_before_order"),
    pytest.param(True, {"crash_after_buy": True}, None, RunStatus.UNKNOWN_STATE, id="app_crash"),
    pytest.param(True, {"other_app_after_buy": True}, None, RunStatus.UNKNOWN_STATE, id="app_left_foreground"),
    pytest.param(True, {}, _PinAfterBuyApp, RunStatus.UNKNOWN_STATE, id="pin_after_buy"),
    pytest.param(True, {"sale_skew_ms": 9500}, None, RunStatus.NOT_STARTED_TIMEOUT, id="not_started_timeout"),
    pytest.param(True, {"go_cart": True}, _StuckCartApp, RunStatus.TIMEOUT, id="timeout_checkout_never_loads"),
    pytest.param(True, {"variants": VARIANTS, "cfg": {"variant": "512GB Emas"}}, None, RunStatus.ERROR,
                 id="error_variant_missing"),
    pytest.param(True, {"cfg": {"expected_name": None}}, None, RunStatus.ERROR, id="error_live_no_expected_name"),
    pytest.param(True, {"deny": True}, None, RunStatus.ABORTED, id="aborted_before_place_order"),
    pytest.param(False, {}, None, RunStatus.DRYRUN_OK, id="dry_run_ok"),
]


def test_stop_cases_cover_every_status_except_live_success():
    """Daftar STOP_CASES mencakup setiap RunStatus selain ORDER_PLACED_AWAIT_PIN (status baru wajib ditambahkan)."""
    covered = {c.values[3] for c in STOP_CASES}
    assert covered == set(RunStatus) - {RunStatus.ORDER_PLACED_AWAIT_PIN}, set(RunStatus) ^ covered


@pytest.mark.parametrize("live, scenario, app_cls, status", STOP_CASES)
def test_no_place_order_click_in_any_stop_status_or_dry_run(tmp_path, monkeypatch, live, scenario, app_cls, status):
    """Setiap status stop yang bisa dicapai LIVE (+ DRYRUN_OK): 0 event pesanan, 0 klik driver bertarget "Buat
    Pesanan", tanpa langkah click_place_order; lock before_place_order hanya diminta di ujung (ABORTED/DRYRUN_OK).
    Alarm hanya untuk status yang butuh tindakan manual. Captcha/verifikasi/UNKNOWN_STATE: stop semua runner +
    alarm + tanpa retry; aplikasi dibiarkan terbuka."""
    # terkait: test_android_runner.test_app_never_closed (subset status, tanpa cek lock & stop_event)
    scenario = dict(scenario)
    deny = scenario.pop("deny", False)
    asked: list[int] = []

    def before() -> bool:
        asked.append(1)
        return not deny

    if app_cls is not None:
        _use_app(monkeypatch, app_cls)
    stop = threading.Event()
    out = run_android(tmp_path, live=live, before=before, runner_attrs={"stop_event": stop}, **scenario)
    assert out.result.status == status, out.result.message
    _hard_rules(out)
    names = out.step_names()
    gate = out.result.step("place_order_gate")
    if status == RunStatus.DRYRUN_OK:
        assert gate is not None and gate.detail.startswith("dry-run")
    else:
        assert gate is None, "status stop tidak boleh sampai gerbang 'Buat Pesanan'"
    assert asked == ([1] if status in (RunStatus.ABORTED, RunStatus.DRYRUN_OK) else []), "lock diminta di luar ujung"
    assert stop.is_set() == (status in STOP_ALL_STATUSES), "stop-semua hanya untuk captcha/verifikasi/UNKNOWN_STATE"
    if status in ALARM_STATUSES:
        assert out.events(), f"{status} butuh tindakan manual: alarm wajib"
    else:
        assert out.events() == [], f"alarm palsu untuk {status}: {out.notifier.events}"
    if status in STOP_ALL_STATUSES:
        assert out.events() == [str(status)], out.notifier.events
        buys = out.kind("buy")
        assert len(buys) == 1 and names.count("click_buy") == 1, "tanpa retry"
        i = out.app.events.index(buys[0])
        assert out.app.events[i + 1:] == [], f"aksi ke aplikasi setelah deteksi: {out.app.events[i + 1:]}"
    if status == RunStatus.UNKNOWN_STATE:
        pin = app_cls is _PinAfterBuyApp
        # PIN tanpa klik "Buat Pesanan" dari alat: pesanan MUNGKIN terbuat; UNKNOWN biasa: penyebab asli dipakai
        assert (out.result.message == MAYBE_ORDERED_MSG) == pin, out.result.message
        assert out.runner.order_clicked == pin
    else:
        assert not out.runner.order_clicked
    if status != RunStatus.LOGIN_REQUIRED:  # LOGIN_REQUIRED berhenti di precheck (tanpa attempt)
        _left_open(out)
        shot = out.result.screenshots[0]
        assert shot.exists() and shot.with_suffix(".xml").exists(), "screenshot + dump status akhir"
    else:
        assert names == ["precheck"] and out.kind("tap") == []


def test_calibration_full_flow_never_taps_even_on_checkout(tmp_path, monkeypatch):
    """Kalibrasi semua langkah (variasi, konfirmasi sheet, "Buat Pesanan" di checkout, metode bayar): alat hanya
    membaca (dump/find_all/shell); 0 tap, 0 intent, 0 back; "Buat Pesanan" direkam dari dump tanpa query device."""
    # terkait: test_android_cli.test_calibrate_android_records_ordered_candidates_without_tapping (langkah dilewati)
    app, driver, steps = _calibrate(tmp_path, monkeypatch)
    assert steps["place_order"][:2] == [{"resourceId": _RidApp.RIDS["place_order"]}, {"text": "Buat Pesanan"}]
    assert steps["sheet_confirm"][0] == {"resourceId": _RidApp.RIDS["confirm"]}
    assert steps["variant_option"][0] == {"text": "{variant}"}
    assert app.screen == "payment_list", "layar hanya dipindah oleh pengguna"
    assert app.events == [], f"alat menyentuh aplikasi: {app.events}"
    ops = {op for op, _ in driver.calls}
    assert ops <= {"dump", "find_all", "shell"}, ops
    assert not ops & ACTION_OPS
    assert not [t for op, t in driver.calls if op == "find_all" and ("Buat" in t or "place_order" in t)]
    assert [t for op, t in driver.calls if op == "shell"] == [f"dumpsys package {PACKAGE}", "wm size"]


# ------------------------------------------------------------------ K: alat tidak pernah mengetik (PIN)


def _is_jsonrpc(node: ast.AST) -> bool:
    """`self.d.jsonrpc`."""
    return isinstance(node, ast.Attribute) and node.attr == "jsonrpc" and isinstance(node.value, ast.Attribute) \
        and node.value.attr == "d" and isinstance(node.value.value, ast.Name) and node.value.value.id == "self"


def _u2_static_calls(source: str) -> tuple[set[str], list[tuple[str, ...]], set[str], list[str]]:
    """Dari source android_driver.py: (metode jsonrpc U2Driver lewat _rpc("<konst>") ATAU self.d.jsonrpc.<nama>,
    argumen konstanta tiap _rpc, atribut self.d, pelanggaran: getattr dinamis pada jsonrpc di luar _rpc)."""
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "U2Driver")
    rpc, args, attrs, bad = set(), [], set(), []
    for node in ast.walk(cls):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_rpc" \
                and node.args and isinstance(node.args[0], ast.Constant):
            rpc.add(node.args[0].value)
            args.append(tuple(a.value for a in node.args if isinstance(a, ast.Constant)))
        if isinstance(node, ast.Attribute) and _is_jsonrpc(node.value):
            rpc.add(node.attr)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "d" \
                and isinstance(node.value.value, ast.Name) and node.value.value.id == "self":
            attrs.add(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
                and isinstance(node.args[0], ast.Attribute) and node.args[0].attr == "d":
            name = node.args[1]
            attrs.add(name.value if isinstance(name, ast.Constant) else "<dinamis>")
    allowed = {id(c) for f in ast.walk(cls) if isinstance(f, ast.FunctionDef) and f.name == "_rpc"
               for c in ast.walk(f) if isinstance(c, ast.Call)}
    for node in ast.walk(tree):  # seluruh modul: getattr(<...jsonrpc...>, nama) hanya boleh di U2Driver._rpc
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
                and node.args and id(node) not in allowed \
                and any(isinstance(a, ast.Attribute) and "jsonrpc" in a.attr for a in ast.walk(node.args[0])):
            bad.append(f"getattr jsonrpc baris {node.lineno}")
    return rpc, args, attrs, bad


def _typing_names(source: str, name: str = "<src>") -> list[str]:
    """Nama/atribut/fungsi/string konstanta (bukan docstring) yang menyebut API mengetik u2 (camelCase/snake)."""
    tree = ast.parse(source)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            s = node.attr
        elif isinstance(node, ast.Name):
            s = node.id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            s = node.name
        elif isinstance(node, ast.keyword) and node.arg:
            s = node.arg
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            s = node.value
        else:
            continue
        if m := TYPING_RX.search(s):
            hits.append(f"{name}:{getattr(node, 'lineno', '?')} {m.group(1)}")
    return hits


ANCHOR = 'self._call(f"click {x},{y}", lambda: self._rpc("click", x, y))\n'
INJECTED = [  # tiap baris = satu jalur mengetik yang harus tertangkap pemindai (disisipkan di U2Driver.click)
    'self.d.jsonrpc.setText(self._selector(Sel("className", "android.widget.EditText")), "123456")',
    "self.d.jsonrpc.pressKeyCode(66)",
    'getattr(self.d.jsonrpc, "press" + "KeyCode")(66)',
    'core._jsonrpc_call(self.d._dev, 0, "injectInputEvent", [], 5, False)',
    'self.d(className="android.widget.EditText").send_keys("123456")',
]


def test_tool_never_types_static_driver_has_no_text_input_path():
    """Tidak ada jalur mengetik di kode: API driver tanpa metode input teks; U2Driver hanya memanggil jsonrpc baca/
    tap/swipe/back (lewat _rpc maupun self.d.jsonrpc.<nama>), tanpa getattr dinamis pada jsonrpc di luar _rpc;
    nama API mengetik u2 (setText/injectInputEvent/pressKeyCode/send_keys/...) tidak muncul di modul Android & CLI.
    Pemindai dibuktikan menangkap tiap jalur yang disisipkan ke U2Driver.click."""
    # terkait: test_spec_i_no_apk_tampering_root_hook_or_private_api (hanya .send_keys/.set_text/.clear_text)
    for cls in (AndroidDriver, U2Driver, FakeDriver, TimedDriver):
        public = {n for n in dir(cls) if not n.startswith("_")}
        assert public == DRIVER_API, f"{cls.__name__}: {public ^ DRIVER_API}"
        assert not {n for n in dir(cls) if n.lstrip("_") in TEXT_INPUT_API | U2_TYPING}, cls.__name__
    src = (ROOT / "flashbuy" / "android_driver.py").read_text(encoding="utf-8")
    rpc, args, attrs, bad = _u2_static_calls(src)
    assert rpc <= U2_RPC, rpc - U2_RPC  # tanpa setText/clearTextField/injectInputEvent
    assert [a for a in args if a[0] == "pressKey"] == [("pressKey", "back")], "hanya tombol back"
    assert attrs <= U2_DEVICE_ATTRS, attrs - U2_DEVICE_ATTRS  # tanpa send_keys/set_text/clear_text/press
    assert bad == [], bad
    files = sorted((ROOT / "flashbuy").glob("android_*.py")) + [ROOT / "flashbuy" / "cli.py"]
    assert len(files) >= 5
    hits = [h for f in files for h in _typing_names(f.read_text(encoding="utf-8"), f.name)]
    assert hits == [], hits

    # pemindai benar-benar menangkap jalur yang lolos versi sebelumnya (jsonrpc mentah, konstanta RPC)
    assert src.count(ANCHOR) == 1, "jangkar U2Driver.click berubah: perbarui ANCHOR"
    indent = " " * 8
    mutated = src.replace(ANCHOR, ANCHOR + "".join(f"{indent}{line}\n" for line in INJECTED))
    rpc, _, attrs, bad = _u2_static_calls(mutated)
    assert rpc - U2_RPC == {"setText", "pressKeyCode"}, rpc - U2_RPC
    assert len(bad) == 1 and "getattr jsonrpc" in bad[0], bad
    found = {h.split()[-1] for h in _typing_names(mutated)}
    assert found >= {"setText", "pressKeyCode", "injectInputEvent", "send_keys"}, found


def test_pin_screen_without_text_after_place_order_left_alone(tmp_path):
    """Layar PIN setelah "Buat Pesanan" hanya dikenali dari resource-id (tanpa teks): ORDER_PLACED_AWAIT_PIN +
    alarm, kolom PIN tidak disentuh, tidak ada aksi apa pun ke aplikasi setelah klik "Buat Pesanan"."""
    # terkait: test_android_runner.test_live_places_one_order_and_stops_at_pin (layar PIN berteks)
    # terkait: test_android_runner.test_pin_screen_after_buy_click_is_unknown_state_and_left_alone (PIN setelah Beli)
    out = run_android(tmp_path, live=True, pin_title=None)
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    _hard_rules(out, ordered=True)
    names = out.step_names()
    assert names[-3:] == ["click_place_order", "pin_screen", "result"], names[-5:]
    assert [n.label for n in out.app.nodes()] == [""], "layar PIN memang tanpa teks (hanya kolom resource-id)"
    assert out.events() == [str(RunStatus.ORDER_PLACED_AWAIT_PIN)]
    assert out.runner.order_clicked
    _left_open(out)


# ------------------------------------------------------------------ E.5: jadwal refresh


@pytest.mark.parametrize("reload", ["swipe", "intent"])
def test_refresh_first_at_t_plus_0_5_then_every_2_s_through_rate_limiter(tmp_path, reload):
    """UI statis, slot server buka T+6 s: reload pertama MULAI di T+0,5..0,65 s, berikutnya mulai tiap 2..2,3 s;
    setiap awal reload = slot RateLimiter yang dipesan; semua aksi polling >= 425 ms & di T-1..T+8 s."""
    # terkait: test_android_runner.test_static_ui_needs_reload (hanya batas bawah >= 500 ms / >= 2000 ms)
    out = run_android(tmp_path, live_update=False, sale_skew_ms=6000, cfg={"android": {"reload": reload}})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    kinds = {k for k, _ in out.driver.reload_starts}
    assert kinds == {reload}, f"metode reload tercampur: {kinds}"
    starts = [round(t * 1000) - _t(out) for _, t in out.driver.reload_starts]
    assert len(starts) == 4 == out.step_names().count("reload"), starts
    assert 500 <= starts[0] <= 650, f"reload pertama mulai T+{starts[0]} ms"
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert all(2000 <= g <= 2300 for g in gaps), f"jarak awal reload {gaps}"
    slots = out.runner.limiter.history
    for _, t in out.driver.reload_starts:
        assert any(-0.001 <= t - s <= 0.010 for s in slots), "reload tanpa slot RateLimiter"
    arrivals = _reload_arrivals(out, reload)
    assert len(arrivals) == 4 and all(_in_window(out, a) for a in arrivals)
    poll_gaps = assert_polling_rules(out)
    assert all(g >= 425 for g in poll_gaps), poll_gaps
    buys = [e["t_server_ms"] for e in out.kind("buy")]
    assert len(buys) == 1 and buys[0] - arrivals[-1] >= 425, "Beli setelah reload terakhir, lewat gate"
    _hard_rules(out)
    _left_open(out)


# ------------------------------------------------------------------ E.5: pilih ulang variasi setelah refresh

VARIANT_RUN = {"variants": VARIANTS, "variants_on_page": True, "sheet": False, "live_update": False,
               "variant_prices": {TARGET: 95_000}}


def test_unreadable_variant_chip_reselected_once_per_intent_reload(tmp_path, monkeypatch):
    """Status terpilih chip tidak terbaca: setelah tiap reload intent (halaman dibuka ulang = variasi bawaan) chip
    di-tap ulang tepat sekali, lewat gate, sebelum reload berikutnya; harga lapis 1 & checkout = variasi target."""
    # terkait: test_android_runner.test_variant_on_page_reselected_once_after_reload (status chip terbaca)
    _use_app(monkeypatch, _UnreadableChipApp)
    out = run_android(tmp_path, button_enabled_before_open=True, flash_price=150_000, sale_skew_ms=2600,
                      cfg={"variant": TARGET, "android": {"reload": "intent"}}, **VARIANT_RUN)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    log = (out.log_dir / "android.log").read_text(encoding="utf-8")
    assert f"status terpilih variasi {TARGET!r} tidak terbaca" in log
    arm_taps = [e for e in out.kind("variant") if e["t_server_ms"] < _t(out) - 1000]
    assert len(arm_taps) == 1, "dipilih sekali saat arm"
    reloads = _reload_arrivals(out, "intent")
    retaps = _variant_taps(out)
    reselects = [s for s in out.result.steps if s.name == "variant_selected" and RESEL in s.detail]
    assert len(reloads) >= 2, reloads
    assert len(retaps) == len(reloads) == len(reselects), (reloads, retaps)
    for k, r in enumerate(reloads):
        nxt = reloads[k + 1] if k + 1 < len(reloads) else float("inf")
        assert r + 425 <= retaps[k] < nxt, f"pilih ulang #{k + 1} di luar (reload + 425 ms, reload berikutnya)"
    buys = [e["t_server_ms"] for e in out.kind("buy")]
    assert len(buys) == 1 and buys[0] > max([*reloads, *retaps])
    assert out.app.selected_variant == TARGET
    assert "harga Rp95.000" in out.result.step("buy_ready").detail
    assert "harga=Rp95.000" in out.result.step("price_guard_ok").detail
    _gated(out)
    _hard_rules(out)


@pytest.mark.parametrize("default_price", [150_000, 99_000], ids=["bawaan_di_atas_maks", "bawaan_dalam_maks"])
def test_swipe_that_resets_variant_reselects_readable_chip_then_orders_target(tmp_path, monkeypatch, default_price):
    """Swipe-down mengembalikan halaman ke variasi bawaan (status chip terbaca): chip target di-tap ulang tepat
    sekali, >= 425 ms setelah swipe sampai (gate), sebelum lapis 1 membaca harga; pesanan = variasi target."""
    # terkait: test_android_runner.test_variant_on_page_reselected_once_after_reload (swipe di fake tidak mereset)
    _use_app(monkeypatch, _SwipeResetsVariantApp)
    out = run_android(tmp_path, live=True, flash_price=default_price, cfg={"variant": TARGET}, **VARIANT_RUN)
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    (swipe,) = _reload_arrivals(out, "swipe")
    taps = [e["t_server_ms"] for e in out.kind("variant")]
    assert len(taps) == 2 and taps[0] < _t(out) - 1000, "sekali saat arm + sekali setelah swipe"
    assert taps[1] - swipe >= 425, f"pilih ulang {taps[1] - swipe} ms setelah swipe (tanpa gate)"
    reselects = [s for s in out.result.steps if s.name == "variant_selected" and RESEL in s.detail]
    assert len(reselects) == 1
    (buy,) = [e["t_server_ms"] for e in out.kind("buy")]
    assert buy - taps[1] >= 425
    buy_ready = out.result.step("buy_ready")
    assert "harga Rp95.000" in buy_ready.detail and buy_ready.t_server_ms > taps[1], "lapis 1 = harga variasi target"
    assert "harga=Rp95.000" in out.result.step("price_guard_ok").detail
    assert out.app.selected_variant == TARGET
    _gated(out)
    _hard_rules(out, ordered=True)


@pytest.mark.parametrize("default_price", [150_000, 99_000], ids=["bawaan_di_atas_maks", "bawaan_dalam_maks"])
def test_swipe_reset_with_unreadable_chip_reselects_once_and_orders_target(tmp_path, monkeypatch, default_price):
    """Swipe mereset variasi DAN status chip tidak terbaca: spesifikasi E.5 'pilih ulang variasi setelah refresh' ->
    chip target di-tap ulang tepat sekali setelah swipe (lewat gate), lalu lapis 1 membaca harga variasi target dan
    pesanan = variasi target (bukan variasi bawaan, baik harga bawaannya di atas maupun di dalam batas)."""
    _use_app(monkeypatch, _SwipeResetsUnreadableChipApp)
    out = run_android(tmp_path, live=True, flash_price=default_price, cfg={"variant": TARGET}, **VARIANT_RUN)
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    (swipe,) = _reload_arrivals(out, "swipe")
    taps = [e["t_server_ms"] for e in out.kind("variant")]
    assert len(taps) == 2 and taps[0] < _t(out) - 1000, "sekali saat arm + sekali setelah swipe"
    assert taps[1] - swipe >= 425, f"pilih ulang {taps[1] - swipe} ms setelah swipe (tanpa gate)"
    assert len(out.kind("order")) == 1 and out.app.selected_variant == TARGET
    assert "harga Rp95.000" in out.result.step("buy_ready").detail
    assert "harga=Rp95.000" in out.result.step("price_guard_ok").detail
    _hard_rules(out, ordered=True)
    _gated(out)


@pytest.mark.parametrize("default_price", [150_000, 99_000], ids=["bawaan_di_atas_maks", "bawaan_dalam_maks"])
def test_variant_chip_rendered_late_after_reload_is_awaited_then_reselected_then_buy(tmp_path, monkeypatch,
                                                                                      default_price):
    """Chip variasi baru tampil 0,4 s setelah reload intent: selama itu runner tidak membaca harga dan tidak mengklik
    Beli (harga variasi bawaan, juga yang <= maks, tidak boleh dipakai); begitu chip tampil -> pilih ulang (gate) ->
    harga variasi target -> Beli."""
    _use_app(monkeypatch, _LateChipApp)
    out = run_android(tmp_path, flash_price=default_price, cfg={"variant": TARGET, "android": {"reload": "intent"}},
                      **VARIANT_RUN)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    (reload,) = _reload_arrivals(out, "intent")
    chip_at = reload + int(_LateChipApp.CHIP_DELAY_S * 1000)
    (retap,) = _variant_taps(out)
    assert retap >= chip_at, f"chip di-tap T+{retap - _t(out)} ms, sebelum tampil (T+{chip_at - _t(out)} ms)"
    price_sel = Sel("textMatches", RP_SHORT_MATCH)
    early = [round(t * 1000) - _t(out) for t, s in out.driver.infos if s == price_sel and reload <= t * 1000 < retap]
    assert early == [], f"harga dibaca sebelum variasi dipilih ulang: {early}"
    assert not [s for s in out.result.steps if s.name in ("price_not_yet", "price_unreadable", "buy_ready")
                and s.t_server_ms < retap], "keputusan harga sebelum variasi dipilih ulang"
    buys = [e["t_server_ms"] for e in out.kind("buy")]
    assert len(buys) == 1 and buys[0] - retap >= 425
    assert "harga Rp95.000" in out.result.step("buy_ready").detail
    assert "harga=Rp95.000" in out.result.step("price_guard_ok").detail and out.app.selected_variant == TARGET
    _gated(out)
    _hard_rules(out)


# ------------------------------------------------------------------ I: larangan alat & API


_BANNED_WORDS = re.compile(r"\b(frida|jadx|apktool|(bak)?smali|magisk|(ls)?xposed|objection|substrate)\b|"
                           r"bypass\W*captcha|captcha\W*bypass", re.I)
_BANNED_STRINGS = {
    "su": re.compile(r"^\s*su(\s|$)|\bsu\s+-c\b", re.I),
    "root": re.compile(r"^root$|\badb\s+root\b", re.I),
    "input": re.compile(r"^input$|\binput\s+(text|tap|keyevent|swipe|press|roll)\b", re.I),
    "pm install": re.compile(r"\bpm\s+install\b", re.I),
    "setprop": re.compile(r"\bsetprop\b", re.I),
    "api privat": re.compile(r"/api/v\d", re.I),
    "captcha solver": re.compile(r"2captcha|anti-?captcha|capsolver|captcha.?solver", re.I),
    "tutup aplikasi": re.compile(r"force-stop|\bam\s+(kill|stop)\b|\bpm\s+clear\b", re.I),
}
_BANNED_MODULES = {"frida", "objection", "androguard", "subprocess", "ctypes", "appium", "pyaxmlparser"}
_BANNED_ATTRS = {"system", "popen", "app_install", "app_uninstall", "app_clear", "app_stop", "app_stop_all",
                 "send_keys", "set_text", "clear_text"}
_BANNED_DEPS = {"frida", "frida-tools", "objection", "androguard", "apktool", "jadx", "pyaxmlparser",
                "appium-python-client", "xposed", "magisk"}


def _spec_i_violations(source: str, name: str = "<src>") -> list[str]:
    """Pelanggaran spesifikasi I di kode (bukan komentar/docstring): nama, modul, atribut, string, dan perintah yang
    disusun dari list/tuple/argumen konstanta (mis. ["pm", "install"] -> "pm install")."""
    tree = ast.parse(source)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    found: list[str] = []
    for node in ast.walk(tree):
        idents: list[str] = []
        if isinstance(node, ast.Name):
            idents.append(node.id)
        elif isinstance(node, ast.Attribute):
            idents.append(node.attr)
            if node.attr in _BANNED_ATTRS:
                found.append(f"{name}: atribut .{node.attr}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            idents.append(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            found += [f"{name}: import {m}" for m in mods if m.split(".")[0] in _BANNED_MODULES]
            idents += [a.name for a in node.names]
        texts: list[str] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            texts.append(node.value)
        elif isinstance(node, (ast.List, ast.Tuple)) or (isinstance(node, ast.Call) and node.args):
            parts = node.elts if isinstance(node, (ast.List, ast.Tuple)) else node.args
            consts = [p.value for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)]
            if len(consts) > 1:
                texts.append(" ".join(consts))
        for s in idents + texts:
            if _BANNED_WORDS.search(s.replace("_", " ")):
                found.append(f"{name}: {s[:60]!r}")
        for label, rx in _BANNED_STRINGS.items():
            found += [f"{name}: {label} {s[:60]!r}" for s in texts if rx.search(s)]
    return found


def test_spec_i_no_apk_tampering_root_hook_or_private_api():
    """Spesifikasi I (statis): kode flashbuy/ dan dependensi pyproject.toml tanpa jadx/apktool/frida/xposed/magisk,
    tanpa su/root, tanpa `input` shell, `pm install`, setprop, API privat Shopee (/api/v*), solver captcha, atau
    subprocess (adb hanya lewat uiautomator2). Pemindai dibuktikan menangkap contoh pelanggaran."""
    bad = '''
import frida
import subprocess
def bypass_captcha(d):
    """dokumentasi boleh menyebut frida & jadx"""
    d.shell(["su", "-c", "setprop persist.x 1"])
    d.shell(["pm", "install", "patched.apk"])
    d.shell("input text 123456")
    d.shell(["input", "keyevent", "66"])
    d.send_keys("123456")
    run(["adb", "root"])
    fetch("https://shopee.co.id/api/v4/pdp/get_pc")
    os.system("apktool d shopee.apk && jadx shopee.apk")
    Xposed_hook = 1
'''
    hits = "\n".join(_spec_i_violations(bad))
    for needle in ("import frida", "import subprocess", "'su -c setprop", "pm install", "input 'input text",
                   "input 'input keyevent", ".send_keys", "root 'adb root'", "api privat", ".system", "apktool",
                   "Xposed_hook", "bypass_captcha"):
        assert needle in hits, f"pemindai tidak menangkap {needle!r}:\n{hits}"
    assert "dokumentasi" not in hits, "docstring tidak dipindai"

    files = sorted((ROOT / "flashbuy").glob("*.py"))
    assert len(files) >= 10 and any(f.name == "android_runner.py" for f in files)
    violations = [v for f in files for v in _spec_i_violations(f.read_text(encoding="utf-8"), f.name)]
    assert violations == [], violations

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = [*meta["dependencies"], *[d for ds in meta.get("optional-dependencies", {}).values() for d in ds]]
    names = {re.split(r"[\s<>=!~;\[]", d, maxsplit=1)[0].lower() for d in deps}
    assert not names & _BANNED_DEPS, names & _BANNED_DEPS
    assert "uiautomator2" in names, "interaksi Android hanya lewat uiautomator2"
