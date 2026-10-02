"""selectors.json: kandidat selector per langkah, terurut (role/teks dulu, CSS terakhir).

Format kandidat (salah satu kunci utama):
    {"role": "button", "name": "Beli Sekarang", "exact": false}
    {"text": "Beli Sekarang", "exact": false}
    {"label": "Kuantitas"}
    {"css": "button.buy"}
`{variant}` di name/text/css diganti dengan teks variasi dari config.

Hasil kalibrasi ditaruh di depan; default berbasis teks Bahasa Indonesia selalu ditambahkan
di belakang sebagai cadangan.
"""

from __future__ import annotations

import copy
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page

DEFAULT_PATH = Path("selectors.json")
SCHEMA_VERSION = 1

WEB_DEFAULT_STEPS: dict[str, list[dict]] = {
    # Harga tampil di halaman produk. Kosong = heuristik (nominal non-coret dengan font terbesar).
    # Hanya kandidat {"css": ...} yang dipakai (dievaluasi di dalam halaman).
    "product_price": [],
    "buy_button": [
        {"role": "button", "name": "Beli Sekarang"},
        {"text": "Beli Sekarang", "exact": True},
    ],
    "variant_option": [
        {"role": "button", "name": "{variant}", "exact": True},
        {"role": "radio", "name": "{variant}", "exact": True},
        {"text": "{variant}", "exact": True},
    ],
    "quantity_input": [
        {"label": "Kuantitas"},
        {"role": "spinbutton"},
    ],
    "cart_checkout": [
        {"role": "button", "name": "Checkout"},
        {"role": "link", "name": "Checkout"},
        {"text": "Checkout", "exact": True},
    ],
    "payment_change": [],  # opsional; isi lewat kalibrasi bila daftar metode tersembunyi
    "payment_shopeepay": [
        {"role": "radio", "name": "ShopeePay"},
        {"role": "button", "name": "ShopeePay"},
        {"text": "ShopeePay"},
    ],
    "place_order": [
        {"role": "button", "name": "Buat Pesanan"},
        {"text": "Buat Pesanan", "exact": True},
    ],
    "sold_out": [
        {"text": "Stok habis"},
    ],
}

# CSS tata letak untuk pengaman harga. Kosong = heuristik (lihat web_js.py).
WEB_DEFAULT_LAYOUT: dict[str, list[str]] = {
    "cart_row": [],  # satu elemen per item keranjang
    "checkout_row": [],  # satu elemen per baris produk di checkout
    "checkout_total": [],  # elemen nominal "Total Pembayaran"
    "checkout_shipping": [],  # elemen nominal ongkos kirim
}

WEB_DEFAULT_URLS: dict[str, str] = {
    # regex (dicocokkan ke URL penuh)
    "cart_pattern": r"/cart(\b|/|\?|$)",
    "checkout_pattern": r"/checkout(\b|/|\?|$)",
    "login_pattern": r"/buyer/login|/login(\b|/|\?|$)",
    # path relatif origin; halaman ShopeePay asli perlu dikonfirmasi saat kalibrasi
    "address_page": "/user/account/address",
    "wallet_page": "/user/shopeepay",
    "login_page": "/buyer/login",
    "cart_page": "/cart",  # rehearsal: isi keranjang dibaca di akhir (tidak dihapus alat)
}


@dataclass
class SelectorSet:
    steps: dict[str, list[dict]] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    guards: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # tambahan aturan guard
    layout: dict[str, list[str]] = field(default_factory=dict)
    calibrated: dict[str, Any] = field(default_factory=dict)  # metadata kalibrasi
    source: Path | None = None

    def candidates(self, step: str) -> list[dict]:
        return self.steps.get(step, [])

    def css(self, step: str) -> list[str]:
        """Kandidat CSS saja (untuk dievaluasi langsung di halaman)."""
        return [c["css"] for c in self.candidates(step) if "css" in c]


def defaults() -> SelectorSet:
    return SelectorSet(steps=copy.deepcopy(WEB_DEFAULT_STEPS), urls=dict(WEB_DEFAULT_URLS),
                       layout=copy.deepcopy(WEB_DEFAULT_LAYOUT))


def _merge(primary: list[dict], fallback: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in [*primary, *fallback]:
        if c not in out:
            out.append(c)
    return out


def load(path: str | Path = DEFAULT_PATH, platform: str = "web") -> SelectorSet:
    """Baca selectors.json (bila ada) dan gabungkan dengan default."""
    path = Path(path)
    sel = defaults()
    if not path.exists():
        return sel
    data = json.loads(path.read_text(encoding="utf-8"))
    section = data.get(platform, {})
    for step, cands in section.get("steps", {}).items():
        sel.steps[step] = _merge(cands, sel.steps.get(step, []))
    sel.urls.update(section.get("urls", {}))
    for key, css in section.get("layout", {}).items():
        sel.layout[key] = list(css)
    sel.guards = section.get("guards", {})
    sel.calibrated = section.get("calibrated", {})
    sel.source = path
    return sel


def save(path: str | Path, platform: str, steps: dict[str, list[dict]], urls: dict[str, str],
         meta: dict[str, Any]) -> Path | None:
    """Simpan hasil kalibrasi untuk satu platform. Versi lama di-backup. Return path backup."""
    path = Path(path)
    data: dict[str, Any] = {"version": SCHEMA_VERSION}
    backup = None
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    section = data.setdefault(platform, {})
    section["steps"] = steps
    section["urls"] = {**section.get("urls", {}), **urls}
    section["calibrated"] = meta
    section.setdefault("guards", {})
    section.setdefault("layout", copy.deepcopy(WEB_DEFAULT_LAYOUT))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return backup


# --------------------------------------------------------------------------- Playwright


def _fill(value: str, variant: str | None) -> str | None:
    if "{variant}" in value:
        return None if not variant else value.replace("{variant}", variant)
    return value


def build_locator(page: Page, cand: dict, variant: str | None = None) -> Locator | None:
    exact = bool(cand.get("exact", False))
    if "role" in cand:
        name = cand.get("name")
        if name is not None and (name := _fill(name, variant)) is None:
            return None
        return page.get_by_role(cand["role"], name=name, exact=exact) if name else \
            page.get_by_role(cand["role"])
    if "text" in cand:
        text = _fill(cand["text"], variant)
        return None if text is None else page.get_by_text(text, exact=exact)
    if "label" in cand:
        label = _fill(cand["label"], variant)
        return None if label is None else page.get_by_label(label, exact=exact)
    if "css" in cand:
        css = _fill(cand["css"], variant)
        return None if css is None else page.locator(css)
    raise ValueError(f"kandidat selector tidak dikenal: {cand}")


async def resolve(page: Page, candidates: list[dict], variant: str | None = None
                  ) -> tuple[Locator, dict] | None:
    """Kandidat pertama yang menemukan elemen (prioritas elemen terlihat)."""
    for cand in candidates:
        loc = build_locator(page, cand, variant)
        if loc is None:
            continue
        n = await loc.count()
        if n == 0:
            continue
        if n > 1:
            visible = loc.filter(visible=True)
            if await visible.count() == 0:
                continue
            loc = visible
        return loc.first, cand
    return None
