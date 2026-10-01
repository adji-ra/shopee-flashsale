"""Pengaman harga & isi pesanan (fail-closed), dipakai bersama jalur web & Android.

Modul ini hanya menerima TEKS yang sudah diambil dari UI (harga coret sudah dibuang oleh
pengambilnya) lalu memutuskan. Nilai yang tidak bisa dibaca dengan yakin = GAGAL, bukan 0.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Spasi aneh yang sering muncul di UI: nbsp, narrow nbsp, thin space, figure space.
_SPACES = re.compile(r"[     \t ]+")
# Nominal: "Rp1.000", "Rp 1.000", "Rp15.999.000", "Rp1000", "Rp99.000,-". Tidak boleh langsung
# diikuti huruf/angka, atau "."/"," + huruf/angka ("Rp1.000,50", "Rp99.OOO", "Rp99rb" = tidak terbaca).
_AMOUNT = r"Rp\s?(\d{1,3}(?:\.\d{3})+|\d+)(?![0-9A-Za-z]|[.,][0-9A-Za-z])"
_AMOUNT_RE = re.compile(_AMOUNT, re.I)
_SINGLE_RE = re.compile(rf"^\s*{_AMOUNT}(?:,-)?\s*$", re.I)
_QTY_RE = re.compile(r"(?:^|[\s(])(?:[x×]\s?(\d{1,3})|(?:jumlah|qty|kuantitas)\s*:?\s*(\d{1,3}))(?!\d|[.,]\d)", re.I)
_ORDER_COUNT_RE = re.compile(r"\((\d{1,3})\s*produk\)", re.I)


def normalize(text: str | None) -> str:
    return _SPACES.sub(" ", text or "").strip()


def _to_int(digits: str) -> int:
    return int(digits.replace(".", ""))


def parse_price(text: str | None) -> int | None:
    """Tepat satu nominal (seluruh teks). Rentang, teks lain, atau format aneh -> None."""
    m = _SINGLE_RE.match(normalize(text))
    return _to_int(m.group(1)) if m else None


def find_amounts(text: str | None) -> list[int]:
    return [_to_int(m.group(1)) for m in _AMOUNT_RE.finditer(normalize(text))]


def first_amount(text: str | None) -> int | None:
    amounts = find_amounts(text)
    return amounts[0] if amounts else None


def consistent_amount(texts: Iterable[str]) -> int | None:
    """Nominal pertama dari tiap teks; semua harus ada dan sama. Selain itu None."""
    values = [first_amount(t) for t in texts]
    if not values or any(v is None for v in values) or len(set(values)) != 1:
        return None
    return values[0]


def parse_qty(text: str | None) -> int | None:
    """Kuantitas dari teks baris ("x1", "×2", "Jumlah: 1"). Harus tepat satu nilai berbeda."""
    found = {int(a or b) for a, b in _QTY_RE.findall(normalize(text))}
    return found.pop() if len(found) == 1 else None


def order_counts(text: str | None) -> list[int]:
    """Semua "(N Produk)" pada teks halaman checkout."""
    return [int(n) for n in _ORDER_COUNT_RE.findall(normalize(text))]


def squash(text: str) -> str:
    """Huruf kecil, hanya huruf & angka ("128GB, Hitam" ~ "128GB Hitam")."""
    return re.sub(r"[\W_]+", "", normalize(text).lower())


def rupiah(n: int | None) -> str:
    return "?" if n is None else "Rp" + f"{n:,}".replace(",", ".")


@dataclass(frozen=True)
class Limits:
    max_item_price: int
    max_total: int
    expected_name: str | None = None
    expected_variant: str | None = None  # bila diisi: teks variasi harus terlihat di baris produk checkout


# --------------------------------------------------------------------------- lapis 1: produk


@dataclass(frozen=True)
class ProductPrice:
    verdict: str  # "ok" | "high" | "unreadable"
    value: int | None
    text: str

    def describe(self, limits: Limits) -> str:
        if self.verdict == "unreadable":
            return f"harga tidak terbaca ({self.text!r})"
        op = "<=" if self.verdict == "ok" else ">"
        return f"harga {rupiah(self.value)} {op} maks {rupiah(limits.max_item_price)}"


def check_product_price(text: str | None, limits: Limits) -> ProductPrice:
    value = parse_price(text)
    if value is None or value <= 0:
        return ProductPrice("unreadable", None, normalize(text))
    return ProductPrice("ok" if value <= limits.max_item_price else "high", value, normalize(text))


# --------------------------------------------------------------------------- lapis 2: keranjang


@dataclass(frozen=True)
class CartRow:
    text: str
    checked: bool
    qty: int | None = None


def name_matches(text: str, name: str | None) -> bool:
    return bool(name) and normalize(name).lower() in normalize(text).lower()


_VARIANT_LINE_RE = re.compile(r"\s*variasi\b", re.I)


def variant_text(text: str, expected_name: str | None = None) -> str:
    """Teks variasi pesanan: baris yang diawali "Variasi". Tanpa baris itu: baris selain nama produk
    (baris pertama / yang memuat expected_name), harga, dan kuantitas. Nama produk TIDAK ikut, supaya judul
    "Kaos Hitam Putih" tidak meloloskan variasi "Hitam" saat baris variasi menunjukkan "Putih"."""
    lines = [ln for ln in normalize(text).splitlines() if ln.strip()]
    labelled = [ln for ln in lines if _VARIANT_LINE_RE.match(ln)]
    if labelled:
        return "\n".join(labelled)
    rest = [ln for ln in lines[1:] if not find_amounts(ln) and parse_qty(ln) is None
            and not name_matches(ln, expected_name)]
    return "\n".join(rest)


@dataclass
class CartVerdict:
    ok: bool
    to_uncheck: list[int] = field(default_factory=list)
    reason: str = ""


def check_cart(rows: list[CartRow], target_name: str | None, strict: bool = False) -> CartVerdict:
    """Tentukan baris yang harus di-uncheck. ok=True bila tepat 1 baris tercentang.

    strict (nama acuan dari expected_name): satu-satunya baris tercentang juga harus memuat nama acuan.
    """
    checked = [i for i, r in enumerate(rows) if r.checked]
    if len(checked) == 1:
        qty = rows[checked[0]].qty
        if qty is not None and qty != 1:
            return CartVerdict(False, reason=f"kuantitas di keranjang {qty}, bukan 1")
        if strict and target_name and not name_matches(rows[checked[0]].text, target_name):
            return CartVerdict(False, reason=f"item tercentang bukan target (nama acuan {target_name!r})")
        return CartVerdict(True, reason="1 item tercentang")
    if not checked:
        return CartVerdict(False, reason="tidak ada item tercentang")
    targets = [i for i in checked if name_matches(rows[i].text, target_name)]
    if len(targets) != 1:
        return CartVerdict(False, reason=f"{len(checked)} item tercentang dan target tidak bisa dipastikan "
                                         f"(nama acuan {target_name!r})")
    return CartVerdict(False, to_uncheck=[i for i in checked if i != targets[0]],
                       reason=f"{len(checked)} item tercentang")


# --------------------------------------------------------------------------- lapis 3: checkout


@dataclass
class CheckoutSnapshot:
    rows: list[str]  # teks tiap baris produk (tanpa harga coret)
    totals: list[str]  # teks setelah label "Total Pembayaran"
    shippings: list[str]  # teks setelah label ongkir
    page_text: str = ""  # untuk "(N Produk)" & fallback nama


@dataclass
class CheckoutVerdict:
    ok: bool
    reasons: list[str]
    values: dict

    def summary(self) -> str:
        v = self.values
        parts = [f"baris={v.get('rows')}", f"qty={v.get('qty')}", f"harga={rupiah(v.get('item_price'))}",
                 f"ongkir={rupiah(v.get('shipping'))}", f"total={rupiah(v.get('total'))}"]
        if v.get("name_checked"):
            parts.append(f"nama cocok={v.get('name_ok')}")
        return ", ".join(parts)


def read_total(snap: CheckoutSnapshot) -> tuple[int | None, int | None]:
    """(ongkir, total) — None bila belum/tidak terbaca atau tidak konsisten."""
    return consistent_amount(snap.shippings), consistent_amount(snap.totals)


def check_checkout(snap: CheckoutSnapshot, limits: Limits) -> CheckoutVerdict:
    reasons: list[str] = []
    values: dict = {"rows": len(snap.rows)}
    counts = order_counts(snap.page_text)
    values["order_counts"] = counts

    if snap.rows:
        if len(snap.rows) != 1:
            reasons.append(f"{len(snap.rows)} baris produk (harus tepat 1)")
        qty = parse_qty(snap.rows[0]) if len(snap.rows) == 1 else None
        amounts = [a for row in snap.rows for a in find_amounts(row)]
    elif counts:
        if len(counts) != 1:  # beberapa grup "(N Produk)" = beberapa toko/produk
            reasons.append(f"{len(counts)} grup pesanan (harus tepat 1)")
        qty = counts[0] if len(set(counts)) == 1 else None
        amounts = []
    else:
        reasons.append("baris produk checkout tidak terbaca")
        qty, amounts = None, []
    values["qty"] = qty
    if qty is None:
        if snap.rows or counts:
            reasons.append("kuantitas tidak terbaca")
    elif qty != 1:
        reasons.append(f"kuantitas {qty}, bukan 1")
    if counts and set(counts) != {1}:
        reasons.append(f"jumlah produk pesanan {sorted(set(counts))}, bukan 1")

    if not amounts and counts and not snap.rows:
        # tanpa baris: pakai nominal "Total Pesanan (1 Produk): RpX"
        m = re.search(r"\(\s*1\s*produk\)\s*:?\s*(" + _AMOUNT + ")", normalize(snap.page_text), re.I)
        amounts = [_to_int(m.group(2))] if m else []
    if not amounts:
        reasons.append("harga satuan tidak terbaca")
        values["item_price"] = None
    else:
        values["item_price"] = max(amounts)
        if max(amounts) > limits.max_item_price:
            reasons.append(f"harga {rupiah(max(amounts))} > maks {rupiah(limits.max_item_price)}")

    if limits.expected_name:
        haystack = " ".join(snap.rows) if snap.rows else snap.page_text
        values["name_checked"] = True
        values["name_ok"] = name_matches(haystack, limits.expected_name)
        if not values["name_ok"]:
            reasons.append(f"nama produk tidak memuat {limits.expected_name!r}")
    if limits.expected_variant:
        haystack = variant_text(" \n".join(snap.rows) if snap.rows else snap.page_text, limits.expected_name)
        values["variant_ok"] = bool(haystack) and squash(limits.expected_variant) in squash(haystack)
        if not values["variant_ok"]:
            reasons.append(f"variasi {limits.expected_variant!r} tidak terlihat di baris produk")

    shipping, total = read_total(snap)
    values["shipping"], values["total"] = shipping, total
    if shipping is None:
        reasons.append("ongkir tidak terbaca")
    if total is None:
        reasons.append("total pembayaran tidak terbaca")
    elif total > limits.max_total:
        reasons.append(f"total {rupiah(total)} > maks {rupiah(limits.max_total)}")
    return CheckoutVerdict(not reasons, reasons, values)
