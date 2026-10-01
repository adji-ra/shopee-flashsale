"""Pengaman harga 3 lapis jalur Android (AndroidRunner + FakeDriver + FakeShopeeApp, waktu virtual).

Lapis 1 halaman produk: klik Beli hanya bila harga <= max_item_price.
Lapis 2 keranjang: hanya item target yang tercentang.
Lapis 3 checkout: ongkir & total stabil, tepat 1 baris, qty 1, nama, harga satuan & total dalam batas.
Setiap kasus yang diblokir: nol event "order" di aplikasi palsu + alarm PRICE_GUARD (fail-closed).
"""

from __future__ import annotations

import dataclasses

import pytest

from flashbuy import android_selectors
from flashbuy.android_driver import Node
from flashbuy.android_screen import checkout_snapshot
from flashbuy.pricing import rupiah
from flashbuy.runner_base import RunStatus
from tests import android_harness
from tests.android_harness import assert_polling_rules, polling_actions, run_android
from tests.fake_android import FakeShopeeApp

BOTH = pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])

MAIN_PRICE = (20, 610, 400, 690)  # harga utama halaman produk (FakeShopeeApp._r_product)
STRIKE_PRICE = (420, 640, 600, 670)  # harga coret halaman produk (lebih kecil)
BAR_TOTAL = (300, 1550, 510, 1595)  # nilai "Total Pembayaran" di bar bawah checkout
PRICE_RID = "com.shopee.id:id/tv_price"
TARGET = "Ponsel Uji Coba 128GB"
OTHER = "Kabel Data USB-C"


# ------------------------------------------------------------------ helper


def _run(tmp_path, monkeypatch=None, app_cls=None, **kw):
    """run_android dengan subkelas FakeShopeeApp (harness membuat app lewat nama modul)."""
    if app_cls is not None:
        monkeypatch.setattr(android_harness, "FakeShopeeApp", app_cls)
    return run_android(tmp_path, **kw)


def _no_order(out):
    assert out.kind("order") == [], "tidak boleh ada event buat pesanan"
    assert not out.runner.order_clicked
    assert "click_place_order" not in out.step_names()


def _alarmed(out, status=RunStatus.PRICE_GUARD):
    assert out.events() == [str(status)]


def _no_buy(out):
    assert out.kind("buy") == [] and out.kind("buy_disabled") == [], "tidak boleh klik Beli"
    assert "click_buy" not in out.step_names()


def _blocked(out, *reasons):
    """PRICE_GUARD: alasan ada di pesan, tidak sampai gerbang Buat Pesanan, nol order, alarm, screenshot."""
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    for r in reasons:
        assert r in out.result.message, out.result.message
    names = out.step_names()
    assert "place_order_gate" not in names and "price_guard_ok" not in names
    assert out.result.screenshots and out.result.screenshots[0].exists()
    _no_order(out)
    _alarmed(out)


def _passed(out, live):
    """Lolos 3 lapis: dry-run berhenti sebelum Buat Pesanan, live tepat satu pesanan lalu layar PIN."""
    if live:
        assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
        assert len(out.kind("order")) == 1
        assert out.result.step("click_place_order").t_server_ms >= out.result.step("price_guard_ok").t_server_ms
        _alarmed(out, RunStatus.ORDER_PLACED_AWAIT_PIN)
    else:
        assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
        _no_order(out)
        assert out.events() == []


def _checkouts(out) -> list[str]:
    return [e["detail"] for e in out.kind("checkout")]


def _rel(out, step: str) -> int:
    return out.result.step(step).t_server_ms - int(out.open_at * 1000)


def _actions_in_window(out):
    """Semua aksi polling (klik Beli + reload) di aplikasi berada dalam jendela T-1 s .. T+8 s."""
    t = int(out.open_at * 1000)
    acts = polling_actions(out)
    assert acts and all(t - 1000 <= a <= t + 8000 for a in acts), [a - t for a in acts]


def _replace(nodes, bounds, **changes):
    return [(k, dataclasses.replace(n, **changes) if n.bounds == bounds else n) for k, n in nodes]


# ------------------------------------------------------------------ app palsu tambahan (subkelas)


