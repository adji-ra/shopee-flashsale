"""E2E jalur web terhadap mock Shopee (Playwright + Chromium)."""

from __future__ import annotations

import asyncio

import pytest

from flashbuy.runner_base import RunStatus
from tests.conftest import assert_polling_rules, run_web


async def _pin_values(runner) -> list[str]:
    return await runner.page.locator("input[type=password]").evaluate_all("els => els.map(e => e.value)")


def _assert_no_order(out) -> None:
    assert out.kind("order") == [], "dry-run / abort tidak boleh mengirim request pesanan"


def test_normal_dry_run(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _assert_no_order(out)
    assert len(out.kind("buy")) == 1
    buy = out.kind("buy")[0]
    assert buy["t_server_ms"] >= out.open_at * 1000
    assert buy["body"]["qty"] == 1
    names = out.step_names()
    for step in ("precheck", "armed", "poll_start", "click_buy", "buy_ok", "click_checkout",
                 "checkout_loaded", "payment_ok", "place_order_gate", "result"):
        assert step in names, names
    assert out.result.screenshots and out.result.screenshots[0].exists()
    assert_polling_rules(out)
    assert out.notifier.events == []  # dry-run sukses tidak membunyikan alarm


def test_live_places_order_and_stops_at_pin(mock, admin, tmp_path, run):
    pins: list = []

    async def inspect(runner):
        pins.extend(await _pin_values(runner))

    out = run(run_web(mock, admin, tmp_path, live=True, inspect=inspect))
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    orders = out.kind("order")
    assert len(orders) == 1 and orders[0]["body"]["payment"] == "shopeepay"
    assert pins == [""] * 6, "PIN tidak boleh diketik alat"
    assert [e["event"] for e in out.notifier.events] == ["ORDER_PLACED_AWAIT_PIN"]
    assert "pin_screen" in out.step_names()


@pytest.mark.parametrize("scenario, status", [
    ("sold_out", RunStatus.SOLD_OUT),
    ("captcha", RunStatus.CAPTCHA),
    ("verification", RunStatus.VERIFICATION),
])
def test_stop_scenarios_no_retry(mock, admin, tmp_path, run, scenario, status):
    out = run(run_web(mock, admin, tmp_path, scenario=scenario, live=True))
    assert out.result.status == status, out.result.message
    assert len(out.kind("buy")) == 1, "tidak boleh retry setelah stok habis/captcha/verifikasi"
    _assert_no_order(out)
    if status in (RunStatus.CAPTCHA, RunStatus.VERIFICATION):
        assert [e["event"] for e in out.notifier.events] == [str(status)]


def test_login_expired_stops_at_precheck(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="login_expired", live=True))
    assert out.result.status == RunStatus.LOGIN_REQUIRED
    assert out.kind("buy") == [] and out.kind("product") == []
    assert [e["event"] for e in out.notifier.events] == ["precheck"]


@pytest.mark.parametrize("live", [False, True])
def test_payment_default_not_shopeepay_is_switched(mock, admin, tmp_path, run, live):
    out = run(run_web(mock, admin, tmp_path, scenario="payment_not_shopeepay", live=live))
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message
    assert "select_shopeepay" in out.step_names()
    if live:
        assert [o["body"]["payment"] for o in out.kind("order")] == ["shopeepay"]
    else:
        _assert_no_order(out)


def test_variant_selected_early(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="variant_required", variant="128GB Hitam"))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    assert names.index("variant_selected") < names.index("poll_start")
    assert [b["body"]["variant"] for b in out.kind("buy")] == ["128GB Hitam"]
    _assert_no_order(out)


def test_variant_required_but_not_configured(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="variant_required"))
    assert out.result.status == RunStatus.ERROR
    assert "variasi" in out.result.message
    assert len(out.kind("buy")) == 1


def test_variant_not_found(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="variant_required", variant="512GB Emas"))
    assert out.result.status == RunStatus.ERROR
    assert "512GB Emas" in out.result.message


