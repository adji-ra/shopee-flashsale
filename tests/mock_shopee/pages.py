"""Template HTML mock. Markup & gaya ditulis sendiri (bukan salinan Shopee); hanya label
Bahasa Indonesia yang meniru alur aslinya."""

from __future__ import annotations

import json
from html import escape

_CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#f5f5f5;color:#222}
header{background:#333;color:#fff;padding:10px 16px}
main{max-width:880px;margin:16px auto;background:#fff;padding:16px;border-radius:6px}
button{font-size:15px;padding:8px 14px;margin:4px;border:1px solid #888;border-radius:4px;
  background:#fff;cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:#0a7;color:#fff;border-color:#0a7}
[aria-pressed=true],[aria-checked=true]{outline:3px solid #0a7}
.badge{background:#c30;color:#fff;padding:2px 8px;border-radius:3px;font-weight:bold}
.price{font-size:28px;color:#c30}.strike{text-decoration:line-through;color:#888;font-size:14px}
#toast{position:fixed;top:40%;left:50%;transform:translateX(-50%);background:#000c;color:#fff;
  padding:14px 22px;border-radius:6px;display:none}
.modal{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center}
.modal>div{background:#fff;padding:24px;border-radius:8px;width:320px}
section{border-top:1px solid #eee;padding:10px 0}
.small{font-size:13px;color:#666}
"""

_RP_JS = "const rp=(n)=>'Rp'+String(n).replace(/\\B(?=(\\d{3})+(?!\\d))/g,'.');"

DEFAULT_DESCRIPTION = " ".join([
    "Ponsel uji coba dengan layar 6,5 inci, baterai 5000 mAh, dan kamera ganda.",
    "Garansi resmi 12 bulan. Isi kotak: unit, kabel data, adaptor, buku panduan.",
    "Pengiriman setiap hari kerja sebelum pukul 15.00. Pesanan di atas jam tersebut dikirim besok.",
] * 12)


def rupiah(n: int) -> str:
    return "Rp" + f"{n:,}".replace(",", ".")


def _layout(title: str, body: str, script: str = "") -> str:
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<title>{escape(title)} | Mock Shop</title><style>{_CSS}</style></head>
<body><header>Mock Shop <small>(tiruan untuk tes)</small></header>
<main>{body}</main><div id="toast" role="alert" aria-live="assertive"></div>
<script>
{_RP_JS}
function showToast(m){{const t=document.getElementById('toast');t.textContent=m;t.style.display='block';
 clearTimeout(window.__tt);window.__tt=setTimeout(()=>{{t.style.display='none';t.textContent='';}},1500);}}
function hideToast(){{const t=document.getElementById('toast');t.style.display='none';t.textContent='';}}
{script}
</script></body></html>"""


def simple(title: str, text: str) -> str:
    return _layout(title, f"<h1>{escape(title)}</h1><p>{escape(text)}</p>")


def home(product_url: str) -> str:
    return _layout("Beranda", f'<h1>Beranda</h1><a href="{escape(product_url)}">Produk flash sale</a>')


def login(next_url: str) -> str:
    return _layout("Log In", f"""<h1>Log In</h1>
<form method="post" action="/buyer/login">
 <input type="hidden" name="next" value="{escape(next_url)}">
 <label>No. Handphone/Username/Email <input name="user" autocomplete="username"></label><br>
 <label>Password <input type="password" name="password" autocomplete="current-password"></label><br>
 <button class="primary" type="submit">Log In</button>
</form>""")


def verification() -> str:
    return _layout("Verifikasi", """<h1>Verifikasi diperlukan</h1>
<p>Kami mendeteksi aktivitas tidak biasa pada akunmu. Silakan verifikasi untuk melanjutkan.</p>
<button type="button">Kirim kode verifikasi</button>""")


def captcha_page() -> str:
    # Sengaja tanpa teks captcha: harus terdeteksi dari URL.
    return _layout("Mohon tunggu", "<h1>Mohon tunggu sebentar</h1><p>Memuat...</p>")


def captcha_frame() -> str:
    return """<!doctype html><html><body style="margin:0;font:14px sans-serif">
<div style="padding:12px">Seret potongan gambar ke tempatnya</div>
<div style="height:120px;background:#ccd"></div></body></html>"""


def unknown_page() -> str:
    return _layout("Promo", "<h1>Halaman promo</h1><p>Selamat datang. Nantikan kejutan menarik dari kami.</p>")


def product(*, item: str, name: str, server_now_ms: int, open_at_ms: int, button_delay_ms: int,
            variants: list[str], variant_prices: dict[str, int], sold_out: bool, price: int,
            original_price: int, shipping: str, active_before_open: bool, live_update: bool,
            initial_flash: bool, flash_available: bool, description: str) -> str:
    var_html = ""
    if variants:
        opts = "".join(f'<button type="button" class="opt" data-variant="{escape(v)}" '
                       f'aria-pressed="false">{escape(v)}</button>' for v in variants)
        var_html = f'<section><div id="variant-label">Variasi</div><div role="group" ' \
                   f'aria-labelledby="variant-label">{opts}</div></section>'
    buy_label = "Habis" if sold_out else "Beli Sekarang"
    body = f"""
<h1>{escape(name)}</h1>
<div><span class="badge">Flash Sale</span> <span id="countdown" class="small"></span></div>
<div><span class="price" id="price"></span> <span class="strike" id="strike"></span></div>
<div class="small">Ongkos kirim: {shipping}</div>
{var_html}
<section><label>Kuantitas <input id="qty" aria-label="Kuantitas" type="number" value="1" min="1"
 max="1"></label></section>
<section>
 <button type="button" id="add-cart">Masukkan Keranjang</button>
 <button type="button" id="buy" class="primary" disabled>{buy_label}</button>
</section>
<section><h2>Deskripsi Produk</h2><p>{escape(description)}</p></section>
<div id="captcha" class="modal" style="display:none" role="dialog" aria-label="Verifikasi Keamanan">
 <div><h2>Verifikasi Keamanan</h2><p>Geser untuk menyelesaikan puzzle</p>
 <div id="captcha-slider" style="height:30px;background:#ddd"></div></div></div>
"""
    cfg = {"SERVER_NOW": server_now_ms, "OPEN_AT": open_at_ms, "BTN_AT": open_at_ms + button_delay_ms,
           "ITEM": item, "VARIANTS": variants, "VPRICES": variant_prices, "PRICE": price,
           "NORMAL": original_price, "ACTIVE_BEFORE_OPEN": active_before_open, "LIVE": live_update,
           "INITIAL_FLASH": initial_flash, "FLASH_AVAILABLE": flash_available}
    script = f"""
const C={json.dumps(cfg)}, T0=performance.now();
let soldOut={json.dumps(sold_out)}, variant=null;
const now=()=>C.SERVER_NOW+(performance.now()-T0);
const buy=document.getElementById('buy'), cd=document.getElementById('countdown');
const priceEl=document.getElementById('price'), strikeEl=document.getElementById('strike');
function fmt(ms){{const s=Math.ceil(ms/1000);return String(Math.floor(s/3600)).padStart(2,'0')+':'+
 String(Math.floor(s/60)%60).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}}
const flashNow=()=>C.LIVE ? now()>=C.BTN_AT : C.INITIAL_FLASH;
function prices(){{
 const flash=flashNow();
 const normal=(flash && !C.FLASH_AVAILABLE) || (!flash && C.ACTIVE_BEFORE_OPEN);
 if(normal) return {{main:rp(C.NORMAL), strike:''}};
 const vals=variant ? [C.VPRICES[variant] ?? C.PRICE]
   : (C.VARIANTS.length ? C.VARIANTS.map(v=>C.VPRICES[v] ?? C.PRICE) : [C.PRICE]);
 const lo=Math.min(...vals), hi=Math.max(...vals);
 return {{main: lo===hi ? rp(lo) : rp(lo)+' - '+rp(hi), strike: rp(C.NORMAL)}};
}}
function tick(){{
 const n=now(), flash=flashNow();
 cd.textContent = !flash ? 'Dimulai dalam '+fmt(Math.max(0,C.OPEN_AT-n))
   : (soldOut ? 'Berakhir' : (C.FLASH_AVAILABLE ? 'Sedang berlangsung' : 'Stok flash sale sudah terjual semua'));
 if(!soldOut) buy.disabled = !(C.ACTIVE_BEFORE_OPEN || flash);
 const p=prices();
 if(priceEl.textContent!==p.main) priceEl.textContent=p.main;
 if(strikeEl.textContent!==p.strike) strikeEl.textContent=p.strike;
}}
setInterval(tick,10); tick();
document.querySelectorAll('[data-variant]').forEach(b=>b.addEventListener('click',()=>{{
 const on=b.getAttribute('aria-pressed')!=='true';
 document.querySelectorAll('[data-variant]').forEach(o=>o.setAttribute('aria-pressed','false'));
 b.setAttribute('aria-pressed',String(on)); variant=on?b.dataset.variant:null; tick();}}));
document.getElementById('add-cart').addEventListener('click',()=>showToast('Produk ditambahkan ke keranjang'));
buy.addEventListener('click',async()=>{{
 hideToast();
 const qty=parseInt(document.getElementById('qty').value||'1');
 const r=await fetch('/api/buy',{{method:'POST',headers:{{'Content-Type':'application/json'}},
   body:JSON.stringify({{item:C.ITEM,variant,qty}})}});
 const j=await r.json();
 if(j.redirect){{location.href=j.redirect;return;}}
 if(j.captcha){{document.getElementById('captcha').style.display='flex';return;}}
 if(j.captcha_iframe){{
   const d=document.createElement('div');
   d.style.cssText='position:fixed;inset:0;background:#0006;display:flex;align-items:center;justify-content:center';
   d.innerHTML='<iframe src="/captcha/frame?id=1" width="340" height="220" style="border:0;background:#fff"></iframe>';
   document.body.appendChild(d); return;}}
 if(j.error==='sold_out'){{soldOut=true;buy.disabled=true;buy.textContent='Habis';}}
 showToast(j.message||'Terjadi kesalahan');
}});
"""
    return _layout(name, body, script)


def cart(sid: str, rows: list[dict], uncheck_fails: bool) -> str:
    items = "".join(f"""
<div class="cart-item" data-cart-item="{r['id']}">
 <label><input type="checkbox" class="pick" data-id="{r['id']}"
   {'checked' if r['checked'] else ''}> <span>{escape(r['name'])}</span></label>
 <div class="small">Variasi: {escape(r.get('variant') or '-')}</div>
 <div>{rupiah(r['price'])}</div>
 <label>Kuantitas <input aria-label="Kuantitas" type="number" value="{r['qty']}" readonly></label>
</div>""" for r in rows)
    body = f"""<h1>Keranjang Belanja</h1>
<section><label><input type="checkbox" id="all"> Pilih Semua ({len(rows)})</label></section>
<section id="items">{items}</section>
<section>Total (<span id="n"></span> produk): <b id="sum"></b>
 <button type="button" class="primary" id="cart-checkout">Checkout</button></section>"""
    prices = {r["id"]: r["price"] * r["qty"] for r in rows}
    script = f"""
const PRICES={json.dumps(prices)}, SID={json.dumps(sid)}, LOCK={json.dumps(uncheck_fails)};
const picks=()=>Array.from(document.querySelectorAll('.pick'));
function upd(){{
 const on=picks().filter(p=>p.checked);
 document.getElementById('n').textContent=on.length;
 document.getElementById('sum').textContent=rp(on.reduce((a,p)=>a+PRICES[p.dataset.id],0));
 document.getElementById('cart-checkout').textContent='Checkout ('+on.length+')';
 document.getElementById('all').checked=on.length===picks().length;
}}
picks().forEach(p=>p.addEventListener('click',(e)=>{{
 if(LOCK && p.dataset.id!=='t' && !p.checked){{e.preventDefault();}}
 setTimeout(upd,0);}}));
document.getElementById('all').addEventListener('click',(e)=>{{e.preventDefault();}});
upd();
document.getElementById('cart-checkout').addEventListener('click',()=>{{
 const ids=picks().filter(p=>p.checked).map(p=>p.dataset.id).join(',');
 location.href='/checkout?sid='+SID+'&items='+ids;}});"""
    return _layout("Keranjang", body, script)


def checkout(*, sid: str, address: str, rows: list[dict], shipping: int, service_fee: int,
             shipping_delay_ms: int, payment_default: str, balance: str | None) -> str:
    methods = [("shopeepay", "ShopeePay", f"Saldo: {balance}" if balance else ""),
               ("bank", "Transfer Bank", ""), ("cod", "COD - Cek Dulu", "")]
    opts = "".join(
        f'<button type="button" role="radio" data-method="{m}" '
        f'aria-checked="{str(m == payment_default).lower()}">'
        f'{label}{(" <small>" + escape(extra) + "</small>") if extra else ""}</button>'
        for m, label, extra in methods)
    items = "".join(f"""
<div class="checkout-item">
 <div class="name">{escape(r['name'])}</div><div class="small">Variasi: {escape(r.get('variant') or '-')}</div>
 <div>Harga satuan: <span>{r['unit_text']}</span> {f"<s>{r['strike']}</s>" if r.get('strike') else ''}</div>
 <div>x{r['qty']}</div>
 <div>Subtotal: {r['subtotal_text']}</div>
</div>""" for r in rows)
    subtotal = sum(r["unit"] * r["qty"] for r in rows)
    qty_sum = sum(r["qty"] for r in rows)
    final_total = subtotal + shipping + service_fee
    delayed = shipping_delay_ms > 0
    fee_html = f'<div>Biaya Layanan <span>{rupiah(service_fee)}</span></div>' if service_fee else ""
    body = f"""<h1>Checkout</h1>
<section><h2>Alamat Pengiriman</h2><div>{escape(address)}</div><button type="button">Ubah</button></section>
<section><h2>Produk Dipesan</h2>{items}</section>
<section><h2 id="pm-label">Metode Pembayaran</h2>
 <div role="radiogroup" aria-labelledby="pm-label">{opts}</div></section>
<section>
 <div>Total Pesanan ({qty_sum} Produk): {rupiah(subtotal)}</div>
 <div>Total Ongkos Kirim <span id="ship">{"Menghitung..." if delayed else rupiah(shipping)}</span></div>
 {fee_html}
 <div>Total Pembayaran <b id="total">{rupiah(subtotal + service_fee) if delayed else rupiah(final_total)}</b></div>
 <button type="button" class="primary" id="place-order">Buat Pesanan</button></section>"""
    script = f"""
let method={json.dumps(payment_default)};
if({json.dumps(delayed)}) setTimeout(()=>{{
 document.getElementById('ship').textContent=rp({shipping});
 document.getElementById('total').textContent=rp({final_total});}}, {shipping_delay_ms});
document.querySelectorAll('[data-method]').forEach(b=>b.addEventListener('click',()=>{{
 document.querySelectorAll('[data-method]').forEach(o=>o.setAttribute('aria-checked','false'));
 b.setAttribute('aria-checked','true'); method=b.dataset.method;}}));
document.getElementById('place-order').addEventListener('click',async()=>{{
 const r=await fetch('/api/order',{{method:'POST',headers:{{'Content-Type':'application/json'}},
   body:JSON.stringify({{sid:{json.dumps(sid)},payment:method}})}});
 const j=await r.json(); if(j.redirect){{location.href=j.redirect;}} else showToast(j.message||'Gagal');
}});"""
    return _layout("Checkout", body, script)


def pin() -> str:
    boxes = "".join(f'<input type="password" inputmode="numeric" maxlength="1" '
                    f'aria-label="Digit PIN {i}" style="width:28px">' for i in range(1, 7))
    return _layout("PIN ShopeePay", f"""<h1>Masukkan PIN ShopeePay</h1>
<p>Masukkan 6 digit PIN ShopeePay untuk menyelesaikan pembayaran.</p><div>{boxes}</div>
<button type="button" disabled>Konfirmasi</button>""")


def address(addr: str | None) -> str:
    if not addr:
        return _layout("Alamat Saya", "<h1>Alamat Saya</h1><p>Belum ada alamat tersimpan.</p>")
    return _layout("Alamat Saya", f"""<h1>Alamat Saya</h1>
<div class="address-card"><span class="badge">Utama</span> <span>{escape(addr)}</span></div>""")


def wallet(balance: str | None) -> str:
    text = f'<div>Saldo ShopeePay</div><div class="price">{balance}</div>' if balance else \
        "<div>Saldo ShopeePay tidak dapat dimuat.</div>"
    return _layout("ShopeePay", f"<h1>ShopeePay</h1>{text}")
