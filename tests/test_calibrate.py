"""Kalibrasi web terhadap mock: event Alt+klik disimulasikan lewat Playwright."""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

from flashbuy import selector_store
from flashbuy.calibrate import WebCalibrator, candidates_from, save_result, stable_text
from flashbuy.config import WebConfig
from flashbuy.runner_base import RunStatus
from flashbuy.web_runner import launch_context
from tests.conftest import HEADLESS, quiet_console, run_web


async def _calibrate(mock, admin, tmp_path):
    admin.scenario(name="variant_required")  # slot sudah dibuka (produk biasa)
    events: dict = {}
    async with async_playwright() as pw:
        ctx = await launch_context(pw, WebConfig(profile_dir=tmp_path / "cal-profile", channel=None),
                                   headless=HEADLESS)
        page = ctx.pages[0]
        cal = WebCalibrator(ctx, page, mock.product_url, console=quiet_console())
        task = asyncio.create_task(cal.run())

        async def at(key):
            await page.wait_for_selector(f"#__fb_cal[data-step='{key}']", state="attached", timeout=10_000)

        variant = page.get_by_role("button", name="128GB Hitam")
        buy = page.get_by_role("button", name="Beli Sekarang")
        await at("variant_option")
        await variant.click(modifiers=["Alt"])
        await at("product_price")
        await page.locator("#price").click(modifiers=["Alt"])
        await at("buy_button")
        events["variant_after_alt"] = await variant.get_attribute("aria-pressed")
        await variant.click()  # klik biasa memilih variasi
        await buy.click(modifiers=["Alt"])
        await at("cart_checkout")
        events["buys_after_alt"] = len(admin.log("buy"))
        await buy.click()
        await page.wait_for_url("**/cart**")
        await page.get_by_role("button", name="Checkout").click(modifiers=["Alt"])
        await at("payment_change")
        await page.get_by_role("button", name="Checkout").click()
        await page.wait_for_url("**/checkout**")
        await page.locator("#__fb_skip").click()
        await at("payment_shopeepay")
        await page.get_by_role("radio", name="ShopeePay").click(modifiers=["Alt"])
        await at("place_order")
        await page.get_by_role("button", name="Buat Pesanan").click(modifiers=["Alt"])
        await at("sold_out")
        # klik biasa pada Buat Pesanan juga harus diblokir
        await page.get_by_role("button", name="Buat Pesanan").click()
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(300)
        events["orders"] = len(admin.log("order"))
        await page.locator("#__fb_skip").click()
        res = await asyncio.wait_for(task, 10)
        await at("done")
        await ctx.close()
    return res, events


def test_calibrate_records_and_blocks_place_order(mock, admin, tmp_path, run):
    res, ev = run(_calibrate(mock, admin, tmp_path))
    assert ev["variant_after_alt"] == "false", "Alt+klik tidak boleh menjalankan aksi"
    assert ev["buys_after_alt"] == 0
    assert ev["orders"] == 0, "Buat Pesanan tidak boleh terkirim saat kalibrasi"
    assert res.skipped == ["payment_change", "sold_out"]
    assert res.steps["variant_option"][0] == {"role": "button", "name": "{variant}", "exact": True}
    # harga: hanya kandidat CSS (teks harga berubah-ubah)
    assert res.steps["product_price"] and all("css" in c for c in res.steps["product_price"])
    assert {"css": '[id="price"]'} in res.steps["product_price"]
    assert res.steps["buy_button"][0] == {"role": "button", "name": "Beli Sekarang", "exact": True}
    # nama tombol "Checkout (1)" berisi angka -> dipakai bagian stabilnya
    assert res.steps["cart_checkout"][0] == {"role": "button", "name": "Checkout"}
    # nama opsi ShopeePay berisi saldo (angka) -> dipakai bagian stabilnya
    assert res.steps["payment_shopeepay"][0] == {"role": "radio", "name": "ShopeePay"}
    assert {"css": '[id="place-order"]'} in res.steps["place_order"]
    assert res.urls["cart_pattern"].startswith(r"/cart")
    assert res.urls["checkout_pattern"].startswith(r"/checkout")
    assert not res.warnings


def test_calibrated_selectors_drive_dry_run(mock, admin, tmp_path, run):
    res, _ = run(_calibrate(mock, admin, tmp_path))
    path = tmp_path / "selectors.json"
    assert save_result(path, res, mock.product_url) is None
    backup = save_result(path, res, mock.product_url)  # simpan ulang -> backup versi lama
    assert backup is not None and backup.exists()
    sel = selector_store.load(path)
    assert sel.source == path
    assert sel.candidates("buy_button")[0] == res.steps["buy_button"][0]
    assert {"text": "Beli Sekarang", "exact": True} in sel.candidates("buy_button")  # cadangan teks
    data = json.loads(path.read_text())
    assert data["web"]["calibrated"]["skipped"] == ["payment_change", "sold_out"]

    admin.reset()
    out = run(run_web(mock, admin, tmp_path, scenario="variant_required", variant="64GB Putih",
                      selectors=sel))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert out.kind("buy")[0]["body"]["variant"] == "64GB Putih"
    assert out.kind("order") == []


def test_stable_text_and_candidates():
    assert stable_text("ShopeePay Saldo: Rp1.250.000") == "ShopeePay Saldo"
    assert stable_text("Checkout (3)") == "Checkout"
    desc = {"role": "button", "name": "Beli Sekarang", "text": "Beli Sekarang",
            "attrs": {"id": "btn-8812731", "data-testid": "buy"}, "css": "body > main > button"}
    c = candidates_from(desc)
    assert c[0] == {"role": "button", "name": "Beli Sekarang", "exact": True}
    assert {"css": '[data-testid="buy"]'} in c
    assert {"css": '[id="btn-8812731"]'} not in c  # id berangka panjang dianggap tidak stabil
    assert c[-1] == {"css": "body > main > button"}
    price = candidates_from({"tag": "span", "role": None, "name": "Rp99.000", "text": "Rp99.000",
                             "attrs": {"id": "price"}, "classes": ["price", "x9f3k2ab"], "css": "body > span"},
                            css_only=True)
    assert price == [{"css": '[id="price"]'}, {"css": "span.price"}, {"css": "body > span"}]
    t = candidates_from({"role": "radio", "name": "64GB Putih", "text": "64GB Putih", "attrs": {}},
                        variant_template=True)
    assert t == [{"role": "radio", "name": "{variant}", "exact": True}, {"text": "{variant}", "exact": True}]
