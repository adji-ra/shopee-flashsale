"""Orchestrator: menjalankan semua jalur (web + Android) dalam satu run dengan satu ServerClock.

Jadwal (waktu server, dari satu ServerClock bersama):
  awal      timesync (oleh pemanggil), prepare semua jalur
  T-10 mnt  precheck semua jalur PARALEL; satu gagal -> lanjut dengan yang lolos + alarm; semua gagal -> batal
  T-2 mnt   resync (satu kali, dipakai semua jalur)
  T-60 s    arm semua jalur paralel
  T-lead    attempt tiap jalur pada lead_ms masing-masing (web.lead_ms / android.lead_ms)

Antar jalur (flashbuy.control.RunControl):
  - lock pemenang lewat before_place_order(): pemanggil pertama True, sisanya False -> ABORTED; tidak pernah
    dilepas; jalur lain menerima event batal (berhenti polling)
  - stop global: CAPTCHA / VERIFICATION / UNKNOWN_STATE di satu jalur -> semua jalur berhenti sebelum tap/klik/
    reload berikutnya; jalur yang SUDAH mengklik "Buat Pesanan" tetap menunggu layar PIN
  - PRICE_GUARD / SOLD_OUT / NOT_STARTED_TIMEOUT di satu jalur tidak menghentikan jalur lain

Hasil: tabel ringkasan, status gabungan berprioritas, SATU alarm hasil akhir (pola mendesak bila pesanan
(mungkin) terbuat), exit code (lihat EXIT_CODES / README).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from flashbuy.control import RunControl
from flashbuy.notifier import DeferredNotifier, Notifier
from flashbuy.runner_base import (
    MAYBE_ORDERED_MSG,
    PrecheckItem,
    PrecheckResult,
    RunLog,
    Runner,
    RunResult,
    RunStatus,
)
from flashbuy.session import Schedule, log_precheck, resync
from flashbuy.timesync import ServerClock, SyncReport

# ---- exit code (didokumentasikan di README)
EXIT_OK = 0  # ORDER_PLACED_AWAIT_PIN (live) / DRYRUN_OK (dry-run)
EXIT_NO_ORDER = 1  # tidak ada pesanan: SOLD_OUT, NOT_STARTED_TIMEOUT, PRICE_GUARD, TIMEOUT, ERROR, ABORTED
EXIT_USAGE = 2  # config / argumen / koneksi device salah (sebelum run dimulai)
EXIT_PRECHECK = 3  # precheck gagal di SEMUA jalur: run dibatalkan
EXIT_MANUAL = 4  # CAPTCHA / VERIFICATION / LOGIN_REQUIRED: selesaikan manual, jangan diulang otomatis
EXIT_UNKNOWN = 5  # UNKNOWN_STATE: cek manual (setelah "Buat Pesanan": pesanan MUNGKIN sudah terbuat)
EXIT_BUSY = 6  # run lain sedang berjalan (lock ~/.flashbuy/run.lock)
EXIT_INTERRUPTED = 130  # Ctrl+C

EXIT_CODES = {
    EXIT_OK: "pesanan dibuat (layar PIN) / dry-run lolos",
    EXIT_NO_ORDER: "tidak ada pesanan (habis, belum dibuka, pengaman harga, timeout, error, dibatalkan)",
    EXIT_USAGE: "config/argumen/koneksi device salah",
    EXIT_PRECHECK: "precheck gagal di semua jalur",
    EXIT_MANUAL: "captcha/verifikasi/login: selesaikan manual",
    EXIT_UNKNOWN: "UNKNOWN_STATE: cek manual (pesanan mungkin sudah terbuat)",
    EXIT_BUSY: "run lain sedang berjalan",
    EXIT_INTERRUPTED: "dibatalkan (Ctrl+C)",
}

UNKNOWN_AFTER_ORDER = "UNKNOWN_STATE (setelah order)"
STOP_POLL_S = 0.05  # jalur yang menunggu (arm / T-lead) memeriksa stop global sesering ini
# Prioritas status gabungan (indeks kecil menang). "UNKNOWN_STATE (setelah order)" = klik "Buat Pesanan" tanpa
# layar PIN: pesanan mungkin terbuat.
PRIORITY: tuple[str, ...] = (
    str(RunStatus.ORDER_PLACED_AWAIT_PIN),
    UNKNOWN_AFTER_ORDER,
    str(RunStatus.CAPTCHA),
    str(RunStatus.VERIFICATION),
    str(RunStatus.UNKNOWN_STATE),
    str(RunStatus.DRYRUN_OK),
    str(RunStatus.LOGIN_REQUIRED),
    str(RunStatus.PRICE_GUARD),
    str(RunStatus.SOLD_OUT),
    str(RunStatus.TIMEOUT),
    str(RunStatus.ERROR),
    str(RunStatus.NOT_STARTED_TIMEOUT),
    str(RunStatus.ABORTED),
)
KEY_STEPS = (("click_buy", "Beli"), ("buy_ok", "Beli OK"), ("checkout_loaded", "Checkout"),
             ("price_guard_ok", "Lapis 3"), ("click_place_order", "Buat Pesanan"), ("pin_screen", "PIN"))


@dataclass
class Lane:
    """Satu jalur (web/android) di bawah orchestrator."""

    name: str
    runner: Runner
    log: RunLog
    lead_ms: float
    precheck: PrecheckResult | None = None
    result: RunResult | None = None
    skipped: str = ""  # alasan tidak ikut attempt (precheck gagal / prepare gagal)

    @property
    def order_clicked(self) -> bool:
        return bool(getattr(self.runner, "order_clicked", False))

    def label(self) -> str:
        """Status untuk prioritas. Setelah klik 'Buat Pesanan' (atau PIN tanpa klik alat) tanpa layar PIN,
        APA PUN statusnya (UNKNOWN_STATE, juga CAPTCHA/VERIFICATION/LOGIN_REQUIRED yang dipertahankan
        after_order_click) = pesanan MUNGKIN terbuat -> UNKNOWN_STATE (setelah order): alarm mendesak, exit 5."""
        if self.result is None:
            return str(RunStatus.ERROR)
        if self.result.status != RunStatus.ORDER_PLACED_AWAIT_PIN and (
                self.order_clicked or self.result.message == MAYBE_ORDERED_MSG):
            return UNKNOWN_AFTER_ORDER
        return str(self.result.status)


@dataclass
class Outcome:
    lanes: list[Lane]
    status: str  # salah satu PRIORITY
    winner: str | None
    exit_code: int
    message: str
    alarms: list[dict] = field(default_factory=list)  # alarm yang dibunyikan orchestrator

    @property
    def order_placed(self) -> bool:
        return self.status in (str(RunStatus.ORDER_PLACED_AWAIT_PIN), UNKNOWN_AFTER_ORDER)


def wire(lane: Lane, control: RunControl) -> None:
    """Hubungkan runner ke sinyal bersama + notifier tunda (alarm hanya dari orchestrator)."""
    r = lane.runner
    r.stop_event = control.stop_view(lane.name)
    r.cancel_event = control.cancel_event(lane.name)
    r.before_place_order = control.place_order_gate(lane.name)
    # rate limit polling TIDAK dibagi: setiap jalur memakai RateLimiter-nya sendiri (1 aksi / 400 ms per jalur)
    r.notifier = DeferredNotifier()


def combined(lanes: list[Lane]) -> str:
    labels = [lane.label() for lane in lanes]
    return min(labels, key=lambda s: PRIORITY.index(s) if s in PRIORITY else len(PRIORITY))


def exit_code_for(status: str, lanes: list[Lane]) -> int:
    if status in (str(RunStatus.ORDER_PLACED_AWAIT_PIN), str(RunStatus.DRYRUN_OK)):
        return EXIT_OK
    if status in (UNKNOWN_AFTER_ORDER, str(RunStatus.UNKNOWN_STATE)):
        return EXIT_UNKNOWN
    if status in (str(RunStatus.CAPTCHA), str(RunStatus.VERIFICATION)):
        return EXIT_MANUAL
    if lanes and all(lane.skipped for lane in lanes):
        return EXIT_PRECHECK
    if status == str(RunStatus.LOGIN_REQUIRED):
        return EXIT_MANUAL
    return EXIT_NO_ORDER


def _deferred(lane: Lane) -> list[dict]:
    n = getattr(lane.runner, "notifier", None)
    return [e for e in getattr(n, "events", []) if e.get("level") == "alarm"]


def _precheck_failure(pre: PrecheckResult) -> str:
    failed = [f"{i.name}: {i.detail}" for i in pre.items if i.ok is False]
    return "; ".join(failed) or str(pre.status or "gagal")


async def _wait_or_stop(clock: ServerClock, t: float, control: RunControl, lead_ms: float = 0.0) -> float | None:
    """Tunggu sampai `t - lead_ms` (jam server) ATAU stop global, mana yang lebih dulu. Stop (mis. captcha dari
    event halaman web saat menunggu arm/T-lead) membangunkan semua jalur segera supaya alarm tidak menunggu T.
    Return keterlambatan ms, atau None bila stop lebih dulu."""
    deadline = t - lead_ms / 1000.0
    while not control.stopped():
        if deadline - clock.now() <= 2 * STOP_POLL_S + clock.spin_threshold_s:
            return await clock.wait_until_async(t, lead_ms)  # sisa singkat: presisi penuh (busy-wait akhir)
        await clock.clock.asleep(STOP_POLL_S)
    return None


async def _guarded(lane: Lane, phase: str, coro) -> object:
    """Jalankan satu fase runner; exception tak terduga -> dicatat (bukan menjatuhkan jalur lain)."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - satu jalur gagal tidak boleh menghentikan jalur lain
        lane.log.error(f"{phase} gagal: {type(e).__name__}: {e}")
        return e