def test_checkout_latency(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, checkout_latency_ms=3000))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    click, loaded = out.result.step("click_checkout"), out.result.step("checkout_loaded")
    assert loaded.t_server_ms - click.t_server_ms >= 3000
    assert len(out.kind("buy")) == 1  # latensi tidak memicu klik ulang
    _assert_no_order(out)


def test_button_enabled_late(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, button_delay_ms=700))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    buys = out.kind("buy")
    assert len(buys) == 1
    assert buys[0]["t_server_ms"] >= out.open_at * 1000 + 700
    assert_polling_rules(out)


def test_early_click_not_started_then_success(mock, admin, tmp_path, run):
    # Header Date (CDN) & tombol sudah "buka", tapi server penjualan baru menerima 1.5 s kemudian.
    out = run(run_web(mock, admin, tmp_path, sale_skew_ms=1500))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    assert names.count("not_started") >= 2
    buys = out.kind("buy")
    assert len(buys) == names.count("click_buy") >= 3
    assert buys[-1]["t_server_ms"] >= out.open_at * 1000 + 1500
    gaps = assert_polling_rules(out)
    assert all(g < 700 for g in gaps), f"retry tidak boleh terlalu lambat: {gaps}"
    _assert_no_order(out)


def test_not_started_until_window_closes(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, sale_skew_ms=9500))
    assert out.result.status == RunStatus.NOT_STARTED_TIMEOUT, out.result.message
    assert_polling_rules(out)
    buys = out.kind("buy")
    # 8 s / 425 ms => maksimal 19-20 klik
    assert 10 <= len(buys) <= 20
    assert buys[-1]["t_server_ms"] <= out.open_at * 1000 + 8000
    _assert_no_order(out)


@pytest.mark.parametrize("live", [False, True])
def test_before_place_order_false_aborts(mock, admin, tmp_path, run, live):
    calls = []

    def deny() -> bool:
        calls.append(1)
        return False

    out = run(run_web(mock, admin, tmp_path, live=live, before=deny))
    assert out.result.status == RunStatus.ABORTED
    assert calls == [1]
    _assert_no_order(out)
    assert "place_order_gate" not in out.step_names()


def test_abort_during_polling(mock, admin, tmp_path, run):
    async def during(runner, open_at):
        while runner.log.clock.now() < open_at + 1.2:
            await asyncio.sleep(0.05)
        await runner.abort("tes abort")

    out = run(run_web(mock, admin, tmp_path, sale_skew_ms=9500, during=during))
    assert out.result.status == RunStatus.ABORTED
    assert "tes abort" in out.result.message
    assert len(out.kind("buy")) <= 4
    _assert_no_order(out)


def test_block_media_option(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, web={"block_media": True}))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _assert_no_order(out)


def test_clock_offset_respected(mock, admin, tmp_path, run):
    # Jam server mock +730 ms dari lokal; runner memakai offset yang sama -> klik tidak terlalu awal.
    out = run(run_web(mock, admin, tmp_path, clock_offset_ms=730))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert out.kind("buy")[0]["t_server_ms"] >= out.open_at * 1000
    assert out.result.step("t_minus_lead").t_server_ms == pytest.approx(out.open_at * 1000 - 150, abs=5)


def test_order_clicked_but_no_pin_screen_still_alarms(mock, admin, tmp_path, run):
    # Metode pembayaran jadi COD setelah ShopeePay diverifikasi -> tidak ada layar PIN.
    async def during(runner, open_at):
        runner.flow_timeout_s = 3.0
        while "payment_ok" not in [st.name for st in runner.log.steps]:
            await asyncio.sleep(0.005)
        await runner.page.evaluate("document.querySelector('[data-method=cod]').click()")

    out = run(run_web(mock, admin, tmp_path, live=True, during=during))
    assert out.result.status in (RunStatus.TIMEOUT, RunStatus.ERROR), out.result.message
    assert "SUDAH diklik" in out.result.message
    assert len(out.kind("order")) == 1
    assert [e["event"] for e in out.notifier.events] == [str(out.result.status)]