class _RbPrice(FakeShopeeApp):
    """Harga flash tampil dalam format singkat "Rp99rb" (tidak bisa diparse); harga coret tetap ada."""

    def _r_product(self):
        out = super()._r_product()
        if self.page_live():
            out = _replace(out, MAIN_PRICE, text=f"Rp{self.unit_price() // 1000}rb")
        return out


class _RbPriceNoStrike(_RbPrice):
    def _r_product(self):
        return [(k, n) for k, n in super()._r_product() if n.bounds != STRIKE_PRICE]


class _RangePrice(FakeShopeeApp):
    """Harga flash tampil sebagai rentang (mis. produk bervariasi): tidak bisa dipastikan."""

    def _r_product(self):
        out = super()._r_product()
        if self.page_live():
            out = _replace(out, MAIN_PRICE, text="Rp89.000 - Rp99.000")
        return out


class _RbPriceCheapChip(_RbPriceNoStrike):
    """Harga utama "Rp149rb" tak terbaca + chip kecil bernominal murah (mis. voucher/cicilan)."""

    def _r_product(self):
        out = super()._r_product()
        if self.page_live():
            out.append(("", Node(text="Rp10.000", bounds=(20, 1000, 200, 1030))))
        return out


def _calibrated_price_app(decoy: int):
    """Harga utama punya resourceId; ada nominal pengecoh yang lebih tinggi (banner) di atasnya."""

    class _CalibratedPrice(FakeShopeeApp):
        def _r_product(self):
            out = _replace(super()._r_product(), MAIN_PRICE, rid=PRICE_RID)
            out.append(("", Node(text=rupiah(decoy), bounds=(20, 300, 700, 500))))
            return out

    return _CalibratedPrice


def _calibrated_selectors(rid: str = PRICE_RID):
    sel = android_selectors.defaults()
    sel.steps["product_price"] = [{"resourceId": rid}]
    return sel


class _CartOtherFirst(FakeShopeeApp):
    """Item lain tampil DI ATAS item target di keranjang."""

    def _confirm(self):
        super()._confirm()
        if self.screen == "cart":
            self.cart = self.cart[1:] + self.cart[:1]


class _CartTargetUnchecked(FakeShopeeApp):
    """Satu-satunya item tercentang di keranjang BUKAN target."""

    def _confirm(self):
        super()._confirm()
        if self.screen == "cart":
            self.cart[0]["checked"] = False


class _EqualHeightStrike(FakeShopeeApp):
    """Harga coret di checkout setinggi harga jual (sebaris "x1"): tidak bisa dibedakan."""

    def _r_checkout(self):
        out = []
        for k, n in super()._r_checkout():
            if n.text == rupiah(self.sc.normal_price) and n.bounds[0] == 140:
                left, top, right, _ = n.bounds
                n = dataclasses.replace(n, bounds=(left, top - 7, right, top + 28))
            out.append((k, n))
        return out


class _TotalMismatch(FakeShopeeApp):
    """Bar bawah "Total Pembayaran" (tanpa ongkir) beda dengan rincian pembayaran."""

    def _r_checkout(self):
        return _replace(super()._r_checkout(), BAR_TOTAL, text=rupiah(99_000))


class _TwoCheckoutRows(FakeShopeeApp):
    """Checkout memuat item kedua (mis. sisa keranjang) di samping target."""

    def _enter_checkout(self, items):
        super()._enter_checkout([*items, (OTHER, 5_000, 1)])


# ------------------------------------------------------------------ lapis 1: halaman produk


@BOTH
def test_l1_flash_price_above_max_blocks_until_window_end(tmp_path, live):
    out = run_android(tmp_path, live=live, flash_price=129_000)
    _blocked(out, "jendela polling habis", "Rp129.000 > maks Rp100.000", "0 klik")
    _no_buy(out)
    assert "Rp129.000" in out.result.step("price_not_yet").detail
    assert _rel(out, "result") >= 8000  # diputuskan setelah jendela T+8 s habis
    assert out.step_names().count("reload") >= 2  # T+0,5 s lalu tiap 2 s, tetap dalam jendela
    assert out.kind("checkout") == []
    assert_polling_rules(out)


