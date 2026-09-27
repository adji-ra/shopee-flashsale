"""Mode kalibrasi web: rekam selector langkah demi langkah di produk biasa (bukan flash sale).

- Alt+klik  = REKAM elemen tanpa menjalankannya (preventDefault + stopPropagation di capture phase).
- Klik biasa = lanjut navigasi seperti biasa.
- Klik apa pun pada "Buat Pesanan" DIBLOKIR selama kalibrasi (termasuk Enter/Space/submit).
Kandidat selector (role+name, teks, atribut stabil, CSS) diverifikasi unik & menunjuk elemen
yang direkam, lalu disimpan ke selectors.json (versi lama di-backup).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from rich.console import Console

from flashbuy import selector_store


@dataclass(frozen=True)
class CalStep:
    key: str
    instruction: str
    optional: bool = False
    variant_template: bool = False
    url_key: str | None = None  # simpan pola URL halaman tempat elemen direkam


WEB_STEPS: list[CalStep] = [
    CalStep("variant_option", "Alt+klik salah satu PILIHAN VARIASI, lalu klik biasa untuk memilihnya. "
            "Lewati jika produk tanpa variasi.", optional=True, variant_template=True),
    CalStep("buy_button", "Alt+klik tombol 'Beli Sekarang'. Setelah terekam, klik biasa untuk lanjut."),
    CalStep("cart_checkout", "Alt+klik tombol 'Checkout' di keranjang, lalu klik biasa untuk lanjut. "
            "Lewati jika langsung masuk halaman checkout.", optional=True, url_key="cart_pattern"),
    CalStep("payment_change", "Jika opsi ShopeePay tersembunyi, Alt+klik tombol untuk membuka daftar "
            "metode pembayaran (mis. 'Ubah'), lalu klik biasa. Lewati jika ShopeePay sudah terlihat.",
            optional=True),
    CalStep("payment_shopeepay", "Alt+klik opsi 'ShopeePay' di Metode Pembayaran.", url_key="checkout_pattern"),
    CalStep("place_order", "Alt+klik tombol 'Buat Pesanan'. (Klik biasa pada tombol ini diblokir.)",
            url_key="checkout_pattern"),
    CalStep("sold_out", "Jika terlihat indikator stok habis, Alt+klik. Biasanya dilewati.", optional=True),
]

MAX_TRIES = 3

INIT_JS = r"""
(() => {
  if (window.__fbCalInstalled) return;
  window.__fbCalInstalled = true;
  const BLOCK_RE = /buat\s+pesanan/i;
  const INTERACTIVE = 'button,a,[role=button],[role=radio],[role=link],[role=option],[role=tab],' +
    '[role=checkbox],[role=menuitem],input,select,textarea,label,[tabindex]';
  const inOverlay = (el) => !!(el && el.closest && el.closest('#__fb_cal'));
  const pick = (el) => (el && el.closest && el.closest(INTERACTIVE)) || el;
  const blocked = (el) => {
    for (let e = el, i = 0; e && e.nodeType === 1 && i < 6; e = e.parentElement, i++) {
      if (e.hasAttribute('data-fb-blocked')) return e;
      const t = (e.innerText || e.value || '').trim();
      if (t && t.length < 40 && BLOCK_RE.test(t)) return e;
    }
    return null;
  };
  const implicitRole = (el) => {
    const r = el.getAttribute('role');
    if (r) return r.split(/\s+/)[0];
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      return {button: 'button', submit: 'button', reset: 'button', checkbox: 'checkbox', radio: 'radio',
              number: 'spinbutton', range: 'slider'}[t] || 'textbox';
    }
    return null;
  };
  const accName = (el) => {
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean)
        .map((e) => e.innerText.trim()).join(' ');
      if (t) return t;
    }
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) {
      if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
      return (el.getAttribute('placeholder') || el.getAttribute('title') || '').trim();
    }
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const cssPath = (el) => {
    const parts = [];
    let e = el;
    for (; e && e.nodeType === 1 && e !== document.body; e = e.parentElement) {
      if (e.id && /^[A-Za-z][\w-]*$/.test(e.id) && !/\d{3,}/.test(e.id)) {
        parts.unshift('#' + e.id);
        return parts.join(' > ');
      }
      let sel = e.tagName.toLowerCase();
      const parent = e.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === e.tagName);
        if (same.length > 1) sel += ':nth-of-type(' + (same.indexOf(e) + 1) + ')';
      }
      parts.unshift(sel);
    }
    parts.unshift('body');
    return parts.join(' > ');
  };
  const describe = (el) => {
    const attrs = {};
    for (const a of ['id', 'data-testid', 'data-test', 'data-qa', 'name', 'aria-label', 'type'])
      if (el.getAttribute(a)) attrs[a] = el.getAttribute(a);
    const own = Array.from(el.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent)
      .join(' ').replace(/\s+/g, ' ').trim();
    return {tag: el.tagName.toLowerCase(), role: implicitRole(el), name: accName(el), own,
            text: (el.innerText || '').trim(), attrs, css: cssPath(el), url: location.href};
  };
  const swallow = (ev) => { ev.preventDefault(); ev.stopPropagation(); ev.stopImmediatePropagation(); };
  let seq = 0;
  const onPointer = (ev) => {
    if (inOverlay(ev.target)) return;
    if (ev.altKey) {
      swallow(ev);
      if (ev.type === 'click') {
        const el = pick(ev.target);
        const token = 'r' + (++seq) + '-' + Date.now();
        el.setAttribute('data-fb-rec', token);
        el.style.outline = '3px dashed #e0f';
        if (window.__fbRecord) window.__fbRecord(Object.assign(describe(el), {token}));
      }
      return;
    }
    if (blocked(ev.target)) {
      swallow(ev);
      if (ev.type === 'click') note('Klik "Buat Pesanan" DIBLOKIR selama kalibrasi. Pakai Alt+klik.');
    }
  };
  for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'auxclick', 'dblclick',
                   'touchstart', 'touchend'])
    window.addEventListener(t, onPointer, {capture: true, passive: false});
  window.addEventListener('keydown', (ev) => {
    if ((ev.key === 'Enter' || ev.key === ' ') && blocked(document.activeElement)) swallow(ev);
  }, {capture: true});
  window.addEventListener('submit', (ev) => {
    if (ev.submitter && blocked(ev.submitter)) swallow(ev);
  }, {capture: true});

  let box = null;
  const ensureBox = () => {
    if (box && box.isConnected) return box;
    box = document.createElement('div');
    box.id = '__fb_cal';
    box.style.cssText = 'position:fixed;top:8px;right:8px;z-index:2147483647;max-width:360px;' +
      'background:#111e;color:#fff;font:13px/1.4 system-ui,sans-serif;padding:10px 12px;' +
      'border-radius:8px;pointer-events:none';
    (document.body || document.documentElement).appendChild(box);
    return box;
  };
  const note = (msg) => { const b = ensureBox(); const n = b.querySelector('.note'); if (n) n.textContent = msg; };
  const render = (s) => {
    if (!s) return;
    const b = ensureBox();
    b.dataset.step = s.key;
    b.innerHTML = '';
    const h = document.createElement('div');
    h.style.fontWeight = 'bold';
    h.textContent = 'flashbuy kalibrasi ' + (s.progress || '');
    const p = document.createElement('div');
    p.textContent = s.instruction || '';
    const n = document.createElement('div');
    n.className = 'note';
    n.style.color = '#fc6';
    n.textContent = s.note || '';
    b.append(h, p, n);
    if (s.optional) {
      const btn = document.createElement('button');
      btn.textContent = 'Lewati langkah ini';
      btn.id = '__fb_skip';
      btn.style.cssText = 'pointer-events:auto;margin-top:6px';
      btn.addEventListener('click', (ev) => { swallow(ev); if (window.__fbSkip) window.__fbSkip(s.key); });
      b.append(btn);
    }
  };
  window.__fbRender = render;
  const init = () => { if (window.__fbState) window.__fbState().then(render).catch(() => {}); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
"""

_RP = re.compile(r"Rp\s?[\d.,]+", re.I)
_DIGIT = re.compile(r"\d")


def stable_text(text: str) -> str:
    """Bagian teks yang stabil: tanpa nominal Rp, berhenti sebelum angka/kurung pertama."""
    t = _RP.sub(" ", text or "")
    t = re.split(r"[\d(]", t)[0]
    t = t.split("\n")[0]
    return re.sub(r"\s+", " ", t).strip(" :-·|")


def _stable_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][\w-]*", value)) and not re.search(r"\d{3,}", value)


def candidates_from(desc: dict, variant_template: bool = False) -> list[dict]:
    role = desc.get("role")
    name = (desc.get("name") or "").strip()
    first_line = (desc.get("text") or "").split("\n")[0].strip()
    out: list[dict] = []
    if variant_template:
        if role:
            out.append({"role": role, "name": "{variant}", "exact": True})
        out.append({"text": "{variant}", "exact": True})
    else:
        if role and name:
            if len(name) <= 60 and not _DIGIT.search(name):
                out.append({"role": role, "name": name, "exact": True})
            # teks milik elemen sendiri (tanpa anak seperti <small>Saldo Rp..</small>), lalu bagian stabil
            for part in (stable_text(desc.get("own") or ""), stable_text(name)):
                if len(part) >= 3 and part != name:
                    out.append({"role": role, "name": part})
        for t in (first_line, stable_text(first_line)):
            if 3 <= len(t) <= 60 and not _DIGIT.search(t):
                out.append({"text": t, "exact": True})
    attrs = desc.get("attrs") or {}
    for key in ("data-testid", "data-test", "data-qa"):
        if attrs.get(key):
            out.append({"css": f'[{key}="{attrs[key]}"]'})
    if not variant_template:
        if attrs.get("aria-label") and not _DIGIT.search(attrs["aria-label"]):
            out.append({"css": f'[aria-label="{attrs["aria-label"]}"]'})
        if attrs.get("id") and _stable_id(attrs["id"]):
            out.append({"css": f'[id="{attrs["id"]}"]'})
        if desc.get("css"):
            out.append({"css": desc["css"]})
    uniq: list[dict] = []
    for c in out:
        if c not in uniq:
            uniq.append(c)
    return uniq


_SAME_JS = """(el, token) => {
  const rec = document.querySelector('[data-fb-rec="' + token + '"]');
  return !!rec && (el === rec || rec.contains(el) || el.contains(rec));
}"""


async def verify_candidates(page: Page, cands: list[dict], desc: dict,
                            variant: str | None = None) -> list[dict]:
    """Pertahankan kandidat yang unik (tepat 1 elemen) dan menunjuk elemen yang direkam."""
    ok: list[dict] = []
    for cand in cands:
        loc = selector_store.build_locator(page, cand, variant)
        if loc is None:
            continue
        try:
            if await loc.count() != 1:
                continue
            if await loc.evaluate(_SAME_JS, desc["token"], timeout=2000):
                ok.append(cand)
        except PlaywrightError:
            continue
    return ok


def _url_pattern(url: str) -> str | None:
    seg = urlsplit(url).path.strip("/").split("/")[0]
    if not seg or "-i." in seg:  # halaman produk bukan pola cart/checkout
        return None
    return re.escape("/" + seg) + r"(\b|/|\?|$)"


@dataclass
class CalibrationResult:
    steps: dict[str, list[dict]] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class WebCalibrator:
    def __init__(self, context: BrowserContext, page: Page, url: str, *,
                 console: Console | None = None, steps: list[CalStep] | None = None):
        self.context = context
        self.page = page
        self.url = url
        self.console = console or Console()
        self.steps = steps or WEB_STEPS
        self.queue: asyncio.Queue[tuple[str, dict | None]] = asyncio.Queue()
        self._state: dict = {"key": "init", "instruction": "Memuat...", "optional": False}

    async def _on_record(self, source, desc: dict) -> None:
        await self.queue.put(("record", desc))

    async def _on_skip(self, source, key: str) -> None:
        await self.queue.put(("skip", {"key": key}))

    def _get_state(self, source) -> dict:
        return self._state

    async def _push(self, **state) -> None:
        self._state = state
        try:
            await self.page.evaluate("s => window.__fbRender && window.__fbRender(s)", state)
        except PlaywrightError:
            pass  # sedang navigasi; halaman baru mengambil state lewat __fbState

    async def run(self) -> CalibrationResult:
        await self.context.expose_binding("__fbRecord", self._on_record)
        await self.context.expose_binding("__fbSkip", self._on_skip)
        await self.context.expose_binding("__fbState", self._get_state)
        await self.context.add_init_script(INIT_JS)
        await self.page.goto(self.url, wait_until="domcontentloaded")

        res = CalibrationResult()
        total = len(self.steps)
        for idx, step in enumerate(self.steps, 1):
            note = ""
            for attempt in range(1, MAX_TRIES + 1):
                await self._push(key=step.key, instruction=step.instruction, optional=step.optional,
                                 progress=f"{idx}/{total}", note=note)
                self.console.print(f"[bold]Langkah {idx}/{total}[/] {step.instruction}"
                                   + (" [dim](opsional)[/]" if step.optional else ""))
                kind, data = await self._next_event(step)
                if kind == "skip":
                    res.skipped.append(step.key)
                    self.console.print("  [yellow]dilewati[/]")
                    break
                cands = candidates_from(data, step.variant_template)
                variant = data.get("name") if step.variant_template else None
                verified = await verify_candidates(self.page, cands, data, variant)
                if verified:
                    res.steps[step.key] = verified
                    if step.url_key and (pat := _url_pattern(data.get("url", ""))):
                        res.urls[step.url_key] = pat
                    self.console.print(f"  [green]terekam[/] {len(verified)} kandidat: {verified[0]}")
                    break
                note = "Tidak ada selector unik untuk elemen itu. Alt+klik elemen yang lebih spesifik."
                self.console.print(f"  [yellow]{note}[/] (percobaan {attempt}/{MAX_TRIES})")
                if attempt == MAX_TRIES:
                    fallback = [c for c in cands if "css" in c][-1:]
                    msg = f"{step.key}: tidak ada kandidat terverifikasi; memakai CSS mentah {fallback}"
                    res.warnings.append(msg)
                    if fallback:
                        res.steps[step.key] = fallback
        await self._push(key="done", instruction="Kalibrasi selesai. Tutup browser bila sudah.",
                         optional=False, progress="", note="")
        return res

    async def _next_event(self, step: CalStep) -> tuple[str, dict]:
        while True:
            kind, data = await self.queue.get()
            if kind == "skip":
                if data.get("key") != step.key:
                    continue  # klik 'Lewati' basi dari langkah sebelumnya
                if step.optional:
                    return kind, data
                continue
            return kind, data


def save_result(path: str | Path, result: CalibrationResult, url: str) -> Path | None:
    meta = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "url": url, "skipped": result.skipped,
            "warnings": result.warnings}
    return selector_store.save(path, "web", result.steps, result.urls, meta)
