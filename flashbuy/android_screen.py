"""Pembacaan layar Android dari node accessibility (tanpa dump): fungsi murni, mudah dites.

Aksesibilitas Android tidak memberi tahu teks yang dicoret, jadi:
- harga utama produk = nominal Rp dengan tinggi teks (bounds) terbesar (harga flash biasanya
  paling besar; harga coret lebih kecil);
- harga satuan di checkout = nominal Rp pada baris yang sama dengan penanda kuantitas "x1".
Keputusan tetap di flashbuy.pricing (fail-closed): nilai yang tidak yakin = tidak terbaca.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from flashbuy import pricing
from flashbuy.android_driver import Node
from flashbuy.web_js import SHIPPING_LABEL, TOTAL_LABEL

# Untuk textMatches di device (regex Java, cocok seluruh teks) DAN re.fullmatch lokal.
PRICE_MATCH = r"\s*Rp\s?[\d.]+(,-)?(\s*[-–]\s*Rp\s?[\d.]+(,-)?)?\s*"
QTY_MATCH = r"\s*[x×]\s?\d{1,3}\s*"
ANY_TEXT_MATCH = r"(?s).*\S.*"
ORDER_COUNT_MATCH = r"(?is).*\(\s*\d{1,3}\s*produk\s*\).*"

_PRICE_RE = re.compile(PRICE_MATCH)
_QTY_RE = re.compile(QTY_MATCH)
_TOTAL_RE = re.compile(TOTAL_LABEL, re.I)
_SHIPPING_RE = re.compile(SHIPPING_LABEL, re.I)
_SELECT_ALL_RE = re.compile(r"(?i)\b(pilih )?semua\b")
_HAS_RP = re.compile(r"Rp\s?\d", re.I)
_CARD_LINES = 6  # tinggi maksimum kartu produk checkout, dalam kelipatan tinggi penanda "x1"


def is_price(n: Node) -> bool:
    return _PRICE_RE.fullmatch(n.label) is not None


def is_qty(n: Node) -> bool:
    return _QTY_RE.fullmatch(n.label) is not None


def vcenter(n: Node) -> float:
    return (n.bounds[1] + n.bounds[3]) / 2


def same_row(a: Node, b: Node) -> bool:
    """Tumpang-tindih vertikal >= 50% dari tinggi yang lebih kecil."""
    overlap = min(a.bounds[3], b.bounds[3]) - max(a.bounds[1], b.bounds[1])
    smaller = min(a.height, b.height)
    return smaller > 0 and overlap >= 0.5 * smaller


def pick_main_price(nodes: Iterable[Node]) -> Node | None:
    """Nominal Rp (tunggal/rentang) dengan tinggi teks terbesar; seri -> yang paling atas."""
    prices = [n for n in nodes if is_price(n) and n.height > 0]
    if not prices:
        return None
    return sorted(prices, key=lambda n: (-n.height, n.bounds[1], n.bounds[0]))[0]


def value_for_label(label: Node, nodes: list[Node], accept: Callable[[Node], bool] | None = None) -> Node | None:
    """Nilai milik label: node di baris yang sama di sebelah kanan (terdekat), atau tepat di bawahnya."""
    accept = accept or (lambda n: True)
    right = [n for n in nodes if n is not label and n != label and same_row(n, label)
             and n.bounds[0] >= label.bounds[2] - 2 and accept(n)]
    if right:
        return min(right, key=lambda n: n.bounds[0])
    lh = max(label.height, 1)
    below = [n for n in nodes if n is not label and n != label and accept(n)
             and 0 <= n.bounds[1] - label.bounds[3] + 2 <= 1.5 * lh
             and n.bounds[0] < label.bounds[2] and n.bounds[2] > label.bounds[0]]
    if below:
        return min(below, key=lambda n: n.bounds[1])
    return None


def label_values(nodes: list[Node], label_re: re.Pattern) -> list[str]:
    """Teks nilai setiap label (sisa teks label itu sendiri, atau node nilai yang berpasangan).

    Nilai yang belum ada (mis. ongkir "Menghitung...") dikembalikan apa adanya, sehingga
    pricing.consistent_amount gagal -> dianggap belum stabil/tidak terbaca.
    """
    out = []
    for n in nodes:
        m = label_re.search(n.label)
        if not m:
            continue
        rest = n.label[m.end():].strip(" :\n\t")
        if rest:
            out.append(rest)
            continue
        # nilai bukan tombol (mis. "Buat Pesanan" sebaris dengan "Total Pembayaran" di bar bawah)
        val = value_for_label(n, nodes, accept=lambda x: not label_re.search(x.label) and not x.clickable)
        out.append(val.label if val is not None else "")
    return out


def checkout_snapshot(nodes: list[Node]) -> pricing.CheckoutSnapshot:
    """Bangun CheckoutSnapshot dari node teks layar checkout.

    Baris produk = satu per penanda kuantitas "xN". Teks baris = nama/variasi di kartu produk
    (di atas penanda, maks _CARD_LINES baris) + harga satuan di baris yang sama dengan penanda + "xN".
    """
    nodes = [n for n in nodes if n.label.strip()]
    markers = sorted((n for n in nodes if is_qty(n)), key=lambda n: n.bounds[1])
    rows: list[str] = []
    prev_bottom = None
    for m in markers:
        lh = max(m.height, 1)
        top = m.bounds[1] - _CARD_LINES * lh
        if prev_bottom is not None:
            top = max(top, prev_bottom)
        card = [n for n in nodes if n is not m and top <= vcenter(n) <= m.bounds[3]]
        # Kandidat harga = teks ber-"Rp" sebaris penanda (termasuk format rusak, mis. "Rp99.OOO", supaya
        # tidak diam-diam diganti harga coret). Harga coret + jual sebaris -> pilih teks tertinggi;
        # masih seri -> semua dipakai (pricing mengambil maksimum = fail-closed).
        price_nodes = [n for n in card if _HAS_RP.search(n.label) and same_row(n, m)]
        if not price_nodes:  # harga di atas penanda (tata letak lain)
            price_nodes = sorted((n for n in card if _HAS_RP.search(n.label)), key=lambda n: -n.bounds[1])[:1]
        elif len(price_nodes) > 1:
            tallest = max(n.height for n in price_nodes)
            price_nodes = [n for n in price_nodes if n.height == tallest]
        names = [n.label for n in sorted(card, key=lambda n: (n.bounds[1], n.bounds[0]))
                 if not _HAS_RP.search(n.label) and not is_qty(n)]
        rows.append("\n".join([*names, *(p.label for p in price_nodes), m.label.strip()]))
        prev_bottom = m.bounds[3]
    return pricing.CheckoutSnapshot(
        rows=rows,
        totals=label_values(nodes, _TOTAL_RE),
        shippings=label_values(nodes, _SHIPPING_RE),
        page_text="\n".join(n.label for n in sorted(nodes, key=lambda n: (n.bounds[1], n.bounds[0]))),
    )


def cart_rows(boxes: list[Node], nodes: list[Node]) -> list[tuple[pricing.CartRow, Node]]:
    """Baris item keranjang: (CartRow, node checkbox). Baris = node di kanan checkbox, dari
    checkbox ini sampai checkbox berikutnya; baris tanpa nominal Rp (toko) & "Pilih Semua" dilewati."""
    boxes = sorted(boxes, key=lambda b: b.bounds[1])
    out = []
    for i, box in enumerate(boxes):
        top = box.bounds[1] - box.height
        bottom = boxes[i + 1].bounds[1] if i + 1 < len(boxes) else box.bounds[3] + 10 * max(box.height, 1)
        band = [n for n in nodes if top <= vcenter(n) < bottom and n.bounds[0] >= box.bounds[2] - 2]
        text = "\n".join(n.label for n in sorted(band, key=lambda n: (n.bounds[1], n.bounds[0])))
        same = " ".join(n.label for n in nodes if same_row(n, box))
        if _SELECT_ALL_RE.search(same) or not pricing.find_amounts(text):
            continue
        out.append((pricing.CartRow(text, box.checked or box.selected, pricing.parse_qty(text)), box))
    return out


def checkout_count(nodes: Iterable[Node]) -> list[int]:
    """Angka N pada tombol "Checkout (N)" di keranjang."""
    out = []
    for n in nodes:
        m = re.fullmatch(r"\s*checkout\s*\(\s*(\d+)\s*\)\s*", n.label, re.I)
        if m:
            out.append(int(m.group(1)))
    return out


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_dump(xml: str) -> list[Node]:
    """Node dari dump_hierarchy (hanya untuk kalibrasi/diagnosa; bukan hot path)."""
    import xml.etree.ElementTree as ET

    out = []
    for el in ET.fromstring(xml).iter("node"):
        m = _BOUNDS_RE.fullmatch(el.get("bounds", ""))
        bounds = tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)
        out.append(Node(text=el.get("text", ""), desc=el.get("content-desc", ""), rid=el.get("resource-id", ""),
                        cls=el.get("class", ""), enabled=el.get("enabled", "true") == "true",
                        clickable=el.get("clickable") == "true", selected=el.get("selected") == "true",
                        checked=el.get("checked") == "true", bounds=bounds))
    return out


def payment_value(nodes: list[Node], label_re: re.Pattern) -> str | None:
    """Teks metode pembayaran yang tampil di baris "Metode Pembayaran" checkout."""
    for n in nodes:
        if label_re.search(n.label):
            rest = n.label[label_re.search(n.label).end():].strip(" :\n\t")
            if rest:
                return rest
            val = value_for_label(n, nodes, accept=lambda x: not label_re.search(x.label) and x.label.strip() != "")
            return val.label if val is not None else None
    return None
