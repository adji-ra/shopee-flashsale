from __future__ import annotations

import asyncio
import json
import threading

import pytest

from flashbuy.runner_base import (
    PollingGate,
    PrecheckItem,
    PrecheckResult,
    RateLimiter,
    RunLog,
    RunResult,
    RunStatus,
)
from flashbuy.timesync import ServerClock
from tests.conftest import FakeClock, quiet_console


def test_rate_limiter_spacing_and_window():
    rl = RateLimiter(margin_ms=0)
    assert rl.reserve(100.0) == 100.0
    assert rl.reserve(100.1) == pytest.approx(100.4)
    assert rl.reserve(100.2) == pytest.approx(100.8)
    assert rl.reserve(101.5) == 101.5
    assert rl.reserve(101.6, not_after=101.8) is None  # slot 101.9 di luar jendela
    assert rl.reserve(50.0, not_before=200.0) == 200.0


def test_rate_limiter_rejects_faster_interval():
    with pytest.raises(ValueError):
        RateLimiter(min_interval_ms=300)


def test_rate_limiter_thread_safe():
    rl = RateLimiter(margin_ms=0)
    slots: list[float] = []

    def worker():
        for _ in range(50):
            slots.append(rl.reserve(0.0))

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    s = sorted(slots)
    assert len(set(s)) == 200
    assert all(b - a >= 0.4 - 1e-9 for a, b in zip(s, s[1:], strict=False))


def test_polling_gate_window():
    clock = FakeClock()
    sc = ServerClock(0.0, clock)
    open_at = sc.now() + 5.0
    gate = PollingGate(sc, open_at)

    async def go():
        times = []
        while await gate.acquire():
            times.append(sc.now())
        return times

    times = asyncio.run(go())
    assert times[0] >= open_at - 1.0 - 1e-3  # tidak sebelum T-1 s
    assert times[-1] <= open_at + 8.0
    assert all(b - a >= 0.4 for a, b in zip(times, times[1:], strict=False))
    assert 20 <= len(times) <= 22  # 9 s / 425 ms
    assert sc.now() >= gate.end - gate.limiter.interval_s  # slot berikut jatuh setelah T+8 s


def test_polling_gate_sync_variant():
    clock = FakeClock()
    sc = ServerClock(0.0, clock)
    gate = PollingGate(sc, sc.now() + 0.5)
    assert gate.acquire_sync()
    assert gate.acquire_sync()
    assert len(gate.limiter.history) == 2


def test_runlog_steps_and_result(tmp_path):
    clock = FakeClock()
    log = RunLog(tmp_path, ServerClock(1.0, clock), "web", quiet_console())
    s = log.mark("click_buy", "#1")
    log.warn("hati-hati")
    res = RunResult("web", RunStatus.DRYRUN_OK, "ok", False, steps=list(log.steps))
    path = log.write_result(res)
    log.close()
    data = json.loads(path.read_text())
    assert data["status"] == "DRYRUN_OK" and data["warnings"] == ["hati-hati"]
    assert data["steps"][0]["name"] == "click_buy" and data["steps"][0]["t_server_ms"] == s.t_server_ms
    text = (tmp_path / "web.log").read_text()
    assert "STEP click_buy - #1" in text and "WARN hati-hati" in text


def test_precheck_result_semantics():
    assert PrecheckResult("web", [PrecheckItem("a", True), PrecheckItem("b", None)]).ok
    assert not PrecheckResult("web", [PrecheckItem("a", False)]).ok
    assert not PrecheckResult("web", [], status=RunStatus.LOGIN_REQUIRED).ok
