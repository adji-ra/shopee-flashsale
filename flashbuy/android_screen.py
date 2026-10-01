"""Pembacaan layar Android dari node accessibility (tanpa dump): fungsi murni, mudah dites.

Aksesibilitas Android tidak memberi tahu teks yang dicoret, jadi:
- harga produk di hot path = teks Rp pendek pertama yang terlihat (urutan dokumen; harga utama
  berada di atas harga coret) atau selector hasil kalibrasi; `pick_main_price` (teks tertinggi)
  dipakai di luar hot path;
- harga satuan di checkout = nominal Rp pada baris yang sama dengan penanda kuantitas "x1".
Keputusan tetap di flashbuy.pricing (fail-closed): nilai yang tidak yakin = tidak terbaca.

Regex *_MATCH dikirim ke device (regex Java, cocok SELURUH teks). Di Java `\\s` hanya ASCII, jadi
spasi UI (NBSP, thin space, narrow NBSP) ditulis eksplisit lewat SP.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from flashbuy import pricing
from flashbuy.android_driver import Node

SP = r"[\s\u00a0\u2007\u2009\u202f]"  # spasi termasuk NBSP & kawan-kawan (Java & Python)

PRICE_MATCH = rf"{SP}*Rp{SP}?[\d.]+(,-)?({SP}*[-\u2013]{SP}*Rp{SP}?[\d.]+(,-)?)?{SP}*"
QTY_MATCH = rf"{SP}*[x\u00d7]{SP}?\d{{1,3}}{SP}*"
ANY_TEXT_MATCH = r"(?s).*\S.*"
ORDER_COUNT_MATCH = rf"(?is).*\({SP}*\d{{1,3}}{SP}*produk{SP}*\).*"
# label baris (harus DIAWALI label; badge "Gratis Ongkir" / "Voucher Gratis Ongkir" tidak ikut)
TOTAL_LABEL_MATCH = rf"(?is){SP}*total pembayaran{SP}*:?.*"
SHIPPING_LABEL_MATCH = rf"(?is){SP}*(subtotal pengiriman|total ongkos kirim|ongkos kirim|biaya pengiriman|ongkir)\b.*"
RP_ANY_MATCH = rf"(?s).*Rp{SP}?\d.*"  # teks ber-Rp apa pun, termasuk format rusak (agar terbaca "tidak terbaca")
RP_SHORT_MATCH = rf"(?s)(?=.{{1,40}}$).*Rp{SP}?\d.*"  # teks Rp pendek (harga, bukan paragraf deskripsi)
PENDING_MATCH = r"(?is).*(menghitung|memuat|loading).*"  # nilai yang belum siap (ikut dibaca agar fail-closed)

_PRICE_RE = re.compile(PRICE_MATCH)
_QTY_RE = re.compile(QTY_MATCH)
_TOTAL_ROW = re.compile(rf"(?is){SP}*total pembayaran{SP}*:?{SP}*(.*)")
_SHIPPING_ROW = re.compile(rf"(?is){SP}*(?:subtotal pengiriman|total ongkos kirim|ongkos kirim|biaya pengiriman|"
                           rf"ongkir)\b{SP}*:?{SP}*(.*)")
_PAYMENT_ROW = re.compile(rf"(?is){SP}*metode pembayaran{SP}*:?{SP}*(.*)")
_SELECT_ALL_RE = re.compile(rf"(?i){SP}*(pilih{SP}+)?semua({SP}*\(\d+\))?{SP}*")
_HAS_RP = re.compile(rf"Rp{SP}?\d", re.I)
# ShopeePay yang benar-benar dipilih: "ShopeePay", "Saldo ShopeePay", "ShopeePay (Rp150.000)".
# Tidak: "SPayLater", "ShopeePay Later", "ShopeePay (Saldo tidak cukup)", "ShopeePay + SPayLater".
_SHOPEEPAY_OK = re.compile(rf"(?i){SP}*(saldo{SP}+)?shopeepay"
                           rf"({SP}*[(\-]{SP}*(saldo{SP}*:?{SP}*)?rp{SP}?[\d.]+{SP}*\)?)?{SP}*")
_CARD_LINES = 6  # tinggi maksimum kartu produk checkout, dalam kelipatan tinggi penanda "x1"
_COLUMN_SLACK_PX = 12  # toleransi kolom nama produk terhadap kolom harga


def is_price(n: Node) -> bool:
    return _PRICE_RE.fullmatch(n.label) is not None


def is_qty(n: Node) -> bool:
    return _QTY_RE.fullmatch(n.label) is not None


def has_rp(n: Node) -> bool:
    return _HAS_RP.search(n.label) is not None


def is_shopeepay(value: str | None) -> bool:
    return value is not None and _SHOPEEPAY_OK.fullmatch(value) is not None


def vcenter(n: Node) -> float:
    return (n.bounds[1] + n.bounds[3]) / 2


def same_row(a: Node, b: Node) -> bool:
    """Tumpang-tindih vertikal >= 50% dari tinggi yang lebih kecil."""
    overlap = min(a.bounds[3], b.bounds[3]) - max(a.bounds[1], b.bounds[1])
    smaller = min(a.height, b.height)
    return smaller > 0 and overlap >= 0.5 * smaller


def pick_main_price(nodes: Iterable[Node]) -> Node | None:
    """Nominal Rp (tunggal/rentang) dengan tinggi teks terbesar; seri -> yang paling atas. Di luar hot path."""
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


def label_values(nodes: list[Node], row_re: re.Pattern) -> list[str]:
    """Teks nilai setiap label baris (`row_re` cocok seluruh teks label, grup terakhir = nilai inline),
    atau node nilai yang berpasangan (kanan/bawah, bukan tombol, bukan label lain).

    Nilai yang belum ada (mis. ongkir "Menghitung...") dikembalikan apa adanya, sehingga
    pricing.consistent_amount gagal -> dianggap belum stabil/tidak terbaca.
    """
    out = []
    for n in nodes:
        m = row_re.fullmatch(n.label)
        if not m:
            continue
        rest = (m.group(m.lastindex) or "").strip(" :\n\t") if m.lastindex else ""
        if rest:
            out.append(rest)
            continue
        # nilai bukan tombol (mis. "Buat Pesanan" sebaris dengan "Total Pembayaran" di bar bawah)
        val = value_for_label(n, nodes, accept=lambda x: not row_re.fullmatch(x.label) and not x.clickable)
        out.append(val.label if val is not None else "")
    return out


def _by_rid(nodes: list[Node], rid_re: re.Pattern | None) -> list[str] | None:
    if rid_re is None:
        return None
    vals = [n.label for n in nodes if n.rid and rid_re.fullmatch(n.rid)]
    return vals or None


def checkout_snapshot(nodes: list[Node], total_rid: re.Pattern | None = None,
                      shipping_rid: re.Pattern | None = None) -> pricing.CheckoutSnapshot:
    """Bangun CheckoutSnapshot dari node teks layar checkout.

    Baris produk = satu per penanda kuantitas "xN". Teks baris = nama/variasi di kartu produk (di atas
    penanda, maks _CARD_LINES baris, HANYA di kolom produk: kiri >= kolom harga - toleransi, sehingga nama
    toko di header tidak ikut dicocokkan dengan expected_name) + harga satuan sebaris penanda + "xN".
    Total & ongkir: node ber-resource-id (testID, mis. labelTotalPayment) DITAMBAH pasangan label baris ->
    nilai di layar; semua harus konsisten.
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
        rp_nodes = [n for n in card if has_rp(n)]
        price_nodes = [n for n in rp_nodes if same_row(n, m)]
        if not price_nodes:  # harga di atas penanda (tata letak lain)
            price_nodes = sorted(rp_nodes, key=lambda n: -n.bounds[1])[:1]
        elif len(price_nodes) > 1:
            tallest = max(n.height for n in price_nodes)
            price_nodes = [n for n in price_nodes if n.height == tallest]
        col_left = min((n.bounds[0] for n in rp_nodes), default=m.bounds[0]) - _COLUMN_SLACK_PX
        names = [n.label for n in sorted(card, key=lambda n: (n.bounds[1], n.bounds[0]))
                 if not has_rp(n) and not is_qty(n) and n.bounds[0] >= col_left]
        rows.append("\n".join([*names, *(p.label for p in price_nodes), m.label.strip()]))
        prev_bottom = m.bounds[3]
    return pricing.CheckoutSnapshot(
        rows=rows,
        # testID DAN label sama-sama dibaca; semua nilai harus sama (pricing.consistent_amount) = fail-closed
        totals=[*(_by_rid(nodes, total_rid) or []), *label_values(nodes, _TOTAL_ROW)],
        shippings=[*(_by_rid(nodes, shipping_rid) or []), *label_values(nodes, _SHIPPING_ROW)],
        page_text="\n".join(n.label for n in sorted(nodes, key=lambda n: (n.bounds[1], n.bounds[0]))),
    )


