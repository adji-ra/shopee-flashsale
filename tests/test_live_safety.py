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


# (klik 'Buat Pesanan' lalu layar tak dikenal -> pesan wajib: lihat
#  test_web_runner.py::test_order_clicked_but_no_pin_screen_still_alarms)


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


@pytest.mark.parametrize("where", ["arm", "ringkasan"])
def test_live_browser_kept_open_even_on_exception(mock, admin, tmp_path, run, monkeypatch, where):
    """Exception di fase runner (arm) -> hasil ERROR jalur itu; exception di luar runner (ringkasan konsol) ->
    menjalar. Keduanya: browser live tetap diserahkan ke pengguna (tidak ditutup alat)."""
    from flashbuy.web_runner import WebRunner

    async def broken_arm(self, open_at):
        raise RuntimeError("arm rusak")

    def broken_summary(outcome, open_at):
        raise RuntimeError("konsol rusak")

    if where == "arm":
        monkeypatch.setattr(WebRunner, "arm", broken_arm)
    else:
        monkeypatch.setattr(cli, "print_summary", broken_summary)
    result, seen = run(_cli_run(mock, admin, tmp_path, scenario="normal", live=True))
    if where == "arm":
        assert result.status == RunStatus.ERROR and result.message == "arm: RuntimeError: arm rusak"
        assert admin.log("buy") == [] and admin.log("order") == []
    else:
        assert isinstance(result, RuntimeError) and str(result) == "konsol rusak"
    assert seen.get("open"), "live: browser tetap diserahkan ke pengguna walau terjadi error"


# ------------------------------------------------------------------ Ctrl+C saat browser diserahkan


async def _cli_run_ctrl_c(mock, admin, tmp_path, *, live: bool, presses: int, scenario: str = "normal"):
    """Ctrl+C (asyncio.run membatalkan task utama) `presses` kali saat menunggu pengguna menutup browser."""
    admin.scenario(name=scenario, open_in_ms=3500)
    st = admin.state()["scenario"]
    cfg = make_cfg(mock, tmp_path, st["open_at"], expected_name=LIVE_NAME)
    opened: list[bool] = []

    async def wait_user(runner):
        opened.append(runner.context is not None and len(runner.context.pages) > 0)
        if len(opened) <= presses:
            asyncio.current_task().cancel()
            await asyncio.sleep(5)
        await runner.context.close()  # pengguna menutup jendela

    report = SimpleNamespace(offset_s=st["clock_offset_ms"] / 1000)
    try:
        result = await cli.run_web(cfg, selector_store.defaults(), live=live, lead_ms=150, report=report,
                                   run_dir=tmp_path / "logs", headless=HEADLESS, samples=1, wait_user=wait_user)
    except asyncio.CancelledError as e:
        result = e
    return result, opened


def test_live_first_ctrl_c_during_handover_does_not_close_browser(mock, admin, tmp_path, run):
    result, opened = run(_cli_run_ctrl_c(mock, admin, tmp_path, live=True, presses=1))
    assert opened == [True, True], "setelah Ctrl+C pertama browser masih terbuka & tetap ditunggu"
    assert result.status == RunStatus.ORDER_PLACED_AWAIT_PIN


def test_live_second_ctrl_c_forces_exit(mock, admin, tmp_path, run):
    result, opened = run(_cli_run_ctrl_c(mock, admin, tmp_path, live=True, presses=2))
    assert opened == [True, True] and isinstance(result, asyncio.CancelledError)


def test_dry_run_ctrl_c_during_handover_exits_at_once(mock, admin, tmp_path, run):
    result, opened = run(_cli_run_ctrl_c(mock, admin, tmp_path, live=False, presses=1, scenario="captcha_redirect"))
    assert opened == [True] and isinstance(result, asyncio.CancelledError)
