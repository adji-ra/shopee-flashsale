from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from flashbuy.notifier import Notifier


def test_alarm_beeps_and_posts_webhook():
    got: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    beeps: list = []
    n = Notifier(f"http://127.0.0.1:{srv.server_address[1]}/hook", beep=lambda f, d: beeps.append(f),
                 repeat=3, gap_s=0)
    t0 = time.monotonic()
    n.alarm("CAPTCHA", "captcha muncul", platform="web")
    assert time.monotonic() - t0 < 0.1, "alarm harus non-blocking"
    n.join(3)
    srv.shutdown()
    assert len(beeps) == 3
    assert got and got[0]["event"] == "CAPTCHA" and got[0]["platform"] == "web" and got[0]["level"] == "alarm"


def test_webhook_error_does_not_raise():
    def boom(url, payload):
        raise OSError("tidak bisa konek")

    n = Notifier("http://127.0.0.1:9/hook", beep=lambda f, d: None, post=boom, repeat=1, gap_s=0)
    n.alarm("X", "y")
    n.notify("Z", "w")
    n.join(3)
    assert n.events[0]["webhook_error"] == "tidak bisa konek"


def test_webhook_timeout_is_bounded():
    n = Notifier("http://10.255.255.1:81/hook", beep=lambda f, d: None, repeat=1, gap_s=0)
    t0 = time.monotonic()
    n.alarm("X", "y")
    assert time.monotonic() - t0 < 0.1
    n.join(5)
    assert time.monotonic() - t0 < 4  # timeout 2 s


def test_beep_failure_does_not_raise():
    def bad(f, d):
        raise RuntimeError("tanpa audio")

    n = Notifier("", beep=bad, repeat=2, gap_s=0)
    n.alarm("X", "y")
    n.join(2)
    assert n.events[0]["event"] == "X"
