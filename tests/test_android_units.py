"""Tes unit jalur Android tanpa menjalankan runner penuh.

- android_screen: pembacaan layar dari node accessibility (harga utama, baris checkout, keranjang,
  metode pembayaran, regex yang dikirim ke device, parse dump);
- android_selectors: validasi kandidat, templating {variant}, urutan prioritas, load/save
  selectors.json, penggabungan regex penanda & sampel teks Indonesia;
- android_driver: semantik selector (node_matches, regex Java), Node.from_u2, U2Driver di atas device u2
  palsu (jsonrpc langsung + d(**kw).selector), AgentDead, TimedDriver + QueryStats, FakeDriver;
- bagian kecil AndroidRunner yang murni membaca layar (penanda habis, kanal toast vs node, harga hot path).

Semua deterministik & cepat: tidak ada device, tidak ada sleep sungguhan.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from adbutils import AdbError
from uiautomator2._selector import Selector as U2Selector
from uiautomator2.exceptions import (
    HTTPError,
    HTTPTimeoutError,
    LaunchUiAutomationError,
    RPCUnknownError,
    UiAutomationNotConnectedError,
    UiObjectNotFoundError,
)

from flashbuy import android_runner, android_selectors, pricing
from flashbuy.android_driver import (
    RPC_TIMEOUT_S,
    SEL_KINDS,
    AgentDead,
    AppInfo,
    DriverError,
    FakeDriver,
    Node,
    QuerySample,
    QueryStats,
    Sel,
    TimedDriver,
    U2Driver,
    _disable_implicit_restart,
    _patch_u2_transport,
    check_shell,
    node_matches,
)
from flashbuy.android_runner import MARKER_MAX_CHARS, Screen
from flashbuy.android_screen import (
    _SHIPPING_ROW,
    _TOTAL_ROW,
    ANY_TEXT_MATCH,
    ORDER_COUNT_MATCH,
    PENDING_MATCH,
    PRICE_MATCH,
    QTY_MATCH,
    RP_ANY_MATCH,
    RP_SHORT_MATCH,
    SHIPPING_LABEL_MATCH,
    TOTAL_LABEL_MATCH,
    cart_rows,
    checkout_count,
    checkout_snapshot,
    is_price,
    is_qty,
    is_shopeepay,
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
from tests.android_harness import FAKE_CALIBRATED_STEPS, calibrated_selectors, make_android
from tests.conftest import FakeClock
from tests.fake_android import AppScenario, FakeShopeeApp

L = Limits(max_item_price=100_000, max_total=120_000, expected_name="Uji Coba")
# regex baris label yang dipakai android_screen (label harus di AWAL teks; grup terakhir = nilai inline)
TOTAL_RE = _TOTAL_ROW
SHIPPING_RE = _SHIPPING_ROW
TOTAL_RID = re.compile(FAKE_CALIBRATED_STEPS["checkout_total"][0]["resourceIdMatches"])  # resource-id "kalibrasi"
SHIPPING_RID = re.compile(FAKE_CALIBRATED_STEPS["checkout_shipping"][0]["resourceIdMatches"])
TARGET = "Ponsel Uji Coba 128GB"
OTHER = "Kabel Data USB-C"
BOX = "android.widget.CheckBox"
RADIO = "android.widget.RadioButton"


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


def _device_match(pattern: str, text: str) -> bool:
    """Seperti di device: textMatches dengan semantik Java (lihat android_driver._jmatch)."""
    return node_matches(_n(text), Sel("textMatches", pattern))


@pytest.mark.parametrize("text, short, any_rp", [
    ("Rp99.000", True, True),
    ("Rp 99.000", True, True),  # NBSP ditulis eksplisit (di Java \s hanya ASCII)
    ("Rp99rb", True, True),  # format rusak tetap terbaca -> diputuskan "tidak terbaca" oleh pricing
    ("Rp99.OOO", True, True),
    ("Hemat Rp51.000", True, True),
    ("Rp89.000 - Rp129.000", True, True),
    ("Gratis ongkir min. belanja Rp0 untuk semua produk toko ini", False, True),  # > 40 karakter: bukan harga
    ("Rp", False, False), ("Beli Sekarang", False, False), ("99.000", False, False),
])
def test_rp_short_and_any_regex(text, short, any_rp):
    assert _device_match(RP_SHORT_MATCH, text) is short
    assert _device_match(RP_ANY_MATCH, text) is any_rp
    assert (re.fullmatch(RP_SHORT_MATCH, text) is not None) is short  # Python & Java sepakat


@pytest.mark.parametrize("text, total, shipping", [
    ("Total Pembayaran", True, False),
    ("Total Pembayaran:", True, False),
    ("TOTAL PEMBAYARAN Rp109.000", True, False),
    (" Total Pembayaran Rp109.000", True, False),
    ("Subtotal Pengiriman", False, True),
    ("Total Ongkos Kirim", False, True),
    ("Ongkos Kirim", False, True),
    ("Biaya Pengiriman", False, True),
    ("Ongkir: Rp10.000", False, True),
    # badge/promo yang MENYEBUT label bukan baris label
    ("Gratis Ongkir", False, False),
    ("Voucher Gratis Ongkir", False, False),
    ("Hemat Ongkir s/d Rp20.000", False, False),
    ("Ongkirnya ditanggung penjual", False, False),
    ("Lihat Total Pembayaran", False, False),
    ("Subtotal untuk Produk", False, False),
])
def test_label_regexes_require_label_at_start(text, total, shipping):
    assert _device_match(TOTAL_LABEL_MATCH, text) is total
    assert (TOTAL_RE.fullmatch(text) is not None) is total
    assert _device_match(SHIPPING_LABEL_MATCH, text) is shipping
    assert (SHIPPING_RE.fullmatch(text) is not None) is shipping


def test_pending_regex_matches_unready_values():
    for s in ("Menghitung...", "menghitung ongkir", "Memuat", "Loading..."):
        assert _device_match(PENDING_MATCH, s), s
    assert not _device_match(PENDING_MATCH, "Rp10.000")


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


def test_label_values_ignore_badges_and_read_inline_values():
    nodes = [
        _n("Gratis Ongkir", (20, 100, 200, 130)), _n("Rp0", (220, 100, 300, 130)),  # badge + nominal di kanannya
        _n("Voucher Gratis Ongkir", (20, 150, 300, 180)), _n("-Rp20.000", (500, 150, 700, 180)),
        _n("Subtotal Pengiriman", (20, 200, 400, 230)), _n("Rp10.000", (500, 200, 700, 230)),
        _n("Ongkir: Rp10.000", (20, 300, 400, 330)),
    ]
    values = label_values(nodes, SHIPPING_RE)
    assert values == ["Rp10.000", "Rp10.000"]  # badge "Gratis Ongkir" tidak ikut (Rp0 tidak mengganggu)
    assert pricing.consistent_amount(values) == 10_000


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


def _checkout_app(clock, **scenario) -> FakeShopeeApp:
    app = FakeShopeeApp(AppScenario(open_at=0.0, **scenario), clock)
    app.screen = "checkout"
    app.checkout_items = [(TARGET, 99_000, 1)]
    return app


def test_checkout_snapshot_from_fake_app_layout(fake_clock):
    app = _checkout_app(fake_clock)
    snap = checkout_snapshot(app.nodes())
    assert len(snap.rows) == 1 and snap.rows[0].endswith("Rp99.000\nx1")
    assert TARGET in snap.rows[0]
    assert "Rp150.000" not in snap.rows[0]  # harga coret sebaris tidak ikut
    assert "Toko Uji Resmi" not in snap.rows[0]  # header toko bukan bagian baris produk
    verdict = pricing.check_checkout(snap, L)
    assert verdict.ok, verdict.reasons
    assert payment_value(app.nodes()) == "ShopeePay"
    assert payment_value(app.nodes(), re.compile("metode pembayaran")) == "ShopeePay"  # label kustom (tanpa flag)


def test_checkout_snapshot_testid_and_label_values_must_agree(fake_clock):
    app = _checkout_app(fake_clock)
    nodes = app.nodes()
    snap = checkout_snapshot(nodes, TOTAL_RID, SHIPPING_RID)
    # testID (bar bawah / ongkir) DAN pasangan label: semua dibaca
    assert snap.totals == ["Rp109.000", "Rp109.000", "Rp109.000"]
    assert snap.shippings == ["Rp10.000", "Rp10.000"]
    assert pricing.read_total(snap) == (10_000, 109_000)
    assert pricing.check_checkout(snap, L).ok

    # testID berbeda dengan label (mis. total belum diperbarui) -> tidak terbaca (fail-closed), bukan pilih salah satu
    tampered = [replace(n, text="Rp119.000") if n.rid == "fake_total_value" else n for n in nodes]
    snap = checkout_snapshot(tampered, TOTAL_RID, SHIPPING_RID)
    assert snap.totals == ["Rp119.000", "Rp109.000", "Rp119.000"]  # testID, label rincian, label bar bawah
    assert pricing.read_total(snap)[1] is None
    assert "total pembayaran tidak terbaca" in pricing.check_checkout(snap, L).reasons

    # tanpa regex testID: hanya pasangan label (nilai ber-rid tetap terbaca lewat labelnya)
    snap = checkout_snapshot(nodes)
    assert snap.totals == ["Rp109.000", "Rp109.000"] and snap.shippings == ["Rp10.000"]


def test_checkout_snapshot_testid_only_values():
    nodes = [_n(TARGET, (140, 300, 700, 340)), _n("Rp99.000", (140, 395, 300, 430)), _n("x1", (640, 398, 700, 428)),
             _n("Rp10.000", (500, 900, 700, 930), rid="com.shopee.id:id/fake_shipping_value"),
             _n("Rp109.000", (300, 1550, 510, 1595), rid="fake_total_value")]
    snap = checkout_snapshot(nodes, TOTAL_RID, SHIPPING_RID)
    assert (snap.totals, snap.shippings) == (["Rp109.000"], ["Rp10.000"])
    assert pricing.check_checkout(snap, L).ok
    assert checkout_snapshot(nodes).totals == []  # tanpa label & tanpa regex testID: tidak terbaca


def test_checkout_snapshot_shop_header_does_not_satisfy_expected_name():
    nodes = [
        _n("Toko Uji Coba Official", (20, 250, 400, 280)),  # header toko DI DALAM jendela kartu, kolom kiri
        _n("Ponsel Lain 64GB", (140, 300, 700, 340)),
        _n("Rp150.000", (140, 402, 300, 427)), _n("Rp99.000", (320, 395, 500, 430)), _n("x1", (640, 398, 700, 428)),
        _n("Subtotal Pengiriman", (20, 800, 400, 830)), _n("Rp10.000", (500, 800, 700, 830)),
        _n("Total Pembayaran", (20, 850, 400, 880)), _n("Rp109.000", (500, 850, 700, 880)),
    ]
    snap = checkout_snapshot(nodes)
    assert snap.rows == ["Ponsel Lain 64GB\nRp99.000\nx1"]
    verdict = pricing.check_checkout(snap, L)
    assert not verdict.ok and verdict.values["name_ok"] is False
    assert "nama produk tidak memuat 'Uji Coba'" in verdict.reasons


def test_checkout_snapshot_keeps_name_when_price_column_is_indented(fake_clock):
    app = _checkout_app(fake_clock, strike_in_checkout=False)
    app.selected_variant = "Hitam"
    snap = checkout_snapshot(app.nodes(), TOTAL_RID, SHIPPING_RID)
    assert TARGET in snap.rows[0] and "Variasi: Hitam" in snap.rows[0], snap.rows
    verdict = pricing.check_checkout(snap, Limits(100_000, 120_000, "Uji Coba", "Hitam"))
    assert verdict.ok, verdict.reasons


@pytest.mark.parametrize("variant, ok", [
    ("Hitam", True), ("hitam", True), ("Variasi Hitam", True), ("Putih", False), ("Hitam, 256GB", False),
])
def test_checkout_snapshot_expected_variant_must_be_in_product_row(variant, ok):
    snap = checkout_snapshot(_checkout())  # baris: "...\nVariasi: Hitam\n..."
    verdict = pricing.check_checkout(snap, Limits(100_000, 120_000, "Uji Coba", variant))
    assert verdict.ok is ok, verdict.reasons
    if not ok:
        assert f"variasi {variant!r} tidak terlihat di baris produk" in verdict.reasons


def test_checkout_variant_check_not_satisfied_by_product_name():
    snap = checkout_snapshot(_checkout((("Kaos Polos Hitam Putih", "Rp99.000", "x1"),), variation="Variasi: Putih"))
    assert snap.rows == ["Kaos Polos Hitam Putih\nVariasi: Putih\nRp99.000\nx1"]
    verdict = pricing.check_checkout(snap, Limits(100_000, 120_000, "Kaos Polos", "Hitam"))
    assert not verdict.ok, verdict.values  # variasi terpilih Putih, bukan Hitam


def test_checkout_snapshot_without_rows_needs_exactly_one_order_group():
    nodes = [n for n in _checkout() if n.label != "x1"]
    nodes.append(_n("Total Pesanan (1 Produk):", (20, 1200, 400, 1230)))  # grup pesanan kedua (toko lain)
    snap = checkout_snapshot(nodes)
    assert snap.rows == [] and pricing.order_counts(snap.page_text) == [1, 1]
    verdict = pricing.check_checkout(snap, L)
    assert not verdict.ok and "2 grup pesanan (harus tepat 1)" in verdict.reasons


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


def test_cart_rows_row_text_does_not_include_next_item(fake_clock):
    # nama item berada DI ATAS checkbox-nya: teks ditempelkan ke checkbox terdekat, tidak bocor ke baris lain
    rows, _ = _cart(_cart_app(fake_clock, [(OTHER, 20_000, True), (TARGET, 99_000, True)]))
    assert TARGET not in rows[0][0].text, rows[0][0].text
    verdict = pricing.check_cart([r for r, _ in rows], "Uji Coba")
    assert verdict.to_uncheck == [0], verdict.reason


def test_cart_rows_three_items_each_row_has_only_its_own_name(fake_clock):
    names = [OTHER, TARGET, "Casing HP Bening"]
    rows, _ = _cart(_cart_app(fake_clock, [(OTHER, 20_000, True), (TARGET, 99_000, True),
                                           ("Casing HP Bening", 15_000, False)]))
    assert len(rows) == 3
    for (row, _box), own in zip(rows, names, strict=True):
        assert [n for n in names if n in row.text] == [own], row.text
    assert [r.checked for r, _ in rows] == [True, True, False]
    assert pricing.check_cart([r for r, _ in rows], "Uji Coba", strict=True).to_uncheck == [0]


def test_cart_single_checked_non_target_blocked_when_strict(fake_clock):
    rows, _ = _cart(_cart_app(fake_clock, [(TARGET, 99_000, False), (OTHER, 20_000, True)]))
    cart = [r for r, _ in rows]
    strict = pricing.check_cart(cart, "Uji Coba", strict=True)  # nama acuan dari expected_name
    assert not strict.ok and strict.to_uncheck == [] and "bukan target" in strict.reason
    assert pricing.check_cart(cart, "Uji Coba", strict=False).ok  # tanpa expected_name: hanya hitungan


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


def test_cart_rows_item_named_semua_is_not_select_all():
    nodes = [_n("Kabel Charger untuk Semua HP", (100, 240, 700, 280)), _n("Rp20.000", (100, 290, 300, 320))]
    boxes = [_n("", (20, 250, 70, 300), cls=BOX, checked=True)]  # sebaris dengan nama item
    rows = cart_rows(boxes, nodes)
    assert len(rows) == 1 and rows[0][0].checked


@pytest.mark.parametrize("label, skipped", [
    ("Semua", True), ("Pilih Semua", True), ("pilih semua (3)", True), ("Semua (2)", True),
    ("Semua Ukuran", False), ("Pilih Semua Varian", False), ("Hapus Semua", False),
])
def test_cart_rows_select_all_only_exact_label_on_same_row(label, skipped):
    nodes = [_n(label, (80, 230, 300, 280)), _n("Rp20.000", (320, 230, 500, 280))]
    boxes = [_n("", (20, 230, 70, 280), cls=BOX, checked=True)]
    assert (cart_rows(boxes, nodes) == []) is skipped


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
    assert payment_value([label, right]) == "ShopeePay"
    assert payment_value([label, below]) == "COD - Cek Dulu"
    assert payment_value([label, below, right]) == "ShopeePay"  # kanan didahulukan
    assert payment_value([_n("Metode Pembayaran ShopeePay", (20, 500, 700, 540))]) == "ShopeePay"
    assert payment_value([_n("Metode Pembayaran: SPayLater", (20, 500, 700, 540))]) == "SPayLater"
    assert payment_value([_n(" metode pembayaran : ShopeePay", (20, 500, 700, 540))]) == "ShopeePay"


def test_payment_value_missing():
    assert payment_value([]) is None
    assert payment_value([_n("ShopeePay", (420, 500, 700, 540))]) is None
    label = _n("Metode Pembayaran", (20, 500, 300, 540))
    assert payment_value([label, _n("   ", (420, 500, 700, 540))]) is None
    assert payment_value([label]) is None


def test_payment_value_label_row_must_start_with_label():
    promo = _n("Diskon s/d Rp10.000 dengan Metode Pembayaran ShopeePay", (20, 100, 700, 140))
    label = _n("Metode Pembayaran", (20, 500, 300, 540))
    cod = _n("COD - Cek Dulu", (420, 500, 700, 540))
    assert payment_value([promo, label, cod]) == "COD - Cek Dulu"  # teks promo yang menyebut label diabaikan
    assert payment_value([promo]) is None


def _method_list(checked: set[str], *, cls: str = RADIO, flag: str = "checked") -> list[Node]:
    out = []
    for i, m in enumerate(["ShopeePay", "SPayLater", "COD - Cek Dulu"]):
        top = 150 + i * 100
        out.append(_n(m, (100, top, 500, top + 40)))
        out.append(_n("", (640, top, 690, top + 40), cls=cls, **{flag: m in checked}))
    return out


def test_payment_value_inline_radio_exactly_one_checked():
    assert payment_value(_method_list({"ShopeePay"})) == "ShopeePay"
    assert payment_value(_method_list({"SPayLater"})) == "SPayLater"
    assert payment_value(_method_list({"COD - Cek Dulu"}, cls=BOX, flag="selected")) == "COD - Cek Dulu"
    assert payment_value(_method_list(set())) is None  # tidak ada yang tercentang: tidak pasti
    assert payment_value(_method_list({"ShopeePay", "SPayLater"})) is None  # dua tercentang: tidak pasti
    # status radio menang atas teks baris "Metode Pembayaran"
    row = [_n("Metode Pembayaran", (20, 40, 300, 80)), _n("ShopeePay", (420, 40, 700, 80))]
    assert payment_value([*row, *_method_list({"COD - Cek Dulu"})]) == "COD - Cek Dulu"
    lonely = [_n("", (640, 150, 690, 190), cls=RADIO, checked=True)]  # radio tercentang tanpa label di kirinya
    assert payment_value(lonely) is None


def test_payment_value_fake_app_payment_list(fake_clock):
    app = _checkout_app(fake_clock)
    app.screen = "payment_list"
    app.list_choice = "SPayLater"
    assert payment_value(app.nodes()) == "SPayLater"
    app.list_choice = "ShopeePay"
    assert payment_value(app.nodes()) == "ShopeePay"  # "Saldo Rp500.000" di bawahnya tidak mengganggu


@pytest.mark.parametrize("value, ok", [
    ("ShopeePay", True), ("Saldo ShopeePay", True), ("ShopeePay (Rp150.000)", True),
    ("ShopeePay (Saldo Rp150.000)", True), ("shopeepay - Rp150.000", True), (" ShopeePay ", True),
    ("SPayLater", False), ("ShopeePay Later", False), ("ShopeePay (Saldo tidak cukup)", False),
    ("ShopeePay + SPayLater", False), ("Saldo ShopeePay tidak cukup", False), ("ShopeePayLater", False),
    ("", False), (None, False),
])
def test_is_shopeepay_accepts_only_plain_shopeepay(value, ok):
    assert is_shopeepay(value) is ok


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


def test_to_sel_variant_escaped_for_description_matches():
    sel = to_sel({"descriptionMatches": "(?i).*{variant}.*"}, "Hitam (128GB)")
    assert sel.value == "(?i).*" + re.escape("Hitam (128GB)") + ".*"
    assert node_matches(_n("", desc="Varian Hitam (128GB)"), sel)


@pytest.mark.parametrize("kind", [k for k in SEL_KINDS if k.endswith("Matches")])
def test_to_sel_variant_escaped_for_every_matches_kind(kind):
    variant = "1+1 (Promo) v2.0"
    sel = to_sel({kind: ".*{variant}"}, variant)
    assert sel.value == ".*" + re.escape(variant)
    attr = next(a for prefix, a in (("resourceId", "rid"), ("description", "desc"), ("text", "text"))
                if kind.startswith(prefix))
    assert node_matches(Node(**{attr: "Paket 1+1 (Promo) v2.0"}), sel)
    assert not node_matches(Node(**{attr: "Paket 11 Promo v2x0"}), sel)  # '+', '()', '.' literal


@pytest.mark.parametrize("kind", [k for k in SEL_KINDS if not k.endswith("Matches")])
def test_to_sel_variant_raw_for_literal_kinds(kind):
    assert to_sel({kind: "{variant}"}, "Hitam (128GB)") == Sel(kind, "Hitam (128GB)")  # bukan regex: apa adanya


def test_candidates_dedupe_and_variant_step():
    sel = android_selectors.defaults()
    assert sel.candidates("variant_option") == []  # tanpa variasi
    # persis (textContains bisa kena judul produk yang memuat teks variasi)
    assert sel.candidates("variant_option", "Hitam") == [Sel("text", "Hitam"), Sel("description", "Hitam")]
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


@pytest.mark.parametrize("step", list(ANDROID_DEFAULT_STEPS))
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
    assert hits("sheet_confirm", "Beli Sekarang") and hits("sheet_confirm", "Konfirmasi")
    assert hits("buy_button", "Beli Sekarang")
    for step in ("buy_button", "sheet_confirm"):  # substring "Beli" tidak aman (mis. "Beli Lagi", "Beli 2 Hemat")
        assert not hits(step, "Beli") and not hits(step, "Beli Sekarang (Rp99.000)") and not hits(step, "Beli Lagi")


def test_default_step_lists_of_redesign():
    s = ANDROID_DEFAULT_STEPS
    assert s["buy_button"] == [{"text": "Beli Sekarang"}, {"description": "Beli Sekarang"}]
    assert s["variant_option"] == [{"text": "{variant}"}, {"description": "{variant}"}]
    assert s["sheet_confirm"] == [{"text": "Beli Sekarang"}, {"text": "Konfirmasi"}]
    assert s["cart_checkout"][0] == {"textMatches": "Checkout(\\s*\\(\\d+\\))?"}
    assert s["product_price"] == []  # kosong = teks Rp pendek pertama (RP_SHORT_MATCH)
    for step in ("checkout_total", "checkout_shipping", "pin_screen"):
        assert s[step] == [], step  # kosong = pasangan label / penanda teks; resource-id hanya dari kalibrasi
    # tombol yang men-tap (aksi) tidak pernah memakai kandidat substring
    for step in ("buy_button", "sheet_confirm", "place_order", "variant_option"):
        assert all(not k.endswith("Contains") for c in s[step] for k in c), step


def test_defaults_contain_no_resource_ids():
    """Aturan tahap 4: selector resource-id hanya dari kalibrasi di device nyata; default alat = teks terlihat."""
    for step, cands in ANDROID_DEFAULT_STEPS.items():
        assert not [c for c in cands if any(k.startswith("resourceId") for k in c)], step


@pytest.mark.parametrize("step, rid, ok", [
    ("checkout_total", "fake_total_value", True),  # resource-id mentah tanpa awalan package
    ("checkout_total", "com.shopee.id:id/fake_total_value", True),
    ("checkout_total", "fake_total_value_discount", False),
    ("checkout_shipping", "fake_shipping_value", True),
    ("checkout_shipping", "fake_shipping_price", False),
    ("cart_checkout", "fake_cart_checkout", True),
    ("cart_checkout", "com.shopee.id:id/fake_cart_checkout", True),
    ("pin_screen", "com.shopee.id:id/fake_pin_field", True),
    ("pin_screen", "com.shopee.id:id/fake_pin_keypad", True),
    ("pin_screen", "com.shopee.id:id/fake_pin_hint", False),
])
def test_calibrated_resource_id_steps_match_intended_ids(step, rid, ok):
    """resource-id hasil kalibrasi (FAKE_CALIBRATED_STEPS) cocok ke seluruh id, dengan/tanpa awalan package."""
    cands = [c for c in calibrated_selectors().candidates(step) if c.by.startswith("resourceId")]
    assert cands, step
    assert any(node_matches(_n(rid=rid), c) for c in cands) is ok


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
        {"resourceId": "com.shopee.id:id/btn_buy"}, {"text": "Beli Sekarang"},
        {"description": "Beli"}, {"description": "Beli Sekarang"}]  # kalibrasi di depan default sejenis
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


@pytest.mark.parametrize("section, match", [
    ({"steps": {"buy_button": [{"xpath": "//x"}]}}, "tepat satu jenis"),
    ({"steps": {"buy_button": [{"text": "a", "description": "b"}]}}, "tepat satu jenis"),
    ({"markers": {"captcha": ["(?is).*(geser"]}}, r"android\.markers\.captcha: regex tidak valid"),
    ({"markers": {"captcha": ["(?a).*geser.*"]}}, "regex tidak valid"),  # flag Python-only: gagal saat digabung
])
def test_load_rejects_invalid_android_section(tmp_path, section, match):
    path = _write_json(tmp_path / "selectors.json", {"android": section})
    with pytest.raises(ValueError, match=match):  # pesan menyebut letak kesalahan, bukan re.error mentah
        android_selectors.load(path)


@pytest.mark.parametrize("cand", [
    {"textMatches": "Checkout(("},
    {"descriptionMatches": "[Checkout"},
    {"resourceIdMatches": "(.*:id/fake_cart_checkout"},
    {"textMatches": "(?i).*{variant}(.*"},  # divalidasi setelah {variant} diisi
    {"textMatches": "Checkout(?i)"},  # flag global di tengah ditolak
])
def test_load_rejects_invalid_selector_regex(tmp_path, cand):
    path = _write_json(tmp_path / "selectors.json", {"android": {"steps": {"cart_checkout": [cand]}}})
    with pytest.raises(ValueError, match=r"android\.steps\.cart_checkout: regex tidak valid"):
        android_selectors.load(path)


def test_load_accepts_valid_selector_regex_and_variant_template(tmp_path):
    path = _write_json(tmp_path / "selectors.json", {"android": {"steps": {
        "variant_option": [{"textMatches": "(?i)(?s){variant}"}],
        "cart_checkout": [{"resourceIdMatches": ".*:id/btn_checkout"}]}}})
    sel = android_selectors.load(path)
    assert sel.candidates("variant_option", "1+1") == [
        Sel("text", "1+1"), Sel("textMatches", "(?i)(?s)1\\+1"), Sel("description", "1+1")]  # urut jenis
    assert sel.candidates("cart_checkout")[:2] == [Sel("resourceIdMatches", ".*:id/btn_checkout"),
                                                   Sel("textMatches", "Checkout(\\s*\\(\\d+\\))?")]


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
    ("(?i)(?s).*x.*", "(?is:.*x.*)"),  # flag bertumpuk digabung
    ("(?s)(?i)(?s)x", "(?is:x)"),
    ("(?si)(?i)x", "(?is:x)"),
    ("(?i)(?:a)", "(?i:(?:a))"),
])
def test_scoped(pattern, expected):
    assert scoped(pattern) == expected
    if pattern != "(?:a)(?i)":  # pola valid -> hasilnya valid dan bisa digabung dengan "|"
        re.compile(pattern)
        re.compile(f"{expected}|(?:z)")


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


def test_stacked_leading_flags_marker_usable(tmp_path):
    path = _write_json(tmp_path / "selectors.json", {"android": {"markers": {
        "antre": ["(?i)(?s).*antrean.*"], "captcha": ["(?s)(?i).*geser.*kepingan.*"]}}})
    sel = android_selectors.load(path)
    assert sel.marker_re("antre").fullmatch("ANTREAN\npenuh")
    assert sel.marker_re("captcha").fullmatch("Geser\nKepingan puzzle")
    assert re.compile(union([p for pats in sel.markers.values() for p in pats]))  # satu query gabungan
    r, *_rest, log, _notifier = make_android(tmp_path / "run", selectors=sel)  # runner bisa dibuat
    assert r._markers["captcha"].fullmatch("GESER\nkepingan")
    log.close()


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
    assert seen.screen == Screen.PRODUCT_ACTIVE
    toast = _n("Stok habis", (160, 1300, 560, 1350))
    assert runner._classify_nodes([long_node, toast, buy], "after_buy").screen == Screen.SOLD_OUT


def test_runner_marker_length_boundary(runner):
    base = "Stok habis "
    at_limit = base + "x" * (MARKER_MAX_CHARS - len(base))
    assert len(at_limit) == MARKER_MAX_CHARS
    assert runner._marker("sold_out", [_n(at_limit)]) is not None
    assert runner._marker("sold_out", [_n(at_limit + "x")]) is None


def test_sold_out_bare_habis_and_strong_phrases(runner):
    runner._screen_h = 1612  # pusat >= 85% tinggi (1370,2 px) = bar tombol bawah
    buy = _n("Beli Sekarang", (360, 1500, 720, 1612), clickable=True)

    def seen(text, bounds=(500, 860, 700, 900), *, clickable=False, with_buy=True, context="product"):
        s = runner._message_screen(_n(text, bounds, clickable=clickable), buy if with_buy else None, context)
        return None if s is None else s.screen

    # "Habis" polos: lencana chip variasi lain (bukan tombol, di tengah layar) bukan produk habis
    assert seen("Habis") is None
    assert seen("Habis", with_buy=False, context="after_buy") is None
    assert seen("Habis", clickable=True) == Screen.SOLD_OUT  # tombol
    assert seen("Habis", (360, 1500, 720, 1612)) == Screen.SOLD_OUT  # di bar bawah
    assert seen("Habis", (360, 1350, 720, 1392)) == Screen.SOLD_OUT  # pusat 1371 >= 1370,2
    assert seen("Habis", (360, 1300, 720, 1340)) is None  # pusat 1320: masih area konten
    assert seen("Stok Habis", (500, 860, 700, 900), with_buy=False) == Screen.SOLD_OUT  # bukan "Habis" polos
    # frasa kuat: dihitung, kecuali di halaman produk yang tombol Beli-nya masih ada (bisa milik variasi lain)
    assert seen("Stok habis") is None
    assert seen("Stok habis", with_buy=False) == Screen.SOLD_OUT
    assert seen("Stok habis", context="after_buy") == Screen.SOLD_OUT
    assert seen("Produk ini terjual habis", context="any") == Screen.SOLD_OUT
    assert seen("Flash Sale telah berakhir") == Screen.SOLD_OUT  # "berakhir" berlaku walau tombol Beli ada
    assert seen("Flash Sale berakhir dalam 01:59:59", with_buy=False, context="after_buy") is None  # hitung mundur
    runner._screen_h = 0  # tinggi layar tak terbaca: hanya "Habis" yang bisa diklik dihitung
    assert seen("Habis", (360, 1500, 720, 1612)) is None
    assert seen("Habis", (360, 1500, 720, 1612), clickable=True) == Screen.SOLD_OUT


def _prepared(tmp_path, **kw):
    """Runner dengan driver terpasang (prepare), jam sudah lewat T (slot buka), aplikasi di halaman produk."""
    runner, app, driver, sclock, open_at, log, _notifier = make_android(tmp_path, **kw)
    asyncio.run(runner.prepare())
    sclock.clock.advance(open_at - sclock.now() + 1.0)
    app.screen = "product"
    return runner, app, driver, log


TOAST_MESSAGES = [("Stok habis", Screen.SOLD_OUT),
                  ("Silakan pilih variasi terlebih dahulu", Screen.VARIANT_REQUIRED),
                  ("Flash sale belum dimulai", Screen.NOT_STARTED)]


@pytest.mark.parametrize("message, screen", TOAST_MESSAGES)
def test_classify_after_buy_reads_android_toast_channel(tmp_path, message, screen):
    runner, app, driver, log = _prepared(tmp_path)  # toast_mode default "toast"
    assert runner._classify("after_buy").screen == Screen.PRODUCT_ACTIVE
    app._show_toast(message)
    assert not driver.exists(Sel("text", message))  # Toast Android: tidak ada di pohon node
    seen = runner._classify("after_buy")
    assert (seen.screen, seen.evidence) == (screen, f"toast {message!r}")
    n_calls = len(driver.calls)
    assert runner._classify("product").screen == Screen.PRODUCT_ACTIVE  # polling tidak membaca toast (bisa basi)
    assert ("last_toast", "") not in driver.calls[n_calls:]
    runner.d.clear_toast()  # dipanggil runner tepat setelah klik: toast lama tidak dianggap reaksi
    assert app.last_toast is None
    assert runner._classify("after_buy").screen == Screen.PRODUCT_ACTIVE
    assert app.events == []  # last_toast/clear_toast hanya operasi agent, tidak menyentuh aplikasi
    log.close()


@pytest.mark.parametrize("message, screen", TOAST_MESSAGES)
def test_classify_after_buy_reads_in_app_message_nodes(tmp_path, message, screen):
    runner, app, driver, log = _prepared(tmp_path, toast_mode="node")  # overlay in-app (ada di pohon node)
    app._show_toast(message)
    assert driver.exists(Sel("text", message)) and app.last_toast is None
    seen = runner._classify("after_buy")
    assert (seen.screen, seen.evidence) == (screen, f"teks {message!r}")
    log.close()


@pytest.mark.parametrize("mode", ["toast", "node"])
def test_classify_message_has_priority_over_open_sheet(tmp_path, mode):
    runner, app, _driver, log = _prepared(tmp_path, toast_mode=mode, variants=["Hitam", "Putih"])
    app.screen = "sheet"
    assert runner._classify("after_buy").screen == Screen.VARIANT_SHEET
    app._show_toast("Silakan pilih variasi terlebih dahulu")
    assert runner._classify("after_buy").screen == Screen.VARIANT_REQUIRED  # bukan SHEET
    log.close()


def test_read_price_is_first_short_rp_text_and_keeps_broken_format(tmp_path):
    runner, _app, _drv, _sclock, _open_at, log, _notifier = make_android(tmp_path)
    nodes = [_n("Deskripsi: harga normal Rp150.000, sekarang jauh lebih murah!", (20, 100, 700, 200)),  # > 40
             _n("Rp99rb", (20, 610, 400, 690)), _n("Rp150.000", (420, 640, 600, 670))]
    runner._raw_driver = FakeDriver(_StaticApp(nodes), FakeClock(tick=0.0))
    asyncio.run(runner.prepare())
    runner.d.stats.samples.clear()
    text = runner._read_price()
    assert text == "Rp99rb"  # TIDAK diganti nominal lain (harga coret Rp150.000) -> tidak terbaca
    assert pricing.check_product_price(text, runner.limits).describe(runner.limits) == "harga tidak terbaca ('Rp99rb')"
    assert [s.op for s in runner.d.stats.samples] == ["info"]  # hot path: satu info, bukan find_all
    runner._raw_driver.app._nodes[1] = _n("Rp99.000", (20, 610, 400, 690))
    assert pricing.check_product_price(runner._read_price(), runner.limits).verdict == "ok"
    log.close()


def test_read_price_uses_calibrated_selector_only(tmp_path):
    sel = android_selectors.defaults()
    sel.steps["product_price"] = [{"resourceId": "com.shopee.id:id/tv_price"}]
    runner, _app, _drv, _sclock, _open_at, log, _notifier = make_android(tmp_path, selectors=sel)
    nodes = [_n("Rp1.000", (20, 100, 200, 140)), _n("Rp99.000", (20, 610, 400, 690), rid="com.shopee.id:id/tv_price")]
    app = _StaticApp(nodes)
    runner._raw_driver = FakeDriver(app, FakeClock(tick=0.0))
    asyncio.run(runner.prepare())
    assert runner._read_price() == "Rp99.000"
    app._nodes.pop()  # selector kalibrasi tidak ketemu -> tidak terbaca (tidak menebak teks Rp lain)
    assert runner._read_price() is None
    log.close()


def test_prepare_builds_u2driver_scoped_to_package(tmp_path, monkeypatch):
    runner, _app, driver, _sclock, _open_at, log, _notifier = make_android(tmp_path)
    made = []

    def factory(serial, package=None):
        made.append((serial, package))
        driver.no_implicit_restart = True
        return driver

    monkeypatch.setattr(android_runner, "U2Driver", factory)
    runner._raw_driver = None
    asyncio.run(runner.prepare())
    assert made == [("FAKE123", "com.shopee.id")]
    assert runner.d.inner is driver and runner._screen_h == 1612
    assert "restart implisit u2 mati" in (tmp_path / "logs" / "android.log").read_text(encoding="utf-8")
    log.close()


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
    (Sel("resourceIdMatches", "(.*:id/)?fake_total_value"), _n(rid="fake_total_value"), True),
    (Sel("resourceIdMatches", "(.*:id/)?fake_total_value"), _n(rid="com.shopee.id:id/fake_total_value"), True),
    (Sel("resourceIdMatches", "fake_total"), _n(rid="fake_total_value"), False),  # seluruh resource-id
    (Sel("resourceIdMatches", ".*"), _n("fake_total_value"), True),  # rid kosong pun cocok ".*"
    (Sel("resourceIdMatches", "fake_total_value"), _n("fake_total_value"), False),  # bukan teks
    # regex Java: \s, \w, \b hanya ASCII -> NBSP bukan spasi (karena itu android_screen menulis SP eksplisit)
    (Sel("textMatches", r"Rp\s99\.000"), _n("Rp 99.000"), True),
    (Sel("textMatches", r"Rp\s99\.000"), _n("Rp\u00a099.000"), False),
    (Sel("textMatches", PRICE_MATCH), _n("Rp\u00a099.000"), True),
    (Sel("textMatches", r"\w+"), _n("Kopi"), True),
    (Sel("textMatches", r"\w+"), _n("Caf\u00e9"), False),
    (Sel("descriptionMatches", r"(?s)Beli\s+Sekarang"), _n("", desc="Beli\nSekarang"), True),
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


class _ShellResponse:
    def __init__(self, output: str):
        self.output = output
        self.exit_code = 0


class _FakeU2Object:
    """Seperti u2 `UiObject`: hanya membawa `selector` (Selector u2 asli); U2Driver memanggil jsonrpc sendiri."""

    def __init__(self, selector: U2Selector):
        self.selector = selector


class _JsonRpc:
    """Seperti u2 `d.jsonrpc`: `d.jsonrpc.<method>(*params, http_timeout=..)` -> `d.jsonrpc_call(method, params, t)`."""

    def __init__(self, dev: FakeU2Device):
        self.dev = dev

    def __getattr__(self, method: str):
        def call(*args, **kwargs):
            http_timeout = kwargs.pop("http_timeout", 300)  # bawaan u2 (HTTP_TIMEOUT) = 300 s
            return self.dev.jsonrpc_call(method, args if args else kwargs, http_timeout)

        return call


def _loose(node: Node, sel: Sel) -> bool:
    """Jalur B server u2: contains/startsWith tidak peka huruf besar/kecil."""
    lower = replace(node, text=node.text.lower(), desc=node.desc.lower())
    return sel.by.endswith(("Contains", "StartsWith")) and node_matches(lower, Sel(sel.by, sel.value.lower()))


class FakeU2Device:
    """Pengganti uiautomator2.Device: server jsonrpc palsu (exist/objInfo/objInfoOfAllInstances/click/deviceInfo/
    getLastToast/clearLastToast) + API adb yang dipakai U2Driver; semua panggilan dicatat."""

    serial = "0123456789ABCDEF"

    def __init__(self, elements=(), *, size=(720, 1612), shell_output="", app=None):
        self.elements = list(elements)
        self.size = size
        self.shell_output = shell_output
        self.app = app if app is not None else {"package": "com.shopee.id",
                                                "activity": "com.shopee.app.ui.home.HomeActivity_"}
        self.errors: dict[str, Exception] = {}  # dilempar setiap kali metode/API itu dipanggil
        self.fail_once: dict[str, list[Exception]] = {}  # dilempar sekali per entri, lalu normal
        self.loose = False  # server mengembalikan elemen yang cocok tanpa peduli huruf (contains/startsWith)
        self.null_entries = False  # objInfoOfAllInstances menyisipkan null (elemen hilang di tengah iterasi)
        self.all_not_found = False  # objInfoOfAllInstances melempar UiObjectNotFoundError
        self.alive: bool | Exception = True
        self.device_info: dict | Exception = {"sdkInt": 33, "productName": "BG6"}
        self.toast: str | None = None
        self.rpcs: list[tuple[str, tuple, float]] = []
        self.actions: list[tuple] = []
        self.shells: list[tuple[list[str], int]] = []
        self.window_size_calls = 0
        self.write_screenshot = True

    def maybe_raise(self, op: str) -> None:
        if self.fail_once.get(op):
            raise self.fail_once[op].pop(0)
        if op in self.errors:
            raise self.errors[op]

    # ---- jsonrpc

    @property
    def jsonrpc(self) -> _JsonRpc:
        return _JsonRpc(self)

    def __call__(self, **kw) -> _FakeU2Object:
        return _FakeU2Object(U2Selector(**kw))

    def jsonrpc_call(self, method: str, params=None, timeout: float = 10):
        self.rpcs.append((method, params, timeout))
        self.maybe_raise(method)
        return getattr(self, f"_rpc_{method}")(*params)

    def _match(self, selector: U2Selector) -> list[dict]:
        ((by, value),) = [(k, selector[k]) for k in selector if k in SEL_KINDS]
        sel = Sel(by, value)
        pkg = selector.get("packageName")
        out = []
        for info in self.elements:
            if pkg is not None and info.get("packageName") != pkg:
                continue
            node = Node.from_u2(info)
            if node_matches(node, sel) or (self.loose and _loose(node, sel)):
                out.append(info)
        return out

    def _rpc_exist(self, selector) -> bool:
        return bool(self._match(selector))

    def _rpc_objInfo(self, selector) -> dict:
        found = self._match(selector)
        if not found:
            raise UiObjectNotFoundError(-32002, "androidx.test.uiautomator.UiObjectNotFoundException", selector)
        return found[0]

    def _rpc_objInfoOfAllInstances(self, selector) -> list:
        if self.all_not_found:
            raise UiObjectNotFoundError(-32002, "androidx.test.uiautomator.UiObjectNotFoundException", selector)
        found = self._match(selector)
        return [x for info in found for x in (None, info)] if self.null_entries else found

    def _rpc_click(self, x, y) -> bool:
        self.actions.append(("click", x, y))
        return True

    def _rpc_deviceInfo(self) -> dict:
        if isinstance(self.device_info, Exception):
            raise self.device_info
        return self.device_info

    def _rpc_getLastToast(self):
        return self.toast

    def _rpc_clearLastToast(self) -> bool:
        self.toast = None
        return True

    # ---- adb / API lain

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

    def _rpc_swipe(self, fx, fy, tx, ty, steps) -> bool:
        self.actions.append(("swipe", fx, fy, tx, ty, steps))
        return True

    def _rpc_pressKey(self, key) -> bool:
        self.actions.append(("press", key))
        return True

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
STALE = "Unknown RPC error: -32001 androidx.test.uiautomator.StaleObjectException"


def _u2(*, rpc_timeout_s: float = RPC_TIMEOUT_S, package: str | None = None, elements=None,
        **kw) -> tuple[U2Driver, FakeU2Device]:
    dev = FakeU2Device([BUY_INFO, DESC_INFO] if elements is None else elements, **kw)
    return U2Driver(device=dev, rpc_timeout_s=rpc_timeout_s, package=package), dev


def test_u2_init_serial():
    dev = FakeU2Device()
    assert U2Driver(device=dev).serial == "0123456789ABCDEF"
    assert U2Driver("SERIALKU", device=dev).serial == "SERIALKU"
    assert U2Driver(device=dev).no_implicit_restart is False  # device disuntikkan: tidak diutak-atik

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
    drv = U2Driver("")
    assert drv.d is dev
    assert drv.no_implicit_restart is False  # device tanpa internal u2 (_dev/_device_server_port): tidak bisa


def test_every_selector_kind_is_a_valid_u2_selector_field():
    for kind in SEL_KINDS:
        sel = U2Selector(**Sel(kind, "x").kwargs(), packageName="com.shopee.id")  # ReferenceError bila tidak dikenal
        assert sel[kind] == "x" and sel["mask"] != 0


def test_u2_exists_info_find_all_via_jsonrpc():
    drv, dev = _u2(rpc_timeout_s=2.5)
    assert drv.exists(Sel("text", "Beli Sekarang")) is True
    assert drv.exists(Sel("text", "Tidak Ada")) is False
    method, (selector,), timeout = dev.rpcs[0]
    assert (method, timeout) == ("exist", 2.5)
    assert isinstance(selector, U2Selector) and selector["text"] == "Beli Sekarang"
    assert "packageName" not in selector  # tanpa package: tidak dibatasi
    node = drv.info(Sel("resourceId", "com.shopee.id:id/buy"))
    assert node.label == "Beli Sekarang" and node.bounds == (360, 1500, 720, 1612) and node.clickable
    assert drv.info(Sel("text", "Tidak Ada")) is None  # UiObjectNotFoundError -> None
    assert [n.label for n in drv.find_all(Sel("className", "android.widget.Button"))] == ["Beli Sekarang"]
    assert drv.find_all(Sel("text", "Tidak Ada")) == []
    assert drv.find_all(Sel("descriptionMatches", ANY_TEXT_MATCH)) == [Node.from_u2(DESC_INFO)]
    assert {m for m, _, _ in dev.rpcs} == {"exist", "objInfo", "objInfoOfAllInstances"}
    assert all(t == 2.5 for *_, t in dev.rpcs)  # timeout per RPC, bukan 300 s bawaan u2
    assert U2Driver(device=dev).rpc_timeout_s == RPC_TIMEOUT_S


def test_u2_package_injected_into_every_selector():
    notif = {**BUY_INFO, "packageName": "com.android.systemui",
             "bounds": {"left": 0, "top": 0, "right": 720, "bottom": 80}}  # notifikasi "Beli Sekarang" di atas
    drv_all, _ = _u2(elements=[notif, BUY_INFO])
    assert drv_all.info(Sel("text", "Beli Sekarang")).bounds == (0, 0, 720, 80)  # tanpa package: kena notifikasi
    drv, dev = _u2(elements=[notif, BUY_INFO], package="com.shopee.id")
    buy = Sel("text", "Beli Sekarang")
    assert drv.info(buy).bounds == (360, 1500, 720, 1612)
    assert [n.bounds for n in drv.find_all(buy)] == [(360, 1500, 720, 1612)]
    assert drv.exists(buy) is True
    assert drv.click(buy) is True and dev.actions == [("click", 540, 1556)]
    assert drv.webview_present() is False
    selectors = [params[0] for m, params, _ in dev.rpcs if m != "click"]
    assert selectors and all(s["packageName"] == "com.shopee.id" for s in selectors)


def test_u2_find_all_drops_null_entries_and_not_found():
    drv, dev = _u2()
    dev.null_entries = True
    assert [n.label for n in drv.find_all(Sel("textMatches", ANY_TEXT_MATCH))] == ["Beli Sekarang"]
    dev.all_not_found = True
    assert drv.find_all(Sel("text", "Beli Sekarang")) == []  # UiObjectNotFoundError -> []


def test_u2_client_side_recheck_of_returned_nodes():
    drv, dev = _u2()
    dev.loose = True  # server mengembalikan elemen yang hanya cocok tanpa peduli huruf
    assert drv.info(Sel("textContains", "beli")) is None
    assert drv.find_all(Sel("textStartsWith", "beli")) == []
    assert drv.click(Sel("textContains", "beli")) is False and dev.actions == []  # tidak men-tap elemen salah
    assert drv.info(Sel("textContains", "Beli")).label == "Beli Sekarang"


def test_u2_info_retries_stale_object_once():
    drv, dev = _u2()
    buy = Sel("text", "Beli Sekarang")
    dev.fail_once["objInfo"] = [RPCUnknownError(STALE, None, "trace")]
    assert drv.info(buy).label == "Beli Sekarang"  # dibangun ulang tepat saat T: coba sekali lagi
    assert [m for m, _, _ in dev.rpcs] == ["objInfo", "objInfo"]
    dev.rpcs.clear()
    dev.fail_once["objInfo"] = [RPCUnknownError(STALE, None, "trace"), RPCUnknownError(STALE, None, "trace")]
    assert drv.info(buy) is None  # dua kali stale -> tidak ada (bukan error, bukan percobaan ketiga)
    assert [m for m, _, _ in dev.rpcs] == ["objInfo", "objInfo"]
    dev.fail_once["objInfo"] = [RPCUnknownError(STALE, None, "trace")]
    assert drv.info(Sel("text", "Tidak Ada")) is None


@pytest.mark.parametrize("op, call", [
    ("exist", lambda d: d.exists(Sel("text", "Beli Sekarang"))),
    ("objInfo", lambda d: d.info(Sel("text", "Beli Sekarang"))),
    ("objInfoOfAllInstances", lambda d: d.find_all(Sel("text", "Beli Sekarang"))),
    ("click", lambda d: d.click(Node(bounds=(0, 0, 10, 10)))),
    ("getLastToast", lambda d: d.last_toast()),
    ("clearLastToast", lambda d: d.clear_toast()),
    ("app_current", lambda d: d.current_app()),
    ("shell", lambda d: d.shell(["getprop"])),
    ("swipe", lambda d: d.swipe_refresh()),
    ("pressKey", lambda d: d.press_back()),
    ("dump_hierarchy", lambda d: d.dump()),
    ("stop_uiautomator", lambda d: d.restart_agent()),
])
def test_u2_errors_wrapped_in_driver_error(op, call):
    drv, dev = _u2()
    original = ConnectionResetError("koneksi ke agent putus")
    dev.errors[op] = original
    with pytest.raises(AgentDead) as ei:  # transport putus = agent mati (bisa dipulihkan dengan restart)
        call(drv)
    assert isinstance(ei.value, DriverError)
    assert "ConnectionResetError: koneksi ke agent putus" in str(ei.value)
    assert ei.value.__cause__ is original


# ConnectionResetError: lihat test_u2_errors_wrapped_in_driver_error (AgentDead + pesan + __cause__)
@pytest.mark.parametrize("exc, dead", [
    (HTTPError("Unable to connect to uiautomator2 server"), True),
    (HTTPTimeoutError("read timeout"), True),
    (UiAutomationNotConnectedError("UiAutomation not connected"), True),
    (LaunchUiAutomationError("uiautomator2 server gagal start"), True),
    (BrokenPipeError("pipe"), True),
    (TimeoutError("timed out"), True),  # = socket.timeout (timeout soket adb)
    (OSError("adb transport"), True),
    (AdbError("device offline"), True),
    (RPCUnknownError("Unknown RPC error: -32001 java.lang.IllegalStateException", None, "trace"), False),
    (RuntimeError("x"), False),
    (ValueError("respon aneh"), False),
])
def test_u2_dead_agent_errors_become_agent_dead(exc, dead):
    drv, dev = _u2()
    dev.errors["exist"] = exc
    with pytest.raises(DriverError) as ei:
        drv.exists(Sel("text", "Beli Sekarang"))
    assert isinstance(ei.value, AgentDead) is dead  # runner hanya me-restart agent untuk AgentDead
    assert ei.value.__cause__ is exc


def test_u2_error_message_names_the_query():
    drv, dev = _u2()
    dev.errors["objInfo"] = RuntimeError("rpc timeout")
    with pytest.raises(DriverError, match=r"^info text='Beli Sekarang': RuntimeError: rpc timeout") as ei:
        drv.info(Sel("text", "Beli Sekarang"))
    assert not isinstance(ei.value, AgentDead)
    dev.errors["objInfo"] = DriverError("sudah DriverError")
    with pytest.raises(DriverError, match="^sudah DriverError$"):  # tidak dibungkus dua kali
        drv.info(Sel("text", "Beli Sekarang"))


def test_u2_click_and_get_text():
    drv, dev = _u2(rpc_timeout_s=1.5)
    assert drv.click(Sel("text", "Beli Sekarang")) is True
    assert dev.actions == [("click", 540, 1556)]  # tap tengah bounds
    assert dev.rpcs[-1] == ("click", (540, 1556), 1.5)
    assert drv.click(Sel("text", "Tidak Ada")) is False
    assert dev.actions == [("click", 540, 1556)]  # tidak ada tap tambahan
    n_rpcs = len(dev.rpcs)
    assert drv.click(Node(text="x", bounds=(0, 100, 101, 201))) is True
    assert dev.actions[-1] == ("click", 50, 150)
    assert [m for m, _, _ in dev.rpcs[n_rpcs:]] == ["click"]  # klik Node = 1 RPC, tanpa query info
    assert drv.get_text(Sel("text", "Beli Sekarang")) == "Beli Sekarang"
    assert drv.get_text(Sel("description", "Keranjang")) == "Keranjang"  # text kosong -> desc
    assert drv.get_text(Sel("text", "Tidak Ada")) is None


def test_u2_last_toast_and_clear_toast():
    drv, dev = _u2(rpc_timeout_s=1.5)
    assert drv.last_toast() is None
    dev.toast = "Stok habis"
    assert drv.last_toast() == "Stok habis"
    dev.toast = ""
    assert drv.last_toast() is None  # string kosong = tidak ada toast
    dev.toast = "Silakan pilih variasi terlebih dahulu"
    drv.clear_toast()
    assert dev.toast is None and drv.last_toast() is None
    assert [r for r in dev.rpcs if r[0] == "clearLastToast"] == [("clearLastToast", {}, 1.5)]
    assert dev.actions == []  # operasi agent saja: tidak ada tap/gestur ke aplikasi


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

    assert U2Driver(device=Raw()).shell(["ps", "-A"]) == "langsung str"

    class Odd(FakeU2Device):
        def shell(self, cmd, timeout=60):
            return 42

    assert U2Driver(device=Odd()).shell(["ps", "-A"]) == ""


@pytest.mark.parametrize("cmd", [
    "input text 123456", "input tap 540 1500", "input keyevent 66", "su -c id", "pm install -r x.apk",
    "pm uninstall com.shopee.id", "setprop debug.x 1", "am force-stop com.shopee.id", "am kill com.shopee.id",
    "settings put secure x 1", "settings put global adb_enabled 0", "svc power stayon true", "id",
    "am start -a android.intent.action.VIEW -d https://evil.example/shopee.co.id -p com.shopee.id",
    "am start -a android.intent.action.VIEW -d https://shopee.co.id/x -p com.evil.app",
])
def test_shell_allowlist_rejects_everything_but_reading_intent_and_stay_awake(cmd):
    """Spesifikasi I: perintah shell di luar allowlist ditolak SEBELUM dikirim (U2Driver & FakeDriver)."""
    dev = FakeU2Device()
    with pytest.raises(DriverError, match="perintah shell tidak diizinkan"):
        U2Driver(device=dev).shell(cmd.split())
    assert dev.shells == []
    fake = FakeDriver(_StaticApp([]), FakeClock(tick=0.0))
    with pytest.raises(DriverError, match="perintah shell tidak diizinkan"):
        fake.shell(cmd.split())
    assert fake.calls == []


@pytest.mark.parametrize("cmd", [
    "getprop", "getprop ro.product.model", "wm size", "wm density", "ps -A", "dumpsys power", "dumpsys window",
    "dumpsys package com.shopee.id", "settings get global stay_on_while_plugged_in",
    "settings get system screen_off_timeout", "settings put global stay_on_while_plugged_in 3",
    "svc power stayon usb", "svc power stayon false", "pm path com.shopee.id",
    "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME",
    "am start -W -a android.intent.action.VIEW -d https://shopee.co.id/Produk-i.1.2 -p com.shopee.id",
    "am start -a android.intent.action.VIEW -d https://shopee.co.id/user/shopeepay -p com.shopee.id",
])
def test_shell_allowlist_accepts_the_commands_the_tool_uses(cmd):
    check_shell(cmd.split())


def test_u2_window_size_cached_and_swipe_refresh_pulls_down():
    drv, dev = _u2(size=(1080, 2460))
    drv.swipe_refresh()
    drv.swipe_refresh()
    assert dev.window_size_calls == 1
    _, fx, fy, tx, ty, steps = dev.actions[0]
    assert (fx, fy, tx, ty) == (540, 738, 540, 1845)  # 30% -> 75% tinggi, koordinat piksel bulat
    assert all(isinstance(v, int) for v in (fx, fy, tx, ty))
    assert fy < ty  # tarik ke bawah = muat ulang
    assert steps == 60  # 60 langkah u2 x 5 ms = 0,3 s: tarik pelan, bukan fling
    assert drv.window_size() == (1080, 2460)
    assert dev.window_size_calls == 1


def test_u2_press_back_webview_screenshot_dump(tmp_path):
    drv, dev = _u2()
    drv.press_back()
    assert dev.actions[-1] == ("press", "back")
    assert drv.webview_present() is False
    method, (selector,), _ = dev.rpcs[-1]
    assert method == "exist" and selector["className"] == "android.webkit.WebView"
    dev.elements.append(_u2_info(cls="android.webkit.WebView", bounds=(0, 0, 720, 1612)))
    assert drv.webview_present() is True
    shot = tmp_path / "akhir.png"
    assert drv.screenshot(shot) is True
    assert dev.actions[-1] == ("screenshot", str(shot))
    dev.write_screenshot = False
    assert drv.screenshot(tmp_path / "tidak-ada.png") is False  # file tidak tertulis -> False
    assert "Beli Sekarang" in drv.dump()


def test_u2_agent_alive_needs_ping_and_real_rpc():
    drv, dev = _u2(rpc_timeout_s=1.5)
    assert drv.agent_alive() is True
    assert dev.rpcs == [("deviceInfo", {}, 1.5)]  # /ping saja tidak cukup: UiAutomation dibuktikan dengan RPC
    dev.rpcs.clear()
    dev.alive = False
    assert drv.agent_alive() is False
    assert dev.rpcs == []  # /ping gagal: tidak perlu RPC
    dev.alive = True
    dev.device_info = UiAutomationNotConnectedError("UiAutomation not connected")
    assert drv.agent_alive() is False  # server HTTP hidup, UiAutomation mati (HiOS)
    dev.device_info = {"sdkInt": 33}
    dev.alive = ConnectionError("port forward hilang")
    assert drv.agent_alive() is False  # error saat cek = dianggap mati

    class NoCheck(FakeU2Device):
        _check_alive = None

    assert U2Driver(device=NoCheck()).agent_alive() is None  # tidak bisa dicek


def test_u2_polling_gestures_bounded_by_rpc_timeout():
    """Reload (swipe) & back adalah aksi polling: lewat jsonrpc dengan rpc_timeout_s, bukan d.swipe()/d.press()
    u2 yang memakai batas 300 s (agent macet tidak boleh menggantung runner hingga 5 menit)."""
    dev = FakeU2Device([BUY_INFO])
    drv = U2Driver(device=dev, rpc_timeout_s=2.0)
    drv.swipe_refresh()  # reload saat polling
    drv.press_back()  # menutup sheet yang terbuka sebelum klik Beli pertama
    assert dev.actions == [("swipe", 360, 483, 360, 1209, 60), ("press", "back")]
    assert [(m, t) for m, _, t in dev.rpcs] == [("swipe", 2.0), ("pressKey", 2.0)]


def test_u2_restart_agent_http_stop_then_stop_start(monkeypatch):
    from uiautomator2 import core

    http = []

    def fake_http(dev_, port, method, path, data=None, timeout=10.0, print_request=False):
        http.append((dev_, port, method, path, timeout))

    monkeypatch.setattr(core, "_http_request", fake_http)
    drv, dev = _u2()
    dev._dev, dev._device_server_port = "ADBDEV", 9008
    drv.restart_agent()
    assert http == [("ADBDEV", 9008, "GET", "/stop", 3)]  # server bukan milik sesi ini juga diminta berhenti
    assert dev.actions == [("stop_uiautomator",), ("start_uiautomator",)]

    def http_down(*a, **kw):
        raise HTTPError("server sudah mati")

    monkeypatch.setattr(core, "_http_request", http_down)
    drv.restart_agent()  # /stop gagal (server sudah mati) bukan alasan berhenti
    assert dev.actions[-2:] == [("stop_uiautomator",), ("start_uiautomator",)]
    dev.errors["start_uiautomator"] = RuntimeError("server not ready")
    with pytest.raises(DriverError, match="restart agent: RuntimeError: server not ready"):
        drv.restart_agent()


def test_u2_implicit_restart_disabled_for_real_connections(monkeypatch):
    import uiautomator2
    from uiautomator2 import core

    calls = []

    def direct_call(dev_, port, method, params, timeout, debug):
        calls.append((dev_, port, method, timeout, debug))
        raise HTTPError("agent dibunuh HiOS")

    monkeypatch.setattr(core, "_jsonrpc_call", direct_call)
    dev = FakeU2Device([BUY_INFO])
    dev._dev, dev._device_server_port, dev._debug = "ADBDEV", 9008, False
    monkeypatch.setattr(uiautomator2, "connect", lambda serial=None: dev)
    drv = U2Driver("SER", rpc_timeout_s=1.5, package="com.shopee.id")
    assert drv.no_implicit_restart is True
    with pytest.raises(AgentDead, match="HTTPError: agent dibunuh HiOS"):
        drv.info(Sel("text", "Beli Sekarang"))
    assert calls == [("ADBDEV", 9008, "objInfo", 1.5, False)]  # sekali, tanpa restart+ulang diam-diam
    assert dev.actions == [] and dev.rpcs == []  # stop/start_uiautomator tidak dipanggil u2
    assert _disable_implicit_restart(object()) is False


def test_patch_u2_transport_sets_socket_timeout_and_nodelay(monkeypatch):
    from uiautomator2 import core

    class Sock:
        def __init__(self):
            self.timeout, self.opts = None, []

        def settimeout(self, t):
            self.timeout = t

        def setsockopt(self, *args):
            self.opts.append(args)

    class NotTcp(Sock):
        def setsockopt(self, *args):
            raise OSError("bukan soket TCP")

    class Conn:  # pengganti AdbHTTPConnection (kelas asli dipulihkan monkeypatch)
        sock_cls = Sock

        def __init__(self, timeout):
            self.timeout = timeout

        def connect(self):
            self.sock = self.sock_cls()

    monkeypatch.setattr(core, "AdbHTTPConnection", Conn)
    _patch_u2_transport()
    conn = Conn(2.5)
    conn.connect()
    assert conn.sock.timeout == 2.5  # u2 menyetel conn.timeout tetapi tidak pernah ke soket
    assert (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) in conn.sock.opts
    patched = Conn.connect
    _patch_u2_transport()
    assert Conn.connect is patched  # idempoten: tidak dibungkus dua kali
    Conn.sock_cls = NotTcp
    conn = Conn(None)
    conn.connect()  # error setsockopt / timeout bukan angka diabaikan
    assert conn.sock.timeout is None


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
    assert td.last_toast() is None
    td.clear_toast()
    assert td.screenshot(tmp_path / "x.png") is True
    assert "<hierarchy>" in td.dump()
    assert td.shell(["getprop", "ro.product.model"]) == "TECNO BG6\n"
    assert td.agent_alive() is True
    td.restart_agent()
    assert td.window_size() == (720, 1612)

    samples = td.stats.samples
    assert [s.op for s in samples] == [
        "exists", "info", "find_all", "click", "click", "get_text", "current_app", "start_url", "swipe_refresh",
        "press_back", "webview", "last_toast", "clear_toast", "screenshot", "dump", "shell", "agent_alive",
        "restart_agent", "window_size"]
    by_op = {s.op: s for s in samples}
    assert samples[0].target == "text='Beli Sekarang'"
    assert samples[4].target == "node 'Beli Sekarang'"
    assert by_op["start_url"].target == PRODUCT_URL
    assert by_op["shell"].target == "getprop ro.product.model"
    assert by_op["screenshot"].target == str(tmp_path / "x.png")
    # find_all = latency + per_match (default latency/2) x 1 elemen cocok; toast = operasi agent ringan
    expected_ms = {"exists": 20, "info": 20, "find_all": 30, "get_text": 20, "start_url": 300, "swipe_refresh": 150,
                   "last_toast": 10, "clear_toast": 10, "screenshot": 200, "dump": 400, "agent_alive": 5,
                   "restart_agent": 2000, "window_size": 0}
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


def test_fake_driver_find_all_cost_scales_with_matches():
    clock = FakeClock(tick=0.0)
    nodes = [_n(f"Rp{i}.000", (0, i * 50, 100, i * 50 + 40)) for i in range(1, 5)]
    rp = Sel("textMatches", RP_ANY_MATCH)

    def cost(fn) -> float:
        t0 = clock.monotonic()
        fn()
        return clock.monotonic() - t0

    drv = FakeDriver(_StaticApp(nodes), clock, latency_s=0.02)
    assert drv.per_match_s == pytest.approx(0.01)  # default latency/2 (info_list: ~16 pencarian per elemen)
    assert cost(lambda: drv.find_all(rp)) == pytest.approx(0.02 + 4 * 0.01)
    assert cost(lambda: drv.find_all(Sel("text", "x"))) == pytest.approx(0.02)
    assert cost(lambda: drv.info(rp)) == pytest.approx(0.02)  # info/exists: satu pencarian, berapa pun cocoknya
    assert cost(lambda: drv.exists(rp)) == pytest.approx(0.02)
    slow = FakeDriver(_StaticApp(nodes), clock, latency_s=0.02, per_match_s=0.05)
    assert cost(lambda: slow.find_all(rp)) == pytest.approx(0.02 + 4 * 0.05)


def test_fake_driver_info_sees_all_windows_find_all_only_active(fake_clock):
    app = FakeShopeeApp(AppScenario(open_at=0.0), fake_clock)  # slot sudah buka (jam palsu jauh setelah 0)
    app.screen = "sheet"
    drv = FakeDriver(app, fake_clock)
    buy = Sel("text", "Beli Sekarang")
    assert drv.info(buy).bounds == (360, 1500, 720, 1612)  # jalur A: tombol halaman produk DI BELAKANG sheet
    assert [n.bounds for n in drv.find_all(buy)] == [(0, 1500, 720, 1612)]  # jalur B: hanya tombol sheet
    assert drv.exists(Sel("text", "Detail Produk")) and drv.find_all(Sel("text", "Detail Produk")) == []


def test_fake_driver_toast_channel_is_agent_only(fake_clock):
    app = FakeShopeeApp(AppScenario(open_at=0.0), fake_clock)
    app.screen = "product"
    drv = FakeDriver(app, fake_clock)
    assert drv.last_toast() is None
    app._show_toast("Stok habis")
    assert drv.last_toast() == "Stok habis"
    assert not drv.exists(Sel("text", "Stok habis"))  # Toast Android bukan node
    drv.clear_toast()
    assert drv.last_toast() is None and app.last_toast is None
    assert [c for c in drv.calls if "toast" in c[0]] == [("last_toast", "")] * 2 + [("clear_toast", "")] + \
        [("last_toast", "")]
    assert app.events == []  # tidak ada tap/intent/refresh: aplikasi & server Shopee tidak tersentuh

    node_app = FakeShopeeApp(AppScenario(open_at=0.0, toast_mode="node"), fake_clock)
    node_app.screen = "product"
    node_drv = FakeDriver(node_app, fake_clock)
    node_app._show_toast("Stok habis")
    assert node_drv.exists(Sel("text", "Stok habis")) and node_drv.last_toast() is None  # overlay in-app = node
    fake_clock.advance(1.6)
    assert not node_drv.exists(Sel("text", "Stok habis"))  # overlay hilang sendiri setelah 1,5 s


# ------------------------------------------------------------------ regex penanda & bacaan longgar (ronde 3)


def _jfull(pattern: str, text: str) -> bool:
    """Seperti textMatches/descriptionMatches di agent: regex cocok SELURUH teks (ASCII, seperti Java)."""
    return re.fullmatch(pattern, text, re.ASCII) is not None


@pytest.mark.parametrize("text, hit", [
    ("Geser untuk verifikasi", True),
    ("Masukkan kode OTP", True),
    ("Flash Sale telah berakhir", True),
    ("Flash Sale berakhir dalam 01:59:59", False),  # hitung mundur sale yang sedang berjalan
    ("Produk original. " * 8 + "Cek kode OTP di situs resmi.", False),  # > 120 karakter: deskripsi
    ("Habis", False),  # "Habis" polos: query terpisah (_bare_sold_out)
])
def test_danger_union_is_short_text_only_and_includes_sale_ended(text, hit):
    runner = android_runner.AndroidRunner.__new__(android_runner.AndroidRunner)
    runner.sel = android_selectors.defaults()
    runner.variant = None
    runner._cand_cache, runner._union_cache = {}, {}
    runner._ended = tuple(p for p in runner.sel.marker("sold_out") if "berakhir" in p.lower())
    sel = runner._union(markers=android_runner.DANGER, extra=runner._ended, short=True)
    assert sel.by == "textMatches"
    assert _jfull(sel.value, text) is hit, sel.value
    desc = runner._union(markers=android_runner.DANGER, extra=runner._ended, short=True, by="descriptionMatches")
    assert desc.by == "descriptionMatches" and desc.value == sel.value


@pytest.mark.parametrize("text, hit", [
    ("Stok habis", True), ("Ingatkan Saya", True), ("Silakan pilih variasi terlebih dahulu", True),
    ("Habis", False), (" HABIS ", False), ("Habis ", False), ("Stok habis. " * 12, False),
])
def test_message_union_skips_bare_habis_chip_and_long_text(text, hit):
    pat = android_runner._MARKER_PREFIX + "(?:" + android_selectors.union(
        [p for name in android_runner.MESSAGES for p in android_selectors.defaults().marker(name)]) + ")"
    assert _jfull(pat, text) is hit


@pytest.mark.parametrize("wanted, text, hit", [
    ("256GB Biru", "Variasi: 256GB, Biru", True),
    ("256GB Biru", "Variasi: 256 GB  /  biru", True),
    ("256GB Biru", "Variasi: 128GB, Biru", False),
    ("Uji Coba", "Ponsel Uji-Coba 128GB Hitam", True),
    ("Uji Coba", "Ponsel Uji 128GB Coba", False),
])
def test_loose_match_tolerates_punctuation_and_spacing(wanted, text, hit):
    pat = android_runner._loose_match(wanted)
    assert _jfull(pat, text) is hit
    assert (pricing.squash(wanted) in pricing.squash(text)) is hit  # sama dengan aturan lapis 3
    assert android_runner._loose_match("  ,. ") is None


@pytest.mark.parametrize("text, expected", [
    ("Kaos Polos Hitam Putih\nVariasi: Putih\nRp99.000\nx1", "Variasi: Putih"),
    ("Kaos Polos Hitam Putih\nPutih, XL\nRp99.000\nx1", "Putih, XL"),  # tanpa label: selain nama/harga/qty
    ("Kaos Polos Hitam Putih\nRp99.000\nx1", ""),  # tidak ada baris variasi -> kosong (fail-closed)
])
def test_variant_text_excludes_product_name(text, expected):
    assert pricing.variant_text(text, "Kaos Polos") == expected


def test_desc_danger_judged_on_content_desc_even_when_node_has_text(tmp_path):
    """Node bertext lain ("Tutup") dengan content-desc captcha: query descriptionMatches menilai desc-nya."""
    runner, app, _driver, log = _prepared(tmp_path)
    orig = app._r_product
    app._r_product = lambda: [*orig(), ("", Node(text="Tutup", desc="Geser untuk verifikasi", bounds=(0, 0, 9, 9)))]
    seen = runner._danger(None, "product", by="descriptionMatches")
    assert seen is not None and seen.screen == Screen.CAPTCHA, seen
    assert runner._danger(None, "product") is None  # text "Tutup" bukan penanda
    log.close()


@pytest.mark.parametrize("screen, expected", [("login", Screen.LOGIN_REQUIRED), ("sold_out", Screen.SOLD_OUT)])
def test_progress_bar_does_not_hide_login_or_sold_out_button(tmp_path, screen, expected):
    """Indikator loading (mis. gambar yang masih dimuat) di layar login / halaman produk habis: tetap terbaca
    LOGIN_REQUIRED / SOLD_OUT (cek murahnya sebelum ProgressBar), bukan LOADING yang ditunggu sampai habis waktu."""
    runner, app, _driver, log = _prepared(tmp_path, variant_chip_sold_out=True)
    spinner = ("", Node(cls="android.widget.ProgressBar", bounds=(310, 760, 410, 860)))
    if screen == "login":
        app.screen = "login"
        orig = app._r_login
        app._r_login = lambda: [*orig(), spinner]
    else:
        app.sold_out = True  # tombol "Habis" + chip variasi lain "Habis" (bukan tombol)
        orig = app._r_product
        app._r_product = lambda: [*orig(), spinner]
    for context in ("product", "after_buy", "any"):
        assert runner._classify(context).screen == expected, context
    log.close()