async def orchestrate(lanes: list[Lane], *, open_at: float, live: bool, clock: ServerClock, notifier: Notifier,
                      control: RunControl, sync_fn: Callable[[], SyncReport] | None = None,
                      schedule: Schedule | None = None) -> Outcome:
    """Jalankan semua jalur sesuai jadwal; runner harus sudah di-`wire`. Tidak menutup runner (pemanggil)."""
    schedule = schedule or Schedule()
    control.set_clock(lambda: clock.now_ms())
    alarms: list[dict] = []

    def alarm(event: str, message: str, pattern: str) -> None:
        notifier.alarm(event, message, pattern=pattern, platform="+".join(lane.name for lane in lanes))
        alarms.append({"event": event, "message": message, "pattern": pattern})

    for lane in lanes:
        lane.log.info(f"orchestrator: mode {'LIVE' if live else 'DRY-RUN'}, lead {lane.lead_ms:.0f} ms, "
                      f"offset {clock.offset_s * 1000:+.1f} ms, jalur {', '.join(x.name for x in lanes)}")

    # ---- prepare (paralel)
    prepared = await asyncio.gather(*(_guarded(lane, "prepare", lane.runner.prepare()) for lane in lanes))
    for lane, res in zip(lanes, prepared, strict=True):
        if isinstance(res, Exception):
            lane.precheck = PrecheckResult(lane.name, [PrecheckItem("prepare", False, f"{type(res).__name__}: {res}")],
                                           status=RunStatus.ERROR)

    # ---- precheck (paralel)
    await clock.wait_until_async(open_at - schedule.precheck_before_s)
    todo = [lane for lane in lanes if lane.precheck is None]
    for lane in todo:
        lane.log.mark("precheck")
    pres = await asyncio.gather(*(_guarded(lane, "precheck", lane.runner.precheck()) for lane in todo))
    for lane, pre in zip(todo, pres, strict=True):
        if isinstance(pre, Exception):
            pre = PrecheckResult(lane.name, [PrecheckItem("precheck", False, f"{type(pre).__name__}: {pre}")],
                                 status=RunStatus.ERROR)
        lane.precheck = pre
        log_precheck(lane.log, pre)
    for lane in lanes:
        if lane.precheck.status in (RunStatus.CAPTCHA, RunStatus.VERIFICATION, RunStatus.UNKNOWN_STATE):
            control.stop_all(lane.name, f"precheck {lane.precheck.status}")
        if not lane.precheck.ok:
            lane.skipped = f"precheck gagal: {_precheck_failure(lane.precheck)}"
    ok_lanes = [lane for lane in lanes if not lane.skipped]
    pre_alarms = [f"{lane.name}: {e['message']}" for lane in lanes for e in _deferred(lane)]
    failed = [f"{lane.name} ({lane.skipped})" for lane in lanes if lane.skipped]
    if control.stopped() and ok_lanes:  # captcha/verifikasi saat precheck = stop semua jalur
        for lane in ok_lanes:
            lane.skipped = f"dibatalkan: {control.stop_reason} di {control.stop_source}"
        ok_lanes = []
    if not ok_lanes:
        msg = "precheck gagal di semua jalur, run dibatalkan: " + "; ".join(failed)
        alarm("precheck", msg, "short")
        return _finish(lanes, live, control, alarms, final_alarm=None)
    if failed:
        alarm("precheck_satu_platform", f"jalan dengan satu platform ({ok_lanes[0].name}): " + "; ".join(failed),
              "short")
    elif pre_alarms:
        alarm("precheck_peringatan", "peringatan precheck (run tetap jalan): " + "; ".join(pre_alarms), "short")
    for lane in lanes:
        if lane.skipped:
            lane.log.warn(f"jalur tidak ikut: {lane.skipped}")

    # ---- resync T-2 mnt (satu kali, jam bersama)
    resync_at = open_at - schedule.resync_before_s
    if sync_fn is not None:
        if clock.now() < resync_at and await _wait_or_stop(clock, resync_at, control) is not None:
            for lane in ok_lanes:
                lane.log.mark("resync")
            clock = await resync(clock, sync_fn, ok_lanes[0].log)
            control.set_clock(lambda: clock.now_ms())
            for lane in lanes:
                lane.log.clock = clock
        elif not control.stopped():
            ok_lanes[0].log.info("run dimulai setelah T-2 menit; timesync awal masih segar, resync dilewati")

    # ---- arm T-60 s (paralel)
    await _wait_or_stop(clock, open_at - schedule.arm_before_s, control)  # stop: arm segera (berhenti tanpa aksi)
    for lane in ok_lanes:
        lane.log.mark("arm")
    armed = await asyncio.gather(*(_guarded(lane, "arm", lane.runner.arm(open_at)) for lane in ok_lanes))
    for lane, res in zip(ok_lanes, armed, strict=True):
        if isinstance(res, Exception):  # arm melempar (bug/driver): jalur ini selesai ERROR, tanpa attempt
            lane.result = RunResult(lane.name, RunStatus.ERROR, f"arm: {type(res).__name__}: {res}", live,
                                    steps=list(lane.log.steps))
            lane.log.write_result(lane.result)

    # ---- attempt T-lead per jalur
    async def attempt(lane: Lane) -> None:
        # stop global / arm gagal (captcha, verifikasi, login, intent gagal di T-60 s): hasil diminta SEKARANG
        # (attempt langsung berhenti tanpa aksi) supaya alarm tidak menunggu T
        if not control.stopped() and not getattr(lane.runner, "arm_blocked", False):
            late_ms = await _wait_or_stop(clock, open_at, control, lane.lead_ms)
            if late_ms is not None:
                lane.log.mark("t_minus_lead", f"lead {lane.lead_ms:.0f} ms, telat {late_ms:.1f} ms")
        res = await _guarded(lane, "attempt", lane.runner.attempt(clock, live))
        if isinstance(res, Exception):
            res = RunResult(lane.name, RunStatus.ERROR, f"{type(res).__name__}: {res}", live,
                            steps=list(lane.log.steps))
        lane.result = res

    await asyncio.gather(*(attempt(lane) for lane in ok_lanes if lane.result is None))
    return _finish(lanes, live, control, alarms, final_alarm=alarm)


def _finish(lanes: list[Lane], live: bool, control: RunControl, alarms: list[dict], final_alarm) -> Outcome:
    for lane in lanes:
        if lane.result is None:
            pre = lane.precheck
            status = pre.status if pre is not None and pre.status is not None else (
                RunStatus.ABORTED if lane.skipped.startswith("dibatalkan") else RunStatus.ERROR)
            lane.result = RunResult(lane.name, status, lane.skipped or "tidak dijalankan", live,
                                    steps=list(lane.log.steps))
            lane.log.write_result(lane.result)
    status = combined(lanes)
    code = exit_code_for(status, lanes)
    parts = [f"{lane.name}: {lane.label()} - {lane.result.message}" for lane in lanes]
    if control.winner:
        parts.append(f"pemenang lock 'Buat Pesanan': {control.winner}")
    message = " | ".join(parts)
    outcome = Outcome(lanes, status, control.winner, code, message, alarms)
    if final_alarm is not None:
        final_alarm(status, message, "urgent" if outcome.order_placed else "short")
    return outcome


def step_ms(lane: Lane, name: str, open_at: float) -> int | None:
    if lane.result is None:
        return None
    step = lane.result.step(name)
    return None if step is None else step.t_server_ms - int(open_at * 1000)
