"""Kontrak runner bersama (web sekarang, Android di tahap 3).

Aturan waktu:
- RateLimiter + jendela T-1 s .. T+8 s HANYA untuk fase polling/retry (klik ulang Beli, reload).
- Langkah maju (Checkout, pilih ShopeePay, Buat Pesanan) dijalankan sekali tanpa throttle,
  dengan batas total FLOW_TIMEOUT_S setelah klik Beli berhasil.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from rich.console import Console

from flashbuy.config import MIN_ACTION_INTERVAL_MS, POLL_WINDOW_AFTER_S, POLL_WINDOW_BEFORE_S
from flashbuy.timesync import ServerClock, format_ts

# Margin kecil di atas 400 ms supaya jitter jaringan tidak membuat server melihat jarak < 400 ms.
RATE_MARGIN_MS = 25


class RunStatus(StrEnum):
    DRYRUN_OK = "DRYRUN_OK"
    ORDER_PLACED_AWAIT_PIN = "ORDER_PLACED_AWAIT_PIN"
    NOT_STARTED_TIMEOUT = "NOT_STARTED_TIMEOUT"
    SOLD_OUT = "SOLD_OUT"
    CAPTCHA = "CAPTCHA"
    VERIFICATION = "VERIFICATION"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    TIMEOUT = "TIMEOUT"
    PRICE_GUARD = "PRICE_GUARD"  # harga/isi pesanan di luar batas atau tidak terbaca (fail-closed)
    UNKNOWN_STATE = "UNKNOWN_STATE"  # halaman tak dikenali terlalu lama (jaring pengaman)
    ABORTED = "ABORTED"
    ERROR = "ERROR"


# CAPTCHA/verifikasi/halaman tak dikenal: hentikan SEMUA runner, alarm, tanpa retry.
STOP_ALL_STATUSES = frozenset({RunStatus.CAPTCHA, RunStatus.VERIFICATION, RunStatus.UNKNOWN_STATE})
# Status yang butuh tindakan manual -> browser/app dibiarkan terbuka.
NEEDS_USER_STATUSES = frozenset({RunStatus.ORDER_PLACED_AWAIT_PIN, RunStatus.CAPTCHA,
                                 RunStatus.VERIFICATION, RunStatus.LOGIN_REQUIRED, RunStatus.UNKNOWN_STATE})
# Status yang membunyikan alarm.
ALARM_STATUSES = NEEDS_USER_STATUSES | {RunStatus.PRICE_GUARD}

# Setelah "Buat Pesanan" diklik tanpa layar PIN, hasilnya tidak pasti. Kalimat ini WAJIB dipakai.
MAYBE_ORDERED_MSG = "Pesanan MUNGKIN sudah terbuat — cek status pesanan manual"
# Status yang tetap dipertahankan setelah klik "Buat Pesanan" (butuh tindakan manual); selain ini -> UNKNOWN_STATE.
_KEEP_AFTER_ORDER = frozenset({RunStatus.ORDER_PLACED_AWAIT_PIN, RunStatus.CAPTCHA, RunStatus.VERIFICATION,
                               RunStatus.LOGIN_REQUIRED})


def after_order_click(status: RunStatus, message: str) -> tuple[RunStatus, str, str]:
    """(status, pesan, detail) final untuk run yang SUDAH mengklik "Buat Pesanan".

    Tanpa layar PIN, pesanan mungkin sudah atau belum terbuat: status jadi UNKNOWN_STATE (stop semua
    runner, alarm) kecuali captcha/verifikasi/login, dan pesan = MAYBE_ORDERED_MSG; penyebab asli di `detail`.
    """
    if status == RunStatus.ORDER_PLACED_AWAIT_PIN:
        return status, message, ""
    final = status if status in _KEEP_AFTER_ORDER else RunStatus.UNKNOWN_STATE
    return final, MAYBE_ORDERED_MSG, f"{status}: {message}"


def keep_open(live: bool, status: RunStatus | None) -> bool:
    """Browser/app dibiarkan terbuka? Live: SELALU (apa pun statusnya, termasuk error).

    Dry-run: hanya bila butuh tindakan manual (captcha/verifikasi/login); selain itu boleh ditutup.
    """
    return live or status in NEEDS_USER_STATUSES


@dataclass
class Step:
    name: str
    t_server_ms: int
    detail: str = ""


@dataclass
class RunResult:
    platform: str
    status: RunStatus
    message: str
    live: bool
    steps: list[Step] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    detail: str = ""  # keterangan tambahan (mis. penyebab asli bila pesan diganti MAYBE_ORDERED_MSG)

    def step(self, name: str) -> Step | None:
        return next((s for s in self.steps if s.name == name), None)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = str(self.status)
        d["screenshots"] = [str(p) for p in self.screenshots]
        return d


@dataclass
class PrecheckItem:
    name: str
    ok: bool | None  # None = tidak terbaca -> peringatan saja
    detail: str = ""


@dataclass
class PrecheckResult:
    platform: str
    items: list[PrecheckItem] = field(default_factory=list)
    status: RunStatus | None = None  # diisi bila gagal fatal (mis. LOGIN_REQUIRED)

    @property
    def ok(self) -> bool:
        return self.status is None and all(i.ok is not False for i in self.items)

    @property
    def warnings(self) -> list[PrecheckItem]:
        return [i for i in self.items if i.ok is None]


class Runner(Protocol):
    name: str
    # Dipanggil tepat sebelum klik "Buat Pesanan"; False -> berhenti ABORTED.
    before_place_order: Callable[[], bool]

    async def prepare(self) -> None: ...

    async def precheck(self) -> PrecheckResult: ...

    async def arm(self, open_at: float) -> None: ...

    async def attempt(self, clock: ServerClock, live: bool) -> RunResult: ...

    async def abort(self, reason: str = "") -> None: ...

    async def close(self) -> None: ...


def always_allow() -> bool:
    return True


# --------------------------------------------------------------------------- rate limit


class RateLimiter:
    """Maks 1 aksi polling/retry per interval. Thread-safe agar bisa dibagi antar runner."""

    def __init__(self, min_interval_ms: float = MIN_ACTION_INTERVAL_MS, margin_ms: float = RATE_MARGIN_MS):
        if min_interval_ms < MIN_ACTION_INTERVAL_MS:
            raise ValueError(f"interval minimal {MIN_ACTION_INTERVAL_MS} ms")
        self.interval_s = (min_interval_ms + margin_ms) / 1000.0
        self._lock = threading.Lock()
        self._last: float | None = None
        self.history: list[float] = []  # waktu server tiap slot yang dipakai

    def reserve(self, now: float, not_before: float = -math.inf,
                not_after: float = math.inf) -> float | None:
        """Pesan slot berikutnya (waktu server). None jika slot jatuh setelah `not_after`."""
        with self._lock:
            slot = max(now, not_before)
            if self._last is not None:
                slot = max(slot, self._last + self.interval_s)
            if slot > not_after:
                return None
            self._last = slot
            self.history.append(slot)
            return slot


class PollingGate:
    """RateLimiter + penjaga jendela T-1 s .. T+8 s untuk fase polling."""

    def __init__(self, clock: ServerClock, open_at: float, limiter: RateLimiter | None = None):
        self.clock = clock
        self.open_at = open_at
        self.start = open_at - POLL_WINDOW_BEFORE_S
        self.end = open_at + POLL_WINDOW_AFTER_S
        self.limiter = limiter or RateLimiter()

    def expired(self) -> bool:
        return self.clock.now() > self.end

    async def acquire(self) -> bool:
        """Tunggu slot aksi berikutnya. False jika jendela polling sudah/akan habis."""
        slot = self.limiter.reserve(self.clock.now(), self.start, self.end)
        if slot is None:
            return False
        await self.clock.wait_until_async(slot)
        return True

    def acquire_sync(self) -> bool:
        slot = self.limiter.reserve(self.clock.now(), self.start, self.end)
        if slot is None:
            return False
        self.clock.wait_until(slot)
        return True


# --------------------------------------------------------------------------- log


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


class RunLog:
    """Timeline langkah dengan timestamp waktu server terkoreksi (ms) + screenshot."""

    def __init__(self, run_dir: Path, clock: ServerClock, platform: str,
                 console: Console | None = None):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock  # boleh diganti setelah resync
        self.platform = platform
        self.console = console or Console(stderr=True)
        self.steps: list[Step] = []
        self.warnings: list[str] = []
        self._file = (self.run_dir / f"{_safe(platform)}.log").open("a", encoding="utf-8")

    def _line(self, level: str, text: str) -> None:
        line = f"{format_ts(self.clock.now())} [{self.platform}] {level} {text}"
        self._file.write(line + "\n")
        self._file.flush()
        style = {"WARN": "yellow", "ERROR": "red"}.get(level, "")
        self.console.print(f"[{style}]{line}[/]" if style else line, markup=bool(style),
                           highlight=False)

    def mark(self, name: str, detail: str = "", t_ms: int | None = None) -> Step:
        step = Step(name, self.clock.now_ms() if t_ms is None else t_ms, detail)
        self.steps.append(step)
        self._line("STEP", f"{name}{' - ' + detail if detail else ''}")
        return step

    def info(self, text: str) -> None:
        self._line("INFO", text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self._line("WARN", text)

    def error(self, text: str) -> None:
        self._line("ERROR", text)

    def screenshot_path(self, label: str) -> Path:
        return self.run_dir / f"{_safe(self.platform)}-{self.clock.now_ms()}-{_safe(label)}.png"

    def write_result(self, result: RunResult) -> Path:
        path = self.run_dir / f"{_safe(self.platform)}-result.json"
        data = result.to_dict() | {"warnings": self.warnings}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def close(self) -> None:
        self._file.close()