@BOTH
@pytest.mark.parametrize("live_update", [True, False], ids=["live_update", "static_ui"])
def test_l1_normal_price_with_enabled_button_before_open_waits_for_flash_price(tmp_path, live, live_update):
    # tombol Beli SUDAH aktif sebelum slot buka, tapi harga masih normal Rp150.000 > maks
    out = run_android(tmp_path, live=live, button_enabled_before_open=True, live_update=live_update)
    _passed(out, live)
    names = out.step_names()
    assert names.index("price_not_yet") < names.index("click_buy")
    assert "Rp150.000" in out.result.step("price_not_yet").detail
    assert "Rp99.000" in out.result.step("buy_ready").detail
    t = int(out.open_at * 1000)
    buys = out.kind("buy")
    assert len(buys) == 1 and buys[0]["t_server_ms"] >= t, "klik Beli sebelum harga flash valid"
    assert out.kind("buy_disabled") == []
    assert _checkouts(out) == [f"[('{TARGET}', 99000, 1)]"]  # checkout dengan harga flash, bukan normal
    if live_update:
        assert_polling_rules(out)
    else:  # reload -> klik Beli: jarak 400 ms diuji terpisah (xfail, lihat tes di bawah)
        _actions_in_window(out)
        assert _rel(out, "reload") < _rel(out, "click_buy")


@pytest.mark.xfail(strict=True, reason=(
    "BUG: RateLimiter memberi jarak 425 ms antar AWAL aksi, tapi swipe_refresh baru berefek di akhir gestur "
    "(~150 ms; reload intent ~300 ms) sedangkan tap berefek ~20 ms -> reload lalu klik Beli tiba di aplikasi "
    "hanya ~295 ms (intent ~145 ms) berselang, melanggar maks 1 aksi polling per 400 ms"))
@pytest.mark.parametrize("reload", ["swipe", "intent"])
def test_l1_reload_then_buy_click_keeps_400ms_gap_at_app(tmp_path, reload):
    # UI statis: harga flash baru tampil setelah reload T+0,5 s, lalu tombol (aktif sejak awal) diklik
    out = run_android(tmp_path, button_enabled_before_open=True, live_update=False,
                      cfg={"android": {"reload": reload}})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert_polling_rules(out)


@BOTH
@pytest.mark.parametrize("app_cls, shown", [
    (_RbPriceNoStrike, "''"),
    (_RangePrice, "Rp89.000 - Rp99.000"),
], ids=["rb_no_strike", "range"])
def test_l1_unreadable_price_never_clicks(tmp_path, monkeypatch, live, app_cls, shown):
    out = _run(tmp_path, monkeypatch, app_cls, live=live)
    _blocked(out, "jendela polling habis", "harga tidak terbaca", shown)
    _no_buy(out)
    assert out.result.step("price_unreadable") is not None
    assert _rel(out, "result") >= 8000
    assert_polling_rules(out)


@BOTH
def test_l1_unreadable_price_with_strike_price_never_clicks(tmp_path, monkeypatch, live):
    # harga flash "Rp99rb" sebenarnya <= maks, tapi tidak terbaca -> tidak boleh klik (fail-closed)
    out = _run(tmp_path, monkeypatch, _RbPrice, live=live)
    _blocked(out, "jendela polling habis")
    _no_buy(out)
    assert_polling_rules(out)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_screen.pick_main_price hanya menimbang node yang cocok PRICE_MATCH; teks ber-'Rp' yang "
    "paling tinggi tapi formatnya rusak ('Rp99rb') diabaikan dan lapis 1 diam-diam memakai nominal lain "
    "(harga coret / chip murah) sebagai harga produk, padahal checkout_snapshot sengaja mencegah hal ini"))
@pytest.mark.parametrize("app_cls, scenario", [
    (_RbPrice, {}),
    (_RbPriceCheapChip, {"flash_price": 149_000}),
], ids=["falls_back_to_strike", "falls_back_to_cheap_chip"])
def test_l1_unparseable_main_price_is_unreadable_not_replaced(tmp_path, monkeypatch, app_cls, scenario):
    out = _run(tmp_path, monkeypatch, app_cls, **scenario)
    _no_order(out)
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    _no_buy(out)  # chip Rp10.000 tidak boleh dianggap harga produk Rp149rb
    assert "harga tidak terbaca" in out.result.message, out.result.message  # bukan "harga Rp150.000 > maks"


