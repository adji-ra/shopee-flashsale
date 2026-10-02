"""Sinyal antar jalur dalam satu run orchestrator (thread-safe: runner Android berjalan di thread executor).

- Stop global: CAPTCHA / VERIFICATION / UNKNOWN_STATE di salah satu jalur -> semua jalur berhenti. Setiap runner
  memeriksanya sebelum SETIAP tap/klik/reload; runner yang SUDAH mengklik "Buat Pesanan" tetap menunggu layar PIN.
- Lock pemenang (`threading.Lock`) lewat hook `before_place_order()`: pemanggil pertama True, sisanya False
  (-> ABORTED). Sekali diambil tidak pernah dilepas dalam run itu, walaupun pemenang gagal setelahnya. Begitu
  diambil, jalur lain menerima event batal: berhenti polling (tidak ada klik Beli/reload lagi); jalur yang sudah
  melewati polling berhenti di gerbang "Buat Pesanan" (hook menjawab False).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RunControl:
    def __init__(self, now_ms: Callable[[], int] | None = None):
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._stop = threading.Event()
        self._winner_lock = threading.Lock()  # tidak pernah dilepas
        self._mu = threading.Lock()
        self._cancel: dict[str, threading.Event] = {}
        self.winner: str | None = None
        self.won_ms: int | None = None
        self.stop_source = ""
        self.stop_reason = ""
        self.stop_ms: int | None = None
        self.gate_calls: list[tuple[str, bool, int]] = []  # (jalur, hasil, waktu ms) untuk ringkasan & tes

    def set_clock(self, now_ms: Callable[[], int]) -> None:
        self._now_ms = now_ms

    # ------------------------------------------------------------------ stop global

    def stop_all(self, source: str, reason: str = "") -> None:
        with self._mu:
            if self._stop.is_set():
                return
            self.stop_source, self.stop_reason, self.stop_ms = source, reason, self._now_ms()
            self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop_view(self, name: str) -> StopView:
        self._cancel.setdefault(name, threading.Event())
        return StopView(self, name)

    # ------------------------------------------------------------------ lock pemenang & batal

    def cancel_event(self, name: str) -> threading.Event:
        return self._cancel.setdefault(name, threading.Event())

    def lock_held(self) -> bool:
        return self._winner_lock.locked()

    def place_order_gate(self, name: str) -> Callable[[], bool]:
        self._cancel.setdefault(name, threading.Event())

        def before_place_order() -> bool:
            ok = not self._stop.is_set() and self._winner_lock.acquire(blocking=False)
            with self._mu:
                if ok:
                    self.winner, self.won_ms = name, self._now_ms()
                    for other, ev in self._cancel.items():
                        if other != name:
                            ev.set()
                self.gate_calls.append((name, ok, self._now_ms()))
            return ok

        return before_place_order


class StopView:
    """Tampilan stop global untuk satu runner (kontrak lama `stop_event`: is_set()/set())."""

    def __init__(self, control: RunControl, name: str):
        self.control = control
        self.name = name

    def is_set(self) -> bool:
        return self.control.stopped()

    def set(self) -> None:
        self.control.stop_all(self.name, "status stop (captcha/verifikasi/UNKNOWN_STATE)")

    @property
    def reason(self) -> str:
        c = self.control
        who = "jalur lain" if c.stop_source != self.name else "jalur ini"
        return f"stop global dari {c.stop_source or who}" + (f": {c.stop_reason}" if c.stop_reason else "")


CANCEL_MESSAGE = "jalur lain sudah memenangkan lock 'Buat Pesanan'; polling dihentikan"
