"""Selector jalur Android (bagian `android` di selectors.json).

Tiap langkah punya kandidat terurut: resourceId -> text -> textContains -> description
(jenis lain: textStartsWith/textMatches setelah textContains, descriptionContains/Matches setelah
description). Hasil kalibrasi ditaruh di depan kandidat default sejenis; default berbasis teks
Bahasa Indonesia selalu ada sebagai cadangan. `{variant}` diganti teks variasi dari config.

Format selectors.json:
    "android": {
      "steps":   {"buy_button": [{"resourceId": "com.shopee.id:id/buy"}, {"text": "Beli Sekarang"}], ...},
      "markers": {"captcha": ["(?is).*geser.*"], ...},   # regex tambahan (textMatches, cocok seluruh teks)
      "urls":    {"address_page": "https://shopee.co.id/user/account/address", ...},
      "calibrated": {...}
    }
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flashbuy.android_driver import SEL_KINDS, Sel

PLATFORM = "android"

# Urutan prioritas jenis selector (angka kecil dicoba dulu).
KIND_RANK = {"resourceId": 0, "resourceIdMatches": 0, "text": 1, "textContains": 2, "textStartsWith": 2,
             "textMatches": 2, "description": 3, "descriptionContains": 4, "descriptionMatches": 4, "className": 5}

# testID React Native di aplikasi Shopee ID muncul sebagai resource-id mentah (tanpa "com.shopee.id:id/").
# Hanya testID yang terverifikasi di app ID yang jadi default (labelTotalPayment, labelShippingFinalPrice,
# labelButtonCheckout); testID dari app TH (buttonProductBuyNow, buttonPlaceOrder, buttonCartPanelSubmit)
# dibiarkan untuk kalibrasi. Teks dicocokkan PERSIS: substring "Beli" tidak aman.
ANDROID_DEFAULT_STEPS: dict[str, list[dict]] = {
    # Harga utama di halaman produk. Kosong = heuristik (nominal Rp terlihat dengan tinggi teks terbesar).
    "product_price": [],
    "buy_button": [
        {"text": "Beli Sekarang"},
        {"description": "Beli Sekarang"},
    ],
    # Opsi variasi (di halaman produk atau bottom sheet). Persis: textContains bisa kena judul produk.
    "variant_option": [
        {"text": "{variant}"},
        {"description": "{variant}"},
    ],
    # Penanda bottom sheet variasi/kuantitas yang muncul setelah klik Beli.
    "sheet_marker": [
        {"text": "Jumlah"},
        {"text": "Kuantitas"},
        {"textMatches": "(?i)stok\\s*:?\\s*\\d+.*"},
    ],
    # Tombol konfirmasi di bottom sheet (teksnya sering sama: "Beli Sekarang").
    "sheet_confirm": [
        {"text": "Beli Sekarang"},
        {"text": "Konfirmasi"},
    ],
    "cart_marker": [
        {"textStartsWith": "Keranjang Saya"},
    ],
    "cart_checkout": [
        {"resourceIdMatches": "(.*:id/)?labelButtonCheckout"},
        {"textMatches": "Checkout(\\s*\\(\\d+\\))?"},
        {"textStartsWith": "Checkout"},
        {"description": "Checkout"},
    ],
    # Baris "Metode Pembayaran" di checkout (diklik untuk membuka daftar metode).
    "payment_change": [
        {"text": "Metode Pembayaran"},
        {"textContains": "Metode Pembayaran"},
    ],
    "payment_shopeepay": [
        {"text": "ShopeePay"},
        {"textMatches": "ShopeePay(\\s*\\(.*\\))?"},
        {"description": "ShopeePay"},
    ],
    # Tombol konfirmasi di daftar metode pembayaran (bila ada).
    "payment_confirm": [
        {"text": "Konfirmasi"},
        {"text": "OK"},
    ],
    "place_order": [
        {"text": "Buat Pesanan"},
        {"description": "Buat Pesanan"},
    ],
    # Nilai "Total Pembayaran" & ongkir di checkout (dibaca langsung bila ada; selain itu dipasangkan
    # dari label di layar).
    "checkout_total": [
        {"resourceIdMatches": "(.*:id/)?labelTotalPayment"},
    ],
    "checkout_shipping": [
        {"resourceIdMatches": "(.*:id/)?labelShippingFinalPrice"},
    ],
    # Layar PIN ShopeePay (id native terverifikasi di app TH; teks Indonesia belum terverifikasi).
    "pin_screen": [
        {"resourceIdMatches": ".*:id/(payment_password_field|keyboard_number_view)"},
    ],
}

# Regex penanda status layar (dicocokkan ke SELURUH teks satu elemen: textMatches/descriptionMatches).
ANDROID_DEFAULT_MARKERS: dict[str, list[str]] = {
    "captcha": [
        r"(?is).*\bcaptcha\b.*",
        r"(?is).*geser untuk (menyelesaikan|verifikasi|melanjutkan).*",
        r"(?is).*(bukan robot|puzzle|verifikasi keamanan).*",
    ],
    "verification": [
        r"(?is).*aktivitas (yang )?(tidak biasa|tidak wajar|mencurigakan).*",
        r"(?is).*(kode verifikasi|\botp\b|verifikasi (akun|identitas|diperlukan)).*",
    ],
    "login": [
        r"(?is)(log ?in|masuk)( dengan .*)?",
        r"(?is).*lupa (kata sandi|password).*",
    ],
    "pin": [
        r"(?is).*masukkan pin.*",
        r"(?is).*(pin shopeepay|shopeepay pin).*",
    ],
    "sold_out": [
        r"(?is)(stok )?habis",
        r"(?is).*(stok habis|terjual habis|habis terjual).*",
        # "Flash Sale berakhir dalam 01:59:59" = hitung mundur saat sale BERJALAN, bukan habis
        r"(?is).*flash sale (telah |sudah )?berakhir(?!\s*dalam).*",
    ],
    "not_started": [
        r"(?is).*(belum dimulai|segera hadir|dimulai dalam).*",
        r"(?is)ingatkan saya",
    ],
    "variant_required": [
        r"(?is).*(silakan|harap) pilih variasi.*",
    ],
    "error_toast": [
        r"(?is).*(terjadi kesalahan|coba lagi nanti|gagal memuat).*",
    ],
}

ANDROID_DEFAULT_URLS: dict[str, str] = {
    # URL https yang dibuka lewat intent VIEW ke package Shopee (bila app tidak menanganinya -> tidak terbaca).
    "address_page": "https://shopee.co.id/user/account/address",
    "wallet_page": "https://shopee.co.id/user/shopeepay",
}


@dataclass
class AndroidSelectors:
    steps: dict[str, list[dict]] = field(default_factory=dict)
    markers: dict[str, list[str]] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    calibrated: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    def candidates(self, step: str, variant: str | None = None) -> list[Sel]:
        out: list[Sel] = []
        for cand in self.steps.get(step, []):
            sel = to_sel(cand, variant)
            if sel is not None and sel not in out:
                out.append(sel)
        return out

    def marker(self, name: str) -> list[str]:
        return self.markers.get(name, [])

    def marker_re(self, name: str) -> re.Pattern | None:
        """Satu regex gabungan untuk sebuah penanda (untuk cek lokal teks node)."""
        pats = self.marker(name)
        return re.compile(union(pats)) if pats else None


_LEADING_FLAGS = re.compile(r"(?:\(\?[imsux]+\))+")


def scoped(pattern: str) -> str:
    """`(?is)X` / `(?i)(?s)X` -> `(?is:X)` supaya bisa digabung dengan `|` (valid di regex Java & Python)."""
    m = _LEADING_FLAGS.match(pattern)
    if not m:
        return f"(?:{pattern})"
    flags = "".join(sorted(set(re.sub(r"[()?]", "", m.group(0)))))
    return f"(?{flags}:{pattern[m.end():]})"


def union(patterns: list[str]) -> str:
    return "|".join(scoped(p) for p in patterns)


def text_regex(sel: Sel) -> str | None:
    """Selector jenis teks -> regex `textMatches` setara (untuk digabung jadi satu query). Lainnya None."""
    v = sel.value
    if sel.by == "text":
        return re.escape(v)
    if sel.by == "textContains":
        return f"(?s:.*{re.escape(v)}.*)"
    if sel.by == "textStartsWith":
        return f"(?s:{re.escape(v)}.*)"
    if sel.by == "textMatches":
        return v
    return None


def to_sel(cand: dict, variant: str | None = None) -> Sel | None:
    keys = [k for k in cand if k in SEL_KINDS]
    if len(keys) != 1:
        raise ValueError(f"kandidat selector Android harus punya tepat satu jenis {SEL_KINDS}: {cand}")
    by = keys[0]
    value = str(cand[by])
    if "{variant}" in value:
        if not variant:
            return None
        value = value.replace("{variant}", re.escape(variant) if by.endswith("Matches") else variant)
    return Sel(by, value)


def order_candidates(calibrated: list[dict], defaults: list[dict]) -> list[dict]:
    """Gabung: urut jenis (resourceId -> text -> textContains -> description); hasil kalibrasi
    di depan default sejenis; duplikat dibuang."""
    merged: list[tuple[int, int, int, dict]] = []
    seen: list[dict] = []
    for origin, cands in ((0, calibrated), (1, defaults)):
        for i, c in enumerate(cands):
            if c in seen:
                continue
            seen.append(c)
            kind = next((k for k in c if k in SEL_KINDS), "className")
            merged.append((KIND_RANK.get(kind, 9), origin, i, c))
    return [c for *_, c in sorted(merged, key=lambda t: t[:3])]


def defaults() -> AndroidSelectors:
    return AndroidSelectors(steps=copy.deepcopy(ANDROID_DEFAULT_STEPS),
                            markers=copy.deepcopy(ANDROID_DEFAULT_MARKERS),
                            urls=dict(ANDROID_DEFAULT_URLS))


def load(path: str | Path) -> AndroidSelectors:
    path = Path(path)
    sel = defaults()
    if not path.exists():
        return sel
    data = json.loads(path.read_text(encoding="utf-8"))
    section = data.get(PLATFORM, {})
    for step, cands in section.get("steps", {}).items():
        for c in cands:
            sel_ = to_sel(c, "x")  # validasi format (+ regex bisa dikompilasi)
            if sel_ is not None and sel_.by.endswith("Matches"):
                try:
                    re.compile(sel_.value)
                except re.error as e:
                    raise ValueError(f"selectors.json android.steps.{step}: regex tidak valid {sel_.value!r}: {e}") \
                        from None
        sel.steps[step] = order_candidates(list(cands), sel.steps.get(step, []))
    for name, pats in section.get("markers", {}).items():
        for p in pats:
            try:
                re.compile(union([p]))
            except re.error as e:
                raise ValueError(f"selectors.json android.markers.{name}: regex tidak valid {p!r}: {e}") from None
        sel.markers[name] = [*pats, *[p for p in sel.markers.get(name, []) if p not in pats]]
    sel.urls.update(section.get("urls", {}))
    sel.calibrated = section.get("calibrated", {})
    sel.source = path
    return sel


def save(path: str | Path, steps: dict[str, list[dict]], meta: dict[str, Any],
         urls: dict[str, str] | None = None) -> Path | None:
    """Simpan hasil kalibrasi Android (bagian lain file dibiarkan). Versi lama di-backup."""
    path = Path(path)
    data: dict[str, Any] = {"version": 1}
    backup = None
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    section = data.setdefault(PLATFORM, {})
    section["steps"] = steps
    section["urls"] = {**section.get("urls", {}), **(urls or {})}
    section["calibrated"] = meta
    section.setdefault("markers", {})
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return backup
