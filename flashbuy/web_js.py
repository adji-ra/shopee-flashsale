"""JavaScript sisi halaman untuk jalur web: baca harga/tombol, keranjang, dan checkout.

Semua pembacaan membuang teks yang dicoret (s/del/strike atau text-decoration line-through)
dan elemen tersembunyi. Keputusan (parse & batas) dilakukan di Python (flashbuy.pricing).
"""

from __future__ import annotations

from flashbuy.guards import ENABLED_JS

_PRELUDE = r"""
  const vis = (el) => {
    if (!el || !el.isConnected) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };
  const struck = (e) => {
    if (/^(S|DEL|STRIKE)$/.test(e.tagName)) return true;
    return /line-through/.test(getComputedStyle(e).textDecorationLine || '');
  };
  const struckUp = (e) => {
    for (let i = 0; e && e.nodeType === 1 && i < 5; e = e.parentElement, i++) if (struck(e)) return true;
    return false;
  };
  // Teks terlihat tanpa bagian yang dicoret, dengan pemisah baris untuk elemen blok.
  const textOf = (root) => {
    const out = [];
    const walk = (n) => {
      if (n.nodeType === 3) { out.push(n.textContent); return; }
      if (n.nodeType !== 1) return;
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden' || struck(n)) return;
      if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(n.tagName)) return;
      const block = !/^inline/.test(st.display);
      if (block) out.push('\n');
      for (const c of n.childNodes) walk(c);
      if (block) out.push('\n');
    };
    walk(root);
    return out.join('').replace(/[ \t   ]+/g, ' ').replace(/ ?\n[\n ]*/g, '\n').trim();
  };
  const ownText = (e) => Array.from(e.childNodes).filter((n) => n.nodeType === 3)
    .map((n) => n.textContent).join(' ');
  const PRICE_FULL = /^\s*Rp\s?[\d.]+(,-)?(\s*[-\u2013]\s*Rp\s?[\d.]+(,-)?)?\s*$/i;
  const HAS_RP = /Rp\s?\d/i;
  const maxFont = (e) => {
    let best = parseFloat(getComputedStyle(e).fontSize) || 0;
    const kids = e.querySelectorAll('*');
    for (let i = 0; i < kids.length && i < 20; i++)
      best = Math.max(best, parseFloat(getComputedStyle(kids[i]).fontSize) || 0);
    return best;
  };
  // Harga utama produk: CSS hasil kalibrasi, atau nominal non-coret terlihat dengan font terbesar.
  const findPriceEl = (css) => {
    for (const c of css || []) {
      for (const el of document.querySelectorAll(c)) if (vis(el) && !struckUp(el)) return el;
    }
    let best = null, bestSize = 0;
    const seen = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!/\d/.test(n.textContent)) continue;
      let e = n.parentElement;
      for (let i = 0; e && i < 3; e = e.parentElement, i++) {
        if (seen.has(e)) break;
        seen.add(e);
        if (!vis(e) || struckUp(e)) break;
        const t = textOf(e);
        if (t.length > 40) break;
        if (PRICE_FULL.test(t)) {
          const size = maxFont(e);
          if (size > bestSize) { best = e; bestSize = size; }
          break;
        }
      }
    }
    return best;
  };
  const readPrice = (css) => {
    let el = window.__fbPriceEl;
    if (!(el && vis(el) && PRICE_FULL.test(textOf(el)))) el = window.__fbPriceEl = findPriceEl(css);
    return el ? textOf(el) : null;
  };
"""

# Tunggu (MutationObserver, tanpa request) sampai tombol Beli aktif DAN teks harga berubah dari
# `lastPrice`, atau elemen tombol terlepas, atau timeout. Satu round-trip memberi status tombol +
# teks harga sekaligus.
WAIT_READY_JS = r"""(el, cfg) => new Promise((resolve) => {
__PRELUDE__
  const enabled = __ENABLED__;
  const snap = () => {
    const connected = el.isConnected;
    const en = connected && enabled(el);
    return {connected, enabled: en, price: en ? readPrice(cfg.priceCss) : null};
  };
  const ready = (s) => !s.connected || (s.enabled && s.price !== cfg.lastPrice);
  const first = snap();
  if (ready(first)) return resolve(first);
  let done = false, timer = null;
  const obs = new MutationObserver(() => { const s = snap(); if (ready(s)) finish(s); });
  const finish = (s) => {
    if (done) return;
    done = true; obs.disconnect(); clearTimeout(timer); resolve(s);
  };
  obs.observe(document.documentElement, {attributes: true, childList: true, subtree: true, characterData: true});
  timer = setTimeout(() => finish(snap()), cfg.timeoutMs);
})"""

PRICE_TEXT_JS = r"""(css) => {
__PRELUDE__
  return readPrice(css);
}"""

PRODUCT_TITLE_JS = r"""() => {
  const h = document.querySelector('h1');
  const t = (h && h.innerText.trim()) || document.title.split('|')[0];
  return t.replace(/\s+/g, ' ').trim();
}"""

