"""Timesync end-to-end ke mock (header Date mengikuti jam mock)."""

from __future__ import annotations

from flashbuy import timesync


def test_http_date_offset_730ms_against_mock(mock, admin):
    admin.scenario(clock_offset_ms=730)
    res = timesync.measure_http_date(mock.base_url + "/", samples=10)
    assert res.ok, res.error
    err = abs(res.offset_ms - 730)
    print(f"offset {res.offset_ms:.2f} ms ±{res.uncertainty_ms:.2f}, rtt {res.rtt_ms:.2f} ms, galat {err:.2f} ms")
    assert err <= res.uncertainty_ms + 0.5
    assert err <= res.rtt_ms + 1.0, "galat harus dalam ±RTT (+1 ms resolusi penjadwalan)"
    assert not res.warnings


def test_sync_prefers_mock_date_over_failed_ntp(mock, admin):
    admin.scenario(clock_offset_ms=-420)
    rep = timesync.sync(samples=6, http_url=mock.base_url + "/",
                        ntp_transport=lambda packet: (_ for _ in ()).throw(TimeoutError("udp diblokir")))
    assert rep.primary is rep.shopee
    assert abs(rep.offset_s * 1000 + 420) <= rep.shopee.uncertainty_ms + 0.5
