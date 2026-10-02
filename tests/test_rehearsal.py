"""Mode rehearsal (`flashbuy rehearse`): produk berharga normal sampai checkout, laporan lengkap, dan TIDAK PERNAH
"Buat Pesanan" (0 request web / 0 ketukan Android di semua skenario, termasuk PIN muncul setelah Beli).

Web: mock Shopee + Playwright headless; produk masih harga normal sebelum flash sale (tombol Beli aktif).
Android: FakeDriver + FakeShopeeApp (jam virtual), tombol Beli aktif sebelum slot buka."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

import tests.android_harness as harness
from flashbuy import android_selectors, selector_store
from flashbuy.android_driver import Node
from flashbuy.notifier import Notifier
from flashbuy.rehearsal import PlaceOrderForbidden, RehearsalReport, combined_exit, forbidden_gate
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunLog
from flashbuy.timesync import ServerClock
from flashbuy.web_runner import WebRunner
from tests.conftest import HEADLESS, LIVE_NAME, make_cfg, quiet_console
from tests.fake_android import FakeShopeeApp

WEB_STEPS = ["buka produk", "tombol Beli", "baca harga produk", "klik Beli", "baca keranjang", "klik Checkout",
             "halaman checkout", "ShopeePay", "baca checkout", "tombol Buat Pesanan (tidak diklik)"]
ANDROID_STEPS = ["mode ketuk", "buka produk", "tombol Beli", "baca harga produk", "klik Beli", "halaman checkout",
                 "ShopeePay", "baca checkout", "tombol Buat Pesanan (tidak diketuk)"]


# ------------------------------------------------------------------ web


def web_rehearse(mock, admin, tmp_path, *, scenario: str = "normal", selectors=None, cfg: dict | None = None,
                 **overrides):
    admin.scenario(name=scenario, open_in_ms=3_600_000, **{"buy_active_before_open": True, **overrides})
    st = admin.state()["scenario"]
    clock = ServerClock(st["clock_offset_ms"] / 1000)
    target = make_cfg(mock, tmp_path, st["open_at"], **(cfg or {}))
    log = RunLog(tmp_path / "logs", clock, "web", quiet_console())
    alarms: list[tuple[str, str]] = []
    notifier = Notifier(beep=lambda f, d: None, post=lambda u, p: None, repeat=1)
    notifier.alarm = lambda event, message, **kw: alarms.append((event, message))
    runner = WebRunner(target, selectors or selector_store.defaults(), log=log, notifier=notifier, headless=HEADLESS)
    runner.stop_event = threading.Event()

    async def go() -> RehearsalReport:
        await runner.prepare()
        try:
            return await runner.rehearse(clock)
        finally:
            await runner.close()

    rep = asyncio.run(go())
    log.close()
    return rep, runner, alarms


def _no_web_order(admin) -> None:
    assert admin.log("order") == [] and admin.log("pin") == [], "tidak ada request 'Buat Pesanan' / halaman PIN"


def test_web_rehearsal_normal_price_reaches_checkout_with_full_report(mock, admin, tmp_path):
    rep, runner, alarms = web_rehearse(mock, admin, tmp_path, cfg={"expected_name": LIVE_NAME})
    assert rep.ok and rep.exit_code() == 0, (rep.status, rep.message, rep.failed)
    assert [s.name for s in rep.steps] == WEB_STEPS
    for s in rep.steps:
        assert s.ok and s.latency_ms is not None and Path(s.screenshot).exists(), s
    by = {s.name: s for s in rep.steps}
    assert "Beli Sekarang" in by["tombol Beli"].selector and "Buat Pesanan" in by[WEB_STEPS[-1]].selector
    assert "Checkout" in by["klik Checkout"].selector and "ShopeePay" in by["ShopeePay"].selector
    assert by["buka produk"].values["nama cocok expected_name"] is True
    assert by["baca harga produk"].values["harga"] == 1_499_000  # harga normal tetap dibaca
    v = by["baca checkout"].values
    assert (v["qty"], v["harga"], v["ongkir"], v["total"], v["nama cocok"]) == (1, 1_499_000, 12_000, 1_511_000, True)
    # pengaman harga dievaluasi & dilaporkan, tidak ditegakkan
    assert rep.guards["lapis 1 harga produk"].startswith("akan gagal: harga Rp1.499.000 > maks Rp100.000")
    assert rep.guards["lapis 2 keranjang"].startswith("akan lolos")
    assert rep.guards["lapis 3 checkout"].startswith("akan gagal: harga Rp1.499.000 > maks Rp100.000")
    assert rep.cart_items == ["Ponsel Uji Coba 128GB"], "sisa keranjang dilaporkan untuk dihapus manual"
    data = json.loads(Path(rep.json_path).read_text(encoding="utf-8"))
    assert data["ok"] and data["exit_code"] == 0 and data["platform"] == "web"
    assert [s["name"] for s in data["steps"]] == WEB_STEPS and set(data["steps"][0]) >= {
        "name", "ok", "selector", "latency_ms", "values", "screenshot"}
    assert data["cart_items"] == ["Ponsel Uji Coba 128GB"]
    _no_web_order(admin)
    assert alarms == [] and runner.before_place_order is forbidden_gate and not runner.order_clicked


def test_web_rehearsal_reports_other_items_left_in_cart(mock, admin, tmp_path):
    rep, _, _ = web_rehearse(mock, admin, tmp_path, scenario="cart_other_checked", cfg={"expected_name": LIVE_NAME})
    assert rep.ok, (rep.status, rep.failed)
    assert rep.guards["lapis 2 keranjang"].startswith("akan lolos")  # item lain di-uncheck seperti run biasa
    assert rep.cart_items == ["Ponsel Uji Coba 128GB", "Kabel Data USB-C 1m", "Casing HP Bening"]
    _no_web_order(admin)


@pytest.mark.parametrize(("scenario", "status", "code"), [
    ("captcha", "CAPTCHA", 4), ("captcha_redirect", "CAPTCHA", 4), ("verification", "VERIFICATION", 4)])
def test_web_rehearsal_captcha_mid_flow_stops_with_alarm(mock, admin, tmp_path, scenario, status, code):
    rep, runner, alarms = web_rehearse(mock, admin, tmp_path, scenario=scenario)
    assert rep.status == status and rep.exit_code() == code, (rep.status, rep.message)
    assert [e for e, _ in alarms] == [status] and runner.stop_event.is_set()
    assert "klik Beli" not in [s.name for s in rep.steps] and rep.cart_items is None, "tanpa langkah lanjut"
    _no_web_order(admin)


def test_web_rehearsal_pin_after_buy_is_maybe_ordered_without_place_order(mock, admin, tmp_path):
    rep, runner, alarms = web_rehearse(mock, admin, tmp_path, scenario="pin_after_buy")
    assert rep.status == "UNKNOWN_STATE" and rep.exit_code() == 5
    assert rep.message.startswith(MAYBE_ORDERED_MSG)
    assert [e for e, _ in alarms] == ["UNKNOWN_STATE"] and runner.stop_event.is_set()
    assert admin.log("order") == [], "PIN muncul tanpa request 'Buat Pesanan' dari alat"


def test_web_rehearsal_selector_failure_names_the_step(mock, admin, tmp_path):
    sels = selector_store.defaults()
    sels.steps["cart_checkout"] = [{"text": "Tombol Yang Tidak Ada", "exact": True}]
    rep, _, _ = web_rehearse(mock, admin, tmp_path, selectors=sels)
    assert rep.status == "GAGAL" and rep.exit_code() == 1
    assert rep.failed == ["klik Checkout"]
    assert next(s for s in rep.steps if s.name == "klik Checkout").detail == "tombol Checkout tidak ditemukan"
    assert rep.cart_items == ["Ponsel Uji Coba 128GB"], "sisa keranjang tetap dilaporkan"
    _no_web_order(admin)


def test_web_place_order_click_function_refuses_rehearsal_and_dry_run(mock, tmp_path):
    clicked = []

    class Loc:
        async def click(self, **kw):
            clicked.append(kw)

    target = make_cfg(mock, tmp_path, 2_000_000_000, expected_name=LIVE_NAME)
    runner = WebRunner(target, selector_store.defaults(), log=RunLog(tmp_path / "logs", ServerClock(0.0), "web",
                                                                     quiet_console()),
                       notifier=Notifier(beep=lambda f, d: None, post=lambda u, p: None))
    for rehearsal, live in ((True, True), (True, False), (False, False)):
        runner.rehearsal, runner._live = rehearsal, live
        with pytest.raises(PlaceOrderForbidden):
            asyncio.run(runner._click_place_order(Loc()))
    assert clicked == [] and not runner.order_clicked
    with pytest.raises(PlaceOrderForbidden):
        forbidden_gate()


# ------------------------------------------------------------------ Android


class _PinAfterBuyApp(FakeShopeeApp):
    def _tap_buy(self, node):
        self._event("buy")
        self.screen = "pin"


def android_rehearse(tmp_path, monkeypatch=None, *, app_cls=None, selectors=None, **scenario):
    if app_cls is not None:
        monkeypatch.setattr(harness, "FakeShopeeApp", app_cls)
    runner, app, driver, sclock, open_at, log, notifier = harness.make_android(
        tmp_path, open_in_s=3600, selectors=selectors, **{"button_enabled_before_open": True, **scenario})
    runner.stop_event = threading.Event()

    async def go() -> RehearsalReport:
        await runner.prepare()
        try:
            return await runner.rehearse(sclock)
        finally:
            await runner.close()

    rep = asyncio.run(go())
    log.close()
    return rep, runner, app, driver, notifier


def _no_android_order(app, driver) -> None:
    assert app.kind("order") == [] and [e for e in app.events if e["detail"] == "place_order"] == []
    assert not [t for op, t in driver.calls if op in ("click", "click_sel") and "Buat Pesanan" in t]


def _alarms(notifier) -> list[str]:
    return [e["event"] for e in notifier.events if e["level"] == "alarm"]


@pytest.mark.parametrize("go_cart", [False, True], ids=["langsung_checkout", "lewat_keranjang"])
def test_android_rehearsal_normal_price_reaches_checkout_with_full_report(tmp_path, go_cart):
    rep, runner, app, driver, notifier = android_rehearse(tmp_path, go_cart=go_cart)
    assert rep.ok and rep.exit_code() == 0, (rep.status, rep.message, rep.failed)
    expected = list(ANDROID_STEPS)
    if go_cart:
        expected[5:5] = ["baca keranjang", "klik Checkout"]
    assert [s.name for s in rep.steps] == expected
    for s in rep.steps:
        assert s.ok and s.latency_ms is not None and Path(s.screenshot).exists(), s
    by = {s.name: s for s in rep.steps}
    assert by["tombol Beli"].selector == "text='Beli Sekarang'" and by["tombol Beli"].values["aktif"] is True
    assert by[ANDROID_STEPS[-1]].selector == "text='Buat Pesanan'"
    assert by["mode ketuk"].values == {"tap_mode": "selector"}
    assert by["baca harga produk"].values["harga"] == 150_000
    v = by["baca checkout"].values
    assert (v["qty"], v["harga"], v["ongkir"], v["total"]) == (1, 150_000, 10_000, 160_000)
    assert rep.guards["lapis 1 harga produk"] == "akan gagal: harga Rp150.000 > maks Rp100.000"
    assert rep.guards["lapis 3 checkout"].startswith("akan gagal: harga Rp150.000 > maks Rp100.000")
    assert ("lapis 2 keranjang" in rep.guards) == go_cart
    assert rep.cart_items == (["Ponsel Uji Coba 128GB"] if go_cart else [])
    assert json.loads(Path(rep.json_path).read_text(encoding="utf-8"))["exit_code"] == 0
    _no_android_order(app, driver)
    assert _alarms(notifier) == [] and runner.before_place_order is forbidden_gate


@pytest.mark.parametrize(("flag", "status"), [("captcha_after_buy", "CAPTCHA"),
                                              ("verification_after_buy", "VERIFICATION")])
def test_android_rehearsal_captcha_mid_flow_stops_with_alarm(tmp_path, flag, status):
    rep, runner, app, driver, notifier = android_rehearse(tmp_path, **{flag: True})
    assert rep.status == status and rep.exit_code() == 4
    assert _alarms(notifier) == [status] and runner.stop_event.is_set()
    assert rep.cart_items is None and app.screen == flag.split("_")[0], "layar dibiarkan, tanpa langkah lanjut"
    _no_android_order(app, driver)


def test_android_rehearsal_pin_after_buy_is_maybe_ordered_without_place_order(tmp_path, monkeypatch):
    rep, runner, app, driver, notifier = android_rehearse(tmp_path, monkeypatch, app_cls=_PinAfterBuyApp)
    assert rep.status == "UNKNOWN_STATE" and rep.exit_code() == 5 and rep.message.startswith(MAYBE_ORDERED_MSG)
    assert runner.order_clicked and runner.stop_event.is_set() and _alarms(notifier) == ["UNKNOWN_STATE"]
    _no_android_order(app, driver)


class _SlowSheetApp(FakeShopeeApp):
    """Bottom sheet baru tampil 300 ms setelah ketuk Beli (animasi/jaringan); halaman produk dengan banner
    "Flash Sale dimulai dalam .." masih terlihat sementara itu."""

    _pending: float | None = None

    def _tap_buy(self, node):
        super()._tap_buy(node)
        if self.screen == "sheet":
            self.screen, self._pending = "product", self.now() + 0.3

    def _render(self):
        if self._pending is not None and self.now() >= self._pending:
            self.screen, self._pending = "sheet", None
        return super()._render()


def test_android_rehearsal_pre_sale_banner_is_not_a_reaction_to_buy(tmp_path, monkeypatch):
    """Pra-sale: banner hitung mundur "dimulai dalam" selalu tampil; bukan reaksi "belum dimulai" atas klik Beli
    (baik saat halaman produk masih terlihat maupun saat sheet terbuka)."""
    rep, _, app, driver, _ = android_rehearse(tmp_path, monkeypatch, app_cls=_SlowSheetApp)
    assert rep.ok, (rep.status, rep.failed, [(s.name, s.detail) for s in rep.steps])
    assert any("Flash Sale dimulai dalam" in n.text for n in app.nodes() if n.text) or app.screen != "product"
    _no_android_order(app, driver)


def test_android_rehearsal_missing_buy_button_names_the_step(tmp_path):
    rep, _, app, driver, _ = android_rehearse(tmp_path, button_before_open="Ingatkan Saya",
                                              button_enabled_before_open=False)
    assert rep.status == "GAGAL" and rep.exit_code() == 1 and rep.failed == ["tombol Beli"]
    assert "jalankan calibrate" in next(s for s in rep.steps if s.name == "tombol Beli").detail
    assert app.kind("tap") == []
    _no_android_order(app, driver)


def test_android_rehearsal_unsafe_buy_selector_stops_before_any_tap(tmp_path):
    path = tmp_path / "selectors.json"
    android_selectors.save(path, {"buy_button": [{"textContains": "Pesan"}]}, {"app_version": "3.40.21"})
    rep, _, app, driver, _ = android_rehearse(tmp_path, selectors=android_selectors.load(path))
    assert rep.failed == ["mode ketuk"] and rep.exit_code() == 1
    assert "cocok dengan 'Buat Pesanan'" in rep.steps[0].detail
    assert app.kind("tap") == [] and app.kind("intent") == []


def test_android_place_order_tap_function_refuses_rehearsal_and_dry_run(tmp_path):
    runner, app, driver, *_ = harness.make_android(tmp_path)
    asyncio.run(runner.prepare())
    place = Node(text="Buat Pesanan", bounds=(520, 1500, 720, 1612))
    for rehearsal, live in ((True, True), (True, False), (False, False)):
        runner.rehearsal, runner._live = rehearsal, live
        with pytest.raises(PlaceOrderForbidden):
            runner._tap("place_order", place)
    assert driver.calls == [] and app.kind("tap") == []
    asyncio.run(runner.close())


def test_combined_exit_code():
    ok, fail, cap, unk = (RehearsalReport("web"), RehearsalReport("web", status="GAGAL"),
                          RehearsalReport("web", status="CAPTCHA"), RehearsalReport("web", status="UNKNOWN_STATE"))
    assert combined_exit([ok, ok]) == 0 and combined_exit([ok, fail]) == 1
    assert combined_exit([fail, cap]) == 4 and combined_exit([cap, unk]) == 5
