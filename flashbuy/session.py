"""Alur satu runner sesuai jadwal: pre-check -> resync T-2 mnt -> arm T-60 s -> attempt T-lead.

Semua titik jadwal ada di `Schedule` supaya tes bisa menskalakannya. Jendela polling
(T-1 s .. T+8 s) dan rate limit 400 ms TIDAK ikut diskalakan (batasan keras).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from flashbuy.config import OPEN_PAGE_BEFORE_S, PRECHECK_BEFORE_S, RESYNC_BEFORE_S, RESYNC_WARN_MS
from flashbuy.notifier import Notifier
from flashbuy.runner_base import PrecheckResult, RunLog, Runner, RunResult, RunStatus
from flashbuy.timesync import ServerClock, SyncReport


@dataclass
class Schedule:
    precheck_before_s: float = PRECHECK_BEFORE_S
    resync_before_s: float = RESYNC_BEFORE_S
    arm_before_s: float = OPEN_PAGE_BEFORE_S


def log_precheck(log: RunLog, pre: PrecheckResult) -> None:
    for item in pre.items:
        tag = {True: "OK", False: "GAGAL", None: "PERINGATAN"}[item.ok]
        text = f"precheck {item.name}: {tag} - {item.detail}"
        (log.info if item.ok else log.warn)(text)


async def resync(clock: ServerClock, sync_fn: Callable[[], SyncReport], log: RunLog) -> ServerClock:
    """Timesync ulang. Offset baru selalu dipakai; beda > RESYNC_WARN_MS dicatat sebagai peringatan."""
    report = await asyncio.to_thread(sync_fn)
    if report.primary is None:
        log.warn("resync gagal (semua sumber waktu gagal); tetap memakai offset lama")
        return clock
    new_offset = report.offset_s
    diff_ms = (new_offset - clock.offset_s) * 1000
    text = (f"resync: offset {clock.offset_s * 1000:+.1f} -> {new_offset * 1000:+.1f} ms "
            f"(beda {diff_ms:+.1f} ms, ±{report.primary.uncertainty_ms:.1f} ms, {report.primary.source})")
    if abs(diff_ms) > RESYNC_WARN_MS:
        log.warn(text + f" > {RESYNC_WARN_MS} ms, memakai offset baru")
    else:
        log.info(text)
    new_clock = ServerClock(new_offset, clock.clock, clock.spin_threshold_s)
    log.clock = new_clock
    return new_clock


async def run_single(runner: Runner, *, open_at: float, live: bool, lead_ms: float,
                     clock: ServerClock, log: RunLog, notifier: Notifier,
                     sync_fn: Callable[[], SyncReport] | None = None,
                     schedule: Schedule | None = None) -> RunResult:
    schedule = schedule or Schedule()
    log.info(f"mode {'LIVE' if live else 'DRY-RUN'}, lead {lead_ms:.0f} ms, offset {clock.offset_s * 1000:+.1f} ms")
    await runner.prepare()

    await clock.wait_until_async(open_at - schedule.precheck_before_s)
    log.mark("precheck")
    pre = await runner.precheck()
    log_precheck(log, pre)
    failed = ", ".join(f"{i.name}: {i.detail}" for i in pre.items if i.ok is False)
    if not pre.ok:
        notifier.alarm("precheck", f"pre-check gagal: {failed}", platform=runner.name)
    if pre.status is not None:
        result = RunResult(runner.name, pre.status, f"pre-check: {failed}", live, steps=list(log.steps))
        log.write_result(result)
        return result

    resync_at = open_at - schedule.resync_before_s
    if sync_fn is not None:
        if clock.now() < resync_at:
            await clock.wait_until_async(resync_at)
            log.mark("resync")
            clock = await resync(clock, sync_fn, log)
        else:
            log.info("run dimulai setelah T-2 menit; timesync awal masih segar, resync dilewati")

    await clock.wait_until_async(open_at - schedule.arm_before_s)
    log.mark("arm")
    await runner.arm(open_at)

    late_ms = await clock.wait_until_async(open_at, lead_ms)
    log.mark("t_minus_lead", f"lead {lead_ms:.0f} ms, telat {late_ms:.1f} ms")
    return await runner.attempt(clock, live)


def step_offsets(result: RunResult, open_at: float) -> list[tuple[str, int, str]]:
    """(nama, ms relatif T, detail) untuk ringkasan."""
    t_ms = int(open_at * 1000)
    return [(s.name, s.t_server_ms - t_ms, s.detail) for s in result.steps]


__all__ = ["RunStatus", "Schedule", "log_precheck", "resync", "run_single", "step_offsets"]