def cart_rows(boxes: list[Node], nodes: list[Node]) -> list[tuple[pricing.CartRow, Node]]:
    """Baris item keranjang: (CartRow, node checkbox). Tiap teks di kanan checkbox ditempelkan ke checkbox
    yang pusat vertikalnya terdekat (nama item bisa berada di atas checkbox-nya). Baris tanpa nominal Rp
    (toko) dan baris "Pilih Semua" (teks sebaris yang persis "Semua"/"Pilih Semua") dilewati."""
    boxes = sorted(boxes, key=lambda b: b.bounds[1])
    if not boxes:
        return []
    groups: dict[int, list[Node]] = {i: [] for i in range(len(boxes))}
    for n in nodes:
        right_of = [i for i, b in enumerate(boxes) if n.bounds[0] >= b.bounds[2] - 2]
        if not right_of:
            continue
        i = min(right_of, key=lambda j: abs(vcenter(n) - vcenter(boxes[j])))
        groups[i].append(n)
    out = []
    for i, box in enumerate(boxes):
        band = sorted(groups[i], key=lambda n: (n.bounds[1], n.bounds[0]))
        text = "\n".join(n.label for n in band)
        select_all = any(same_row(n, box) and _SELECT_ALL_RE.fullmatch(n.label) for n in nodes)
        if select_all or not pricing.find_amounts(text):
            continue
        out.append((pricing.CartRow(text, box.checked or box.selected, pricing.parse_qty(text)), box))
    return out


