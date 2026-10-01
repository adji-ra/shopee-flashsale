"""Pengaman harga 3 lapis jalur Android (AndroidRunner + FakeDriver + FakeShopeeApp, waktu virtual).

Lapis 1 halaman produk: klik Beli hanya bila harga <= max_item_price. Harga = selector kalibrasi
(product_price) atau teks Rp PENDEK PERTAMA dalam urutan dokumen (satu query info di hot path);
format rusak ("Rp99rb", rentang) ikut terbaca dan dilaporkan "harga tidak terbaca ('...')".
Lapis 2 keranjang: hanya item target yang tercentang (ketat bila expected_name diisi).
Lapis 3 checkout: ongkir & total stabil, tepat 1 baris, qty 1, nama (kolom produk), variasi, harga satuan
& total dalam batas; label baris harus DIAWALI label; testID + pasangan label harus sepakat.
Setiap kasus yang diblokir: nol event "order" di aplikasi palsu + alarm PRICE_GUARD (fail-closed).
"""

from __future__ import annotations

import dataclasses

import pytest

from flashbuy import android_selectors
from flashbuy.android_driver import FakeDriver, Node, Sel
from flashbuy.android_screen import RP_SHORT_MATCH, checkout_snapshot
from flashbuy.pricing import rupiah
from flashbuy.runner_base import RunStatus
from tests import android_harness
from tests.android_harness import assert_polling_rules, run_android
from tests.fake_android import FakeShopeeApp

BOTH = pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])

MAIN_PRICE = (20, 610, 400, 690)  # harga utama halaman produk (FakeShopeeApp._r_product)
STRIKE_PRICE = (420, 640, 600, 670)  # harga coret halaman produk (lebih kecil)
DECOY = (20, 300, 700, 500)  # banner/voucher bernominal di atas harga utama (lebih tinggi)
BAR_TOTAL = (300, 1550, 510, 1595)  # nilai "Total Pembayaran" di bar bawah checkout (testID labelTotalPayment)
PRICE_RID = "com.shopee.id:id/tv_price"
TARGET = "Ponsel Uji Coba 128GB"
OTHER = "Kabel Data USB-C"
VARIANTS = ["128GB Hitam", "256GB Biru"]


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


def _between(out, a: str, b: str) -> int:
    return out.result.step(b).t_server_ms - out.result.step(a).t_server_ms


def _replace(nodes, bounds, **changes):
    return [(k, dataclasses.replace(n, **changes) if n.bounds == bounds else n) for k, n in nodes]


def _nbsp(nodes):
    """Nominal dengan NBSP setelah "Rp" (umum di UI). Di regex Java NBSP BUKAN \\s."""
    return [(k, dataclasses.replace(n, text=n.text.replace("Rp", "Rp ")) if n.text.startswith("Rp") else n)
            for k, n in nodes]


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


class _NoPrice(FakeShopeeApp):
    """Saat sale berjalan harga tidak tampil sama sekali (mis. masih dimuat): tidak ada teks Rp."""

    def _r_product(self):
        out = super()._r_product()
        if self.page_live():
            out = [(k, n) for k, n in out if n.bounds not in (MAIN_PRICE, STRIKE_PRICE)]
        return out


class _RbPriceCheapChip(_RbPriceNoStrike):
    """Harga utama "Rp149rb" tak terbaca + chip kecil bernominal murah (mis. voucher/cicilan) di bawahnya."""

    def _r_product(self):
        out = super()._r_product()
        if self.page_live():
            out.append(("", Node(text="Rp10.000", bounds=(20, 1000, 200, 1030))))
        return out


def _decoy_price_app(decoy: str, *, before: bool, rid: str | None = None):
    """Nominal pengecoh (banner/voucher, lebih tinggi dari harga) di halaman produk, SEBELUM (urutan dokumen)
    atau SESUDAH harga utama. rid: harga utama diberi resourceId (target selector kalibrasi)."""

    class _DecoyPrice(FakeShopeeApp):
        def _r_product(self):
            out = super()._r_product()
            if rid:
                out = _replace(out, MAIN_PRICE, rid=rid)
            node = ("", Node(text=decoy, bounds=DECOY))
            return [out[0], node, *out[1:]] if before else [*out, node]

    return _DecoyPrice


