from __future__ import annotations

import asyncio
import math
import struct
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from flashbuy import timesync
from flashbuy.timesync import (
    OffsetResult,
    ServerClock,
    SyncReport,
    TimeSyncError,
    measure_http_date,
    measure_ntp,
    parse_http_date,
)
from tests.conftest import FakeClock

# ------------------------------------------------------------------ server palsu


class FakeNtpServer:
    def __init__(self, clock: FakeClock, offset: float, out: float = 0.02, back: float = 0.02,
                 stratum: int = 2, li: int = 0, bad_originate: bool = False):
        self.clock, self.offset, self.out, self.back = clock, offset, out, back
        self.stratum, self.li, self.bad_originate = stratum, li, bad_originate

    def __call__(self, packet: bytes) -> bytes:
        self.clock.advance(self.out)
        t1 = self.clock.time() + self.offset
        self.clock.advance(0.001)  # waktu proses server
        t2 = self.clock.time() + self.offset
        self.clock.advance(self.back)
        originate = b"\x00" * 8 if self.bad_originate else packet[40:48]
        head = bytes([(self.li << 6) | (4 << 3) | 4, self.stratum, 6, 0xEC]) + b"\x00" * 20
        return head + originate + timesync._to_ntp(t1) + timesync._to_ntp(t2)


class FakeShopee:
    """Server HTTP palsu: header Date = floor(waktu server) saat titik tengah request."""

    def __init__(self, clock: FakeClock, offset: float, rtt: float = 0.04,
                 offsets: list[float] | None = None):
        self.clock, self.offset, self.rtt = clock, offset, rtt
        self.offsets = offsets  # jika diisi: offset berbeda per request (CDN beda jam)
        self.calls = 0

    def __call__(self) -> str:
        off = self.offsets[self.calls % len(self.offsets)] if self.offsets else self.offset
        self.calls += 1
        self.clock.advance(self.rtt / 2)
        server = self.clock.time() + off
        self.clock.advance(self.rtt / 2)
        return format_datetime(datetime.fromtimestamp(math.floor(server), timezone.utc), usegmt=True)


def failing(exc: Exception):
    def _f(*_a):
        raise exc
    return _f


# ------------------------------------------------------------------ NTP


@pytest.mark.parametrize("offset", [0.0, 0.4321, -3.25, 120.007])
def test_ntp_offset_symmetric(fake_clock, offset):
    res = measure_ntp("fake", 5, clock=fake_clock, transport=FakeNtpServer(fake_clock, offset))
    assert res.ok and res.samples == 5
    assert res.offset_ms == pytest.approx(offset * 1000, abs=1.0)
    assert res.rtt_ms == pytest.approx(40, abs=1.0)


def test_ntp_asymmetric_path_bias_is_half_difference(fake_clock):
    res = measure_ntp("fake", 3, clock=fake_clock,
                      transport=FakeNtpServer(fake_clock, 1.0, out=0.05, back=0.01))
    # batas teori RFC 5905: galat = (out - back) / 2 = 20 ms
    assert res.offset_ms == pytest.approx(1000 + 20, abs=1.0)


@pytest.mark.parametrize("kw, msg", [
    ({"stratum": 0}, "kiss"),
    ({"li": 3}, "LI=3"),
    ({"bad_originate": True}, "originate"),
])
def test_ntp_rejects_bad_responses(fake_clock, kw, msg):
    res = measure_ntp("fake", 2, clock=fake_clock, transport=FakeNtpServer(fake_clock, 0.5, **kw))
    assert not res.ok and msg in res.error


def test_ntp_timeout_all_samples(fake_clock):
    res = measure_ntp("fake", 3, clock=fake_clock, transport=failing(TimeoutError("timed out")))
    assert not res.ok and "timed out" in res.error and res.samples == 0


def test_ntp_partial_failure_warns(fake_clock):
    server = FakeNtpServer(fake_clock, 0.25)
    calls = iter([None, TimeoutError("x"), None])

    def flaky(packet):
        if isinstance(e := next(calls), Exception):
            raise e
        return server(packet)

    res = measure_ntp("fake", 3, clock=fake_clock, transport=flaky)
    assert res.ok and res.samples == 2 and res.warnings
    assert res.offset_ms == pytest.approx(250, abs=1.0)


def test_ntp_short_packet():
    req = timesync.build_ntp_request(0.0)
    with pytest.raises(TimeSyncError):
        timesync.parse_ntp_response(b"\x24" * 10, req, 0.0, 0.1)


# ------------------------------------------------------------------ HTTP Date


def test_parse_http_date():
    assert parse_http_date("Sun, 27 Sep 2026 04:00:00 GMT") == int(
        datetime(2026, 9, 27, 4, tzinfo=timezone.utc).timestamp())