# Keranjang: baris = elemen terkecil di atas checkbox yang memuat nominal Rp dan hanya berisi
# satu checkbox (checkbox toko / "Pilih Semua" otomatis tersaring). Checkbox disimpan di
# window.__fbCartBoxes agar bisa diklik lewat indeks.
CART_JS = r"""(cfg) => {
__PRELUDE__
  const BOX = 'input[type=checkbox],[role=checkbox]';
  const isChecked = (b) => b.checked === true || b.getAttribute('aria-checked') === 'true';
  const shown = (b) => vis(b) || vis(b.parentElement);
  const rows = [], boxes = [], out = [];
  const add = (row, box) => {
    if (!box || rows.includes(row)) return;
    const q = row.querySelector('input[type=number],input[aria-label*=kuantitas i],input[aria-label*=jumlah i]');
    rows.push(row); boxes.push(box);
    out.push({text: textOf(row), checked: isChecked(box), qty: q ? parseInt(q.value, 10) : null});
  };
  if (cfg.rowCss && cfg.rowCss.length) {
    for (const c of cfg.rowCss) for (const row of document.querySelectorAll(c))
      if (vis(row)) add(row, row.querySelector(BOX));
  } else {
    for (const box of document.querySelectorAll(BOX)) {
      if (!shown(box)) continue;
      const lab = box.closest('label') || box.parentElement || box;
      const label = (box.getAttribute('aria-label') || '') + ' ' + (lab.innerText || '');
      if (/pilih semua|select all/i.test(label)) continue;
      let row = box.parentElement;
      for (let i = 0; row && i < 8 && !HAS_RP.test(textOf(row)); i++) row = row.parentElement;
      if (!row || row.querySelectorAll(BOX).length !== 1) continue;
      add(row, box);
    }
  }
  window.__fbCartBoxes = boxes;
  const counts = document.body.innerText.match(/checkout\s*\(\s*\d+\s*\)/gi) || [];
  return {rows: out, counts};
}"""

CART_BOX_JS = "(i) => (window.__fbCartBoxes || [])[i] || null"

# Checkout: baris produk (CSS kalibrasi, atau heuristik berjangkar penanda kuantitas "x1"/input),
# teks nilai setelah label total & ongkir, dan teks halaman (untuk "(N Produk)").
CHECKOUT_JS = r"""(cfg) => {
__PRELUDE__
  const rows = [];
  if (cfg.rowCss && cfg.rowCss.length) {
    for (const c of cfg.rowCss) for (const r of document.querySelectorAll(c)) if (vis(r)) rows.push(r);
  } else {
    const anchors = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode())
      if (/^\s*[x×]\s?\d{1,3}\s*$/i.test(n.textContent) && vis(n.parentElement)) anchors.push(n.parentElement);
    document.querySelectorAll('input[aria-label*=kuantitas i],input[aria-label*=jumlah i]')
      .forEach((q) => { if (vis(q)) anchors.push(q); });
    for (const a of anchors) {
      let row = a.parentElement;
      for (let i = 0; row && i < 8; i++, row = row.parentElement) {
        const t = textOf(row);
        if (HAS_RP.test(t) && t.length >= 10) break;
      }
      if (row && !rows.includes(row)) rows.push(row);
    }
  }
  const leaf = rows.filter((r) => !rows.some((o) => o !== r && r.contains(o)));
  const byLabel = (css, labelRe) => {
    const out = [];
    if (css && css.length) {
      for (const c of css) for (const el of document.querySelectorAll(c)) if (vis(el)) out.push(textOf(el));
      return out;
    }
    const re = new RegExp(labelRe, 'i');
    // Nilai = sisa baris label; bila kosong, baris berikutnya. Tidak melompat ke label lain,
    // jadi ongkir "Menghitung..." tidak salah terbaca sebagai nominal total di bawahnya.
    const valueAfter = (t) => {
      const m = t.match(re);
      if (!m) return '';
      const lines = t.slice(m.index + m[0].length).split('\n');
      const first = lines[0].replace(/^[\s:]+/, '');
      return first || (lines[1] || '');
    };
    for (const el of document.body.querySelectorAll('*')) {
      if (!re.test(ownText(el)) || !vis(el) || struckUp(el)) continue;
      let c = el;
      for (let i = 0; c && i < 4; i++, c = c.parentElement) {
        const v = valueAfter(textOf(c)).trim();
        if (v) { out.push(v); break; }
      }
    }
    return out;
  };
  return {
    rows: leaf.map(textOf),
    totals: byLabel(cfg.totalCss, cfg.totalLabel),
    shippings: byLabel(cfg.shippingCss, cfg.shippingLabel),
    pageText: textOf(document.body),
  };
}"""


def _fill(js: str) -> str:
    return js.replace("__PRELUDE__", _PRELUDE).replace("__ENABLED__", ENABLED_JS)


WAIT_READY_JS, PRICE_TEXT_JS, CART_JS, CHECKOUT_JS = map(_fill, (WAIT_READY_JS, PRICE_TEXT_JS, CART_JS, CHECKOUT_JS))

TOTAL_LABEL = r"total pembayaran"
SHIPPING_LABEL = r"(total )?ongkos kirim|\bongkir\b|subtotal pengiriman|biaya pengiriman"
