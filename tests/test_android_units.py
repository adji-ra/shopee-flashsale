"""Tes unit jalur Android tanpa menjalankan runner.

- android_screen: pembacaan layar dari node accessibility (harga utama, baris checkout, keranjang,
  metode pembayaran, parse dump);
- android_selectors: validasi kandidat, templating {variant}, urutan prioritas, load/save
  selectors.json, penggabungan regex penanda & sampel teks Indonesia;
- android_driver: semantik selector (node_matches), Node.from_u2, U2Driver di atas device u2 palsu,
  TimedDriver + QueryStats (latensi dari monotonic yang disuntikkan).

Semua deterministik & cepat: tidak ada device, tidak ada sleep sungguhan.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from flashbuy import android_selectors, pricing
from flashbuy.android_driver import (
    SEL_KINDS,
    AppInfo,
    DriverError,
    FakeDriver,
    Node,
    QuerySample,
    QueryStats,
    Sel,
    TimedDriver,
    U2Driver,
    node_matches,
)
from flashbuy.android_runner import MARKER_MAX_CHARS, Screen
from flashbuy.android_screen import (
    ANY_TEXT_MATCH,
    ORDER_COUNT_MATCH,
    PRICE_MATCH,
    QTY_MATCH,
    cart_rows,
    checkout_count,
    checkout_snapshot,
    is_price,
    is_qty,
    label_values,
    parse_dump,
    payment_value,
    pick_main_price,
    same_row,
    value_for_label,
)
from flashbuy.android_selectors import (
    ANDROID_DEFAULT_MARKERS,
    ANDROID_DEFAULT_STEPS,
    KIND_RANK,
    order_candidates,
    scoped,
    to_sel,
    union,
)
from flashbuy.pricing import Limits
from flashbuy.web_js import SHIPPING_LABEL, TOTAL_LABEL
from tests.android_harness import make_android
from tests.conftest import FakeClock
from tests.fake_android import AppScenario, FakeShopeeApp

L = Limits(max_item_price=100_000, max_total=120_000, expected_name="Uji Coba")
TOTAL_RE = re.compile(TOTAL_LABEL, re.I)
SHIPPING_RE = re.compile(SHIPPING_LABEL, re.I)
PAYMENT_RE = re.compile(r"(?i)metode pembayaran")
TARGET = "Ponsel Uji Coba 128GB"
OTHER = "Kabel Data USB-C"
BOX = "android.widget.CheckBox"


def _n(text: str = "", bounds=(0, 0, 0, 0), **kw) -> Node:
    return Node(text=text, bounds=tuple(bounds), **kw)


# =========================================================================== android_screen


# ------------------------------------------------------------------ regex dasar


@pytest.mark.parametrize("text, ok", [
    ("Rp99.000", True), ("Rp 99.000", True), (" Rp99.000 ", True), ("Rp99.000,-", True),
    ("Rp89.000 - Rp129.000", True), ("Rp89.000 – Rp129.000", True), ("Rp89.000-Rp129.000", True),
    ("Rp99rb", False), ("Rp99.OOO", False), ("Hemat Rp51.000", False), ("Rp99.000 Rp150.000", False),
    ("99.000", False), ("Terjual 1RB", False),
])
def test_is_price_full_text(text, ok):
    assert is_price(_n(text)) is ok
    assert (re.fullmatch(PRICE_MATCH, text) is not None) is ok


@pytest.mark.parametrize("text, ok", [
    ("x1", True), ("x 1", True), ("×1", True), (" x12 ", True),
    ("x1000", False), ("Box 2x3", False), ("1", False), ("x", False),
])
def test_is_qty(text, ok):
    assert is_qty(_n(text)) is ok
    assert (re.fullmatch(QTY_MATCH, text) is not None) is ok


def test_any_text_and_order_count_regex():
    for s in ("a", "  a  ", "baris 1\nbaris 2", "\nRp99.000\n"):
        assert re.fullmatch(ANY_TEXT_MATCH, s), s
    for s in ("", "   ", "\n", "\t \n"):
        assert re.fullmatch(ANY_TEXT_MATCH, s) is None, repr(s)
    for s in ("Total Pesanan (1 Produk):", "Total Pesanan ( 12 produk )", "Subtotal\n(1 Produk)"):
        assert re.fullmatch(ORDER_COUNT_MATCH, s), s
    for s in ("Total Pesanan:", "(1 Item)", "(1000 Produk)"):
        assert re.fullmatch(ORDER_COUNT_MATCH, s) is None, s


# ------------------------------------------------------------------ geometri


def test_node_geometry_and_label():
    n = _n("", (10, 20, 15, 31), desc="Beli Sekarang")
    assert n.label == "Beli Sekarang"  # text kosong -> content-desc
    assert n.center == (12, 25)  # pembagian bulat ke bawah
    assert (n.width, n.height) == (5, 11)
    assert _n("Teks", desc="Desc").label == "Teks"


@pytest.mark.parametrize("b, expected", [
    ((0, 80, 10, 120), True),  # tinggi 40, tumpang-tindih 20 = 50% -> sebaris
    ((0, 81, 10, 121), False),  # tumpang-tindih 19 < 50%
    ((0, 20, 10, 60), True),  # sepenuhnya di dalam
    ((0, -30, 10, 10), False),  # tumpang-tindih 10 < 50% dari tinggi terkecil 40
    ((0, 100, 10, 140), False),  # bersentuhan saja (overlap 0)
    ((0, 200, 10, 240), False),  # terpisah
    ((0, 50, 10, 50), False),  # tinggi 0 tidak pernah sebaris
])
def test_same_row_threshold(b, expected):
    a = _n("A", (0, 0, 10, 100))
    other = _n("B", b)
    assert same_row(a, other) is expected
    assert same_row(other, a) is expected  # simetris


# ------------------------------------------------------------------ harga utama


def test_pick_main_price_tallest_wins_over_strike_price():
    nodes = [
        _n("Ponsel Uji Coba 128GB", (20, 700, 700, 800)),  # teks bukan harga (lebih tinggi) diabaikan
        _n("Rp150.000", (420, 640, 600, 670)),  # coret, kecil
        _n("Rp99.000", (20, 610, 400, 690)),  # harga flash, paling tinggi
        _n("Hemat Rp51.000", (20, 900, 400, 1000)),
        _n("Rp5.000", (20, 1000, 200, 1000)),  # tinggi 0
    ]
    assert pick_main_price(nodes).label == "Rp99.000"


def test_pick_main_price_tie_topmost_then_leftmost():
    lower = _n("Rp120.000", (20, 700, 300, 740))
    upper_right = _n("Rp110.000", (400, 600, 700, 640))
    upper_left = _n("Rp100.000", (20, 600, 300, 640))
    assert pick_main_price([lower, upper_right, upper_left]) is upper_left
    assert pick_main_price([lower, upper_right]) is upper_right


def test_pick_main_price_range_is_picked_but_unreadable():
    nodes = [_n("Rp89.000 - Rp129.000", (20, 610, 500, 690)), _n("Rp150.000", (420, 700, 600, 730))]
    main = pick_main_price(nodes)
    assert main.label == "Rp89.000 - Rp129.000"
    verdict = pricing.check_product_price(main.label, L)
    assert verdict.verdict == "unreadable"  # rentang = tidak yakin = tidak boleh klik (fail-closed)


def test_pick_main_price_none_and_desc_only():
    assert pick_main_price([]) is None
    assert pick_main_price([_n("Rp99rb", (0, 0, 100, 50)), _n("Rp99.OOO", (0, 60, 100, 110))]) is None
    desc_only = _n("", (0, 0, 100, 50), desc="Rp99.000")
    assert pick_main_price([desc_only]) is desc_only


# ------------------------------------------------------------------ pasangan label -> nilai


def test_value_for_label_right_closest_beats_below():
    label = _n("Total Pembayaran", (20, 700, 400, 730))
    far = _n("Rp1", (600, 700, 700, 730))
    near = _n("Rp109.000", (450, 700, 590, 730))
    below = _n("Rp7", (20, 732, 400, 762))
    assert value_for_label(label, [label, far, below, near]) is near


def test_value_for_label_right_edge_tolerance():
    label = _n("Label", (0, 100, 200, 130))
    touching = _n("Nilai", (198, 100, 300, 130))  # mulai 2 px sebelum tepi kanan label: masih "kanan"
    assert value_for_label(label, [label, touching]) is touching
    overlapping = _n("Nilai", (197, 100, 300, 130))  # 3 px tumpang-tindih: bukan kanan, bukan bawah
    assert value_for_label(label, [label, overlapping]) is None
    left = _n("Kiri", (0, 100, 0, 130))
    assert value_for_label(label, [left, label]) is None


def test_value_for_label_below_window_and_horizontal_overlap():
    label = _n("Total Pembayaran", (100, 100, 300, 130))  # tinggi 30 -> jendela bawah 1,5 x 30 = 45 px
    at_edge = _n("Rp1", (100, 173, 300, 200))  # 173 - 130 + 2 = 45 -> masih
    too_far = _n("Rp2", (100, 174, 300, 200))
    slightly_above = _n("Rp3", (100, 128, 300, 160))  # mulai 2 px sebelum bawah label: masih
    no_overlap = _n("Rp4", (300, 140, 400, 170))  # tidak tumpang-tindih horizontal
    assert value_for_label(label, [label, at_edge]) is at_edge
    assert value_for_label(label, [label, too_far]) is None
    assert value_for_label(label, [label, too_far, slightly_above, at_edge]) is slightly_above  # paling atas
    assert value_for_label(label, [label, no_overlap]) is None


def test_value_for_label_accept_filter_and_self_copies():
    label = _n("Total Pembayaran", (20, 700, 400, 730))
    twin = _n("Total Pembayaran", (20, 700, 400, 730))  # salinan identik label (== label) diabaikan
    button = _n("Buat Pesanan", (520, 690, 720, 740), clickable=True)
    below = _n("Rp109.000", (20, 735, 400, 765))
    assert value_for_label(label, [label, twin, button, below]) is button
    assert value_for_label(label, [label, twin, button, below], accept=lambda n: not n.clickable) is below


def test_label_values_inline_right_below_and_missing():
    nodes = [
        _n("Total Pembayaran: Rp109.000", (20, 100, 400, 130)),  # nilai sebaris di teks label
        _n("Total Pembayaran", (20, 200, 300, 230)), _n("Rp109.000", (500, 200, 700, 230)),  # kanan
        _n("Total Pembayaran", (300, 1515, 510, 1545)), _n("Rp109.000", (300, 1550, 510, 1595)),  # bawah
        _n("Buat Pesanan", (520, 1500, 720, 1612), clickable=True),  # tombol sebaris: bukan nilai
        _n("Total Pembayaran", (20, 900, 300, 930)),  # tanpa nilai
    ]
    assert label_values(nodes, TOTAL_RE) == ["Rp109.000", "Rp109.000", "Rp109.000", ""]


def test_label_values_skips_neighbour_label():
    a = _n("Total Pembayaran", (20, 700, 250, 730))
    b = _n("Total Pembayaran", (260, 700, 500, 730))  # label lain di kanan: bukan nilai milik `a`
    va = _n("Rp109.000", (20, 735, 250, 765))
    vb = _n("Rp109.000", (260, 735, 500, 765))
    assert label_values([a, b, va, vb], TOTAL_RE) == ["Rp109.000", "Rp109.000"]


def test_label_values_pending_value_kept_as_is():
    nodes = [_n("Subtotal Pengiriman", (20, 100, 400, 130)), _n("Menghitung...", (500, 100, 700, 130))]
    values = label_values(nodes, SHIPPING_RE)
    assert values == ["Menghitung..."]
    assert pricing.consistent_amount(values) is None  # belum stabil -> tidak terbaca


# ------------------------------------------------------------------ checkout


def _checkout(items=((TARGET, "Rp99.000", "x1"),), *, strike: str | None = "Rp150.000", strike_h: int = 25,
              variation: str | None = "Variasi: Hitam", shipping: str = "Rp10.000", total: str = "Rp109.000",
              bar_total: str | None = None, count_text: str = "Total Pesanan (1 Produk):") -> list[Node]:
    """Layar checkout sintetis (720 px): kartu produk tiap 170 px, harga coret sebaris harga & "x1"."""
    out = [_n("Checkout", (20, 30, 300, 70)), _n("Alamat Pengiriman", (20, 90, 400, 120)),
           _n("Toko Uji Resmi", (20, 150, 400, 180))]
    y = 300
    for name, price, qty in items:
        out.append(_n(name, (140, y, 700, y + 40)))
        if variation:
            out.append(_n(variation, (140, y + 45, 600, y + 75)))
        if strike:
            out.append(_n(strike, (140, y + 102, 300, y + 102 + strike_h)))
        out.append(_n(price, (320, y + 95, 500, y + 130)))
        out.append(_n(qty, (640, y + 98, 700, y + 128)))
        y += 170
    out += [
        _n(count_text, (20, y + 10, 400, y + 40)), _n("Rp99.000", (500, y + 10, 700, y + 40)),
        _n("Metode Pembayaran", (20, y + 80, 300, y + 120), clickable=True),
        _n("ShopeePay", (420, y + 80, 700, y + 120), clickable=True),
        _n("Subtotal Pengiriman", (20, y + 160, 400, y + 190)), _n(shipping, (500, y + 160, 700, y + 190)),
        _n("Total Pembayaran", (20, y + 200, 400, y + 235)), _n(total, (500, y + 200, 700, y + 235)),
        _n("Total Pembayaran", (300, 1515, 510, 1545)), _n(bar_total or total, (300, 1550, 510, 1595)),
        _n("Buat Pesanan", (520, 1500, 720, 1612), clickable=True),
        _n("", (0, 0, 720, 1612), cls="android.widget.FrameLayout"),  # node tanpa teks dibuang
    ]
    return out


def test_checkout_snapshot_single_row_drops_strike_price():
    snap = checkout_snapshot(_checkout())
    assert snap.rows == [f"{TARGET}\nVariasi: Hitam\nRp99.000\nx1"]
    assert snap.totals == ["Rp109.000", "Rp109.000"]
    assert snap.shippings == ["Rp10.000"]
    assert "Total Pesanan (1 Produk):" in snap.page_text
    assert pricing.order_counts(snap.page_text) == [1]
    lines = snap.page_text.splitlines()
    assert lines[:3] == ["Checkout", "Alamat Pengiriman", "Toko Uji Resmi"]  # urut atas -> bawah
    verdict = pricing.check_checkout(snap, L)
    assert verdict.ok, verdict.reasons
    v = verdict.values
    assert (v["rows"], v["qty"], v["item_price"], v["shipping"], v["total"], v["name_ok"]) == (
        1, 1, 99_000, 10_000, 109_000, True)


def test_checkout_snapshot_from_fake_app_layout(fake_clock):
    app = FakeShopeeApp(AppScenario(open_at=0.0), fake_clock)
    app.screen = "checkout"
    app.checkout_items = [(TARGET, 99_000, 1)]
    snap = checkout_snapshot(app.nodes())
    assert len(snap.rows) == 1 and snap.rows[0].endswith("Rp99.000\nx1")
    assert "Rp150.000" not in snap.rows[0]  # harga coret sebaris tidak ikut
    verdict = pricing.check_checkout(snap, L)
    assert verdict.ok, verdict.reasons
    assert payment_value(app.nodes(), PAYMENT_RE) == "ShopeePay"


def test_checkout_snapshot_equal_height_strike_kept_fail_closed():
    snap = checkout_snapshot(_checkout(strike_h=35))  # coret setinggi harga jual: dua-duanya dipakai
    assert "Rp150.000" in snap.rows[0] and "Rp99.000" in snap.rows[0]
    verdict = pricing.check_checkout(snap, L)
    assert not verdict.ok
    assert verdict.values["item_price"] == 150_000  # maksimum diambil -> fail-closed
    assert any("> maks Rp100.000" in r for r in verdict.reasons)


def test_checkout_snapshot_broken_price_not_replaced_by_strike():
    snap = checkout_snapshot(_checkout(((TARGET, "Rp99.OOO", "x1"),)))
    assert "Rp99.OOO" in snap.rows[0]
    assert "Rp150.000" not in snap.rows[0]  # harga coret TIDAK diam-diam menggantikan harga rusak
    verdict = pricing.check_checkout(snap, L)
    assert not verdict.ok and "harga satuan tidak terbaca" in verdict.reasons


def test_checkout_snapshot_price_above_marker_layout():
    nodes = [
        _n(TARGET, (140, 300, 700, 340)),
        _n("Rp150.000", (140, 345, 300, 365)),  # coret di atas harga jual
        _n("Rp99.000", (140, 368, 300, 395)),  # harga jual tepat di atas penanda (tidak sebaris)
        _n("x1", (640, 400, 700, 430)),
    ]
    snap = checkout_snapshot(nodes)
    assert snap.rows == [f"{TARGET}\nRp99.000\nx1"]  # Rp terdekat di atas penanda


def test_checkout_snapshot_multiple_rows_do_not_bleed():
    snap = checkout_snapshot(_checkout(((TARGET, "Rp99.000", "x1"), (OTHER, "Rp20.000", "x1")),
                                       count_text="Total Pesanan (2 Produk):"))
    assert snap.rows == [f"{TARGET}\nVariasi: Hitam\nRp99.000\nx1", f"{OTHER}\nVariasi: Hitam\nRp20.000\nx1"]
    verdict = pricing.check_checkout(snap, L)
    assert not verdict.ok
    assert "2 baris produk (harus tepat 1)" in verdict.reasons
    assert "jumlah produk pesanan [2], bukan 1" in verdict.reasons


def test_checkout_snapshot_qty_and_order_count_mismatch():
    snap = checkout_snapshot(_checkout(((TARGET, "Rp99.000", "x2"),), count_text="Total Pesanan (2 Produk):"))
    verdict = pricing.check_checkout(snap, L)
    assert not verdict.ok
    assert "kuantitas 2, bukan 1" in verdict.reasons
    assert "jumlah produk pesanan [2], bukan 1" in verdict.reasons


def test_checkout_snapshot_unstable_totals_unreadable():
    snap = checkout_snapshot(_checkout(shipping="Menghitung...", bar_total="Rp119.000"))
    assert snap.shippings == ["Menghitung..."]
    assert snap.totals == ["Rp109.000", "Rp119.000"]
    assert pricing.read_total(snap) == (None, None)
    reasons = pricing.check_checkout(snap, L).reasons
    assert "ongkir tidak terbaca" in reasons and "total pembayaran tidak terbaca" in reasons


def test_checkout_snapshot_without_marker_has_no_rows():
    nodes = [n for n in _checkout() if n.label != "x1"]
    snap = checkout_snapshot(nodes)
    assert snap.rows == []
    verdict = pricing.check_checkout(snap, L)
    # tanpa baris: jatuh ke "(1 Produk)" + nominal sesudahnya; kuantitas dari hitungan produk
    assert verdict.values["qty"] == 1


# ------------------------------------------------------------------ keranjang


def _cart_app(clock, items: list[tuple[str, int, bool]]) -> FakeShopeeApp:
    app = FakeShopeeApp(AppScenario(open_at=0.0), clock)
    app.screen = "cart"
    app.cart = [{"name": n, "price": p, "checked": c} for n, p, c in items]
    return app


def _cart(app: FakeShopeeApp):
    nodes = app.nodes()
    boxes = [n for n in nodes if n.cls == BOX]
    return cart_rows(boxes, [n for n in nodes if n.label.strip()]), nodes


def test_cart_rows_skip_shop_and_select_all_and_map_checked(fake_clock):
    rows, nodes = _cart(_cart_app(fake_clock, [(TARGET, 99_000, True), (OTHER, 20_000, False)]))
    assert len(rows) == 2  # baris toko (tanpa Rp) & "Semua" dilewati
    (r0, b0), (r1, b1) = rows
    assert TARGET in r0.text and "Rp99.000" in r0.text and r0.checked
    assert OTHER in r1.text and "Rp20.000" in r1.text and not r1.checked
    assert b0.bounds == (20, 270, 70, 320) and b1.bounds == (20, 470, 70, 520)  # checkbox item
    assert checkout_count(nodes) == [1]
    assert pricing.check_cart([r for r, _ in rows], "Uji Coba").ok


def test_cart_rows_extra_checked_item_below_target_is_unchecked(fake_clock):
    rows, nodes = _cart(_cart_app(fake_clock, [(TARGET, 99_000, True), (OTHER, 20_000, True)]))
    verdict = pricing.check_cart([r for r, _ in rows], "Uji Coba")
    assert verdict.to_uncheck == [1]
    assert checkout_count(nodes) == [2]


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_screen.cart_rows - pita baris item i berakhir di bounds.top checkbox berikutnya, padahal pita "
    "item berikutnya dimulai box.height di atas checkbox itu; nama item berikutnya (di atas checkbox-nya) ikut "
    "masuk teks baris i. Bila target BUKAN item teratas dan item lain tercentang, check_cart melihat nama target "
    "di 2 baris -> 'target tidak bisa dipastikan' -> PRICE_GUARD, bukan uncheck item lain."))
def test_cart_rows_row_text_does_not_include_next_item(fake_clock):
    rows, _ = _cart(_cart_app(fake_clock, [(OTHER, 20_000, True), (TARGET, 99_000, True)]))
    assert TARGET not in rows[0][0].text, rows[0][0].text
    verdict = pricing.check_cart([r for r, _ in rows], "Uji Coba")
    assert verdict.to_uncheck == [0], verdict.reason


def test_cart_rows_selected_flag_and_select_all_bar_with_amount():
    nodes = [
        _n("Ponsel Uji Coba 128GB", (100, 200, 700, 240)), _n("Rp99.000", (100, 250, 300, 280)),
        _n("Pilih Semua", (80, 1530, 250, 1580)), _n("Total Rp99.000", (300, 1530, 470, 1580)),
    ]
    boxes = [_n("", (20, 230, 70, 280), cls=BOX, selected=True, checked=False),
             _n("", (20, 1530, 70, 1580), cls=BOX, checked=True)]
    rows = cart_rows(boxes, nodes)
    assert len(rows) == 1  # bar "Pilih Semua" dilewati walau ada nominal Rp di sebelahnya
    row, box = rows[0]
    assert row.checked and box is boxes[0]
    assert row.text == "Ponsel Uji Coba 128GB\nRp99.000"


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_screen.cart_rows - _SELECT_ALL_RE dicari di SEMUA teks sebaris checkbox, jadi item yang "
    "namanya memuat kata 'Semua' (mis. 'Kabel Charger untuk Semua HP') dianggap bar 'Pilih Semua' dan dilewati; "
    "lapis 2 tidak bisa meng-uncheck item itu."))
def test_cart_rows_item_named_semua_is_not_select_all():
    nodes = [_n("Kabel Charger untuk Semua HP", (100, 240, 700, 280)), _n("Rp20.000", (100, 290, 300, 320))]
    boxes = [_n("", (20, 250, 70, 300), cls=BOX, checked=True)]  # sebaris dengan nama item
    rows = cart_rows(boxes, nodes)
    assert len(rows) == 1 and rows[0][0].checked


@pytest.mark.parametrize("text, counts", [
    ("Checkout (1)", [1]), ("checkout(2)", [2]), (" Checkout ( 12 ) ", [12]),
    ("Checkout", []), ("Checkout (1) sekarang", []), ("Beli (1)", []),
])
def test_checkout_count(text, counts):
    assert checkout_count([_n(text)]) == counts


# ------------------------------------------------------------------ metode pembayaran


def test_payment_value_right_below_inline():
    label = _n("Metode Pembayaran", (20, 500, 300, 540), clickable=True)
    right = _n("ShopeePay", (420, 500, 700, 540), clickable=True)  # nilai boleh clickable
    below = _n("COD - Cek Dulu", (20, 545, 400, 580))
    assert payment_value([label, right], PAYMENT_RE) == "ShopeePay"
    assert payment_value([label, below], PAYMENT_RE) == "COD - Cek Dulu"
    assert payment_value([label, below, right], PAYMENT_RE) == "ShopeePay"  # kanan didahulukan
    assert payment_value([_n("Metode Pembayaran ShopeePay", (20, 500, 700, 540))], PAYMENT_RE) == "ShopeePay"
    assert payment_value([_n("Metode Pembayaran: SPayLater", (20, 500, 700, 540))], PAYMENT_RE) == "SPayLater"


def test_payment_value_missing():
    assert payment_value([], PAYMENT_RE) is None
    assert payment_value([_n("ShopeePay", (420, 500, 700, 540))], PAYMENT_RE) is None
    label = _n("Metode Pembayaran", (20, 500, 300, 540))
    assert payment_value([label, _n("   ", (420, 500, 700, 540))], PAYMENT_RE) is None


# ------------------------------------------------------------------ parse_dump


class _StaticApp:
    """FakeApp minimal: node tetap, mencatat tap."""

    package = "com.shopee.id"

    def __init__(self, nodes: list[Node]):
        self._nodes = nodes
        self.taps: list[tuple[int, int]] = []
        self.backs = 0

    def nodes(self) -> list[Node]:
        return list(self._nodes)

    def activity(self) -> str:
        return "com.shopee.app.ui.product.Activity"

    def on_tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def on_intent(self, url: str, package: str) -> None:
        pass

    def on_refresh(self) -> None:
        pass

    def on_back(self) -> None:
        self.backs += 1

    def webview(self) -> bool:
        return False


DUMP_NODES = [
    _n('Harga "spesial" & <hemat>', (20, 610, 400, 690)),
    _n("Baris 1\nBaris 2\tTab", (0, 0, 720, 60), desc="Tombol 'Beli'", rid="com.shopee.id:id/title",
       cls="android.widget.TextView"),
    _n("", (360, 1500, 720, 1612), desc="Beli Sekarang", clickable=True, enabled=False),
    _n("", (20, 270, 70, 320), cls=BOX, checked=True, selected=True),
    _n("Di luar layar", (-10, -5, 50, 20)),
]


def test_parse_dump_roundtrip_fake_driver_dump():
    drv = FakeDriver(_StaticApp(DUMP_NODES), FakeClock(tick=0.0))
    assert parse_dump(drv.dump()) == DUMP_NODES


def test_parse_dump_nested_u2_style_xml():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<hierarchy rotation="0">'
        '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.shopee.id" '
        'content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" '
        'selected="false" bounds="[0,0][720,1612]">'
        '<node index="0" text="Beli Sekarang" resource-id="com.shopee.id:id/buy" class="android.widget.Button" '
        'content-desc="" checked="false" clickable="true" enabled="true" selected="false" '
        'bounds="[360,1500][720,1612]" />'
        '<node index="1" text="Tanpa bounds" />'
        '</node></hierarchy>'
    )
    nodes = parse_dump(xml)
    assert [n.text for n in nodes] == ["", "Beli Sekarang", "Tanpa bounds"]
    buy = nodes[1]
    assert (buy.rid, buy.cls, buy.clickable, buy.enabled, buy.bounds) == (
        "com.shopee.id:id/buy", "android.widget.Button", True, True, (360, 1500, 720, 1612))
    bare = nodes[2]
    assert bare.bounds == (0, 0, 0, 0) and bare.enabled and not bare.clickable  # default atribut hilang


# =========================================================================== android_selectors


def test_to_sel_validation():
    assert to_sel({"text": "Beli Sekarang"}) == Sel("text", "Beli Sekarang")
    assert to_sel({"resourceId": 123}) == Sel("resourceId", "123")  # nilai dijadikan str
    for bad in ({}, {"xpath": "//node"}, {"text": "a", "textContains": "b"}, {"resource_id": "x"}):
        with pytest.raises(ValueError):
            to_sel(bad)


def test_to_sel_variant_templating():
    assert to_sel({"text": "{variant}"}) is None  # tanpa variasi: kandidat dilewati
    assert to_sel({"text": "{variant}"}, "") is None
    assert to_sel({"text": "{variant}"}, "Hitam (128GB)") == Sel("text", "Hitam (128GB)")
    assert to_sel({"textContains": "Warna {variant}"}, "1+1") == Sel("textContains", "Warna 1+1")
    sel = to_sel({"textMatches": "(?i).*{variant}.*"}, "1+1 (Promo)")
    assert sel.value == "(?i).*" + re.escape("1+1 (Promo)") + ".*"
    assert node_matches(_n("Paket 1+1 (PROMO)"), sel)
    assert not node_matches(_n("Paket 11 Promo"), sel)  # '+' dan '()' literal, bukan regex


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_selectors.to_sel - {variant} hanya di-escape untuk textMatches; descriptionMatches juga regex "
    "(UiSelector) tetapi variasi dimasukkan mentah, jadi variasi berisi '(', '+', '.' dsb. salah cocok/tidak "
    "ditemukan."))
def test_to_sel_variant_escaped_for_description_matches():
    sel = to_sel({"descriptionMatches": "(?i).*{variant}.*"}, "Hitam (128GB)")
    assert sel.value == "(?i).*" + re.escape("Hitam (128GB)") + ".*"
    assert node_matches(_n("", desc="Varian Hitam (128GB)"), sel)


def test_candidates_dedupe_and_variant_step():
    sel = android_selectors.defaults()
    assert sel.candidates("variant_option") == []  # tanpa variasi
    assert sel.candidates("variant_option", "Hitam") == [
        Sel("text", "Hitam"), Sel("textContains", "Hitam"), Sel("description", "Hitam")]
    sel.steps["variant_option"] = [{"text": "{variant}"}, {"text": "Hitam"}, {"description": "{variant}"}]
    assert sel.candidates("variant_option", "Hitam") == [Sel("text", "Hitam"), Sel("description", "Hitam")]
    assert sel.candidates("tidak_ada") == []


def test_order_candidates_priority_calibrated_first_within_kind():
    calibrated = [{"description": "Beli"}, {"textContains": "Beli"}, {"text": "Beli Sekarang"},
                  {"resourceId": "com.shopee.id:id/buy"}, {"description": "Beli"}]
    defaults = [{"text": "Beli Sekarang"}, {"textContains": "Beli Sekarang"}, {"textMatches": "Beli.*"},
                {"description": "Beli Sekarang"}, {"descriptionContains": "Beli"}, {"text": "Beli"}]
    assert order_candidates(calibrated, defaults) == [
        {"resourceId": "com.shopee.id:id/buy"},
        {"text": "Beli Sekarang"},  # dari kalibrasi (duplikat default dibuang)
        {"text": "Beli"},
        {"textContains": "Beli"},  # kalibrasi di depan default sejenis
        {"textContains": "Beli Sekarang"},
        {"textMatches": "Beli.*"},
        {"description": "Beli"},
        {"description": "Beli Sekarang"},
        {"descriptionContains": "Beli"},
    ]
    assert order_candidates([], []) == []


_UNORDERED_DEFAULT = pytest.mark.xfail(strict=True, reason=(
    "BUG: android_selectors.ANDROID_DEFAULT_STEPS['sheet_confirm'] = text -> textContains -> text ('Konfirmasi'), "
    "melanggar urutan wajib resourceId -> text -> textContains -> description; urutan default juga berbeda dengan "
    "hasil load() bila selectors.json punya kalibrasi sheet_confirm (order_candidates mengurutkan ulang)."))


@pytest.mark.parametrize("step", [pytest.param(s, marks=_UNORDERED_DEFAULT) if s == "sheet_confirm" else s
                                  for s in ANDROID_DEFAULT_STEPS])
def test_default_steps_valid_and_already_in_priority_order(step):
    cands = ANDROID_DEFAULT_STEPS[step]
    ranks = []
    for c in cands:
        sel = to_sel(c, "Hitam")
        assert sel is not None and sel.by in SEL_KINDS, c
        if sel.by.endswith("Matches"):
            re.compile(sel.value)
        ranks.append(KIND_RANK[sel.by])
    assert ranks == sorted(ranks), cands
    assert order_candidates([], cands) == cands


def test_default_step_regexes_match_intended_texts():
    sel = android_selectors.defaults()

    def hits(step: str, text: str) -> bool:
        return any(node_matches(_n(text, desc=text), s) for s in sel.candidates(step, "Hitam"))

    assert hits("sheet_marker", "Stok: 25") and hits("sheet_marker", "Jumlah")
    assert not hits("sheet_marker", "Stok Habis")
    assert hits("cart_checkout", "Checkout (1)") and hits("cart_checkout", "Checkout")
    assert hits("payment_shopeepay", "ShopeePay") and hits("payment_shopeepay", "ShopeePay (Saldo Rp500.000)")
    assert not hits("payment_shopeepay", "SPayLater") and not hits("payment_shopeepay", "ShopeePay Later")
    assert hits("place_order", "Buat Pesanan") and not hits("place_order", "Buat Pesanan Sekarang")
    assert hits("cart_marker", "Keranjang Saya (3)") and not hits("cart_marker", "Masukkan Keranjang")


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_missing_file_returns_fresh_defaults(tmp_path):
    a = android_selectors.load(tmp_path / "tidak-ada.json")
    assert a.source is None and a.steps == ANDROID_DEFAULT_STEPS and a.markers == ANDROID_DEFAULT_MARKERS
    a.steps["buy_button"].append({"text": "X"})
    a.markers["captcha"].append("x")
    b = android_selectors.load(tmp_path / "tidak-ada.json")
    assert {"text": "X"} not in b.steps["buy_button"] and "x" not in b.markers["captcha"]  # deep copy


def test_load_merges_android_section_with_defaults(tmp_path):
    path = _write_json(tmp_path / "selectors.json", {
        "version": 1,
        "web": {"buy_button": ["button.buy"]},
        "android": {
            "steps": {
                "buy_button": [{"description": "Beli"}, {"resourceId": "com.shopee.id:id/btn_buy"},
                               {"text": "Beli Sekarang"}],
                "product_price": [{"resourceId": "com.shopee.id:id/tv_price"}],
                "langkah_baru": [{"text": "Baru"}],
            },
            "markers": {"captcha": ["(?is).*geser.*", ANDROID_DEFAULT_MARKERS["captcha"][1]],
                        "antre": ["(?i).*antrean penuh.*"]},
            "urls": {"wallet_page": "https://shopee.co.id/x/wallet"},
            "calibrated": {"at": "2026-10-01T10:00:00+07:00", "serial": "FAKE123"},
        },
    })
    sel = android_selectors.load(path)
    assert sel.source == path
    assert sel.steps["buy_button"] == [
        {"resourceId": "com.shopee.id:id/btn_buy"}, {"text": "Beli Sekarang"}, {"textContains": "Beli Sekarang"},
        {"description": "Beli"}, {"description": "Beli Sekarang"}]
    assert sel.candidates("buy_button")[0] == Sel("resourceId", "com.shopee.id:id/btn_buy")
    assert sel.steps["product_price"] == [{"resourceId": "com.shopee.id:id/tv_price"}]
    assert sel.steps["langkah_baru"] == [{"text": "Baru"}]
    assert sel.steps["place_order"] == ANDROID_DEFAULT_STEPS["place_order"]  # tak disentuh = default
    defaults_captcha = ANDROID_DEFAULT_MARKERS["captcha"]
    assert sel.markers["captcha"] == ["(?is).*geser.*", defaults_captcha[1], defaults_captcha[0], defaults_captcha[2]]
    assert sel.markers["antre"] == ["(?i).*antrean penuh.*"]
    assert sel.marker_re("antre").fullmatch("ANTREAN PENUH, coba lagi")
    assert sel.markers["pin"] == ANDROID_DEFAULT_MARKERS["pin"]
    assert sel.urls["wallet_page"] == "https://shopee.co.id/x/wallet"
    assert sel.urls["address_page"] == android_selectors.ANDROID_DEFAULT_URLS["address_page"]
    assert sel.calibrated["serial"] == "FAKE123"


def test_load_without_android_section_is_defaults(tmp_path):
    path = _write_json(tmp_path / "selectors.json", {"version": 1, "web": {"buy_button": ["button.buy"]}})
    sel = android_selectors.load(path)
    assert sel.steps == ANDROID_DEFAULT_STEPS and sel.markers == ANDROID_DEFAULT_MARKERS
    assert sel.source == path


@pytest.mark.parametrize("section, err", [
    ({"steps": {"buy_button": [{"xpath": "//x"}]}}, ValueError),
    ({"steps": {"buy_button": [{"text": "a", "description": "b"}]}}, ValueError),
    ({"markers": {"captcha": ["(?is).*(geser"]}}, re.error),
])
def test_load_rejects_invalid_android_section(tmp_path, section, err):
    path = _write_json(tmp_path / "selectors.json", {"android": section})
    with pytest.raises(err):
        android_selectors.load(path)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_selectors.load - nilai regex kandidat (textMatches/descriptionMatches) tidak divalidasi "
    "(penanda divalidasi dengan re.compile); regex rusak lolos lalu meledak sebagai re.error (bukan DriverError) "
    "di AndroidRunner._local/node_matches saat klasifikasi layar."))
def test_load_rejects_invalid_selector_regex(tmp_path):
    path = _write_json(tmp_path / "selectors.json",
                       {"android": {"steps": {"cart_checkout": [{"textMatches": "Checkout(("}]}}})
    with pytest.raises((ValueError, re.error)):
        android_selectors.load(path)


def test_save_new_file_without_backup(tmp_path):
    path = tmp_path / "selectors.json"
    steps = {"buy_button": [{"resourceId": "com.shopee.id:id/btn_buy"}]}
    assert android_selectors.save(path, steps, {"serial": "FAKE123"}) is None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "android": {"steps": steps, "urls": {}, "calibrated": {"serial": "FAKE123"},
                                              "markers": {}}}
    assert list(tmp_path.iterdir()) == [path]


def test_save_keeps_web_section_and_creates_backup(tmp_path):
    original = {
        "version": 1,
        "web": {"buy_button": ["button.buy", "text=Beli Sekarang"]},
        "android": {"steps": {"sheet_marker": [{"text": "Jumlah Barang"}]},
                    "markers": {"captcha": ["(?is).*geser.*"]},
                    "urls": {"address_page": "https://shopee.co.id/a", "wallet_page": "https://shopee.co.id/w"},
                    "calibrated": {"serial": "LAMA"}},
    }
    path = _write_json(tmp_path / "selectors.json", original)
    before = path.read_bytes()
    steps = {"buy_button": [{"resourceId": "com.shopee.id:id/btn_buy"}, {"text": "Beli Sekarang"}]}
    backup = android_selectors.save(path, steps, {"serial": "BARU"}, urls={"wallet_page": "https://shopee.co.id/w2"})
    assert backup is not None and backup.exists()
    assert re.fullmatch(r"selectors\.json\.bak-\d{8}-\d{6}", backup.name), backup.name
    assert backup.read_bytes() == before  # versi lama utuh
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["web"] == original["web"]  # bagian web dibiarkan
    sec = data["android"]
    assert sec["steps"] == steps
    assert sec["markers"] == {"captcha": ["(?is).*geser.*"]}
    assert sec["urls"] == {"address_page": "https://shopee.co.id/a", "wallet_page": "https://shopee.co.id/w2"}
    assert sec["calibrated"] == {"serial": "BARU"}
    loaded = android_selectors.load(path)  # hasil kalibrasi didahulukan saat dimuat lagi
    assert loaded.candidates("buy_button")[:2] == [Sel("resourceId", "com.shopee.id:id/btn_buy"),
                                                   Sel("text", "Beli Sekarang")]
    assert loaded.markers["captcha"][0] == "(?is).*geser.*"


# ------------------------------------------------------------------ scoped / union


@pytest.mark.parametrize("pattern, expected", [
    ("(?is).*x.*", "(?is:.*x.*)"),
    ("(?i)a|b", "(?i:a|b)"),
    ("abc", "(?:abc)"),
    ("a|b", "(?:a|b)"),
    ("(?:a)(?i)", "(?:(?:a)(?i))"),  # bukan flag di awal -> dibungkus apa adanya
])
def test_scoped(pattern, expected):
    assert scoped(pattern) == expected


def test_union_keeps_per_pattern_flags():
    rx = re.compile(union(["(?i)beli", "Habis", "(?s)a.b", "x|y"]))
    assert rx.fullmatch("BELI") and rx.fullmatch("Habis") and rx.fullmatch("a\nb") and rx.fullmatch("y")
    assert rx.fullmatch("habis") is None  # 'i' milik pola pertama tidak bocor
    assert re.compile(union(["a.b"])).fullmatch("a\nb") is None  # 's' tidak bocor
    assert rx.fullmatch("BELIHabis") is None  # alternatif utuh, tidak tergabung


def test_union_of_all_default_markers_and_watch_pattern_compile():
    every = [p for pats in ANDROID_DEFAULT_MARKERS.values() for p in pats]
    re.compile(union(every))
    watch = re.compile(union([PRICE_MATCH, *every]))
    assert watch.fullmatch("Rp99.000") and watch.fullmatch("Stok habis")
    assert watch.fullmatch("Beli Sekarang") is None
    sel = android_selectors.defaults()
    for name in ANDROID_DEFAULT_MARKERS:
        assert sel.marker_re(name) is not None
    assert sel.marker_re("tidak_ada") is None


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_selectors.scoped - hanya SATU grup flag di awal yang dipindah; pola '(?i)(?s)...' lolos "
    "validasi load() (re.compile OK) tetapi union()/marker_re menghasilkan '(?i:(?s)...)' yang ditolak Python re "
    "-> re.error saat AndroidRunner dibuat."))
def test_stacked_leading_flags_marker_usable(tmp_path):
    path = _write_json(tmp_path / "selectors.json", {"android": {"markers": {"antre": ["(?i)(?s).*antrean.*"]}}})
    try:
        sel = android_selectors.load(path)
    except (ValueError, re.error):
        return  # ditolak saat load juga benar (gagal lebih awal)
    assert sel.marker_re("antre").fullmatch("ANTREAN\npenuh")


# ------------------------------------------------------------------ sampel penanda (teks Indonesia)


MARKER_SAMPLES: dict[str, list[str]] = {
    "captcha": ["captcha", "Masukkan kode CAPTCHA", "Geser untuk verifikasi", "Geser untuk menyelesaikan puzzle",
                "Geser untuk melanjutkan", "Saya bukan robot", "Selesaikan puzzle", "Verifikasi keamanan",
                "Verifikasi\nGeser untuk verifikasi"],
    "verification": ["Kami mendeteksi aktivitas tidak biasa pada akun Anda", "Aktivitas yang mencurigakan",
                     "Masukkan kode OTP", "Masukkan OTP", "Kode verifikasi telah dikirim melalui SMS",
                     "Verifikasi akun Anda", "Verifikasi identitas", "Verifikasi diperlukan"],
    "login": ["Log in", "Login", "Masuk", "Masuk dengan Google", "Log In dengan Facebook", "Lupa Password?",
              "Lupa kata sandi?"],
    "pin": ["Masukkan PIN ShopeePay", "Masukkan PIN", "PIN ShopeePay"],
    "sold_out": ["Habis", "Stok Habis", "Produk ini terjual habis", "Habis Terjual", "Flash Sale telah berakhir",
                 "Flash Sale sudah berakhir", "Flash Sale berakhir"],
    "not_started": ["Flash Sale dimulai dalam 00:00:05", "Flash sale belum dimulai", "Segera Hadir",
                    "Ingatkan Saya"],
    "variant_required": ["Silakan pilih variasi terlebih dahulu", "Harap pilih variasi"],
    "error_toast": ["Terjadi kesalahan, silakan coba lagi", "Coba lagi nanti", "Gagal memuat halaman"],
}

# Teks layar normal yang TIDAK boleh cocok dengan penanda mana pun.
NORMAL_TEXTS = [
    "Detail Produk", "Beli Sekarang", "Masukkan Keranjang", "Flash Sale berakhir dalam 01:59:59",
    "FLASH SALE BERAKHIR DALAM 00:10:00", "Flash Sale berakhir\ndalam 01:59:59", "Terjual 1RB", "Rp99.000",
    "Stok: 25", "Hampir habis", "Gratis Ongkir", "Checkout (1)", "Buat Pesanan", "Metode Pembayaran", "ShopeePay",
    "Saldo Rp500.000", "Jumlah", "Pilih Variasi", "Masukkan Kode Voucher", "Promo Spesial Hari Ini",
    "Terima kasih!", "Toko Uji Resmi", "Subtotal Pengiriman", "Total Pembayaran", "Keranjang Saya (1)",
]


def test_marker_samples_cover_every_default_marker_and_pattern():
    assert set(MARKER_SAMPLES) == set(ANDROID_DEFAULT_MARKERS)
    for name, pats in ANDROID_DEFAULT_MARKERS.items():
        for p in pats:
            re.compile(p)
            assert any(re.fullmatch(p, s) for s in MARKER_SAMPLES[name]), f"{name}: pola {p!r} tanpa sampel"


@pytest.mark.parametrize("name", sorted(MARKER_SAMPLES))
def test_default_marker_matches_samples(name):
    rx = android_selectors.defaults().marker_re(name)
    for s in MARKER_SAMPLES[name]:
        assert rx.fullmatch(s), f"{name} tidak cocok dengan {s!r}"


@pytest.mark.parametrize("text", NORMAL_TEXTS)
def test_default_markers_ignore_normal_texts(text):
    sel = android_selectors.defaults()
    hit = [name for name in ANDROID_DEFAULT_MARKERS if sel.marker_re(name).fullmatch(text)]
    assert hit == [], f"{text!r} salah dikenali sebagai {hit}"


def test_countdown_banner_is_not_sold_out_nor_masukkan_keranjang_login():
    sel = android_selectors.defaults()
    assert sel.marker_re("sold_out").fullmatch("Flash Sale berakhir dalam 01:59:59") is None
    assert sel.marker_re("sold_out").fullmatch("Flash Sale telah berakhir")
    assert sel.marker_re("login").fullmatch("Masukkan Keranjang") is None
    assert sel.marker_re("login").fullmatch("Masuk")


@pytest.fixture
def runner(tmp_path):
    r, _app, _drv, _clock, _open_at, log, _notifier = make_android(tmp_path)
    yield r
    log.close()


def test_runner_marker_ignores_long_description(runner):
    desc = ("Deskripsi produk: ponsel uji coba dengan baterai besar dan layar lebar. " * 2 +
            "Catatan: jika stok habis, pesanan dibatalkan otomatis oleh penjual.")
    assert len(desc) > MARKER_MAX_CHARS
    long_node = _n(desc, (20, 900, 700, 1400))
    assert runner._markers["sold_out"].fullmatch(desc)  # regex-nya cocok; yang menyaring = batas panjang
    assert runner._marker("sold_out", [long_node]) is None
    buy = _n("Beli Sekarang", (360, 1500, 720, 1612), clickable=True)
    seen = runner._classify_nodes([long_node, buy], "after_buy")
    assert seen.screen == Screen.PRODUCT
    toast = _n("Stok habis", (160, 1300, 560, 1350))
    assert runner._classify_nodes([long_node, toast, buy], "after_buy").screen == Screen.SOLD_OUT


def test_runner_marker_length_boundary(runner):
    base = "Stok habis "
    at_limit = base + "x" * (MARKER_MAX_CHARS - len(base))
    assert len(at_limit) == MARKER_MAX_CHARS
    assert runner._marker("sold_out", [_n(at_limit)]) is not None
    assert runner._marker("sold_out", [_n(at_limit + "x")]) is None


# =========================================================================== android_driver


def test_sel_kinds_and_rendering():
    with pytest.raises(ValueError):
        Sel("xpath", "//node")
    with pytest.raises(ValueError):
        Sel("Text", "Beli")  # jenis case-sensitive
    s = Sel("textContains", "Beli")
    assert s.kwargs() == {"textContains": "Beli"}
    assert str(s) == "textContains='Beli'"
    assert {s, Sel("textContains", "Beli")} == {s}  # frozen & hashable (dipakai sebagai kunci cache)


@pytest.mark.parametrize("sel, node, expected", [
    (Sel("text", "Beli Sekarang"), _n("Beli Sekarang"), True),
    (Sel("text", "beli sekarang"), _n("Beli Sekarang"), False),  # case-sensitive
    (Sel("text", "Beli"), _n("Beli Sekarang"), False),  # sama persis
    (Sel("text", "Beli Sekarang"), _n("Beli Sekarang "), False),
    (Sel("text", "Beli Sekarang"), _n("", desc="Beli Sekarang"), False),  # text tidak melihat desc
    (Sel("textContains", "Sekarang"), _n("Beli Sekarang"), True),
    (Sel("textContains", "sekarang"), _n("Beli Sekarang"), False),
    (Sel("textStartsWith", "Keranjang Saya"), _n("Keranjang Saya (2)"), True),
    (Sel("textStartsWith", "Saya"), _n("Keranjang Saya (2)"), False),
    (Sel("textMatches", "Beli"), _n("Beli Sekarang"), False),  # Matches = seluruh teks
    (Sel("textMatches", "Beli.*"), _n("Beli Sekarang"), True),
    (Sel("textMatches", "a|ab"), _n("ab"), True),
    (Sel("textMatches", "(?i)beli sekarang"), _n("BELI SEKARANG"), True),
    (Sel("description", "Beli Sekarang"), _n("Beli Sekarang"), False),  # desc tidak melihat text
    (Sel("description", "Beli Sekarang"), _n("", desc="Beli Sekarang"), True),
    (Sel("description", "beli sekarang"), _n("", desc="Beli Sekarang"), False),
    (Sel("descriptionContains", "Sekarang"), _n("", desc="Beli Sekarang"), True),
    (Sel("descriptionMatches", "Beli"), _n("", desc="Beli Sekarang"), False),
    (Sel("descriptionMatches", "Beli.*"), _n("", desc="Beli Sekarang"), True),
    (Sel("resourceId", "com.shopee.id:id/buy"), _n(rid="com.shopee.id:id/buy"), True),
    (Sel("resourceId", "buy"), _n(rid="com.shopee.id:id/buy"), False),
    (Sel("className", "android.widget.CheckBox"), _n(cls=BOX), True),
    (Sel("className", "CheckBox"), _n(cls=BOX), False),
])
def test_node_matches_semantics(sel, node, expected):
    assert node_matches(node, sel) is expected


def _u2_info(text=None, desc=None, rid="", cls="android.widget.TextView", bounds=(0, 0, 0, 0), visible=None,
             **flags) -> dict:
    def box(b):
        return {"left": b[0], "top": b[1], "right": b[2], "bottom": b[3]}

    info = {"text": text, "contentDescription": desc, "resourceName": rid, "className": cls,
            "bounds": box(bounds), "enabled": True, "clickable": False, "selected": False, "checked": False,
            "packageName": "com.shopee.id", "childCount": 0}
    if visible is not None:
        info["visibleBounds"] = box(visible)
    info.update(flags)
    return info


def test_node_from_u2():
    n = Node.from_u2(_u2_info("Beli Sekarang", None, "com.shopee.id:id/buy", "android.widget.Button",
                              (360, 1500, 720, 1700), visible=(360, 1500, 720, 1612), clickable=True))
    assert n == Node(text="Beli Sekarang", desc="", rid="com.shopee.id:id/buy", cls="android.widget.Button",
                     enabled=True, clickable=True, bounds=(360, 1500, 720, 1612))  # visibleBounds didahulukan
    only_bounds = Node.from_u2(_u2_info(None, "Rp99.000", bounds=(20, 610, 400, 690), enabled=False,
                                        checked=True, selected=True))
    assert only_bounds.bounds == (20, 610, 400, 690) and only_bounds.label == "Rp99.000"
    assert (only_bounds.enabled, only_bounds.checked, only_bounds.selected) == (False, True, True)
    empty_visible = Node.from_u2({**_u2_info("x", bounds=(1, 2, 3, 4)), "visibleBounds": {}})
    assert empty_visible.bounds == (1, 2, 3, 4)
    bare = Node.from_u2({})
    assert bare == Node()  # semua default; enabled default True


# ------------------------------------------------------------------ device uiautomator2 palsu


class UiObjectNotFoundError(Exception):
    """Nama sama persis dengan uiautomator2.exceptions.UiObjectNotFoundError (U2Driver mengenali lewat nama)."""


class _ShellResponse:
    def __init__(self, output: str):
        self.output = output
        self.exit_code = 0


class _Lazy:
    """Seperti u2 `Exists`: RPC baru terjadi saat bool()."""

    def __init__(self, fn):
        self.fn = fn

    def __bool__(self) -> bool:
        return self.fn()


class _FakeU2Object:
    def __init__(self, dev: FakeU2Device, kw: dict):
        self.dev, self.kw = dev, kw

    def _found(self, op: str) -> list[dict]:
        self.dev.queries.append((op, dict(self.kw)))
        self.dev.maybe_raise(op)
        ((by, value),) = self.kw.items()
        sel = Sel(by, value)
        return [i for i in self.dev.elements if node_matches(Node.from_u2(i), sel)]

    @property
    def exists(self):
        return _Lazy(lambda: bool(self._found("exists")))

    @property
    def info(self) -> dict:
        found = self._found("info")
        if not found:
            raise UiObjectNotFoundError({"code": -32002, "message": "UiObjectNotFoundException", "data": self.kw})
        return found[0]

    def info_list(self) -> list[dict]:
        found = self._found("info_list")
        if not found and self.dev.info_list_not_found:
            raise UiObjectNotFoundError({"code": -32002, "data": self.kw})
        return found


class FakeU2Device:
    """Pengganti uiautomator2.Device: hanya API yang dipakai U2Driver; semua panggilan dicatat."""

    serial = "0123456789ABCDEF"

    def __init__(self, elements=(), *, size=(720, 1612), shell_output="", app=None):
        self.elements = list(elements)
        self.size = size
        self.shell_output = shell_output
        self.app = app if app is not None else {"package": "com.shopee.id",
                                                "activity": "com.shopee.app.ui.home.HomeActivity_"}
        self.errors: dict[str, Exception] = {}
        self.info_list_not_found = False
        self.alive: bool | Exception = True
        self.queries: list[tuple[str, dict]] = []
        self.actions: list[tuple] = []
        self.shells: list[tuple[list[str], int]] = []
        self.window_size_calls = 0
        self.write_screenshot = True

    def maybe_raise(self, op: str) -> None:
        if op in self.errors:
            raise self.errors[op]

    def __call__(self, **kw) -> _FakeU2Object:
        return _FakeU2Object(self, kw)

    def click(self, x, y) -> None:
        self.maybe_raise("click")
        self.actions.append(("click", x, y))

    def app_current(self) -> dict:
        self.maybe_raise("app_current")
        return self.app

    def shell(self, cmd, timeout=60):
        self.maybe_raise("shell")
        self.shells.append((cmd, timeout))
        return _ShellResponse(self.shell_output) if isinstance(self.shell_output, str) else self.shell_output

    def window_size(self):
        self.window_size_calls += 1
        return self.size

    def swipe(self, fx, fy, tx, ty, duration=None, steps=None) -> None:
        self.maybe_raise("swipe")
        self.actions.append(("swipe", fx, fy, tx, ty, duration))

    def press(self, key) -> None:
        self.maybe_raise("press")
        self.actions.append(("press", key))

    def screenshot(self, filename=None):
        self.maybe_raise("screenshot")
        self.actions.append(("screenshot", filename))
        if self.write_screenshot:
            Path(filename).write_bytes(b"\x89PNG\r\n\x1a\n")

    def dump_hierarchy(self) -> str:
        self.maybe_raise("dump_hierarchy")
        return '<hierarchy rotation="0"><node text="Beli Sekarang" bounds="[0,0][10,10]" /></hierarchy>'

    def _check_alive(self) -> bool:
        if isinstance(self.alive, Exception):
            raise self.alive
        return self.alive

    def stop_uiautomator(self) -> None:
        self.maybe_raise("stop_uiautomator")
        self.actions.append(("stop_uiautomator",))

    def start_uiautomator(self) -> None:
        self.maybe_raise("start_uiautomator")
        self.actions.append(("start_uiautomator",))


BUY_INFO = _u2_info("Beli Sekarang", None, "com.shopee.id:id/buy", "android.widget.Button", (360, 1500, 720, 1612),
                    clickable=True)
DESC_INFO = _u2_info("", "Keranjang", "", "android.widget.ImageView", (600, 40, 680, 120))
PRODUCT_URL = "https://shopee.co.id/Ponsel-Uji-Coba-128GB-i.1001.2002"


def _u2(**kw) -> tuple[U2Driver, FakeU2Device]:
    dev = FakeU2Device([BUY_INFO, DESC_INFO], **kw)
    return U2Driver(device=dev), dev


def test_u2_init_serial():
    dev = FakeU2Device()
    assert U2Driver(device=dev).serial == "0123456789ABCDEF"
    assert U2Driver("SERIALKU", device=dev).serial == "SERIALKU"

    class NoSerial(FakeU2Device):
        serial = None

    assert U2Driver(device=NoSerial()).serial == ""


def test_u2_connect_failure_wrapped(monkeypatch):
    import uiautomator2

    def boom(serial=None):
        raise RuntimeError("device 'ABC' not found")

    monkeypatch.setattr(uiautomator2, "connect", boom)
    with pytest.raises(DriverError, match="gagal konek ke device ABC"):
        U2Driver("ABC")
    dev = FakeU2Device()
    monkeypatch.setattr(uiautomator2, "connect", lambda serial=None: dev)
    assert U2Driver("").d is dev


def test_u2_exists_info_find_all():
    drv, dev = _u2()
    assert drv.exists(Sel("text", "Beli Sekarang")) is True
    assert drv.exists(Sel("text", "Tidak Ada")) is False
    assert dev.queries[0] == ("exists", {"text": "Beli Sekarang"})  # selector diteruskan apa adanya
    node = drv.info(Sel("resourceId", "com.shopee.id:id/buy"))
    assert node.label == "Beli Sekarang" and node.bounds == (360, 1500, 720, 1612) and node.clickable
    assert drv.info(Sel("text", "Tidak Ada")) is None  # UiObjectNotFoundError -> None
    assert [n.label for n in drv.find_all(Sel("className", "android.widget.Button"))] == ["Beli Sekarang"]
    assert drv.find_all(Sel("text", "Tidak Ada")) == []
    dev.info_list_not_found = True
    assert drv.find_all(Sel("text", "Tidak Ada")) == []  # UiObjectNotFoundError -> []


@pytest.mark.parametrize("op, call", [
    ("exists", lambda d: d.exists(Sel("text", "Beli Sekarang"))),
    ("info", lambda d: d.info(Sel("text", "Beli Sekarang"))),
    ("info_list", lambda d: d.find_all(Sel("text", "Beli Sekarang"))),
    ("click", lambda d: d.click(Node(bounds=(0, 0, 10, 10)))),
    ("app_current", lambda d: d.current_app()),
    ("shell", lambda d: d.shell(["getprop"])),
    ("swipe", lambda d: d.swipe_refresh()),
    ("press", lambda d: d.press_back()),
    ("dump_hierarchy", lambda d: d.dump()),
    ("stop_uiautomator", lambda d: d.restart_agent()),
])
def test_u2_errors_wrapped_in_driver_error(op, call):
    drv, dev = _u2()
    original = ConnectionResetError("koneksi ke agent putus")
    dev.errors[op] = original
    with pytest.raises(DriverError) as ei:
        call(drv)
    assert "ConnectionResetError: koneksi ke agent putus" in str(ei.value)
    assert ei.value.__cause__ is original


def test_u2_error_message_names_the_query():
    drv, dev = _u2()
    dev.errors["info"] = RuntimeError("rpc timeout")
    with pytest.raises(DriverError, match=r"^info text='Beli Sekarang': RuntimeError: rpc timeout"):
        drv.info(Sel("text", "Beli Sekarang"))
    dev.errors["info"] = DriverError("sudah DriverError")
    with pytest.raises(DriverError, match="^sudah DriverError$"):  # tidak dibungkus dua kali
        drv.info(Sel("text", "Beli Sekarang"))


def test_u2_click_and_get_text():
    drv, dev = _u2()
    assert drv.click(Sel("text", "Beli Sekarang")) is True
    assert dev.actions == [("click", 540, 1556)]  # tap tengah bounds
    assert drv.click(Sel("text", "Tidak Ada")) is False
    assert dev.actions == [("click", 540, 1556)]  # tidak ada tap tambahan
    n_queries = len(dev.queries)
    assert drv.click(Node(text="x", bounds=(0, 100, 101, 201))) is True
    assert dev.actions[-1] == ("click", 50, 150)
    assert len(dev.queries) == n_queries  # klik Node = 1 RPC, tanpa query info
    assert drv.get_text(Sel("text", "Beli Sekarang")) == "Beli Sekarang"
    assert drv.get_text(Sel("description", "Keranjang")) == "Keranjang"  # text kosong -> desc
    assert drv.get_text(Sel("text", "Tidak Ada")) is None


def test_u2_current_app():
    drv, dev = _u2()
    assert drv.current_app() == AppInfo("com.shopee.id", "com.shopee.app.ui.home.HomeActivity_")
    dev.app = {}
    assert drv.current_app() == AppInfo("", "")


START_OK = ("Starting: Intent { act=android.intent.action.VIEW dat=https://shopee.co.id/... pkg=com.shopee.id }\n"
            "Status: ok\nLaunchState: WARM\nActivity: com.shopee.id/com.shopee.app.ui.home.HomeActivity_\n"
            "TotalTime: 412\nWaitTime: 420\nComplete\n")
START_TOP = ("Starting: Intent { act=android.intent.action.VIEW dat=https://shopee.co.id/... pkg=com.shopee.id }\n"
             "Warning: Activity not started, intent has been delivered to currently running top-most instance.\n"
             "Status: ok\nLaunchState: UNKNOWN (0)\nActivity: com.shopee.id/.MainActivity\nWaitTime: 15\nComplete\n")


@pytest.mark.parametrize("output", [START_OK, START_TOP])
def test_u2_start_url_command_and_success(output):
    drv, dev = _u2(shell_output=output)
    drv.start_url(PRODUCT_URL, "com.shopee.id")
    assert dev.shells == [(["am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", PRODUCT_URL,
                            "-p", "com.shopee.id"], 30)]


@pytest.mark.parametrize("output", [
    "Starting: Intent { act=android.intent.action.VIEW dat=https://shopee.co.id/... pkg=com.shopee.id }\n"
    "Error: Activity not started, unable to resolve Intent { act=android.intent.action.VIEW "
    "dat=https://shopee.co.id/... flg=0x10000000 pkg=com.shopee.id }\n",
    "Error type 3\nError: Activity class {com.shopee.id/com.shopee.app.ui.Foo} does not exist.\n",
    "Exception occurred while executing 'start':\njava.lang.SecurityException: Permission Denial\n",
])
def test_u2_start_url_error_output_raises(output):
    drv, _ = _u2(shell_output=output)
    with pytest.raises(DriverError, match="am start gagal"):
        drv.start_url(PRODUCT_URL, "com.shopee.id")


def test_u2_start_url_unable_to_resolve_message():
    drv, _ = _u2(shell_output="Error: Activity not started, unable to resolve Intent { act=VIEW }")
    with pytest.raises(DriverError, match="unable to resolve Intent"):
        drv.start_url("https://shopee.co.id/user/shopeepay", "com.shopee.id")


def test_u2_shell_output_variants():
    drv, dev = _u2(shell_output="Physical size: 1080x2460\n")
    assert drv.shell(["wm", "size"]) == "Physical size: 1080x2460\n"
    assert dev.shells[-1] == (["wm", "size"], 30)
    dev.shell_output = "teks mentah"  # u2 lama: string langsung
    assert drv.shell(["getprop"]) == "teks mentah"

    class Raw(FakeU2Device):
        def shell(self, cmd, timeout=60):
            return "langsung str"

    assert U2Driver(device=Raw()).shell(["id"]) == "langsung str"

    class Odd(FakeU2Device):
        def shell(self, cmd, timeout=60):
            return 42

    assert U2Driver(device=Odd()).shell(["id"]) == ""


def test_u2_window_size_cached_and_swipe_refresh_pulls_down():
    drv, dev = _u2(size=(1080, 2460))
    drv.swipe_refresh()
    drv.swipe_refresh()
    assert dev.window_size_calls == 1
    _, fx, fy, tx, ty, duration = dev.actions[0]
    assert (fx, tx) == (540, 540)
    assert fy == pytest.approx(2460 * 0.30) and ty == pytest.approx(2460 * 0.75)
    assert fy < ty  # tarik ke bawah = muat ulang
    assert duration == pytest.approx(0.12)
    assert drv.window_size() == (1080, 2460)
    assert dev.window_size_calls == 1


def test_u2_press_back_webview_screenshot_dump(tmp_path):
    drv, dev = _u2()
    drv.press_back()
    assert dev.actions[-1] == ("press", "back")
    assert drv.webview_present() is False
    assert dev.queries[-1] == ("exists", {"className": "android.webkit.WebView"})
    dev.elements.append(_u2_info(cls="android.webkit.WebView", bounds=(0, 0, 720, 1612)))
    assert drv.webview_present() is True
    shot = tmp_path / "akhir.png"
    assert drv.screenshot(shot) is True
    assert dev.actions[-1] == ("screenshot", str(shot))
    dev.write_screenshot = False
    assert drv.screenshot(tmp_path / "tidak-ada.png") is False  # file tidak tertulis -> False
    assert "Beli Sekarang" in drv.dump()


def test_u2_agent_alive_and_restart():
    drv, dev = _u2()
    assert drv.agent_alive() is True
    dev.alive = False
    assert drv.agent_alive() is False
    dev.alive = ConnectionError("port forward hilang")
    assert drv.agent_alive() is False  # error saat cek = dianggap mati
    drv.restart_agent()
    assert dev.actions[-2:] == [("stop_uiautomator",), ("start_uiautomator",)]
    dev.errors["start_uiautomator"] = RuntimeError("server not ready")
    with pytest.raises(DriverError, match="restart agent: RuntimeError: server not ready"):
        drv.restart_agent()

    class NoCheck(FakeU2Device):
        _check_alive = None

    assert U2Driver(device=NoCheck()).agent_alive() is None  # tidak bisa dicek


# ------------------------------------------------------------------ TimedDriver & QueryStats


class _SeqMono:
    """monotonic palsu: mengembalikan nilai berurutan."""

    def __init__(self, *values: float):
        self.values = list(values)

    def __call__(self) -> float:
        return self.values.pop(0)


def _fake_driver(latency_s: float = 0.02):
    clock = FakeClock(tick=0.0)
    app = _StaticApp([_n("Beli Sekarang", (360, 1500, 720, 1612), clickable=True), _n("Rp99.000", (20, 610, 400, 690))])
    drv = FakeDriver(app, clock, latency_s=latency_s, props={"ro.product.model": "TECNO BG6"})
    return drv, app, clock


def test_timed_driver_records_every_call_with_latency(tmp_path):
    inner, app, clock = _fake_driver()
    server = iter(range(1_000, 10_000, 10))
    td = TimedDriver(inner, clock.monotonic, lambda: next(server))
    assert td.serial == "FAKE123"
    buy = Sel("text", "Beli Sekarang")
    assert td.exists(buy) is True
    node = td.info(buy)
    assert td.find_all(Sel("textContains", "Rp")) == [app.nodes()[1]]
    assert td.click(buy) is True
    assert td.click(node) is True
    assert td.get_text(buy) == "Beli Sekarang"
    assert td.current_app() == AppInfo("com.shopee.id", "com.shopee.app.ui.product.Activity")
    td.start_url(PRODUCT_URL, "com.shopee.id")
    td.swipe_refresh()
    td.press_back()
    assert td.webview_present() is False
    assert td.screenshot(tmp_path / "x.png") is True
    assert "<hierarchy>" in td.dump()
    assert td.shell(["getprop", "ro.product.model"]) == "TECNO BG6\n"
    assert td.agent_alive() is True
    td.restart_agent()
    assert td.window_size() == (720, 1612)

    samples = td.stats.samples
    assert [s.op for s in samples] == [
        "exists", "info", "find_all", "click", "click", "get_text", "current_app", "start_url", "swipe_refresh",
        "press_back", "webview", "screenshot", "dump", "shell", "agent_alive", "restart_agent", "window_size"]
    by_op = {s.op: s for s in samples}
    assert samples[0].target == "text='Beli Sekarang'"
    assert samples[4].target == "node 'Beli Sekarang'"
    assert by_op["start_url"].target == PRODUCT_URL
    assert by_op["shell"].target == "getprop ro.product.model"
    assert by_op["screenshot"].target == str(tmp_path / "x.png")
    expected_ms = {"exists": 20, "info": 20, "find_all": 20, "get_text": 20, "start_url": 300, "swipe_refresh": 150,
                   "screenshot": 200, "dump": 400, "agent_alive": 5, "restart_agent": 2000, "window_size": 0}
    for op, ms in expected_ms.items():
        assert by_op[op].ms == pytest.approx(ms), op
    assert samples[3].ms == pytest.approx(40)  # click(Sel) = info + tap, dicatat SEKALI sebagai satu query
    assert samples[4].ms == pytest.approx(20)  # click(Node) = 1 RPC
    assert [s.t_server_ms for s in samples] == list(range(1_000, 1_000 + 10 * len(samples), 10))
    assert app.taps == [(540, 1556), (540, 1556)] and app.backs == 1  # aksi diteruskan ke driver dalam


def test_timed_driver_uses_injected_monotonic_and_records_failures():
    inner, _, _ = _fake_driver()
    td = TimedDriver(inner, _SeqMono(10.0, 10.0425, 20.0, 20.5), lambda: 123)
    td.exists(Sel("text", "x"))
    inner.fail_next.append(DriverError("agent mati"))
    with pytest.raises(DriverError, match="agent mati"):
        td.info(Sel("text", "Beli Sekarang"))
    assert [(s.op, round(s.ms, 3), s.t_server_ms) for s in td.stats.samples] == [
        ("exists", 42.5, 123), ("info", 500.0, 123)]  # query gagal tetap tercatat


def test_query_stats_summary_slow_csv(tmp_path):
    stats = QueryStats([
        QuerySample("info", "text='Beli'", 10.0, 1), QuerySample("info", "text='Beli'", 30.0, 2),
        QuerySample("info", "text='Beli'", 20.0, 3), QuerySample("exists", 'text="a,b"', 100.0, 4),
        QuerySample("click", "node 'Beli'", 100.5, 1_790_000_000_123),
    ])
    assert stats.summary() == [
        "click: n=1 median=100.5 ms p95=100.5 ms max=100.5 ms",
        "exists: n=1 median=100.0 ms p95=100.0 ms max=100.0 ms",
        "info: n=3 median=20.0 ms p95=30.0 ms max=30.0 ms",
    ]
    assert stats.by_op()["info"] == [10.0, 30.0, 20.0]
    assert [s.op for s in stats.slow(100.0)] == ["click"]  # tepat 100 ms tidak dianggap lambat
    assert stats.slow(1_000.0) == []
    path = tmp_path / "q.csv"
    stats.write_csv(path)
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0] == ["t_server_ms", "op", "target", "ms"]
    assert rows[4] == ["4", "exists", "text='a,b'", "100.00"]  # koma tetap di satu kolom, kutip ganda diganti
    assert rows[5] == ["1790000000123", "click", "node 'Beli'", "100.50"]
    assert len(rows) == 6


def test_query_stats_p95_and_median_many_samples():
    stats = QueryStats([QuerySample("exists", "", float(ms), 0) for ms in range(1, 21)])
    assert stats.summary() == ["exists: n=20 median=10.5 ms p95=19.0 ms max=20.0 ms"]
    assert QueryStats().summary() == []


# ------------------------------------------------------------------ FakeDriver (dipakai tes lain)


def test_fake_driver_device_shell_and_failures():
    drv, app, clock = _fake_driver(latency_s=0.05)
    drv.wm_size = "Physical size: 1080x2460\nOverride size: 720x1640"
    assert drv.window_size() == (1080, 2460)  # ukuran fisik (baris pertama)
    assert drv.shell(["getprop", "ro.product.model"]) == "TECNO BG6\n"
    assert drv.shell(["getprop"]) == "[ro.product.model]: [TECNO BG6]"
    assert drv.shell(["wm", "size"]).startswith("Physical size: 1080x2460")
    t0 = clock.monotonic()
    drv.fail_next.append(DriverError("UiAutomation not connected"))
    with pytest.raises(DriverError):
        drv.exists(Sel("text", "Beli Sekarang"))
    assert clock.monotonic() - t0 == pytest.approx(0.05)  # query gagal tetap memakan waktu
    assert drv.exists(Sel("text", "Beli Sekarang")) is True  # hanya sekali
    assert drv.calls[-1] == ("exists", "text='Beli Sekarang'")