@BOTH
@pytest.mark.parametrize("variants_on_page, reload, layer", [
    (True, "swipe", 1),
    (True, "intent", 1),
    (False, "swipe", 3),
], ids=["on_page", "on_page_intent_reload", "in_sheet"])
def test_l1_variant_with_flash_price_too_high_never_ordered(tmp_path, live, variants_on_page, reload, layer):
    out = run_android(tmp_path, live=live, variants=["128GB Hitam", "256GB Biru"],
                      variant_prices={"256GB Biru": 129_000}, variants_on_page=variants_on_page,
                      cfg={"variant": "256GB Biru", "android": {"reload": reload}})
    _blocked(out, "Rp129.000 > maks Rp100.000")
    if layer == 1:  # variasi terpilih di halaman produk -> harga variasi terbaca sebelum klik Beli
        _no_buy(out)
        assert "jendela polling habis" in out.result.message
        assert out.kind("checkout") == []
        assert out.app.selected_variant == "256GB Biru"
        assert_polling_rules(out)
    else:  # variasi baru dipilih di bottom sheet -> diblokir lapis 3 di checkout
        assert len(out.kind("buy")) == 1
        assert out.kind("variant") and out.kind("variant")[-1]["detail"] == "256GB Biru"
        assert _checkouts(out) == [f"[('{TARGET}', 129000, 1)]"]
        assert "checkout_loaded" in out.step_names()


@BOTH
def test_l1_calibrated_price_selector_beats_taller_decoy(tmp_path, monkeypatch, live):
    # banner Rp500.000 lebih tinggi dari harga: heuristik akan memblokir, selector kalibrasi membaca Rp99.000
    out = _run(tmp_path, monkeypatch, _calibrated_price_app(500_000), live=live,
               selectors=_calibrated_selectors())
    _passed(out, live)
    assert "Rp99.000" in out.result.step("buy_ready").detail
    assert ("info", f"resourceId='{PRICE_RID}'") in out.driver.calls
    assert_polling_rules(out)


@BOTH
def test_l1_calibrated_price_selector_blocks_despite_cheap_decoy(tmp_path, monkeypatch, live):
    # nominal pengecoh Rp5.000 lebih tinggi; harga sebenarnya (resourceId) Rp129.000 > maks
    out = _run(tmp_path, monkeypatch, _calibrated_price_app(5_000), live=live, flash_price=129_000,
               selectors=_calibrated_selectors())
    _blocked(out, "jendela polling habis", "Rp129.000 > maks Rp100.000")
    _no_buy(out)
    assert "Rp5.000" not in out.result.message
    assert_polling_rules(out)


def test_l1_heuristic_without_calibration_reads_decoy_but_layer3_still_blocks(tmp_path, monkeypatch):
    # kontrol: tanpa kalibrasi, heuristik "teks tertinggi" membaca pengecoh -> lapis 3 tetap menahan
    out = _run(tmp_path, monkeypatch, _calibrated_price_app(5_000), live=True, flash_price=129_000)
    assert "Rp5.000" in out.result.step("buy_ready").detail
    _blocked(out, "harga Rp129.000 > maks Rp100.000")
    assert _checkouts(out) == [f"[('{TARGET}', 129000, 1)]"]


def test_l1_calibrated_price_selector_not_found_is_fail_closed(tmp_path):
    # selector kalibrasi usang (resourceId tidak ada): tidak jatuh ke heuristik, harga = tidak terbaca
    out = run_android(tmp_path, selectors=_calibrated_selectors("com.shopee.id:id/hilang"))
    _blocked(out, "harga tidak terbaca")
    _no_buy(out)
    assert_polling_rules(out)


# ------------------------------------------------------------------ lapis 2: keranjang


CART_OTHER = {"go_cart": True, "cart_other_items": [(OTHER, 25_000, True)]}


