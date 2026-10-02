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


def test_run_without_only_runs_all_enabled_lanes_config_checked_first(tmp_path, capsys):
    # tanpa --only: orchestrator menjalankan semua jalur enabled; config tetap divalidasi lebih dulu (exit 2).
    # Jalur gabungan diuji di tests/test_orchestrator.py, jalur Android sendiri di tests/test_android_cli.py.
    assert cli.main(["run", "--config", str(tmp_path / "x.yaml")]) == 2
    assert "file config tidak ditemukan" in capsys.readouterr().out


def test_missing_config_exit_2(tmp_path):
    assert cli.main(["precheck", "--config", str(tmp_path / "nope.yaml"), "--only", "web"]) == 2


def test_run_rejects_non_shopee_url_without_hidden_flag(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text('product_url: "http://127.0.0.1:1/P-i.1.2"\nstart_time: "2099-01-01T00:00:00+07:00"\n'
                 'max_item_price: 1\nmax_total: 1\n')
    assert cli.main(["run", "--config", str(p), "--only", "web"]) == 2


def _write_cfg(tmp_path, mock, open_at):
    from datetime import datetime

    from flashbuy.timesync import WIB

    p = tmp_path / "target.yaml"
    p.write_text(f"""product_url: "{mock.product_url}"
start_time: "{datetime.fromtimestamp(open_at, WIB).isoformat()}"
max_item_price: 100000
max_total: 120000
web:
  profile_dir: "{tmp_path / 'profile'}"
  channel: null
android:
  enabled: false
""")
    return p


def test_cli_run_web_dry_run_against_mock(mock, admin, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    admin.scenario(name="payment_not_shopeepay", open_in_ms=4000)
    open_at = admin.state()["scenario"]["open_at"]
    cfg = _write_cfg(tmp_path, mock, open_at)
    monkeypatch.setattr(timesync, "sync", lambda **kw: _report())
    rc = cli.main(["run", "--config", str(cfg), "--only", "web", "--lead-ms", "100",
                   "--allow-local", "--headless", "--selectors", str(tmp_path / "none.json")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "DRYRUN_OK" in out and "DRY-RUN" in out
    assert admin.log("order") == []
    assert len(list((tmp_path / "logs").glob("*-web/web-result.json"))) == 1


def test_cli_precheck_web(mock, admin, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = _write_cfg(tmp_path, mock, mock.now() + 3600)
    rc = cli.main(["precheck", "--config", str(cfg), "--only", "web", "--allow-local", "--headless"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "alamat default" in out and "saldo ShopeePay" in out


def test_login_web_opens_login_page_and_waits_for_close(mock, admin, tmp_path, run):
    from flashbuy import selector_store
    from flashbuy.config import load_config

    cfg = load_config(_write_cfg(tmp_path, mock, mock.now() + 3600), allow_local=True)
    seen = {}

    async def on_open(ctx):
        seen["url"] = ctx.pages[0].url
        await ctx.close()  # simulasi pengguna menutup browser

    run(cli.login_web(cfg, selector_store.defaults(), headless=True, on_open=on_open))
    assert "/buyer/login" in seen["url"]
    assert (tmp_path / "profile").exists()


def test_cli_doctor_web_read_only(mock, admin, tmp_path, monkeypatch, capsys):
    """doctor jalur web: precheck membuka alamat/saldo/produk saja; tidak ada beli/keranjang/checkout/pesanan."""
    from types import SimpleNamespace

    from flashbuy import doctor

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(timesync, "sync", lambda **kw: _report())
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: SimpleNamespace(free=50 * 2**30))
    cfg = _write_cfg(tmp_path, mock, mock.now() + 3600)
    cfg.write_text(cfg.read_text() + 'expected_name: "Uji Coba"\n')
    rc = cli.main(["doctor", "--config", str(cfg), "--allow-local", "--headless",
                   "--selectors", str(tmp_path / "none.json")])
    out = capsys.readouterr().out
    assert rc == 0, out
    for row in ("config: start_time", "timesync", "precheck web", "umur selector web", "ruang disk log"):
        assert row in out, row
    assert "belum dikalibrasi" in out and "Hasil doctor: WARN" in out
    assert "versi Shopee" not in out and "latensi query Android" not in out, "jalur Android disabled"
    kinds = {e["kind"] for e in admin.log()}
    assert not kinds & {"buy", "cart", "checkout", "order", "pin"}, kinds
    assert {"address", "wallet"} <= kinds
