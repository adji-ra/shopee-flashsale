"""Alarm lokal (beep) + webhook opsional. Tidak pernah menghentikan run karena error."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

log = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_S = 2.0
# Pola alarm: "urgent" = pesanan (mungkin) terbuat -> panjang, dua nada bergantian; "short" = hasil lain;
# "normal" = alarm runner tunggal (perilaku lama).
PATTERNS = ("normal", "short", "urgent")


def _default_beep(freq: int, duration_ms: int) -> None:
    if sys.platform == "win32":
        import winsound

        winsound.Beep(freq, duration_ms)
    else:
        sys.stderr.write("\a")
        sys.stderr.flush()
        time.sleep(duration_ms / 1000)


def _default_post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_S) as resp:
        resp.read()


class Notifier:
    def __init__(self, webhook_url: str = "", *, beep: Callable[[int, int], None] | None = None,
                 post: Callable[[str, dict], None] | None = None, repeat: int = 10,
                 freq: int = 1800, duration_ms: int = 350, gap_s: float = 0.15):
        self.webhook_url = webhook_url
        self._beep = beep or _default_beep
        self._post = post or _default_post
        self.repeat, self.freq, self.duration_ms, self.gap_s = repeat, freq, duration_ms, gap_s
        self.events: list[dict] = []  # riwayat, berguna untuk tes & ringkasan
        self._threads: list[threading.Thread] = []

    def alarm(self, event: str, message: str, *, pattern: str = "normal", **extra) -> None:
        """Beep berulang + webhook, keduanya di thread latar (non-blocking)."""
        if pattern not in PATTERNS:
            raise ValueError(f"pola alarm tidak dikenal: {pattern}")
        payload = self._payload("alarm", event, message, {"pattern": pattern, **extra})
        self._spawn(lambda: self._beep_loop(pattern))
        self._send(payload)

    def notify(self, event: str, message: str, **extra) -> None:
        """Tanpa beep; hanya webhook (mis. ringkasan dry-run)."""
        self._send(self._payload("info", event, message, extra))

    def _payload(self, level: str, event: str, message: str, extra: dict) -> dict:
        payload = {"level": level, "event": event, "message": message,
                   "time": datetime.now(UTC).isoformat(timespec="milliseconds"), **extra}
        self.events.append(payload)
        return payload

    def beeps(self, pattern: str = "normal") -> list[tuple[int, int, float]]:
        """(frekuensi Hz, durasi ms, jeda s) setiap beep pola ini."""
        if pattern == "short":
            return [(self.freq, self.duration_ms, self.gap_s)] * min(3, self.repeat)
        if pattern == "urgent":  # 3x lebih panjang, nada tinggi-rendah bergantian, jeda rapat
            tones = (self.freq + 400, max(400, self.freq - 400))
            return [(tones[i % 2], self.duration_ms + 100, self.gap_s / 2) for i in range(self.repeat * 3)]
        return [(self.freq, self.duration_ms, self.gap_s)] * self.repeat

    def _beep_loop(self, pattern: str = "normal") -> None:
        for freq, duration_ms, gap_s in self.beeps(pattern):
            try:
                self._beep(freq, duration_ms)
            except Exception as e:  # noqa: BLE001 - alarm tidak boleh menjatuhkan run
                log.warning("beep gagal: %s", e)
                return
            time.sleep(gap_s)

    def _send(self, payload: dict) -> None:
        if not self.webhook_url:
            return

        def run() -> None:
            try:
                self._post(self.webhook_url, payload)
            except Exception as e:  # noqa: BLE001
                log.warning("webhook gagal: %s", e)
                payload.setdefault("webhook_error", str(e))

        self._spawn(run)

    def _spawn(self, fn: Callable[[], None]) -> None:
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self._threads.append(t)

    def join(self, timeout: float = 5.0) -> None:
        """Tunggu thread latar (dipakai tes / sebelum proses keluar)."""
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))


class DeferredNotifier(Notifier):
    """Notifier runner di bawah orchestrator: alarm dicatat (tanpa beep/webhook); orchestrator yang membunyikan
    SATU alarm hasil akhir (dan paling banyak satu alarm precheck)."""

    def __init__(self):
        super().__init__("", beep=lambda f, d: None, post=lambda u, p: None, repeat=0)

    def alarm(self, event: str, message: str, *, pattern: str = "normal", **extra) -> None:
        self._payload("alarm", event, message, {"pattern": pattern, **extra})

    def notify(self, event: str, message: str, **extra) -> None:
        self._payload("info", event, message, extra)
