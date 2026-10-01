"""Kalibrasi selector Android: pengguna mengoperasikan HP sendiri, alat hanya MEMBACA layar.

Alat tidak pernah men-tap apa pun selama kalibrasi, jadi "Buat Pesanan" tidak mungkin tertekan
oleh alat. Tiap langkah: pengguna membuka layar yang diminta lalu menekan Enter; alat membaca
hierarki (dump, boleh di kalibrasi), mencari elemen lewat kandidat default atau teks yang diketik
pengguna, menyusun kandidat terurut (resourceId -> text -> textContains -> description), dan
memverifikasi tiap kandidat di device (query sungguhan) sebelum disimpan ke selectors.json.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from flashbuy.android_driver import AndroidDriver, Node, Sel, node_matches
from flashbuy.android_screen import is_price, parse_dump
from flashbuy.android_selectors import AndroidSelectors, order_candidates


@dataclass(frozen=True)
class CalStep:
    key: str
    instruction: str
    optional: bool = False
    dynamic_text: bool = False  # teks berubah-ubah (harga): hanya resourceId/description yang disimpan
    needs_variant: bool = False


ANDROID_CAL_STEPS: list[CalStep] = [
    CalStep("buy_button", "Buka halaman PRODUK BIASA yang murah di aplikasi Shopee. Tombol 'Beli Sekarang' terlihat."),
    CalStep("product_price", "Masih di halaman produk: harga utama terlihat.", optional=True, dynamic_text=True),
    CalStep("sheet_marker", "Tap 'Beli Sekarang' SENDIRI sampai pilihan variasi/jumlah muncul (jangan konfirmasi). "
                            "Elemen: tulisan 'Jumlah'/'Kuantitas'.", optional=True),
    CalStep("variant_option", "Masih di pilihan variasi: opsi variasi dari config terlihat.", optional=True,
            needs_variant=True),
    CalStep("sheet_confirm", "Masih di pilihan variasi: tombol konfirmasi ('Beli Sekarang').", optional=True),
    CalStep("place_order", "Tap konfirmasi SENDIRI sampai halaman Checkout. Elemen: tombol 'Buat Pesanan' - "
                           "JANGAN DITEKAN."),
    CalStep("payment_change", "Masih di checkout: baris 'Metode Pembayaran'.", optional=True),
    CalStep("payment_shopeepay", "Tap 'Metode Pembayaran' SENDIRI sampai daftar metode terlihat. Elemen: 'ShopeePay'."),
    CalStep("payment_confirm", "Masih di daftar metode: tombol 'Konfirmasi' (bila ada).", optional=True),
]


@dataclass
class AndroidCalResult:
    steps: dict[str, list[dict]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def candidates_for(node: Node, *, variant: str | None, dynamic_text: bool) -> list[dict]:
    """Kandidat dari satu node, urut resourceId -> text -> textContains -> description."""
    out: list[dict] = []
    if node.rid:
        out.append({"resourceId": node.rid})
    text = node.text.strip()
    if text and not dynamic_text:
        if variant and text == variant:
            out.append({"text": "{variant}"})
        else:
            out.append({"text": node.text})
            if len(text) > 12:
                out.append({"textContains": text[:12]})
    if node.desc and not (dynamic_text and is_price(Node(text=node.desc))):
        out.append({"description": node.desc})
    return out


class AndroidCalibrator:
    def __init__(self, driver: AndroidDriver, selectors: AndroidSelectors, *, variant: str | None,
                 prompt: Callable[[str], str] = input, say: Callable[[str], None] = print):
        self.d = driver
        self.sel = selectors
        self.variant = variant
        self.prompt = prompt
        self.say = say

    def run(self) -> AndroidCalResult:
        res = AndroidCalResult()
        self.say("Kalibrasi Android: Anda yang men-tap HP; alat hanya membaca layar. "
                 "Ketik 'lewati' untuk melewati langkah opsional.")
        for step in ANDROID_CAL_STEPS:
            if step.needs_variant and not self.variant:
                res.skipped.append(step.key)
                continue
            cands = self._calibrate(step, res)
            if cands:
                res.steps[step.key] = cands
            elif step.optional:
                res.skipped.append(step.key)
            else:
                res.warnings.append(f"{step.key}: tidak terekam; runner memakai default teks")
        return res

    def _calibrate(self, step: CalStep, res: AndroidCalResult) -> list[dict]:
        while True:
            answer = self.prompt(f"[{step.key}] {step.instruction}\n  Enter bila siap (atau 'lewati'): ").strip()
            if answer.lower() in ("lewati", "skip", "s"):
                return []
            nodes = parse_dump(self.d.dump())
            node = self._default_hit(step, nodes)
            if node is None:
                typed = self.prompt(f"  Elemen {step.key} tidak dikenali otomatis. Ketik teks/desc yang terlihat "
                                    "persis (kosong = ulangi): ").strip()
                if not typed:
                    continue
                node = self._typed_hit(typed, nodes)
                if node is None:
                    self.say(f"  Tidak ada elemen bertulisan {typed!r} di layar; ulangi.")
                    continue
            cands = self._verify(step, node, candidates_for(node, variant=self.variant,
                                                            dynamic_text=step.dynamic_text), res)
            if cands:
                self.say(f"  {step.key}: {', '.join(str(c) for c in cands)}")
                return cands
            self.say(f"  {step.key}: tidak ada kandidat unik untuk elemen itu; ulangi atau 'lewati'.")

    def _active(self, hits: list[Node]) -> Node:
        """Beberapa elemen cocok (mis. konfirmasi sheet & tombol Beli di belakangnya bertulisan sama): pilih
        yang ada di jendela AKTIF (model yang sama dengan verifikasi & runner), paling bawah di layar."""
        if len(hits) == 1:
            return hits[0]
        active = []
        for n in hits:
            for by, value in (("text", n.text), ("description", n.desc)):
                if value and any(f.bounds == n.bounds for f in self.d.find_all(Sel(by, value))):
                    active.append(n)
                    break
        pool = active or hits
        return max(enumerate(pool), key=lambda t: (t[1].bounds[3], t[0]))[1]

    def _default_hit(self, step: CalStep, nodes: list[Node]) -> Node | None:
        for sel in self.sel.candidates(step.key, self.variant):
            hits = [n for n in nodes if node_matches(n, sel)]
            if hits:
                return self._active(hits)
        return None

    def _typed_hit(self, typed: str, nodes: list[Node]) -> Node | None:
        exact = [n for n in nodes if typed in (n.text.strip(), n.desc.strip())]
        if exact:
            return self._active(exact)
        low = typed.lower()
        loose = [n for n in nodes if low in n.text.lower() or low in n.desc.lower()]
        return self._active(loose) if loose else None

    def _verify(self, step: CalStep, node: Node, cands: list[dict], res: AndroidCalResult) -> list[dict]:
        """Simpan hanya kandidat yang di device menemukan elemen yang sama sebagai hasil pertama."""
        ok = []
        for c in cands:
            by, value = next(iter(c.items()))
            sel = Sel(by, value.replace("{variant}", self.variant or ""))
            found = self.d.find_all(sel)
            if found and found[0].bounds == node.bounds:
                ok.append(c)
                if len(found) > 1:
                    res.warnings.append(f"{step.key}: {sel} cocok {len(found)} elemen (dipakai yang pertama)")
            else:
                res.warnings.append(f"{step.key}: kandidat {sel} tidak menunjuk elemen yang sama, dibuang")
        return order_candidates(ok, [])
