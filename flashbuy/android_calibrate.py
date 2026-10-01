"""Kalibrasi selector Android: pengguna mengoperasikan HP sendiri, alat hanya MEMBACA layar.

Alat tidak pernah men-tap apa pun selama kalibrasi (tidak ada click/press/swipe/intent), jadi "Buat Pesanan"
tidak mungkin tertekan oleh alat. Tiap langkah: pengguna membuka layar yang diminta lalu menekan Enter; alat
membaca hierarki (dump), menampilkan kandidat elemen bernomor (teks/resourceId/bounds) yang cocok dengan kata
kunci langkah, pengguna memilih nomornya; alat menyusun selector terurut (resourceId -> text -> textContains ->
description), memverifikasi tiap selector UNIK (tepat satu elemen, elemen yang dipilih), lalu menyimpannya
bersama versi aplikasi Shopee & resolusi layar. "Buat Pesanan" diverifikasi dari dump saja (tanpa query ke
device).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from flashbuy.android_driver import AndroidDriver, Node, Sel, node_matches
from flashbuy.android_screen import is_price, parse_dump
from flashbuy.android_selectors import AndroidSelectors, order_candidates

MAX_LISTED = 15  # kandidat yang ditampilkan per langkah


@dataclass(frozen=True)
class CalStep:
    key: str
    instruction: str
    keywords: tuple[str, ...] = ()  # kata kunci (huruf kecil) di teks/desc/resourceId; "{variant}" = variasi config
    optional: bool = False
    dynamic_text: bool = False  # teks berubah-ubah (harga): hanya resourceId/description yang disimpan
    needs_variant: bool = False
    dump_only: bool = False  # diverifikasi dari dump saja (tidak ada query ke device untuk elemen ini)


ANDROID_CAL_STEPS: list[CalStep] = [
    CalStep("buy_button", "Buka halaman PRODUK BIASA yang murah di aplikasi Shopee. Tombol 'Beli Sekarang' terlihat.",
            ("beli sekarang", "beli", "buy")),
    CalStep("product_price", "Masih di halaman produk: harga utama terlihat.", ("rp", "price", "harga"),
            optional=True, dynamic_text=True),
    CalStep("sheet_marker", "Tap 'Beli Sekarang' SENDIRI sampai pilihan variasi/jumlah muncul (jangan konfirmasi). "
                            "Elemen: tulisan 'Jumlah'/'Kuantitas'.", ("jumlah", "kuantitas", "stok", "qty"),
            optional=True),
    CalStep("variant_option", "Masih di pilihan variasi: opsi variasi dari config terlihat.", ("{variant}",),
            optional=True, needs_variant=True),
    CalStep("sheet_confirm", "Masih di pilihan variasi: tombol konfirmasi ('Beli Sekarang').",
            ("beli sekarang", "konfirmasi", "beli"), optional=True),
    CalStep("place_order", "Tap konfirmasi SENDIRI sampai halaman Checkout. Elemen: tombol 'Buat Pesanan' - "
                           "JANGAN DITEKAN.", ("buat pesanan", "pesan", "checkout", "order"), dump_only=True),
    CalStep("payment_change", "Masih di checkout: baris 'Metode Pembayaran'.", ("metode pembayaran", "pembayaran"),
            optional=True),
    CalStep("payment_shopeepay", "Tap 'Metode Pembayaran' SENDIRI sampai daftar metode terlihat. Elemen: 'ShopeePay'.",
            ("shopeepay",)),
    CalStep("payment_confirm", "Masih di daftar metode: tombol 'Konfirmasi' (bila ada).", ("konfirmasi", "ok"),
            optional=True),
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


def describe(node: Node) -> str:
    """Baris daftar kandidat: teks/desc/resourceId/bounds (+ status)."""
    parts = []
    if node.text:
        parts.append(f"teks={node.text[:40]!r}")
    if node.desc:
        parts.append(f"desc={node.desc[:40]!r}")
    if node.rid:
        parts.append(f"id={node.rid}")
    left, top, right, bottom = node.bounds
    parts.append(f"bounds=[{left},{top}][{right},{bottom}]")
    if node.clickable:
        parts.append("klik")
    if not node.enabled:
        parts.append("nonaktif")
    return " ".join(parts)


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
        self.say("Kalibrasi Android: Anda yang men-tap HP; alat hanya membaca layar (tidak pernah mengklik). "
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

    def device_meta(self) -> dict[str, str]:
        """Versi aplikasi Shopee & resolusi layar saat kalibrasi (dibandingkan saat precheck)."""
        package = getattr(self.d, "package", None) or "com.shopee.id"
        ver = re.search(r"versionName=(\S+)", self.d.shell(["dumpsys", "package", package]))
        size = self.d.shell(["wm", "size"]).strip().replace("\n", "; ")
        return {"app_version": ver.group(1) if ver else "", "wm_size": size}

    def _calibrate(self, step: CalStep, res: AndroidCalResult) -> list[dict]:
        while True:
            answer = self.prompt(f"[{step.key}] {step.instruction}\n  Enter bila siap (atau 'lewati'): ").strip()
            if answer.lower() in ("lewati", "skip", "s"):
                return []
            nodes = parse_dump(self.d.dump())
            node = self._choose(step, nodes)
            if node is None:
                continue
            if isinstance(node, str):  # "skip"
                return []
            cands = self._verify(step, node, nodes, candidates_for(node, variant=self.variant,
                                                                    dynamic_text=step.dynamic_text), res)
            if cands:
                self.say(f"  {step.key}: {', '.join(str(c) for c in cands)}")
                return cands
            self.say(f"  {step.key}: tidak ada selector UNIK untuk elemen itu; pilih elemen lain, ulangi, "
                     "atau 'lewati'.")

    def _listed(self, step: CalStep, nodes: list[Node], query: str | None = None) -> list[Node]:
        """Kandidat: cocok selector default langkah dulu, lalu kata kunci (atau teks ketikan) di teks/desc/id."""
        if query is not None:
            keys = (query.lower(),)
        else:
            keys = tuple(k.replace("{variant}", (self.variant or "").lower()) for k in step.keywords)
        out: list[Node] = []
        if query is None:
            for sel in self.sel.candidates(step.key, self.variant):
                out += [n for n in nodes if node_matches(n, sel) and n not in out]
        for n in nodes:
            hay = f"{n.text}\n{n.desc}\n{n.rid}".lower()
            if n not in out and (n.text or n.desc or n.rid) and any(k and k in hay for k in keys):
                out.append(n)
        if step.dynamic_text and query is None:
            out = [n for n in out if n.rid or n.desc or is_price(n)]
        return self._rank(step, out)[:MAX_LISTED]

    def _rank(self, step: CalStep, nodes: list[Node]) -> list[Node]:
        """Elemen di jendela AKTIF dulu (mis. konfirmasi bottom sheet, bukan tombol Beli halaman produk di
        belakangnya yang bertulisan sama). Hanya query baca; langkah dump_only tidak menyentuh device."""
        if step.dump_only or len(nodes) < 2:
            return nodes
        cache: dict[Sel, list[Node]] = {}

        def active(n: Node) -> bool:
            for by, value in (("text", n.text), ("description", n.desc)):
                if value:
                    sel = Sel(by, value)
                    if sel not in cache:
                        cache[sel] = self.d.find_all(sel)
                    if any(f.bounds == n.bounds for f in cache[sel]):
                        return True
            return False

        flags = [active(n) for n in nodes]
        return [n for _, n in sorted(zip(flags, nodes, strict=True), key=lambda t: not t[0])]

    def _choose(self, step: CalStep, nodes: list[Node]) -> Node | str | None:
        """Tampilkan kandidat bernomor; pengguna memilih nomor. None = ambil dump ulang."""
        listed = self._listed(step, nodes)
        while True:
            if listed:
                self.say(f"  Kandidat {step.key}:")
                for i, n in enumerate(listed, 1):
                    self.say(f"   {i:>2}. {describe(n)}")
            else:
                self.say(f"  Tidak ada elemen yang cocok dengan kata kunci {step.key}.")
            answer = self.prompt("  Nomor elemen (Enter = 1; teks lain = cari teks itu; 'u' = baca ulang layar; "
                                 "'lewati'): ").strip()
            low = answer.lower()
            if low in ("lewati", "skip", "s"):
                return "skip"
            if low in ("u", "ulang"):
                return None
            if not answer and listed:
                return listed[0]
            if answer.isdigit() and 1 <= int(answer) <= len(listed):
                return listed[int(answer) - 1]
            if answer and not answer.isdigit():
                listed = self._listed(step, nodes, query=answer)
                continue
            self.say("  Pilihan tidak valid.")

    def _verify(self, step: CalStep, node: Node, nodes: list[Node], cands: list[dict],
                res: AndroidCalResult) -> list[dict]:
        """Simpan hanya selector yang UNIK: tepat satu elemen cocok dan itu elemen yang dipilih, di dump (semua
        jendela, seperti query info runner) DAN - kecuali langkah dump_only - di device (query baca, jendela aktif).
        Contoh: teks 'Beli Sekarang' konfirmasi sheet juga ada di tombol halaman produk di belakangnya -> dibuang,
        resource-id konfirmasi yang unik disimpan."""
        ok = []
        for c in cands:
            by, value = next(iter(c.items()))
            sel = Sel(by, value.replace("{variant}", self.variant or ""))
            checks = [("dump", [n for n in nodes if node_matches(n, sel)])]
            if not step.dump_only:
                checks.append(("device", self.d.find_all(sel)))
            for where, found in checks:
                if not found or all(f.bounds != node.bounds for f in found):
                    res.warnings.append(f"{step.key}: {sel} tidak menunjuk elemen yang dipilih ({where}), dibuang")
                    break
                if len(found) > 1:
                    res.warnings.append(f"{step.key}: {sel} tidak unik ({len(found)} elemen di {where}), dibuang")
                    break
            else:
                ok.append(c)
        return order_candidates(ok, [])
