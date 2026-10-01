from __future__ import annotations

import pytest

from flashbuy import pricing
from flashbuy.pricing import CartRow, CheckoutSnapshot, Limits

L = Limits(max_item_price=100_000, max_total=120_000)


@pytest.mark.parametrize("text, value", [
    ("Rp1.000", 1_000),
    ("Rp 1.000", 1_000),
    ("Rp15.999.000", 15_999_000),
    ("  Rp 15.999.000 ", 15_999_000),
    ("Rp 99.000", 99_000),
    ("rp99.000", 99_000),
    ("Rp99.000,-", 99_000),
    ("Rp1000", 1_000),
])
def test_parse_price_ok(text, value):
    assert pricing.parse_price(text) == value


@pytest.mark.parametrize("text", [
    None, "", "Rp", "Gratis", "Rp99.000 - Rp129.000", "Rp99,000", "Rp99.OOO", "Rp1.000,50",
    "Rp99rb", "99.000", "Rp10.00", "Harga Rp99.000", "Rp99.000 Rp1.499.000",
])
def test_parse_price_unreadable_is_none(text):
    assert pricing.parse_price(text) is None


def test_find_amounts_and_consistency():
    assert pricing.find_amounts("Subtotal Rp99.000, ongkir Rp 12.000; salah Rp9,000") == [99_000, 12_000]
    assert pricing.find_amounts("Rp99.OOO Rp99rb Rp1.000,50 Rp5.000.") == [5_000]
    assert pricing.consistent_amount(["Rp111.000", "Total: Rp111.000 (termasuk PPN)"]) == 111_000
    assert pricing.consistent_amount(["Rp111.000", "Rp99.000"]) is None
    assert pricing.consistent_amount(["Rp111.000", "Menghitung..."]) is None
    assert pricing.consistent_amount([]) is None


@pytest.mark.parametrize("text, qty", [
    ("Ponsel\nRp99.000\nx1", 1), ("× 2", 2), ("Jumlah: 1", 1), ("Qty 3", 3),
    ("Box 2x3 cm", None), ("x1 dan x2", None), ("tanpa jumlah", None),
])
def test_parse_qty(text, qty):
    assert pricing.parse_qty(text) == qty


def test_product_price_verdicts():
    assert pricing.check_product_price("Rp99.000", L).verdict == "ok"
    assert pricing.check_product_price("Rp100.000", L).verdict == "ok"
    high = pricing.check_product_price("Rp1.499.000", L)
    assert high.verdict == "high" and high.value == 1_499_000
    assert "> maks Rp100.000" in high.describe(L)
    un = pricing.check_product_price("Rp89.000 - Rp129.000", L)
    assert un.verdict == "unreadable" and un.value is None
    assert pricing.check_product_price("Rp0", L).verdict == "unreadable"  # 0 bukan harga valid


def test_cart_verdicts():
    rows = [CartRow("Ponsel Uji Coba 128GB\nRp99.000", True, 1),
            CartRow("Kabel Data USB-C\nRp25.000", True, 1),
            CartRow("Casing\nRp15.000", False, 1)]
    v = pricing.check_cart(rows, "ponsel uji")
    assert not v.ok and v.to_uncheck == [1]
    assert pricing.check_cart(rows, None).to_uncheck == []  # target tak bisa dipastikan
    assert pricing.check_cart([rows[0], rows[2]], None).ok
    assert not pricing.check_cart([CartRow("Ponsel", True, 2)], None).ok  # qty 2
    assert not pricing.check_cart([rows[2]], None).ok  # tidak ada yang tercentang


def _snap(rows=("Ponsel Uji Coba\nHarga satuan: Rp99.000\nx1\nSubtotal: Rp99.000",),
          totals=("Rp111.000",), ships=("Rp12.000",), page="Total Pesanan (1 Produk): Rp99.000"):
    return CheckoutSnapshot(list(rows), list(totals), list(ships), page)


def test_checkout_ok_and_summary():
    v = pricing.check_checkout(_snap(), Limits(100_000, 120_000, "uji coba"))
    assert v.ok, v.reasons
    assert v.values["total"] == 111_000 and v.values["qty"] == 1 and v.values["name_ok"]
    assert "total=Rp111.000" in v.summary()


@pytest.mark.parametrize("snap, reason", [
    (_snap(rows=("A\nRp99.000\nx1", "B\nRp25.000\nx1")), "2 baris produk"),
    (_snap(rows=("A\nRp99.000\nx2",), page="(2 Produk)"), "kuantitas 2"),
    (_snap(rows=("A\nRp99,000\nx1",)), "harga satuan tidak terbaca"),
    (_snap(rows=("A\nRp1.499.000\nx1",)), "harga Rp1.499.000 > maks"),
    (_snap(totals=("Rp121.000",)), "total Rp121.000 > maks"),
    (_snap(ships=("Menghitung...",)), "ongkir tidak terbaca"),
    (_snap(totals=("Rp111.000", "Rp99.000")), "total pembayaran tidak terbaca"),
    (_snap(rows=(), page=""), "baris produk checkout tidak terbaca"),
    (_snap(rows=("A\nRp99.000",)), "kuantitas tidak terbaca"),
])
def test_checkout_failures(snap, reason):
    v = pricing.check_checkout(snap, L)
    assert not v.ok
    assert any(reason in r for r in v.reasons), v.reasons


def test_checkout_name_mismatch():
    v = pricing.check_checkout(_snap(), Limits(100_000, 120_000, "Laptop"))
    assert not v.ok and any("Laptop" in r for r in v.reasons)


def test_checkout_without_rows_uses_order_count():
    v = pricing.check_checkout(_snap(rows=(), page="Total Pesanan (1 Produk): Rp99.000"), L)
    assert v.ok, v.reasons
    assert v.values["item_price"] == 99_000
