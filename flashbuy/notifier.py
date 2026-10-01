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

    def alarm(self, event: str, message: str, **extra) -> None:
        """Beep berulang + webhook, keduanya di thread latar (non-blocking)."""
        payload = self._payload("alarm", event, message, extra)
        self._spawn(self._beep_loop)
        self._send(payload)

    def notify(self, event: str, message: str, **extra) -> None:
        """Tanpa beep; hanya webhook (mis. ringkasan dry-run)."""
        self._send(self._payload("info", event, message, extra))

    def _payload(self, level: str, event: str, message: str, extra: dict) -> dict:
        payload = {"level": level, "event": event, "message": message,
                   "time": datetime.now(UTC).isoformat(timespec="milliseconds"), **extra}
        self.events.append(payload)
        return payload

    def _beep_loop(self) -> None:
        for _ in range(self.repeat):
            try:
                self._beep(self.freq, self.duration_ms)
            except Exception as e:  # noqa: BLE001 - alarm tidak boleh menjatuhkan run
                log.warning("beep gagal: %s", e)
                return
            time.sleep(self.gap_s)

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
