"""Klasifikasi status halaman (web) berbasis URL, selector, dan teks Bahasa Indonesia.

Aturan dievaluasi dalam SATU `page.evaluate` (satu round-trip) sesuai urutan prioritas.
Teks lemah (mis. "kode verifikasi", "stok habis") hanya dicari di overlay (dialog/alert/toast)
atau di halaman pendek (interstitial), supaya deskripsi produk dari penjual tidak memicu
false positive. Teks kuat (mis. "aktivitas tidak biasa") dicari di seluruh halaman.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from enum import StrEnum

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from flashbuy.runner_base import RunStatus
from flashbuy.selector_store import SelectorSet


class PageState(StrEnum):
    PRODUCT_WAITING = "PRODUCT_WAITING"
    PRODUCT_ACTIVE = "PRODUCT_ACTIVE"
    NOT_STARTED = "NOT_STARTED"
    VARIANT_REQUIRED = "VARIANT_REQUIRED"
    SOLD_OUT = "SOLD_OUT"
    CAPTCHA = "CAPTCHA"
    VERIFICATION = "VERIFICATION"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CART = "CART"
    CHECKOUT = "CHECKOUT"
    PIN_SCREEN = "PIN_SCREEN"
    UNKNOWN = "UNKNOWN"


# Status halaman yang langsung mengakhiri run.
TERMINAL: dict[PageState, RunStatus] = {
    PageState.CAPTCHA: RunStatus.CAPTCHA,
    PageState.VERIFICATION: RunStatus.VERIFICATION,
    PageState.LOGIN_REQUIRED: RunStatus.LOGIN_REQUIRED,
    PageState.SOLD_OUT: RunStatus.SOLD_OUT,
}

ORDER = [
    PageState.CAPTCHA, PageState.VERIFICATION, PageState.PIN_SCREEN, PageState.LOGIN_REQUIRED,
    PageState.SOLD_OUT, PageState.NOT_STARTED, PageState.VARIANT_REQUIRED,
    PageState.CHECKOUT, PageState.CART,
]

# url: regex ke URL main frame DAN src iframe terlihat berukuran signifikan (CAPTCHA/VERIFICATION).
# text/overlay_text: regex (case-insensitive). css: harus terlihat. button_text: teks persis.
# Pola URL bisa ditambah lewat selectors.json -> web.guards.{CAPTCHA|VERIFICATION}.url
DEFAULT_RULES: dict[str, dict[str, list[str]]] = {
    "CAPTCHA": {
        "url": [r"captcha"],
        "text": [r"geser untuk (menyelesaikan|verifikasi)", r"bukan robot", r"selesaikan puzzle"],
        "overlay_text": [r"verifikasi keamanan", r"\bcaptcha\b", r"geser"],
        "css": ["[id*='captcha' i]", "[class*='captcha' i]"],
    },
    "VERIFICATION": {
        "url": [r"verify", r"traffic"],
        "text": [r"aktivitas (yang )?tidak biasa", r"aktivitas mencurigakan"],
        "overlay_text": [r"kode verifikasi", r"masukkan (kode )?otp", r"verifikasi (akun|identitas|diperlukan)"],
    },
    "PIN_SCREEN": {
        "text": [r"masukkan pin"],
    },
    "LOGIN_REQUIRED": {
        "url": [],  # diisi dari selectors urls.login_pattern
    },
    "SOLD_OUT": {
        "overlay_text": [r"stok habis", r"flash sale (telah |sudah )?berakhir", r"produk (ini )?(sudah )?habis",
                         r"terjual habis"],
        "button_text": ["habis", "stok habis", "terjual habis"],
    },
    "NOT_STARTED": {
        "text": [r"flash sale belum dimulai"],
        "overlay_text": [r"belum dimulai"],
    },
    "VARIANT_REQUIRED": {
        "text": [r"silakan pilih variasi"],
        "overlay_text": [r"pilih variasi"],
    },
    "CHECKOUT": {"url": []},  # dari urls.checkout_pattern
    "CART": {"url": []},  # dari urls.cart_pattern
}

_CLASSIFY_JS = r"""
(cfg) => {
  const vis = (el) => {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };
  const url = location.href;
  const bigVisible = (f) => {
    if (!vis(f)) return false;
    const r = f.getBoundingClientRect();
    return r.width >= cfg.minIframe && r.height >= cfg.minIframe;
  };
  const body = document.body;
  const text = body ? body.innerText : '';
  let overlay = null;
  const overlayText = () => {
    if (overlay !== null) return overlay;
    const parts = [];
    document.querySelectorAll('[role=dialog],[role=alertdialog],[role=alert],[aria-modal=true],[aria-live]')
      .forEach((el) => { if (vis(el)) parts.push(el.innerText || ''); });
    if (text.length < cfg.shortPage) parts.push(text);
    overlay = parts.join('\n');
    return overlay;
  };
  let buttons = null;
  const buttonTexts = () => {
    if (buttons !== null) return buttons;
    buttons = [];
    document.querySelectorAll('button,[role=button]').forEach((b) => {
      if (vis(b)) buttons.push((b.innerText || '').trim().toLowerCase());
    });
    return buttons;
  };
  for (const state of cfg.order) {
    const r = cfg.rules[state];
    if (!r) continue;
    for (const p of r.url || []) {
      const re = new RegExp(p, 'i');
      if (re.test(url)) return [state, 'url ' + p];
      if (cfg.frameStates.includes(state)) {
        for (const f of document.querySelectorAll('iframe')) {
          const src = f.src || '';
          if (src && re.test(src) && bigVisible(f)) return [state, 'iframe ' + src.slice(0, 100)];
        }
      }
    }
    for (const p of r.text || []) {
      const m = text.match(new RegExp(p, 'i'));
      if (m) return [state, 'teks "' + m[0] + '"'];
    }
    for (const p of r.overlay_text || []) {
      const m = overlayText().match(new RegExp(p, 'i'));
      if (m) return [state, 'overlay "' + m[0] + '"'];
    }
    for (const c of r.css || []) {
      for (const el of document.querySelectorAll(c)) if (vis(el)) return [state, 'css ' + c];
    }
    for (const t of r.button_text || []) if (buttonTexts().includes(t)) return [state, 'tombol "' + t + '"'];
  }
  return null;
}
"""

# Tombol dianggap aktif jika tidak disabled (atribut, aria, atau class 'disabled').
ENABLED_JS = r"""el => el.isConnected && !el.disabled && el.getAttribute('aria-disabled') !== 'true'
  && !/(^|[\s_-])disabled([\s_-]|$)/i.test(typeof el.className === 'string' ? el.className : '')"""

SHORT_PAGE_CHARS = 1500
MIN_IFRAME_PX = 60  # iframe tracking kecil/tersembunyi diabaikan
FRAME_STATES = (PageState.CAPTCHA, PageState.VERIFICATION)


@dataclass(frozen=True)
class Classification:
    state: PageState
    evidence: str = ""

    @property
    def terminal_status(self) -> RunStatus | None:
        return TERMINAL.get(self.state)


def build_rules(sel: SelectorSet) -> dict:
    rules = copy.deepcopy(DEFAULT_RULES)
    rules["LOGIN_REQUIRED"]["url"].append(sel.urls["login_pattern"])
    rules["CHECKOUT"]["url"].append(sel.urls["checkout_pattern"])
    rules["CART"]["url"].append(sel.urls["cart_pattern"])
    for state, extra in sel.guards.items():
        for kind, patterns in extra.items():
            rules.setdefault(state, {}).setdefault(kind, []).extend(patterns)
    return {"order": [str(s) for s in ORDER], "rules": rules, "shortPage": SHORT_PAGE_CHARS,
            "minIframe": MIN_IFRAME_PX, "frameStates": [str(s) for s in FRAME_STATES]}


class Guard:
    def __init__(self, sel: SelectorSet):
        self.payload = build_rules(sel)
        self._url_rules = [(state, [re.compile(p, re.I) for p in self.payload["rules"][str(state)]["url"]])
                           for state in FRAME_STATES]

    def url_state(self, url: str) -> PageState | None:
        """CAPTCHA/VERIFICATION dari URL (main frame atau iframe) tanpa round-trip ke halaman."""
        for state, patterns in self._url_rules:
            if any(p.search(url or "") for p in patterns):
                return state
        return None

    async def classify(self, page: Page, buy: Locator | None = None) -> Classification:
        try:
            hit = await page.evaluate(_CLASSIFY_JS, self.payload)
        except PlaywrightError as e:  # biasanya sedang navigasi
            return Classification(PageState.UNKNOWN, f"evaluate gagal: {e.message.splitlines()[0][:80]}")
        if hit:
            return Classification(PageState(hit[0]), hit[1])
        if buy is not None:
            try:
                if await buy.count():
                    enabled = await buy.evaluate(ENABLED_JS, timeout=1000)
                    return Classification(PageState.PRODUCT_ACTIVE if enabled else PageState.PRODUCT_WAITING,
                                          "tombol Beli")
            except PlaywrightError:
                pass
        return Classification(PageState.UNKNOWN)