@pytest.mark.parametrize("offset", [0.0, 0.3725, -1.84, 12.999, -0.0005])
@pytest.mark.parametrize("rtt", [0.01, 0.04])
def test_http_date_bisection_beats_one_second_resolution(offset, rtt):
    clock = FakeClock(wall0=1_790_000_000.123)
    res = measure_http_date("https://shopee.co.id/", 5, clock=clock,
                            probe=FakeShopee(clock, offset, rtt))
    assert res.ok, res.error
    err = abs(res.offset_ms - offset * 1000)
    assert err <= res.uncertainty_ms + 1  # nilai benar ada di dalam interval
    assert res.uncertainty_ms < rtt * 1000 + 35  # jauh lebih baik dari ±500 ms
    assert res.rtt_ms == pytest.approx(rtt * 1000, abs=1)
    assert res.samples == 8  # 1 kasar + 5 bisection + 2 verifikasi
    assert not res.warnings


def test_http_date_more_samples_tighter():
    clock = FakeClock()
    res = measure_http_date("https://shopee.co.id/", 10, clock=clock,
                            probe=FakeShopee(clock, 0.777, 0.02))
    assert res.uncertainty_ms < 25
    assert abs(res.offset_ms - 777) <= res.uncertainty_ms + 1


@pytest.mark.parametrize("wrong", [0.9, -0.6])
def test_http_date_verification_catches_server_outside_interval(wrong):
    # 6 request pertama dijawab server ber-offset 0.1 s, 2 sampel verifikasi oleh server lain.
    clock = FakeClock()
    probe = FakeShopee(clock, 0, 0.02, offsets=[0.1] * 6 + [wrong] * 2)
    res = measure_http_date("https://shopee.co.id/", 5, clock=clock, probe=probe)
    assert res.ok
    assert "fallback" in res.method and res.uncertainty_ms == 500
    assert any("bertentangan" in w for w in res.warnings)


def test_http_date_first_request_fails():
    res = measure_http_date("https://shopee.co.id/", 5, clock=FakeClock(),
                            probe=failing(ConnectionRefusedError("refused")))
    assert not res.ok and "refused" in res.error


def test_http_date_fails_midway_keeps_samples():
    clock = FakeClock()
    ok = FakeShopee(clock, 0.5, 0.02)
    n = {"i": 0}

    def probe():
        n["i"] += 1
        if n["i"] > 3:
            raise TimeoutError("timeout")
        return ok()

    res = measure_http_date("https://shopee.co.id/", 5, clock=clock, probe=probe)
    assert res.ok and res.samples == 3
    assert any("berhenti" in w for w in res.warnings)
    assert abs(res.offset_ms - 500) <= res.uncertainty_ms + 1


def test_http_date_missing_header_is_error():
    res = measure_http_date("https://shopee.co.id/", 3, clock=FakeClock(),
                            probe=failing(TimeSyncError("respons HTTP 403 tanpa header Date")))
    assert not res.ok and "Date" in res.error


# ------------------------------------------------------------------ gabungan


def test_sync_prefers_shopee_and_reports_both(fake_clock):
    rep = timesync.sync(samples=5, clock=fake_clock,
                        ntp_transport=FakeNtpServer(fake_clock, 0.300),
                        http_probe=FakeShopee(fake_clock, 0.310, 0.02))
    assert rep.primary is rep.shopee
    assert rep.ntp.ok and rep.shopee.ok
    assert rep.offset_s == pytest.approx(0.310, abs=rep.shopee.uncertainty_ms / 1000 + 0.001)
    assert abs(rep.disagreement_ms) < 50
    assert not rep.shopee.warnings


def test_sync_warns_on_disagreement(fake_clock):
    rep = timesync.sync(samples=5, clock=fake_clock,
                        ntp_transport=FakeNtpServer(fake_clock, 0.0),
                        http_probe=FakeShopee(fake_clock, 2.0, 0.02))
    assert any("NTP" in w for w in rep.shopee.warnings)


def test_sync_falls_back_to_ntp(fake_clock):
    rep = timesync.sync(samples=3, clock=fake_clock,
                        ntp_transport=FakeNtpServer(fake_clock, 1.5),
                        http_probe=failing(OSError("down")))
    assert rep.primary is rep.ntp
    assert rep.offset_s == pytest.approx(1.5, abs=0.001)


def test_sync_all_fail():
    rep = SyncReport(ntp=OffsetResult("ntp", error="x"), shopee=OffsetResult("http", error="y"))
    assert rep.primary is None
    with pytest.raises(TimeSyncError):
        _ = rep.offset_s


# ------------------------------------------------------------------ ServerClock / wait_until


