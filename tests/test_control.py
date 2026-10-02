"""Unit: RunControl (lock pemenang, batal, stop global), file lock antar-proses, status gabungan & exit code,
pola alarm, dan pemeriksaan stop sebelum setiap aksi Android (TimedDriver.before_action)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from flashbuy.android_driver import FakeDriver, Sel, TimedDriver
from flashbuy.control import RunControl
from flashbuy.notifier import DeferredNotifier, Notifier
from flashbuy.orchestrator import (
    EXIT_CODES,
    EXIT_MANUAL,
    EXIT_NO_ORDER,
    EXIT_OK,
    EXIT_PRECHECK,
    EXIT_UNKNOWN,
    PRIORITY,
    UNKNOWN_AFTER_ORDER,
    Lane,
    combined,
    exit_code_for,
)
from flashbuy.runlock import RunLock, RunLockBusy, home_dir
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunResult, RunStatus
from tests.android_harness import RealClock
from tests.fake_android import AppScenario, FakeShopeeApp

# ------------------------------------------------------------------ RunControl


def test_first_gate_call_wins_rest_false_and_lock_never_released():
    c = RunControl()
    web, android = c.place_order_gate("web"), c.place_order_gate("android")
    assert android() is True
    assert web() is False and android() is False, "lock tidak boleh diambil dua kali, juga oleh pemenang"
    assert c.winner == "android" and c.lock_held()
    c.stop_all("android", "UNKNOWN_STATE")  # pemenang gagal setelahnya: lock tetap dipegang
    assert web() is False and c.lock_held()
    assert [(n, ok) for n, ok, _ in c.gate_calls] == [("android", True), ("web", False), ("android", False),
                                                      ("web", False)]


def test_winning_sets_cancel_event_of_other_lanes_only():
    c = RunControl()
    gates = {n: c.place_order_gate(n) for n in ("web", "android")}
    events = {n: c.cancel_event(n) for n in gates}
    assert not any(e.is_set() for e in events.values())
    assert gates["web"]() is True
    assert events["android"].is_set() and not events["web"].is_set()


def test_stop_blocks_gate_before_anyone_won():
    c = RunControl()
    gate = c.place_order_gate("web")
    c.stop_view("android").set()
    assert gate() is False and c.winner is None and not c.lock_held()


def test_stop_all_idempotent_first_source_kept():
    c = RunControl(now_ms=lambda: 1234)
    c.stop_all("web", "CAPTCHA")
    c.stop_all("android", "VERIFICATION")
    assert (c.stop_source, c.stop_reason, c.stop_ms) == ("web", "CAPTCHA", 1234)
    view = c.stop_view("android")
    assert view.is_set() and view.reason == "stop global dari web: CAPTCHA"


def test_concurrent_gate_calls_exactly_one_winner():
    c = RunControl()
    names = [f"lane{i}" for i in range(16)]
    gates = [c.place_order_gate(n) for n in names]
    start = threading.Barrier(len(gates))
    results: list[bool] = []
    mu = threading.Lock()

    def call(g):
        start.wait()
        ok = g()
        with mu:
            results.append(ok)

    threads = [threading.Thread(target=call, args=(g,)) for g in gates]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(results) == [False] * 15 + [True]
    assert sum(c.cancel_event(n).is_set() for n in names) == 15


# ------------------------------------------------------------------ stop sebelum setiap aksi Android


def test_timed_driver_checks_before_every_action_and_skips_the_action_when_stopped():
    clock = RealClock()
    app = FakeShopeeApp(AppScenario(open_at=time.time() + 60), clock)
    inner = FakeDriver(app, clock, latency_s=0)
    d = TimedDriver(inner, time.perf_counter, lambda: int(time.time() * 1000))
    seen: list[str] = []
    stopped = threading.Event()

    def before(op: str) -> None:
        seen.append(op)
        if stopped.is_set():
            raise RuntimeError("stop")

    d.before_action = before
    d.start_url("https://shopee.co.id/x", "com.shopee.id")
    d.swipe_refresh()
    d.press_back()
    d.click(Sel("text", "Beli Sekarang"))
    d.exists(Sel("text", "Beli Sekarang"))  # baca: tidak lewat before_action
    assert seen == ["start_url", "swipe_refresh", "press_back", "click"]
    stopped.set()
    n = len(inner.calls)
    for act in (lambda: d.click(Sel("text", "Beli Sekarang")), d.swipe_refresh, d.press_back,
                lambda: d.start_url("https://shopee.co.id/x", "com.shopee.id")):
        with pytest.raises(RuntimeError):
            act()
    assert inner.calls[n:] == [], "aksi tidak boleh sampai ke device setelah stop"


# ------------------------------------------------------------------ status gabungan & exit code


def _lane(name: str, status: RunStatus | None, *, message: str = "", skipped: str = "",
          order_clicked: bool = False) -> Lane:
    runner = type("R", (), {"order_clicked": order_clicked})()
    res = None if status is None else RunResult(name, status, message, live=True)
    return Lane(name, runner, None, 0, result=res, skipped=skipped)


@pytest.mark.parametrize(("a", "b", "expected"), [
    (RunStatus.ORDER_PLACED_AWAIT_PIN, RunStatus.CAPTCHA, "ORDER_PLACED_AWAIT_PIN"),
    (RunStatus.ABORTED, RunStatus.ORDER_PLACED_AWAIT_PIN, "ORDER_PLACED_AWAIT_PIN"),
    ("after_order", RunStatus.CAPTCHA, UNKNOWN_AFTER_ORDER),
    (RunStatus.CAPTCHA, RunStatus.DRYRUN_OK, "CAPTCHA"),
    (RunStatus.VERIFICATION, RunStatus.PRICE_GUARD, "VERIFICATION"),
    (RunStatus.UNKNOWN_STATE, RunStatus.DRYRUN_OK, "UNKNOWN_STATE"),
    (RunStatus.DRYRUN_OK, RunStatus.ABORTED, "DRYRUN_OK"),
    (RunStatus.PRICE_GUARD, RunStatus.SOLD_OUT, "PRICE_GUARD"),
    (RunStatus.ERROR, RunStatus.NOT_STARTED_TIMEOUT, "ERROR"),
    (RunStatus.ABORTED, RunStatus.NOT_STARTED_TIMEOUT, "NOT_STARTED_TIMEOUT"),
])
def test_combined_status_priority(a, b, expected):
    def mk(name, s):
        if s == "after_order":
            return _lane(name, RunStatus.UNKNOWN_STATE, message=MAYBE_ORDERED_MSG, order_clicked=True)
        return _lane(name, s)

    assert combined([mk("web", a), mk("android", b)]) == expected
    assert combined([mk("android", b), mk("web", a)]) == expected


@pytest.mark.parametrize("status", [RunStatus.CAPTCHA, RunStatus.VERIFICATION, RunStatus.LOGIN_REQUIRED,
                                    RunStatus.UNKNOWN_STATE])
def test_any_status_after_order_click_is_maybe_ordered(status):
    """Captcha/verifikasi/login SETELAH "Buat Pesanan" (status dipertahankan after_order_click): pesanan MUNGKIN
    terbuat -> status gabungan UNKNOWN_STATE (setelah order), exit 5 (bukan 4), mengalahkan CAPTCHA jalur lain."""
    after = _lane("web", status, message=MAYBE_ORDERED_MSG, order_clicked=True)
    other = _lane("android", RunStatus.CAPTCHA)
    assert after.label() == UNKNOWN_AFTER_ORDER
    status_all = combined([other, after])
    assert status_all == UNKNOWN_AFTER_ORDER and exit_code_for(status_all, [other, after]) == EXIT_UNKNOWN
    pin = _lane("web", RunStatus.ORDER_PLACED_AWAIT_PIN, order_clicked=True)
    assert pin.label() == "ORDER_PLACED_AWAIT_PIN"


def test_unknown_state_before_order_is_not_after_order():
    assert _lane("web", RunStatus.UNKNOWN_STATE, message="layar tidak dikenali").label() == "UNKNOWN_STATE"
    assert _lane("web", RunStatus.UNKNOWN_STATE, order_clicked=True).label() == UNKNOWN_AFTER_ORDER
    assert PRIORITY.index(UNKNOWN_AFTER_ORDER) < PRIORITY.index("CAPTCHA") < PRIORITY.index("UNKNOWN_STATE")


@pytest.mark.parametrize(("status", "skipped", "code"), [
    ("ORDER_PLACED_AWAIT_PIN", False, EXIT_OK),
    ("DRYRUN_OK", False, EXIT_OK),
    (UNKNOWN_AFTER_ORDER, False, EXIT_UNKNOWN),
    ("UNKNOWN_STATE", False, EXIT_UNKNOWN),
    ("CAPTCHA", False, EXIT_MANUAL),
    ("VERIFICATION", True, EXIT_MANUAL),  # captcha saat precheck: tetap 4 (tangani manual), bukan 3
    ("LOGIN_REQUIRED", True, EXIT_PRECHECK),
    ("LOGIN_REQUIRED", False, EXIT_MANUAL),
    ("ERROR", True, EXIT_PRECHECK),
    ("PRICE_GUARD", False, EXIT_NO_ORDER),
    ("SOLD_OUT", False, EXIT_NO_ORDER),
    ("ABORTED", False, EXIT_NO_ORDER),
])
def test_exit_code_mapping(status, skipped, code):
    lanes = [_lane("web", RunStatus.ERROR, skipped="precheck gagal" if skipped else "")]
    assert exit_code_for(status, lanes) == code


def test_exit_codes_documented_and_distinct():
    assert len(set(EXIT_CODES.values())) == len(EXIT_CODES)
    assert {0, 1, 2, 3, 4, 5, 6, 130} == set(EXIT_CODES)


# ------------------------------------------------------------------ pola alarm


def test_alarm_patterns_short_and_urgent():
    n = Notifier(beep=lambda f, d: None, post=lambda u, p: None, repeat=10, freq=1800, duration_ms=350, gap_s=0.2)
    assert n.beeps("short") == [(1800, 350, 0.2)] * 3
    urgent = n.beeps("urgent")
    assert len(urgent) == 30 and {f for f, _, _ in urgent} == {2200, 1400}
    assert all(d == 450 and g == 0.1 for _, d, g in urgent)
    assert sum(d for _, d, _ in urgent) > 10 * sum(d for _, d, _ in n.beeps("short"))  # panjang vs pendek
    with pytest.raises(ValueError):
        n.alarm("x", "y", pattern="aneh")


def test_alarm_pattern_in_webhook_payload_and_deferred_notifier_silent():
    beeps, posts = [], []
    n = Notifier("http://hook", beep=lambda f, d: beeps.append(f), post=lambda u, p: posts.append(p), repeat=1)
    n.alarm("ORDER_PLACED_AWAIT_PIN", "pesanan", pattern="urgent")
    n.join(5)
    assert posts[0]["pattern"] == "urgent" and len(beeps) == 3
    d = DeferredNotifier()
    d.alarm("CAPTCHA", "captcha", pattern="short")
    d.join(1)
    assert [e["event"] for e in d.events] == ["CAPTCHA"]


# ------------------------------------------------------------------ file lock antar-proses

HOLDER = textwrap.dedent("""
    import sys
    from flashbuy.runlock import RunLock
    lock = RunLock(sys.argv[1]).acquire()
    print("held", flush=True)
    sys.stdin.read()
""")


@pytest.fixture
def holder():
    """Proses lain yang memegang ~/.flashbuy/run.lock (FLASHBUY_HOME dari conftest) sampai stdin ditutup."""
    procs: list[subprocess.Popen] = []

    def start(command: str = "run") -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", HOLDER, command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, cwd=Path(__file__).parents[1])  # repo root: paket flashbuy terimpor
        procs.append(p)
        assert p.stdout.readline().strip() == "held"
        return p

    yield start
    for p in procs:
        if p.poll() is None:
            p.stdin.close()
            p.wait(10)


def test_second_process_rejected_with_clear_message(holder):
    p = holder("run")
    with pytest.raises(RunLockBusy) as e:
        RunLock("doctor").acquire()
    msg = str(e.value)
    assert f"PID {p.pid} (run," in msg and "flashbuy lain sedang berjalan" in msg
    assert str(home_dir() / "run.lock") in msg


def test_lock_released_when_holder_exits_even_on_kill(holder):
    p = holder("run")
    p.kill()
    p.wait(10)
    with RunLock("run"):
        with pytest.raises(RunLockBusy):
            RunLock("precheck").acquire()  # deskriptor kedua di proses yang sama juga ditolak
    RunLock("run").acquire().release()