@BOTH
def test_l2_other_checked_item_is_unchecked_then_only_target_proceeds(tmp_path, live):
    out = run_android(tmp_path, live=live, cfg={"expected_name": "Uji Coba"}, **CART_OTHER)
    _passed(out, live)
    assert OTHER in out.result.step("cart_uncheck").detail
    assert [e["detail"] for e in out.kind("cart_toggle")] == [OTHER]
    assert "1 item tercentang" in out.result.step("cart_ok").detail
    assert _checkouts(out) == [f"[('{TARGET}', 99000, 1)]"]  # hanya target yang masuk checkout
    names = out.step_names()
    assert names.index("cart_ok") < names.index("click_checkout") < names.index("price_guard_ok")


def test_l2_other_checked_item_without_expected_name_is_price_guard(tmp_path):
    # dry-run tanpa expected_name: target tidak bisa dipastikan -> tidak menebak, tidak meng-uncheck
    out = run_android(tmp_path, **CART_OTHER)
    _blocked(out, "keranjang", "target tidak bisa dipastikan")
    assert out.kind("cart_toggle") == [] and out.kind("checkout") == []


@BOTH
def test_l2_uncheck_fails_is_price_guard(tmp_path, live):
    out = run_android(tmp_path, live=live, cfg={"expected_name": "Uji Coba"}, cart_uncheck_fails=True,
                      **CART_OTHER)
    _blocked(out, "keranjang", "2 item tercentang", OTHER)
    assert len(out.kind("cart_toggle")) == 1  # dicoba sekali, lalu dibaca ulang
    assert out.kind("checkout") == []
    assert "cart_ok" not in out.step_names()


@BOTH
@pytest.mark.parametrize("expected", ["Uji Coba", "Ponsel Uji Coba 128GB"])
def test_l2_two_target_like_items_are_ambiguous(tmp_path, live, expected):
    out = run_android(tmp_path, live=live, cfg={"expected_name": expected}, go_cart=True,
                      cart_other_items=[("Ponsel Uji Coba 128GB Bekas", 50_000, True)])
    _blocked(out, "keranjang", "target tidak bisa dipastikan", repr(expected))
    assert out.kind("cart_toggle") == [], "tidak boleh meng-uncheck salah satu item secara tebakan"
    assert out.kind("checkout") == []


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_screen.cart_rows memakai pita baris dari checkbox ini sampai TOP checkbox berikutnya, "
    "sehingga nama item berikutnya (tergambar di atas checkbox-nya) ikut masuk ke baris sebelumnya; "
    "item lain di atas target jadi 'cocok' expected_name -> PRICE_GUARD 'target tidak bisa dipastikan'"))
def test_l2_other_item_listed_above_target_is_unchecked(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, _CartOtherFirst, live=True, **CART_OTHER)
    _passed(out, live=True)
    assert [e["detail"] for e in out.kind("cart_toggle")] == [OTHER]
    assert _checkouts(out) == [f"[('{TARGET}', 99000, 1)]"]


def test_l2_other_item_listed_above_target_never_orders_both(tmp_path, monkeypatch):
    # apa pun hasil lapis 2 pada tata letak ini: tidak pernah checkout/pesan dua item
    out = _run(tmp_path, monkeypatch, _CartOtherFirst, live=True, **CART_OTHER)
    assert all(OTHER not in d for d in _checkouts(out)), _checkouts(out)
    assert len(out.kind("order")) <= 1
    if out.result.status != RunStatus.ORDER_PLACED_AWAIT_PIN:
        _blocked(out)


def test_l2_single_checked_non_target_never_ordered(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, _CartTargetUnchecked, live=True, **CART_OTHER)
    _blocked(out, "nama produk tidak memuat 'Uji Coba'")


@pytest.mark.xfail(strict=True, reason=(
    "BUG: lapis 2 (pricing.check_cart) menganggap 'tepat 1 item tercentang' = OK tanpa memeriksa bahwa item "
    "itu target walau expected_name diisi; item lain lolos ke checkout dan baru ditahan lapis 3"))
def test_l2_single_checked_non_target_is_blocked_in_cart(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, _CartTargetUnchecked, live=True, **CART_OTHER)
    _blocked(out, "keranjang")
    assert out.kind("checkout") == [], "item non-target tidak boleh sampai checkout"


# ------------------------------------------------------------------ lapis 3: checkout


