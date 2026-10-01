"""Koreksi tahap 3-A: browser live tidak pernah ditutup alat, pesan pasca "Buat Pesanan", expected_name wajib live."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from flashbuy import cli, selector_store, timesync
from flashbuy.config import ConfigError, require_live_ready
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus, after_order_click, keep_open
from tests.conftest import HEADLESS, LIVE_NAME, make_cfg, run_web

# ------------------------------------------------------------------ unit


@pytest.mark.parametrize("status", [RunStatus.TIMEOUT, RunStatus.ERROR, RunStatus.UNKNOWN_STATE, RunStatus.SOLD_OUT,
                                    RunStatus.ABORTED, RunStatus.PRICE_GUARD])
def test_after_order_click_becomes_unknown_with_mandatory_message(status):
    final, msg, detail = after_order_click(status, "layar PIN tidak muncul")
    assert final == RunStatus.UNKNOWN_STATE
    assert msg == "Pesanan MUNGKIN sudah terbuat — cek status pesanan manual" == MAYBE_ORDERED_MSG
    assert detail == f"{status}: layar PIN tidak muncul"


@pytest.mark.parametrize("status", [RunStatus.CAPTCHA, RunStatus.VERIFICATION, RunStatus.LOGIN_REQUIRED])
def test_after_order_click_keeps_manual_statuses_but_warns(status):
    final, msg, detail = after_order_click(status, "x")
    assert final == status and msg == MAYBE_ORDERED_MSG and "x" in detail


def test_after_order_click_pin_screen_unchanged():
    assert after_order_click(RunStatus.ORDER_PLACED_AWAIT_PIN, "ok") == (RunStatus.ORDER_PLACED_AWAIT_PIN, "ok", "")


def test_keep_open_rules():
    for status in [*RunStatus, None]:
        assert keep_open(True, status), f"live harus selalu terbuka ({status})"
    assert keep_open(False, RunStatus.CAPTCHA) and keep_open(False, RunStatus.VERIFICATION)
    assert not keep_open(False, RunStatus.DRYRUN_OK)
    assert not keep_open(False, RunStatus.SOLD_OUT)
    assert not keep_open(False, None)


def test_require_live_ready(mock, tmp_path):
    with pytest.raises(ConfigError, match="expected_name"):
        require_live_ready(make_cfg(mock, tmp_path, 2_000_000_000))
    require_live_ready(make_cfg(mock, tmp_path, 2_000_000_000, expected_name="Ponsel"))


def test_cli_run_live_without_expected_name_rejected_before_anything(tmp_path, monkeypatch, capsys):
    p = tmp_path / "t.yaml"
    p.write_text('product_url: "http://127.0.0.1:1/P-i.1.2"\nstart_time: "2099-01-01T00:00:00+07:00"\n'
                 'max_item_price: 1000\nmax_total: 2000\n')

    def boom(**kw):
        raise AssertionError("timesync tidak boleh jalan")

    monkeypatch.setattr(timesync, "sync", boom)
    assert cli.main(["run", "--config", str(p), "--only", "web", "--live", "--allow-local"]) == 2
    assert "expected_name" in capsys.readouterr().out


def test_runner_refuses_live_without_expected_name(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, live=True, cfg={"expected_name": None}))
    assert out.result.status == RunStatus.ERROR
    assert "expected_name" in out.result.message
    assert out.kind("buy") == [] and out.kind("order") == []


# ------------------------------------------------------------------ e2e: pesan wajib pasca "Buat Pesanan"


def test_order_clicked_then_unknown_uses_mandatory_message(mock, admin, tmp_path, run):
    async def during(runner, open_at):
        runner.flow_timeout_s = 3.0
        while "payment_ok" not in [st.name for st in runner.log.steps]:
            await asyncio.sleep(0.005)
        await runner.page.evaluate("document.querySelector('[data-method=cod]').click()")

    out = run(run_web(mock, admin, tmp_path, live=True, during=during))
    assert out.result.status == RunStatus.UNKNOWN_STATE
    assert out.result.message == MAYBE_ORDERED_MSG
    assert out.result.detail
    assert [e["event"] for e in out.notifier.events] == ["UNKNOWN_STATE"]


# ------------------------------------------------------------------ e2e: browser live tidak pernah ditutup


async def _cli_run(mock, admin, tmp_path, *, scenario: str, live: bool, patch=None):
    admin.scenario(name=scenario, open_in_ms=3500)
    st = admin.state()["scenario"]
    cfg = make_cfg(mock, tmp_path, st["open_at"], expected_name=LIVE_NAME)
    seen: dict = {}

    async def wait_user(runner):
        ctx = runner.context
        seen["open"] = ctx is not None and len(ctx.pages) > 0
        seen["url"] = runner.page.url
        seen["text"] = await runner.page.evaluate("() => document.body.innerText")
        await ctx.close()  # simulasi pengguna menutup jendela

    report = SimpleNamespace(offset_s=st["clock_offset_ms"] / 1000)
    try:
        result = await cli.run_web(cfg, selector_store.defaults(), live=live, lead_ms=150, report=report,
                                   run_dir=tmp_path / "logs", headless=HEADLESS, samples=1,
                                   wait_user=wait_user)
    except Exception as e:  # noqa: BLE001 - tes jalur exception
        result = e
    return result, seen


@pytest.mark.parametrize("scenario, live, kept", [
    ("captcha_redirect", True, True),
    ("captcha_redirect", False, True),  # captcha: dry-run pun dibiarkan terbuka untuk diselesaikan manual
    ("sold_out", True, True),  # live: apa pun statusnya
    ("sold_out", False, False),  # dry-run boleh menutup
    ("normal", True, True),
    ("normal", False, False),
])
def test_browser_handover(mock, admin, tmp_path, run, scenario, live, kept):
    result, seen = run(_cli_run(mock, admin, tmp_path, scenario=scenario, live=live))
    assert bool(seen) == kept, (result.status, seen)
    if kept:
        assert seen["open"], "browser harus masih terbuka saat diserahkan ke pengguna"
    if scenario == "captcha_redirect":
        assert result.status == RunStatus.CAPTCHA
        assert "/verify/captcha" in seen["url"], "halaman captcha harus dibiarkan apa adanya"
        assert admin.log("order") == []


def test_live_browser_kept_open_even_on_exception(mock, admin, tmp_path, run, monkeypatch):
    from flashbuy.web_runner import WebRunner

    async def broken_arm(self, open_at):
        raise RuntimeError("arm rusak")

    monkeypatch.setattr(WebRunner, "arm", broken_arm)
    result, seen = run(_cli_run(mock, admin, tmp_path, scenario="normal", live=True))
    assert isinstance(result, RuntimeError)
    assert seen.get("open"), "live: browser tetap diserahkan ke pengguna walau terjadi error"
