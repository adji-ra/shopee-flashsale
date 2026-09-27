"""Klasifikasi status halaman pada HTML sintetis."""

from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from flashbuy import selector_store
from flashbuy.guards import Guard, PageState

LONG = "<p>" + ("Deskripsi produk panjang. " * 120) + "</p>"


async def _classify(html: str, url: str = "http://mock.test/Produk-i.1.2", buy: bool = False):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.route("**/*", lambda r: r.fulfill(body=html, content_type="text/html"))
        await page.goto(url)
        guard = Guard(selector_store.defaults())
        loc = page.get_by_role("button", name="Beli Sekarang") if buy else None
        c = await guard.classify(page, loc)
        await browser.close()
        return c


@pytest.mark.parametrize("html, url, state", [
    ('<div role="dialog"><h2>Verifikasi Keamanan</h2><p>Geser untuk menyelesaikan puzzle</p></div>' + LONG,
     None, PageState.CAPTCHA),
    ('<iframe src="https://x.test/captcha/frame"></iframe>' + LONG, None, PageState.CAPTCHA),
    ("<h1>Tunggu</h1>", "http://mock.test/verify/captcha?x=1", PageState.CAPTCHA),
    ("<p>Kami mendeteksi aktivitas tidak biasa pada akunmu</p>" + LONG, None, PageState.VERIFICATION),
    ("<h1>Verifikasi</h1>", "http://mock.test/verify/traffic", PageState.VERIFICATION),
    ('<div role="dialog">Masukkan kode OTP yang dikirim ke nomormu</div>' + LONG, None, PageState.VERIFICATION),
    ("<h1>Masukkan PIN ShopeePay</h1>", "http://mock.test/pin", PageState.PIN_SCREEN),
    ("<form>login</form>", "http://mock.test/buyer/login?next=/", PageState.LOGIN_REQUIRED),
    ('<div role="alert">Stok habis</div>' + LONG, None, PageState.SOLD_OUT),
    ("<button disabled>Habis</button>" + LONG, None, PageState.SOLD_OUT),
    ('<div role="alert">Flash sale belum dimulai</div>' + LONG, None, PageState.NOT_STARTED),
    ('<div role="alert">Silakan pilih variasi produk terlebih dahulu</div>' + LONG, None,
     PageState.VARIANT_REQUIRED),
    ("<h1>Checkout</h1>", "http://mock.test/checkout?sid=1", PageState.CHECKOUT),
    ("<h1>Keranjang</h1>", "http://mock.test/cart", PageState.CART),
    ("<h1>Tunggu</h1>", "http://mock.test/x/traffic?y=1", PageState.VERIFICATION),
    ('<div style="position:fixed;inset:0"><iframe src="/verify/frame" width="320" height="200"></iframe></div>'
     + LONG, None, PageState.VERIFICATION),
])
def test_classify_states(run, html, url, state):
    c = run(_classify(html, url or "http://mock.test/Produk-i.1.2"))
    assert c.state == state, c


def test_product_active_and_waiting(run):
    assert run(_classify("<button>Beli Sekarang</button>" + LONG, buy=True)).state == PageState.PRODUCT_ACTIVE
    assert run(_classify("<button disabled>Beli Sekarang</button>" + LONG, buy=True)).state == \
        PageState.PRODUCT_WAITING
    assert run(_classify('<button class="btn btn--disabled">Beli Sekarang</button>' + LONG, buy=True)).state \
        == PageState.PRODUCT_WAITING


@pytest.mark.parametrize("desc", [
    "Garansi resmi. Jika stok habis, pesanan dibatalkan otomatis.",
    "Cek kode verifikasi IMEI di situs resmi. Masukkan kode OTP bawaan box.",
    "Promo SEGERA HABIS! Pilih variasi warna sesuai selera.",
])
def test_seller_description_does_not_trigger_stop(run, desc):
    html = f"<button>Beli Sekarang</button><p>{desc}</p>" + LONG
    assert run(_classify(html, buy=True)).state == PageState.PRODUCT_ACTIVE


def test_hidden_captcha_is_ignored(run):
    html = ('<div id="captcha" style="display:none"><p>Geser untuk menyelesaikan puzzle</p></div>'
            "<button>Beli Sekarang</button>" + LONG)
    assert run(_classify(html, buy=True)).state == PageState.PRODUCT_ACTIVE


def test_small_or_hidden_captcha_iframe_is_ignored(run):
    html = ('<iframe src="https://t.test/captcha/pixel" width="1" height="1"></iframe>'
            '<iframe src="https://t.test/captcha/x" width="300" height="200" style="display:none"></iframe>'
            "<button>Beli Sekarang</button>" + LONG)
    assert run(_classify(html, buy=True)).state == PageState.PRODUCT_ACTIVE


def test_url_state_and_custom_patterns():
    g = Guard(selector_store.defaults())
    assert g.url_state("https://shopee.co.id/verify/captcha?x") == PageState.CAPTCHA
    assert g.url_state("https://shopee.co.id/verify/traffic") == PageState.VERIFICATION
    assert g.url_state("https://shopee.co.id/Ponsel-i.1.2") is None
    sel = selector_store.defaults()
    sel.guards = {"CAPTCHA": {"url": [r"/cek-robot"]}}
    assert Guard(sel).url_state("https://shopee.co.id/cek-robot/1") == PageState.CAPTCHA