@BOTH
@pytest.mark.parametrize("scenario, cfg, reasons", [
    ({"checkout_qty": 2}, {}, ["kuantitas 2, bukan 1", "jumlah produk pesanan [2]"]),
    ({"checkout_name": "Ponsel Palsu KW"}, {"expected_name": "Uji Coba"}, ["nama produk tidak memuat 'Uji Coba'"]),
    ({"price_format_broken": True}, {}, ["harga satuan tidak terbaca"]),
    ({"service_fee": 15_000}, {}, ["total Rp124.000 > maks Rp120.000"]),
    ({"shipping_delay_ms": 500}, {"max_total": 105_000}, ["total Rp109.000 > maks Rp105.000"]),
    ({"shipping_delay_ms": 5_000}, {}, ["total checkout tidak stabil/terbaca", "ongkir ?"]),
], ids=["qty2", "nama", "format", "biaya_layanan", "ongkir_telat_total_final", "ongkir_tidak_siap"])
def test_l3_checkout_guard_blocks(tmp_path, live, scenario, cfg, reasons):
    out = run_android(tmp_path, live=live, cfg=cfg, **scenario)
    _blocked(out, *reasons)
    assert len(out.kind("buy")) == 1
    assert "checkout_loaded" in out.step_names() and "payment_ok" in out.step_names()


@BOTH
def test_l3_delayed_shipping_waits_for_final_total(tmp_path, live):
    out = run_android(tmp_path, live=live, shipping_delay_ms=500)
    _passed(out, live)
    guard = out.result.step("price_guard_ok")
    assert "ongkir=Rp10.000" in guard.detail and "total=Rp109.000" in guard.detail
    assert guard.t_server_ms - out.result.step("checkout_loaded").t_server_ms >= 450


def test_l3_shipping_never_ready_waits_full_stability_timeout(tmp_path):
    out = run_android(tmp_path, shipping_delay_ms=5_000)
    _blocked(out, "tidak stabil")
    waited = out.result.step("result").t_server_ms - out.result.step("payment_ok").t_server_ms
    assert 1_400 <= waited <= 2_500, waited


@BOTH
def test_l3_strike_price_on_qty_row_is_not_unit_price(tmp_path, live):
    # harga coret Rp150.000 (> maks) sebaris dengan "x1"; harga jual Rp99.000 lebih tinggi teksnya
    out = run_android(tmp_path, live=live, strike_in_checkout=True, normal_price=150_000)
    _passed(out, live)
    assert "harga=Rp99.000" in out.result.step("price_guard_ok").detail
    if not live:  # layar checkout masih tampil: baris produk tidak memuat harga coret
        rows = checkout_snapshot(out.app.nodes()).rows
        assert len(rows) == 1 and "Rp99.000" in rows[0] and "Rp150.000" not in rows[0], rows


@BOTH
def test_l3_equal_height_prices_on_qty_row_use_max(tmp_path, monkeypatch, live):
    out = _run(tmp_path, monkeypatch, _EqualHeightStrike, live=live)
    _blocked(out, "harga Rp150.000 > maks Rp100.000", "harga=Rp150.000")


def test_l3_equal_height_prices_within_limit_pass(tmp_path, monkeypatch):
    # dua nominal setinggi sama, keduanya <= maks -> maksimum (Rp100.000) dipakai dan lolos
    out = _run(tmp_path, monkeypatch, _EqualHeightStrike, normal_price=100_000)
    _passed(out, live=False)
    assert "harga=Rp100.000" in out.result.step("price_guard_ok").detail


@BOTH
def test_l3_total_mismatch_between_total_labels(tmp_path, monkeypatch, live):
    out = _run(tmp_path, monkeypatch, _TotalMismatch, live=live)
    _blocked(out, "total checkout tidak stabil/terbaca", "total ?")


@BOTH
def test_l3_two_product_rows_in_checkout(tmp_path, monkeypatch, live):
    out = _run(tmp_path, monkeypatch, _TwoCheckoutRows, live=live)
    _blocked(out, "2 baris produk (harus tepat 1)", "jumlah produk pesanan [2]")
    assert _checkouts(out) == [f"[('{TARGET}', 99000, 1), ('{OTHER}', 5000, 1)]"]