def test_server_clock_applies_offset(fake_clock):
    sc = ServerClock(2.5, fake_clock)
    assert sc.now() == pytest.approx(fake_clock.wall0 + fake_clock.t + 2.5, abs=0.001)


def test_server_clock_ignores_wall_clock_jump():
    clock = FakeClock()
    sc = ServerClock(0.0, clock)
    before = sc.now()
    clock.wall0 += 3600  # Windows menyetel ulang jam dinding
    assert sc.now() - before < 0.01


@pytest.mark.parametrize("oversleep", [0.0, 0.010])
def test_wait_until_precise_with_lead(oversleep):
    clock = FakeClock(oversleep=oversleep)
    sc = ServerClock(0.75, clock)
    target = sc.now() + 5.0
    late_ms = sc.wait_until(target, lead_ms=150)
    reached = sc.now()
    assert 0 <= late_ms < 1.0
    assert reached == pytest.approx(target - 0.150, abs=0.001)
    assert max(clock.sleeps) <= 1.0  # sleep dipecah per <= 1 s
    assert sum(1 for s in clock.sleeps if s == 0) > 0  # fase busy-wait terjadi


def test_wait_until_accepts_aware_datetime(fake_clock):
    sc = ServerClock(0.0, fake_clock)
    target = datetime.fromtimestamp(sc.now() + 2, timezone.utc)
    assert 0 <= sc.wait_until(target) < 1.0


def test_wait_until_rejects_naive_datetime(fake_clock):
    with pytest.raises(ValueError, match="zona waktu"):
        ServerClock(0.0, fake_clock).wait_until(datetime(2026, 10, 10))


def test_wait_until_past_target_returns_immediately(fake_clock):
    sc = ServerClock(0.0, fake_clock)
    late_ms = sc.wait_until(sc.now() - 2.0)
    assert late_ms == pytest.approx(2000, abs=1)
    assert fake_clock.sleeps == []


def test_wait_until_async():
    clock = FakeClock(oversleep=0.005)
    sc = ServerClock(-0.2, clock)
    target = sc.now() + 3.0
    late_ms = asyncio.run(sc.wait_until_async(target, lead_ms=100))
    assert 0 <= late_ms < 1.0
    assert sc.now() == pytest.approx(target - 0.1, abs=0.001)


def test_wait_until_real_clock_precision():
    """Sanity check dengan jam asli: meleset < 5 ms (longgar untuk CI)."""
    sc = ServerClock(0.0)
    target = sc.now() + 0.2
    late_ms = sc.wait_until(target, lead_ms=50)
    assert 0 <= late_ms < 5


def test_format_ts_wib():
    assert timesync.format_ts(0.0) == "1970-01-01T07:00:00.000+07:00"


def test_ntp_timestamp_roundtrip():
    t = 1_790_000_000.123456
    data = b"\x00" * 40 + timesync._to_ntp(t)
    assert timesync._from_ntp(data, 40) == pytest.approx(t, abs=1e-6)
    assert struct.unpack("!I", timesync._to_ntp(0.0)[:4])[0] == 2_208_988_800


def test_bypass_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,.example.com,*.corp")
    monkeypatch.delenv("no_proxy", raising=False)
    assert timesync._bypass_proxy("a.example.com")
    assert timesync._bypass_proxy("x.corp")
    assert not timesync._bypass_proxy("shopee.co.id")


class _FakeResp:
    def __init__(self, status, date):
        self.status, self._date, self.will_close = status, date, False

    def read(self):
        return b""

    def getheader(self, name):
        return self._date if name == "Date" else None


class _FakeConn:
    def __init__(self, head_has_date: bool):
        self.head_has_date = head_has_date
        self.methods: list[str] = []

    def request(self, method, path, headers):
        self.methods.append(method)
        assert headers["User-Agent"] == timesync.USER_AGENT

    def getresponse(self):
        ok = self.methods[-1] == "GET" or self.head_has_date
        return _FakeResp(200 if ok else 405, "Sun, 27 Sep 2026 04:00:00 GMT" if ok else None)

    def close(self):
        pass


@pytest.mark.parametrize("head_has_date, expected", [
    (True, ["HEAD", "HEAD"]),
    (False, ["HEAD", "GET", "GET"]),
])
def test_probe_head_then_get_fallback(monkeypatch, head_has_date, expected):
    conn = _FakeConn(head_has_date)
    probe = timesync.HttpDateProbe("https://shopee.co.id/")
    monkeypatch.setattr(probe, "_connect", lambda: conn)
    assert probe() == "Sun, 27 Sep 2026 04:00:00 GMT"
    assert probe() == "Sun, 27 Sep 2026 04:00:00 GMT"
    assert conn.methods == expected


def test_probe_requires_https():
    with pytest.raises(ValueError):
        timesync.HttpDateProbe("http://shopee.co.id/")