def _calibrated_selectors(rid: str = PRICE_RID):
    sel = android_selectors.defaults()
    sel.steps["product_price"] = [{"resourceId": rid}]
    return sel


class _NbspPrices(FakeShopeeApp):
    """Semua nominal di halaman produk & checkout memakai NBSP ("Rp 99.000")."""

    def _r_product(self):
        return _nbsp(super()._r_product())

    def _r_checkout(self):
        return _nbsp(super()._r_checkout())


class _StampedDriver(FakeDriver):
    """FakeDriver yang mencatat waktu (ms) setiap RPC, untuk memeriksa isi hot path."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stamps: list[tuple[int, str, str]] = []

    def _rpc(self, op, target="", latency=None):
        self.stamps.append((int(self.clock.time() * 1000), op, str(target)))
        super()._rpc(op, target, latency)


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


class _CartNameBesideBox(FakeShopeeApp):
    """Nama item sebaris dengan checkbox-nya (tata letak lain; bawaan FakeShopeeApp: nama di atas checkbox)."""

    def _r_cart(self):
        names = {c["name"] for c in self.cart}
        out = []
        for k, n in super()._r_cart():
            if n.text in names:
                left, top, right, _ = n.bounds
                n = dataclasses.replace(n, bounds=(left, top + 45, right, top + 85))
            out.append((k, n))
        return out


def _slow_uncheck_app(delay_s: float):
    """Centang keranjang baru berubah setelah round-trip server `delay_s`."""

    class _SlowUncheck(FakeShopeeApp):
        def __init__(self, sc, clock):
            super().__init__(sc, clock)
            self.pending: list[tuple[float, int]] = []

        def _handle(self, key, node):
            if key.startswith("cartbox:"):
                i = int(key.split(":")[1])
                self._event("cart_toggle", self.cart[i]["name"])
                self.pending.append((self.now() + delay_s, i))
                return
            super()._handle(key, node)

        def _r_cart(self):
            for due, i in [p for p in self.pending if self.now() >= p[0]]:
                self.cart[i]["checked"] = not self.cart[i]["checked"]
                self.pending.remove((due, i))
            return super()._r_cart()

    return _SlowUncheck


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
    """Bar bawah "Total Pembayaran" (testID, tanpa ongkir) beda dengan rincian pembayaran."""

    def _r_checkout(self):
        return _replace(super()._r_checkout(), BAR_TOTAL, text=rupiah(99_000))


class _TwoCheckoutRows(FakeShopeeApp):
    """Checkout memuat item kedua (mis. sisa keranjang) di samping target."""

    def _enter_checkout(self, items):
        super()._enter_checkout([*items, (OTHER, 5_000, 1)])


class _CheckoutPromoTexts(FakeShopeeApp):
    """Teks promo yang MENYEBUT label (tidak diawali label) dengan nominal lain: bukan baris ongkir/total."""

    def _r_checkout(self):
        out = super()._r_checkout()
        order = next(n for _, n in out if n.text.startswith("Total Pesanan"))
        top = order.bounds[3] + 5
        out.insert(1, ("", Node(text="Gratis Ongkir", bounds=(560, 60, 700, 100))))  # lencana di header
        out.append(("", Node(text="Voucher Gratis Ongkir s/d Rp20.000", bounds=(20, top, 480, top + 30))))
        out.append(("", Node(text="Hemat Rp5.000 dari Total Pembayaran", bounds=(20, 1450, 700, 1490))))
        return out


def _shipping_testid_app(value: int):
    """Nilai ongkir ber-testID (labelShippingFinalPrice) terpisah dari pasangan label "Subtotal Pengiriman"."""

    class _ShippingTestId(FakeShopeeApp):
        def _r_checkout(self):
            out = [(k, dataclasses.replace(n, rid="") if n.rid == "labelShippingFinalPrice" else n)
                   for k, n in super()._r_checkout()]
            out.append(("", Node(text=rupiah(value), bounds=(500, 1000, 700, 1030), rid="labelShippingFinalPrice")))
            return out

    return _ShippingTestId


class _NoTestIds(FakeShopeeApp):
    """Checkout tanpa testID sama sekali: total & ongkir hanya dari pasangan label -> nilai."""

    def _r_checkout(self):
        return [(k, dataclasses.replace(n, rid="")) for k, n in super()._r_checkout()]


def _no_qty_rows_app(groups: int):
    """Checkout tanpa penanda "x1" (tata letak lain): hanya grup "Total Pesanan (1 Produk):" sebanyak `groups`."""

    class _NoQtyRows(FakeShopeeApp):
        def _r_checkout(self):
            out = [(k, n) for k, n in super()._r_checkout() if not (n.text.startswith("x") and n.bounds[0] == 640)]
            for g in range(1, groups):
                top = 950 + g * 60
                out += [("", Node(text="Total Pesanan (1 Produk):", bounds=(20, top, 400, top + 30))),
                        ("", Node(text=rupiah(5_000), bounds=(500, top, 700, top + 30)))]
            return out

    return _NoQtyRows


def _variant_shown_app(text: str):
    """Baris "Variasi: ..." di checkout menampilkan `text` (bukan variasi yang dipilih di sheet)."""

    class _VariantShown(FakeShopeeApp):
        def _r_checkout(self):
            return [(k, dataclasses.replace(n, text=text) if n.text.startswith("Variasi:") else n)
                    for k, n in super()._r_checkout()]

    return _VariantShown


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
    assert_polling_rules(out)  # jendela T-1..T+8 s + jarak >= 400 ms, termasuk reload -> klik Beli (UI statis)
    if not live_update:
        assert _rel(out, "reload") < _rel(out, "click_buy")


@pytest.mark.parametrize("reload", ["swipe", "intent"])
def test_l1_reload_then_buy_click_keeps_400ms_gap_at_app(tmp_path, reload):
    # UI statis: harga flash baru tampil setelah reload T+0,5 s, lalu tombol (aktif sejak awal) diklik.
    # RateLimiter.touch(akhir reload): klik Beli tiba di aplikasi >= 425 ms setelah reload BERDAMPAK.
    out = run_android(tmp_path, button_enabled_before_open=True, live_update=False,
                      cfg={"android": {"reload": reload}})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert_polling_rules(out)
    t = int(out.open_at * 1000)
    kind = "refresh" if reload == "swipe" else "intent"
    reloads = [e["t_server_ms"] for e in out.kind(kind) if e["t_server_ms"] >= t - 1000]
    buys = [e["t_server_ms"] for e in out.kind("buy")]
    assert len(reloads) == 1 and len(buys) == 1, (reloads, buys)
    assert buys[0] - reloads[0] >= 425, buys[0] - reloads[0]


@BOTH
@pytest.mark.parametrize("app_cls, shown", [
    (_RbPriceNoStrike, "'Rp99rb'"),
    (_RangePrice, "'Rp89.000 - Rp99.000'"),
    (_NoPrice, "''"),
], ids=["rb_no_strike", "range", "no_price"])
def test_l1_unreadable_price_never_clicks(tmp_path, monkeypatch, live, app_cls, shown):
    out = _run(tmp_path, monkeypatch, app_cls, live=live)
    _blocked(out, "jendela polling habis", f"harga tidak terbaca ({shown})")
    _no_buy(out)
    assert out.result.step("price_unreadable") is not None
    assert _rel(out, "result") >= 8000
    assert_polling_rules(out)


@BOTH
def test_l1_unreadable_price_with_strike_price_never_clicks(tmp_path, monkeypatch, live):
    # harga flash "Rp99rb" sebenarnya <= maks, tapi tidak terbaca -> tidak boleh klik (fail-closed);
    # harga coret Rp150.000 sesudahnya TIDAK dipakai sebagai pengganti
    out = _run(tmp_path, monkeypatch, _RbPrice, live=live)
    _blocked(out, "jendela polling habis", "harga tidak terbaca ('Rp99rb')")
    _no_buy(out)
    assert "Rp150.000" not in out.result.message
    assert_polling_rules(out)


@pytest.mark.parametrize("app_cls, scenario, shown", [
    (_RbPrice, {}, "Rp99rb"),
    (_RbPriceCheapChip, {"flash_price": 149_000}, "Rp149rb"),
], ids=["falls_back_to_strike", "falls_back_to_cheap_chip"])
def test_l1_unparseable_main_price_is_unreadable_not_replaced(tmp_path, monkeypatch, app_cls, scenario, shown):
    out = _run(tmp_path, monkeypatch, app_cls, **scenario)
    _no_order(out)
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    _no_buy(out)  # chip Rp10.000 tidak boleh dianggap harga produk Rp149rb
    # bukan "harga Rp150.000 > maks" (harga coret) / "Rp10.000 <= maks" (chip)
    assert f"harga tidak terbaca ({shown!r})" in out.result.message, out.result.message


@BOTH
@pytest.mark.parametrize("variants_on_page, reload, layer", [
    (True, "swipe", 1),
    (True, "intent", 3),
    (False, "swipe", 3),
], ids=["on_page", "on_page_intent_reload", "in_sheet"])
def test_l1_variant_with_flash_price_too_high_never_ordered(tmp_path, live, variants_on_page, reload, layer):
    out = run_android(tmp_path, live=live, variants=VARIANTS, variant_prices={"256GB Biru": 129_000},
                      variants_on_page=variants_on_page, cfg={"variant": "256GB Biru", "android": {"reload": reload}})
    _blocked(out, "Rp129.000 > maks Rp100.000")
    assert_polling_rules(out)
    if layer == 1:  # variasi terpilih di halaman produk -> harga variasi terbaca sebelum klik Beli
        _no_buy(out)
        assert "jendela polling habis" in out.result.message
        assert out.kind("checkout") == []
        assert out.app.selected_variant == "256GB Biru"
        return
    # variasi dipilih (ulang) di bottom sheet -> diblokir lapis 3 di checkout. Reload intent membuang pilihan
    # variasi dari T-60 s; desain ronde 2 TIDAK memilih ulang variasi saat polling (tidak ada tap ekstra di
    # jendela polling) -> lapis 1 membaca harga umum, lapis 3 menahan harga variasi.
    assert len(out.kind("buy")) == 1
    assert out.kind("variant") and out.kind("variant")[-1]["detail"] == "256GB Biru"
    assert _checkouts(out) == [f"[('{TARGET}', 129000, 1)]"]
    assert "checkout_loaded" in out.step_names()
    t = int(out.open_at * 1000)
    assert all(e["t_server_ms"] < t or e["t_server_ms"] > out.kind("buy")[0]["t_server_ms"]
               for e in out.kind("variant")), "tidak ada tap variasi di antara T dan klik Beli"
    if variants_on_page:
        assert [e["t_server_ms"] < t for e in out.kind("variant")] == [True, False]  # T-60 s, lalu di sheet


@BOTH
def test_l1_calibrated_price_selector_beats_decoy_read_first(tmp_path, monkeypatch, live):
    # banner Rp500.000 tampil SEBELUM harga (heuristik "Rp pendek pertama" akan membacanya dan memblokir);
    # selector kalibrasi (resourceId) membaca harga sebenarnya Rp99.000
    out = _run(tmp_path, monkeypatch, _decoy_price_app("Rp500.000", before=True, rid=PRICE_RID), live=live,
               selectors=_calibrated_selectors())
    _passed(out, live)
    assert "Rp99.000" in out.result.step("buy_ready").detail
    assert ("info", f"resourceId='{PRICE_RID}'") in out.driver.calls
    assert ("info", str(Sel("textMatches", RP_SHORT_MATCH))) not in out.driver.calls  # tidak jatuh ke heuristik
    assert_polling_rules(out)


@BOTH
def test_l1_calibrated_price_selector_blocks_despite_cheap_decoy(tmp_path, monkeypatch, live):
    # nominal pengecoh Rp5.000 tampil lebih dulu; harga sebenarnya (resourceId) Rp129.000 > maks
    out = _run(tmp_path, monkeypatch, _decoy_price_app("Rp5.000", before=True, rid=PRICE_RID), live=live,
               flash_price=129_000, selectors=_calibrated_selectors())
    _blocked(out, "jendela polling habis", "Rp129.000 > maks Rp100.000")
    _no_buy(out)
    assert "Rp5.000" not in out.result.message
    assert_polling_rules(out)


def test_l1_heuristic_without_calibration_reads_decoy_but_layer3_still_blocks(tmp_path, monkeypatch):
    # kontrol: tanpa kalibrasi, heuristik "teks Rp pendek pertama" membaca pengecoh yang tampil lebih dulu
    # -> klik Beli, tetapi lapis 3 tetap menahan harga sebenarnya di checkout
    out = _run(tmp_path, monkeypatch, _decoy_price_app("Rp5.000", before=True), live=True, flash_price=129_000)
    assert "Rp5.000" in out.result.step("buy_ready").detail
    _blocked(out, "harga Rp129.000 > maks Rp100.000")
    assert _checkouts(out) == [f"[('{TARGET}', 129000, 1)]"]
    assert len(out.kind("buy")) == 1
    assert_polling_rules(out)


def test_l1_heuristic_decoy_read_first_too_high_is_fail_closed(tmp_path, monkeypatch):
    # kontrol untuk kalibrasi: tanpa resourceId, banner Rp500.000 yang tampil lebih dulu memblokir (tidak klik)
    out = _run(tmp_path, monkeypatch, _decoy_price_app("Rp500.000", before=True))
    _blocked(out, "jendela polling habis", "Rp500.000 > maks Rp100.000")
    _no_buy(out)
    assert_polling_rules(out)


@BOTH
@pytest.mark.parametrize("decoy, before, flash_price, ok", [
    ("Rp5.000", False, 129_000, False),  # nominal murah SESUDAH harga: diabaikan, Rp129.000 memblokir
    ("Rp500.000", False, 99_000, True),  # nominal tinggi SESUDAH harga: diabaikan, Rp99.000 lolos
    ("Gratis Ongkir s/d Rp5.000 min. belanja Rp0 untuk semua produk", True, 129_000, False),  # teks panjang
    ("Gratis Ongkir s/d Rp500.000 min. belanja Rp0 untuk semua produk", True, 99_000, True),
], ids=["cheap_after", "high_after", "long_cheap_before", "long_high_before"])
def test_l1_heuristic_reads_first_short_rp_text(tmp_path, monkeypatch, live, decoy, before, flash_price, ok):
    out = _run(tmp_path, monkeypatch, _decoy_price_app(decoy, before=before), live=live, flash_price=flash_price)
    if ok:
        _passed(out, live)
        assert "harga Rp99.000 <= maks" in out.result.step("buy_ready").detail
    else:
        _blocked(out, "jendela polling habis", "harga Rp129.000 > maks Rp100.000")
        _no_buy(out)
    assert_polling_rules(out)


def test_l1_calibrated_price_selector_not_found_is_fail_closed(tmp_path):
    # selector kalibrasi usang (resourceId tidak ada): tidak jatuh ke heuristik, harga = tidak terbaca
    out = run_android(tmp_path, selectors=_calibrated_selectors("com.shopee.id:id/hilang"))
    _blocked(out, "harga tidak terbaca ('')")
    _no_buy(out)
    assert ("info", str(Sel("textMatches", RP_SHORT_MATCH))) not in out.driver.calls
    assert_polling_rules(out)


@BOTH
def test_l1_nbsp_prices_are_read_with_java_regex_semantics(tmp_path, monkeypatch, live):
    # "Rp 99.000": regex device harus menulis NBSP eksplisit (Java \s tidak memuat NBSP)
    out = _run(tmp_path, monkeypatch, _NbspPrices, live=live)
    _passed(out, live)
    assert "harga Rp99.000 <= maks" in out.result.step("buy_ready").detail
    guard = out.result.step("price_guard_ok").detail
    assert "harga=Rp99.000" in guard and "ongkir=Rp10.000" in guard and "total=Rp109.000" in guard


def test_l1_price_read_in_hot_path_is_single_info_query(tmp_path, monkeypatch):
    # hot path polling: hanya info/exists; harga = 1 info(textMatches RP_SHORT_MATCH) per iterasi, tanpa find_all
    monkeypatch.setattr(android_harness, "FakeDriver", _StampedDriver)
    out = run_android(tmp_path, button_enabled_before_open=True)
    _passed(out, live=False)
    start, click = out.result.step("poll_start").t_server_ms, out.result.step("click_buy").t_server_ms
    hot = [(op, target) for t, op, target in out.driver.stamps if start <= t < click]
    assert hot and {op for op, _ in hot} <= {"info", "exists"}, sorted({op for op, _ in hot})
    price_reads = hot.count(("info", str(Sel("textMatches", RP_SHORT_MATCH))))
    buy_reads = hot.count(("info", str(Sel("text", "Beli Sekarang"))))
    assert price_reads >= 3, hot[:12]
    assert price_reads <= buy_reads, (price_reads, buy_reads)  # paling banyak satu bacaan harga per iterasi


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
@pytest.mark.parametrize("delay_s, ok", [(0.8, True), (3.0, False)], ids=["settles_0.8s", "never_within_2s"])
def test_l2_uncheck_is_polled_until_cart_confirms(tmp_path, monkeypatch, live, delay_s, ok):
    # centang baru berubah setelah round-trip server: keranjang dibaca ulang maks 2 s, klik uncheck tetap sekali
    out = _run(tmp_path, monkeypatch, _slow_uncheck_app(delay_s), live=live, cfg={"expected_name": "Uji Coba"},
               **CART_OTHER)
    assert [e["detail"] for e in out.kind("cart_toggle")] == [OTHER]
    if ok:
        _passed(out, live)
        assert _checkouts(out) == [f"[('{TARGET}', 99000, 1)]"]
        assert _between(out, "cart_uncheck", "cart_ok") >= 800
    else:
        _blocked(out, "keranjang", "2 item tercentang", OTHER)
        assert out.kind("checkout") == []
        assert _between(out, "cart_uncheck", "result") >= 2000


@BOTH
@pytest.mark.parametrize("expected", ["Uji Coba", "Ponsel Uji Coba 128GB"])
def test_l2_two_target_like_items_are_ambiguous(tmp_path, live, expected):
    out = run_android(tmp_path, live=live, cfg={"expected_name": expected}, go_cart=True,
                      cart_other_items=[("Ponsel Uji Coba 128GB Bekas", 50_000, True)])
    _blocked(out, "keranjang", "target tidak bisa dipastikan", repr(expected))
    assert out.kind("cart_toggle") == [], "tidak boleh meng-uncheck salah satu item secara tebakan"
    assert out.kind("checkout") == []


@BOTH
def test_l2_other_item_listed_above_target_is_unchecked(tmp_path, monkeypatch, live):
    # teks item ditempelkan ke checkbox TERDEKAT: nama item target (di atas checkbox-nya) tidak bocor ke item lain
    out = _run(tmp_path, monkeypatch, _CartOtherFirst, live=live, cfg={"expected_name": "Uji Coba"}, **CART_OTHER)
    _passed(out, live)
    assert [e["detail"] for e in out.kind("cart_toggle")] == [OTHER]
    assert _checkouts(out) == [f"[('{TARGET}', 99000, 1)]"]


def test_l2_other_item_listed_above_target_never_orders_both(tmp_path, monkeypatch):
    # apa pun hasil lapis 2 pada tata letak ini: tidak pernah checkout/pesan dua item
    out = _run(tmp_path, monkeypatch, _CartOtherFirst, live=True, **CART_OTHER)
    assert all(OTHER not in d for d in _checkouts(out)), _checkouts(out)
    assert len(out.kind("order")) <= 1
    if out.result.status != RunStatus.ORDER_PLACED_AWAIT_PIN:
        _blocked(out)


@BOTH
@pytest.mark.parametrize("app_cls", [FakeShopeeApp, _CartNameBesideBox], ids=["name_above_box", "name_beside_box"])
def test_l2_item_named_with_semua_is_not_treated_as_select_all(tmp_path, monkeypatch, live, app_cls):
    # baris "Pilih Semua" hanya bila ada teks sebaris yang PERSIS "Semua"/"Pilih Semua"; nama item yang memuat
    # kata "Semua" (sebaris checkbox-nya) tetap dihitung (kalau dilewati, item itu ikut ter-checkout)
    other = "Paket Semua Warna Kabel"
    out = _run(tmp_path, monkeypatch, app_cls, live=live, cfg={"expected_name": "Uji Coba"}, go_cart=True,
               cart_other_items=[(other, 25_000, True)])
    _passed(out, live)
    assert [e["detail"] for e in out.kind("cart_toggle")] == [other]
    assert _checkouts(out) == [f"[('{TARGET}', 99000, 1)]"]


def test_l2_single_checked_non_target_never_ordered(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, _CartTargetUnchecked, live=True, **CART_OTHER)
    _blocked(out, "keranjang", "item tercentang bukan target (nama acuan 'Uji Coba')", OTHER)


@BOTH
def test_l2_single_checked_non_target_is_blocked_in_cart(tmp_path, monkeypatch, live):
    # lapis 2 ketat (expected_name): satu-satunya item tercentang harus target; tidak menebak/menukar centang
    out = _run(tmp_path, monkeypatch, _CartTargetUnchecked, live=live, cfg={"expected_name": "Uji Coba"},
               **CART_OTHER)
    _blocked(out, "keranjang", "item tercentang bukan target")
    assert out.kind("checkout") == [], "item non-target tidak boleh sampai checkout"
    assert out.kind("cart_toggle") == []
    assert "click_checkout" not in out.step_names()


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
    _blocked(out, "tidak stabil/terbaca dalam 1.5 s")
    waited = _between(out, "payment_ok", "result")
    assert 1_400 <= waited <= 2_500, waited


@BOTH
@pytest.mark.parametrize("change, scenario, cfg, min_ms, max_ms", [
    ("none", {}, {}, 100, 999),
    ("payment_switch", {"payment_default": "SPayLater"}, {}, 1_000, 2_000),
    ("cart_uncheck", CART_OTHER, {"expected_name": "Uji Coba"}, 1_000, 2_000),
], ids=["no_change", "payment_switch", "cart_uncheck"])
def test_l3_total_stability_window_after_change(tmp_path, live, change, scenario, cfg, min_ms, max_ms):
    # total stabil >= 100 ms; setelah ganti metode bayar / uncheck keranjang >= 1 s (server menghitung ulang)
    out = run_android(tmp_path, live=live, cfg=cfg, **scenario)
    _passed(out, live)
    if change == "payment_switch":
        assert "select_shopeepay" in out.step_names()
    if change == "cart_uncheck":
        assert "cart_uncheck" in out.step_names()
    waited = _between(out, "payment_ok", "price_guard_ok")
    assert min_ms <= waited <= max_ms, waited


def test_l3_stability_timeout_extended_after_payment_switch(tmp_path):
    out = run_android(tmp_path, payment_default="SPayLater", shipping_delay_ms=10_000)
    _blocked(out, "total checkout tidak stabil/terbaca dalam 2.5 s", "ongkir ?")
    assert "select_shopeepay" in out.step_names()
    waited = _between(out, "payment_ok", "result")
    assert 2_400 <= waited <= 3_500, waited


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
@pytest.mark.parametrize("testid_value, ok", [(10_000, True), (15_000, False)], ids=["agree", "disagree"])
def test_l3_shipping_testid_and_label_pair_must_agree(tmp_path, monkeypatch, live, testid_value, ok):
    # ongkir = nilai testID labelShippingFinalPrice DITAMBAH nilai pasangan label "Subtotal Pengiriman"
    out = _run(tmp_path, monkeypatch, _shipping_testid_app(testid_value), live=live)
    if ok:
        _passed(out, live)
        assert "ongkir=Rp10.000" in out.result.step("price_guard_ok").detail
    else:
        _blocked(out, "total checkout tidak stabil/terbaca", "ongkir ?")


@BOTH
def test_l3_label_pairs_alone_without_testids(tmp_path, monkeypatch, live):
    out = _run(tmp_path, monkeypatch, _NoTestIds, live=live)
    _passed(out, live)
    guard = out.result.step("price_guard_ok").detail
    assert "ongkir=Rp10.000" in guard and "total=Rp109.000" in guard


@BOTH
def test_l3_promo_texts_mentioning_labels_are_not_label_rows(tmp_path, monkeypatch, live):
    # "Gratis Ongkir", "Voucher Gratis Ongkir s/d Rp20.000", "Hemat Rp5.000 dari Total Pembayaran":
    # tidak DIAWALI label -> bukan nilai ongkir/total (kalau dihitung, nilai tidak sepakat -> blok palsu)
    out = _run(tmp_path, monkeypatch, _CheckoutPromoTexts, live=live)
    _passed(out, live)
    guard = out.result.step("price_guard_ok").detail
    assert "ongkir=Rp10.000" in guard and "total=Rp109.000" in guard and "harga=Rp99.000" in guard


@BOTH
def test_l3_two_product_rows_in_checkout(tmp_path, monkeypatch, live):
    out = _run(tmp_path, monkeypatch, _TwoCheckoutRows, live=live)
    _blocked(out, "2 baris produk (harus tepat 1)", "jumlah produk pesanan [2]")
    assert _checkouts(out) == [f"[('{TARGET}', 99000, 1), ('{OTHER}', 5000, 1)]"]


@BOTH
@pytest.mark.parametrize("groups, ok", [(1, True), (2, False)], ids=["one_group", "two_groups"])
def test_l3_without_qty_rows_needs_exactly_one_order_group(tmp_path, monkeypatch, live, groups, ok):
    # tanpa penanda "x1": kuantitas & harga dari "Total Pesanan (1 Produk): RpX" - hanya bila tepat 1 grup
    out = _run(tmp_path, monkeypatch, _no_qty_rows_app(groups), live=live)
    if ok:
        _passed(out, live)
        assert "baris=0, qty=1, harga=Rp99.000" in out.result.step("price_guard_ok").detail
    else:
        _blocked(out, "2 grup pesanan (harus tepat 1)")


@BOTH
def test_l3_shop_header_does_not_satisfy_expected_name(tmp_path, live):
    # nama toko "Toko Uji Resmi" ada di header kartu checkout, tetapi bukan kolom produk
    out = run_android(tmp_path, live=live, cfg={"expected_name": "Toko Uji"})
    _blocked(out, "nama produk tidak memuat 'Toko Uji'")
    assert len(out.kind("buy")) == 1 and "checkout_loaded" in out.step_names()


@BOTH
def test_l3_variant_must_be_visible_in_product_row(tmp_path, monkeypatch, live):
    # variasi dipilih di sheet, tetapi checkout menampilkan variasi lain -> PRICE_GUARD (cfg.variant wajib terlihat)
    out = _run(tmp_path, monkeypatch, _variant_shown_app("Variasi: 128GB Hitam"), live=live, variants=VARIANTS,
               cfg={"variant": "256GB Biru"})
    _blocked(out, "variasi '256GB Biru' tidak terlihat di baris produk")
    assert out.kind("variant")[-1]["detail"] == "256GB Biru"


@BOTH
def test_l3_variant_check_is_case_insensitive(tmp_path, monkeypatch, live):
    out = _run(tmp_path, monkeypatch, _variant_shown_app("Variasi: 256gb BIRU"), live=live, variants=VARIANTS,
               cfg={"variant": "256GB Biru"})
    _passed(out, live)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: lapis 3 variasi dinormalisasi (huruf/angka saja) di pricing.check_checkout, tetapi "
    "AndroidRunner._checkout_read hanya mengambil node yang memuat teks variasi PERSIS (regex .*256GB Biru.*); "
    "'Variasi: 256GB, Biru' tidak pernah terbaca -> PRICE_GUARD palsu (fail-closed, live tidak bisa memesan)"))
def test_l3_variant_check_normalizes_punctuation_end_to_end(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, _variant_shown_app("Variasi: 256GB, Biru"), variants=VARIANTS,
               cfg={"variant": "256GB Biru"})
    _passed(out, live=False)
