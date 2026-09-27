from __future__ import annotations

from flashbuy import cli, timesync
from flashbuy.timesync import OffsetResult, SyncReport


def _report(shopee_ok: bool = True, ntp_ok: bool = True) -> SyncReport:
    ok = lambda src, off: OffsetResult(src, offset_ms=off, uncertainty_ms=20, rtt_ms=15,  # noqa: E731
                                       samples=6, method="m")
    bad = lambda src: OffsetResult(src, error="timeout")  # noqa: E731
    return SyncReport(ntp=ok("NTP", 12.0) if ntp_ok else bad("NTP"),
                      shopee=ok("HTTP", 20.0) if shopee_ok else bad("HTTP"))


def test_timesync_ok(monkeypatch, capsys):
    monkeypatch.setattr(timesync, "sync", lambda **kw: _report())
    assert cli.main(["timesync"]) == 0
    out = capsys.readouterr().out
    assert "acuan" in out and "+20.0 ms" in out and "Selisih" in out


def test_timesync_all_fail(monkeypatch):
    monkeypatch.setattr(timesync, "sync", lambda **kw: _report(False, False))
    assert cli.main(["timesync"]) == 1


def test_unimplemented_commands_exit_2():
    assert cli.main(["run", "--config", "x.yaml"]) == 2
    assert cli.main(["calibrate", "--config", "x.yaml", "--platform", "web"]) == 2