def checkout_count(nodes: Iterable[Node]) -> list[int]:
    """Angka N pada tombol "Checkout (N)" di keranjang."""
    out = []
    for n in nodes:
        m = re.fullmatch(rf"{SP}*checkout{SP}*\({SP}*(\d+){SP}*\){SP}*", n.label, re.I)
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


def payment_value(nodes: list[Node], label_re: re.Pattern | None = None) -> str | None:
    """Metode pembayaran yang TERPILIH di checkout.

    1. Ada kontrol radio/checkbox (daftar metode inline): tepat satu yang tercentang -> label di kirinya
       pada baris yang sama; selain itu None (tidak pasti).
    2. Selain itu: baris yang DIAWALI "Metode Pembayaran" (bukan teks promo yang menyebutnya) -> nilai
       inline, atau node di kanan/bawahnya.
    """
    row_re = _PAYMENT_ROW if label_re is None else re.compile(rf"(?is){SP}*(?:{label_re.pattern}){SP}*:?{SP}*(.*)")
    radios = [n for n in nodes if "radio" in n.cls.lower() or n.cls.lower().endswith("checkbox")]
    if radios:
        checked = [r for r in radios if r.checked or r.selected]
        if len(checked) != 1:
            return None
        r = checked[0]
        left = [n for n in nodes if n is not r and n.label.strip() and same_row(n, r)
                and n.bounds[2] <= r.bounds[0] + 2]
        return max(left, key=lambda n: n.bounds[2]).label if left else None
    for n in nodes:
        m = row_re.fullmatch(n.label)
        if not m:
            continue
        rest = (m.group(1) or "").strip(" :\n\t")
        if rest:
            return rest
        val = value_for_label(n, nodes, accept=lambda x: x.label.strip() != "" and not row_re.fullmatch(x.label))
        return val.label if val is not None else None
    return None
