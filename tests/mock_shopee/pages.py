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
.price{font-size:28px;color:#c30}.strike{text-decoration:line-through;color:#888}
#toast{position:fixed;top:40%;left:50%;transform:translateX(-50%);background:#000c;color:#fff;
  padding:14px 22px;border-radius:6px;display:none}
.modal{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center}
.modal>div{background:#fff;padding:24px;border-radius:8px;width:320px}
section{border-top:1px solid #eee;padding:10px 0}
"""


def _layout(title: str, body: str, script: str = "") -> str:
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<title>{escape(title)} | Mock Shop</title><style>{_CSS}</style></head>
<body><header>Mock Shop <small>(tiruan untuk tes)</small></header>
<main>{body}</main><div id="toast" role="alert" aria-live="assertive"></div>
<script>
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


def product(*, item: str, server_now_ms: int, open_at_ms: int, button_delay_ms: int,
            variants: list[str], sold_out: bool, price: str, original_price: str,
            shipping: str) -> str:
    var_html = ""
    if variants:
        opts = "".join(f'<button type="button" class="opt" data-variant="{escape(v)}" '
                       f'aria-pressed="false">{escape(v)}</button>' for v in variants)
        var_html = f'<section><div id="variant-label">Variasi</div><div role="group" ' \
                   f'aria-labelledby="variant-label">{opts}</div></section>'
    buy_label = "Habis" if sold_out else "Beli Sekarang"
    body = f"""
<h1>Ponsel Uji Coba 128GB</h1>
<div><span class="badge">Flash Sale</span> <span id="countdown"></span></div>
<div><span class="price" data-role="flash-price">{price}</span>
 <span class="strike">{original_price}</span></div>
<div>Ongkos kirim: {shipping}</div>
{var_html}
<section><label>Kuantitas <input id="qty" aria-label="Kuantitas" type="number" value="1" min="1"
 max="1"></label></section>
<section>
 <button type="button" id="add-cart">Masukkan Keranjang</button>
 <button type="button" id="buy" class="primary" disabled>{buy_label}</button>
</section>
<div id="captcha" class="modal" style="display:none" role="dialog" aria-label="Verifikasi Keamanan">
 <div><h2>Verifikasi Keamanan</h2><p>Geser untuk menyelesaikan puzzle</p>
 <div id="captcha-slider" style="height:30px;background:#ddd"></div></div></div>
"""
    script = f"""
const SERVER_NOW={server_now_ms}, T0=performance.now(), OPEN_AT={open_at_ms},
      BTN_AT={open_at_ms + button_delay_ms}, ITEM={json.dumps(item)};
let soldOut={json.dumps(sold_out)}, variant=null;
const now=()=>SERVER_NOW+(performance.now()-T0);
const buy=document.getElementById('buy'), cd=document.getElementById('countdown');
function fmt(ms){{const s=Math.ceil(ms/1000);return String(Math.floor(s/3600)).padStart(2,'0')+':'+
 String(Math.floor(s/60)%60).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}}
function tick(){{const n=now();
 cd.textContent = n<OPEN_AT ? 'Dimulai dalam '+fmt(OPEN_AT-n) : (soldOut?'Berakhir':'Sedang berlangsung');
 if(!soldOut) buy.disabled = n<BTN_AT;}}
setInterval(tick,10); tick();
document.querySelectorAll('[data-variant]').forEach(b=>b.addEventListener('click',()=>{{
 const on=b.getAttribute('aria-pressed')!=='true';
 document.querySelectorAll('[data-variant]').forEach(o=>o.setAttribute('aria-pressed','false'));
 b.setAttribute('aria-pressed',String(on)); variant=on?b.dataset.variant:null;}}));
document.getElementById('add-cart').addEventListener('click',()=>showToast('Produk ditambahkan ke keranjang'));
buy.addEventListener('click',async()=>{{
 hideToast();
 const qty=parseInt(document.getElementById('qty').value||'1');
 const r=await fetch('/api/buy',{{method:'POST',headers:{{'Content-Type':'application/json'}},
   body:JSON.stringify({{item:ITEM,variant,qty}})}});
 const j=await r.json();
 if(j.redirect){{location.href=j.redirect;return;}}
 if(j.captcha){{document.getElementById('captcha').style.display='flex';return;}}
 if(j.error==='sold_out'){{soldOut=true;buy.disabled=true;buy.textContent='Habis';}}
 showToast(j.message||'Terjadi kesalahan');
}});
"""
    return _layout("Ponsel Uji Coba 128GB", body, script)


def cart(sid: str, variant: str | None, qty: int, subtotal: str) -> str:
    body = f"""<h1>Keranjang Belanja</h1>
<section><label><input type="checkbox" checked aria-label="Pilih produk"> Ponsel Uji Coba 128GB</label>
 <div>Variasi: {escape(variant or '-')}</div>
 <label>Kuantitas <input aria-label="Kuantitas" type="number" value="{qty}" readonly></label>
 <div>Subtotal: {subtotal}</div></section>
<section>Total (1 produk): {subtotal}
 <button type="button" class="primary" id="cart-checkout">Checkout</button></section>"""
    script = f"""document.getElementById('cart-checkout').addEventListener('click',()=>{{
 location.href='/checkout?sid={escape(sid)}';}});"""
    return _layout("Keranjang", body, script)


def checkout(*, sid: str, address: str, variant: str | None, qty: int, price: str,
             shipping: str, total: str, payment_default: str, balance: str | None) -> str:
    methods = [("shopeepay", "ShopeePay", f"Saldo: {balance}" if balance else ""),
               ("bank", "Transfer Bank", ""), ("cod", "COD - Cek Dulu", "")]
    opts = "".join(
        f'<button type="button" role="radio" data-method="{m}" aria-checked="{str(m == payment_default).lower()}">'
        f'{label}{(" <small>" + escape(extra) + "</small>") if extra else ""}</button>'
        for m, label, extra in methods)
    body = f"""<h1>Checkout</h1>
<section><h2>Alamat Pengiriman</h2><div>{escape(address)}</div><button type="button">Ubah</button></section>
<section><h2>Produk Dipesan</h2><div>Ponsel Uji Coba 128GB — Variasi: {escape(variant or '-')}</div>
 <div>{price} x{qty}</div><div>Ongkos kirim: {shipping}</div></section>
<section><h2 id="pm-label">Metode Pembayaran</h2>
 <div role="radiogroup" aria-labelledby="pm-label">{opts}</div></section>
<section><div>Total Pembayaran: <b>{total}</b></div>
 <button type="button" class="primary" id="place-order">Buat Pesanan</button></section>"""
    script = f"""
let method={json.dumps(payment_default)};
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
