"""Jadwal run_single dengan runner palsu & jam palsu (tanpa menunggu sungguhan)."""

from __future__ import annotations

import asyncio

from flashbuy.notifier import Notifier
from flashbuy.runner_base import PrecheckItem, PrecheckResult, RunLog, RunResult, RunStatus
from flashbuy.session import Schedule, run_single
from flashbuy.timesync import OffsetResult, ServerClock, SyncReport
from tests.conftest import FakeClock, quiet_console


class FakeRunner:
    name = "fake"

    def __init__(self, log, pre: PrecheckResult | None = None):
        self.log = log
        self.pre = pre or PrecheckResult("fake", [PrecheckItem("login", True)])
        self.calls: list[tuple[str, float]] = []
        self.attempt_clock = None
        self.before_place_order = lambda: True

    def _t(self):
        return self.log.clock.now()

    async def prepare(self):
        self.calls.append(("prepare", self._t()))

    async def precheck(self):
        self.calls.append(("precheck", self._t()))
        return self.pre

    async def arm(self, open_at):
        self.calls.append(("arm", self._t()))

    async def attempt(self, clock, live):
        self.calls.append(("attempt", clock.now()))
        self.attempt_clock = clock
        return RunResult(self.name, RunStatus.DRYRUN_OK, "ok", live)

    async def abort(self, reason=""):
        pass

    async def close(self):
        pass


def _report(offset_ms: float) -> SyncReport:
    return SyncReport(ntp=OffsetResult("ntp", error="x"),
                      shopee=OffsetResult("http", offset_ms=offset_ms, uncertainty_ms=10, rtt_ms=5, samples=8))


def _setup(tmp_path, offset_s=0.0):
    clock = ServerClock(offset_s, FakeClock())
    log = RunLog(tmp_path, clock, "fake", quiet_console())
    return clock, log


def test_schedule_order_and_resync_warning(tmp_path):
    clock, log = _setup(tmp_path, 0.100)
    open_at = clock.now() + 700.0  # mulai > T-10 menit
    runner = FakeRunner(log)
    syncs = []

    def sync_fn():
        syncs.append(1)
        return _report(180.0)  # beda +80 ms dari offset awal +100 ms

    res = asyncio.run(run_single(runner, open_at=open_at, live=False, lead_ms=150, clock=clock, log=log,
                                 notifier=Notifier(beep=lambda f, d: None), sync_fn=sync_fn))
    assert res.status == RunStatus.DRYRUN_OK
    names = [c[0] for c in runner.calls]
    assert names == ["prepare", "precheck", "arm", "attempt"]
    t = dict(runner.calls)
    # waktu dicatat dengan jam log (offset baru setelah resync)
    assert abs(t["precheck"] - (open_at - 600)) < 0.01
    assert abs(t["arm"] - (open_at - 60)) < 0.01
    assert abs(t["attempt"] - (open_at - 0.150)) < 0.002
    assert syncs == [1]
    assert runner.attempt_clock.offset_s == 0.180  # offset baru dipakai
    assert log.clock is runner.attempt_clock
    assert any("beda +80.0 ms" in w for w in log.warnings)
    step_names = [s.name for s in log.steps]
    assert step_names.index("resync") < step_names.index("arm")


def test_resync_small_difference_no_warning(tmp_path):
    clock, log = _setup(tmp_path, 0.100)
    open_at = clock.now() + 300.0
    runner = FakeRunner(log)
    asyncio.run(run_single(runner, open_at=open_at, live=False, lead_ms=150, clock=clock, log=log,
                           notifier=Notifier(beep=lambda f, d: None), sync_fn=lambda: _report(120.0)))
    assert runner.attempt_clock.offset_s == 0.120
    assert not log.warnings


def test_resync_failure_keeps_old_offset(tmp_path):
    clock, log = _setup(tmp_path, 0.05)
    runner = FakeRunner(log)
    bad = SyncReport(ntp=OffsetResult("ntp", error="x"), shopee=OffsetResult("http", error="y"))
    asyncio.run(run_single(runner, open_at=clock.now() + 200, live=False, lead_ms=0, clock=clock, log=log,
                           notifier=Notifier(beep=lambda f, d: None), sync_fn=lambda: bad))
    assert runner.attempt_clock.offset_s == 0.05
    assert any("resync gagal" in w for w in log.warnings)


def test_late_start_skips_resync(tmp_path):
    clock, log = _setup(tmp_path)
    runner = FakeRunner(log)
    called = []
    asyncio.run(run_single(runner, open_at=clock.now() + 30, live=False, lead_ms=150, clock=clock, log=log,
                           notifier=Notifier(beep=lambda f, d: None),
                           sync_fn=lambda: called.append(1) or _report(0)))
    assert called == []
    assert "resync" not in [s.name for s in log.steps]


def test_scaled_schedule(tmp_path):
    clock, log = _setup(tmp_path)
    runner = FakeRunner(log)
    open_at = clock.now() + 10
    asyncio.run(run_single(runner, open_at=open_at, live=False, lead_ms=150, clock=clock, log=log,
                           notifier=Notifier(beep=lambda f, d: None), sync_fn=lambda: _report(0),
                           schedule=Schedule(precheck_before_s=6, resync_before_s=4, arm_before_s=2)))
    t = dict(runner.calls)
    assert abs(t["precheck"] - (open_at - 6)) < 0.01
    assert abs(t["arm"] - (open_at - 2)) < 0.01


def test_login_required_precheck_stops_and_alarms(tmp_path):
    clock, log = _setup(tmp_path)
    pre = PrecheckResult("fake", [PrecheckItem("login", False, "tidak login")], status=RunStatus.LOGIN_REQUIRED)
    runner = FakeRunner(log, pre)
    n = Notifier(beep=lambda f, d: None, repeat=1)
    res = asyncio.run(run_single(runner, open_at=clock.now() + 5, live=True, lead_ms=150, clock=clock,
                                 log=log, notifier=n))
    assert res.status == RunStatus.LOGIN_REQUIRED
    assert [c[0] for c in runner.calls] == ["prepare", "precheck"]
    assert n.events[0]["event"] == "precheck"


def test_precheck_warning_only_continues(tmp_path):
    clock, log = _setup(tmp_path)
    pre = PrecheckResult("fake", [PrecheckItem("login", True), PrecheckItem("saldo", None, "tak terbaca")])
    runner = FakeRunner(log, pre)
    n = Notifier(beep=lambda f, d: None, repeat=1)
    res = asyncio.run(run_single(runner, open_at=clock.now() + 5, live=False, lead_ms=150, clock=clock,
                                 log=log, notifier=n))
    assert res.status == RunStatus.DRYRUN_OK and n.events == []


def test_precheck_failure_alarms_but_continues(tmp_path):
    clock, log = _setup(tmp_path)
    pre = PrecheckResult("fake", [PrecheckItem("login", True), PrecheckItem("saldo", False, "kurang")])
    runner = FakeRunner(log, pre)
    n = Notifier(beep=lambda f, d: None, repeat=1)
    res = asyncio.run(run_single(runner, open_at=clock.now() + 5, live=False, lead_ms=150, clock=clock,
                                 log=log, notifier=n))
    assert res.status == RunStatus.DRYRUN_OK
    assert n.events[0]["event"] == "precheck" and "kurang" in n.events[0]["message"]
