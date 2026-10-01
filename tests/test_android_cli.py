"""Integrasi CLI jalur Android: `precheck/run/login/calibrate` di atas FakeDriver + FakeShopeeApp.

CLI memakai ServerClock() = jam sistem, jadi device palsu di sini memakai jam NYATA (RealClock)
dengan latensi kecil (5 ms); latensi tetap FakeDriver yang besar (start_url 0,3 s, restart agent 2 s,
dump 0,4 s) dipotong supaya tes tetap cepat. `cli._android_driver` diganti pabrik FakeDriver,
`timesync.sync` diganti laporan palsu (tanpa jaringan), alarm tidak berbunyi.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from flashbuy import android_selectors, cli, notifier, timesync
from flashbuy.android_driver import DriverError, FakeDriver, Sel
from flashbuy.android_selectors import KIND_RANK
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus
from flashbuy.timesync import WIB, OffsetResult, SyncReport
from tests.conftest import LIVE_NAME, PRICE_DEFAULTS
from tests.fake_android import PACKAGE, PRODUCT_URL, AppScenario, FakeShopeeApp

SERIAL = "FAKE123"
LATENCY_S = 0.005
SLOW_QUERY_S = 0.15
OPEN_IN_S = 1.0  # start_time di depan: precheck + arm (~0,35 s) lalu benar-benar menunggu T-lead
WM_SIZE = "Physical size: 1080x2460"  # beda dari tata letak app palsu: resolusi dibaca, bukan diasumsikan
PROPS = {"ro.product.brand": "TECNO", "ro.product.manufacturer": "TECNO", "ro.product.model": "TECNO BG6",
         "ro.build.version.release": "13", "ro.build.version.sdk": "33", "ro.tranos.version": "hios13.6.0"}
# Perintah shell yang menutup/mematikan aplikasi atau mengetik/menekan tombol (termasuk PIN).
FORBIDDEN_SHELL = re.compile(r"force-stop|\bam\s+(kill|stop)|\bpm\s+clear|\binput\b|\bkill\b", re.I)
PRECHECK_ROWS = ["device", "agent uiautomator2", "layar", "aplikasi Shopee", "login", "tombol Beli",
                 "harga produk", "latensi query", "alamat default", "saldo ShopeePay"]


# ------------------------------------------------------------------ device palsu (jam nyata)


class RealClock:
    """Jam nyata untuk FakeDriver/FakeShopeeApp (sama dengan jam yang dipakai CLI).

    `sleep` dipotong `cap` detik: latensi tetap FakeDriver yang besar tidak memperlambat tes,
    latensi query biasa (5 ms) tetap nyata sehingga terukur oleh TimedDriver.
    """

    def __init__(self, cap: float = 0.02):
        self.cap = cap

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.perf_counter()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(min(seconds, self.cap))


class SlowQueryDriver(FakeDriver):
    """Query `exists` 150 ms (kabel/port USB lambat); dipakai uji latensi precheck."""

    def exists(self, sel: Sel) -> bool:
        time.sleep(SLOW_QUERY_S)
        return super().exists(sel)


class AgentWontRestartDriver(FakeDriver):
    """Agent uiautomator2 mati dan tidak bisa dihidupkan ulang."""

    def restart_agent(self) -> None:
        self._rpc("restart_agent")
        raise DriverError("uiautomator2 tidak bisa dijalankan ulang")


@dataclass
class Device:
    app: FakeShopeeApp
    driver: FakeDriver
    configs: list = field(default_factory=list)  # TargetConfig yang diterima cli._android_driver

    def ops(self) -> list[str]:
        return [op for op, _ in self.driver.calls]

    def shell_cmds(self) -> list[str]:
        return [target for op, target in self.driver.calls if op == "shell"]


@pytest.fixture(autouse=True)
def _chdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # logs/ dan selectors.json default relatif ke cwd


@pytest.fixture(autouse=True)
def term(monkeypatch) -> io.StringIO:
    """Console CLI lebar tanpa warna, supaya baris tabel tidak terpotong dan mudah dibaca tes."""
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=300, color_system=None))
    return buf


@pytest.fixture(autouse=True)
def sync_calls(monkeypatch) -> list[dict]:
    """timesync.sync palsu (tanpa jaringan); mencatat setiap panggilan."""
    calls: list[dict] = []

    def fake_sync(**kw) -> SyncReport:
        calls.append(kw)
        ok = lambda src, off: OffsetResult(src, offset_ms=off, uncertainty_ms=5, rtt_ms=10,  # noqa: E731
                                           samples=6, method="m")
        return SyncReport(ntp=ok("NTP", 1.0), shopee=ok("HTTP", 0.0))

    monkeypatch.setattr(timesync, "sync", fake_sync)
    return calls


@pytest.fixture(autouse=True)
def alarms(monkeypatch) -> list[str]:
    """Notifier CLI tanpa beep/webhook; mengembalikan daftar event alarm (diisi saat run)."""
    made: list[notifier.Notifier] = []
    events: list[str] = []

    class QuietNotifier(notifier.Notifier):
        def __init__(self, webhook_url: str = "", **kw):
            super().__init__(webhook_url, beep=lambda f, d: None, post=lambda u, p: None, repeat=1, **kw)
            made.append(self)

        def alarm(self, event: str, message: str, **extra) -> None:
            events.append(event)
            super().alarm(event, message, **extra)

    monkeypatch.setattr(notifier, "Notifier", QuietNotifier)
    return events


@pytest.fixture
def prechecks(monkeypatch) -> list:
    """PrecheckResult yang dicetak CLI (untuk cek status selain tabel)."""
    seen = []
    orig = cli._print_precheck

    def spy(pre, platform):
        seen.append(pre)
        orig(pre, platform)

    monkeypatch.setattr(cli, "_print_precheck", spy)
    return seen


@pytest.fixture
def device(monkeypatch) -> Callable[..., Device]:
    """Pasang device palsu sebagai `cli._android_driver`."""

    def install(open_at: float, *, app_cls=FakeShopeeApp, driver_cls=FakeDriver, driver_kw: dict | None = None,
                **scenario) -> Device:
        clock = RealClock()
        app = app_cls(AppScenario(open_at=open_at, **scenario), clock)
        driver = driver_cls(app, clock, latency_s=LATENCY_S, serial=SERIAL, props=dict(PROPS), wm_size=WM_SIZE,
                            **(driver_kw or {}))
        dev = Device(app, driver)

        def factory(cfg):
            dev.configs.append(cfg)
            return driver

        monkeypatch.setattr(cli, "_android_driver", factory)
        return dev

    return install


def write_cfg(tmp_path: Path, open_at: float, **top) -> Path:
    android = {"serial": SERIAL, **top.pop("android", {})}
    web = top.pop("web", {"enabled": False})
    data = {"product_url": PRODUCT_URL, "start_time": datetime.fromtimestamp(open_at, WIB).isoformat(),
            **PRICE_DEFAULTS, "web": web, "android": android, **top}
    path = tmp_path / "target.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def table_rows(text: str) -> dict[str, tuple[str, str]]:
    """Baris tabel Rich -> {nama cek: (status, detail)} (header & baris lanjutan dilewati)."""
    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"[│┃|]", line)]
        if len(cells) == 5 and cells[1] and cells[1] != "Cek":
            rows[cells[1]] = (cells[2], cells[3])
    return rows


def only_log(pattern: str) -> Path:
    found = list(Path.cwd().glob(pattern))
    assert len(found) == 1, f"{pattern}: {found}"
    return found[0]


def no_forbidden_shell(dev: Device) -> None:
    bad = [c for c in dev.shell_cmds() if FORBIDDEN_SHELL.search(c)]
    assert bad == [], f"perintah shell terlarang (menutup app / mengetik): {bad}"


def _subsequence(names: list[str], expected: list[str]) -> None:
    it = iter(names)
    missing = [e for e in expected if e not in it]
    assert not missing, f"langkah {missing} tidak muncul berurutan di {names}"


# ------------------------------------------------------------------ precheck --only android


def run_precheck(tmp_path, device, **kw) -> tuple[int, Device]:
    dev = device(time.time() + 3600, **kw)
    rc = cli.main(["precheck", "--config", str(write_cfg(tmp_path, dev.app.sc.open_at)), "--only", "android"])
    return rc, dev


def test_precheck_android_ok_table_device_props_and_latency_csv(tmp_path, device, term, prechecks):
    rc, dev = run_precheck(tmp_path, device)
    text = term.getvalue()
    assert rc == 0, text
    rows = table_rows(text)
    assert list(rows) == PRECHECK_ROWS, rows
    assert all(status == "OK" for status, _ in rows.values()), rows
    # versi Android & resolusi dibaca dari device (getprop, wm size), bukan diasumsikan
    assert rows["device"][1] == "TECNO TECNO BG6 Android 13 (SDK 33), Physical size: 1080x2460"
    assert rows["agent uiautomator2"][1] == "hidup"
    assert rows["aplikasi Shopee"][1] == f"{PACKAGE} 3.40.21"
    assert "enabled=False" in rows["tombol Beli"][1]  # sebelum slot buka: tombol ada tapi belum aktif
    assert rows["harga produk"][1].startswith("harga Rp150.000 > maks Rp100.000")
    assert re.fullmatch(r"median \d+ ms, p95 \d+ ms, maks \d+ ms \(13 query\)", rows["latensi query"][1])
    assert "Rp500.000" in rows["saldo ShopeePay"][1]
    assert prechecks[0].status is None and prechecks[0].warnings == []
    assert dev.configs and dev.configs[0].android.serial == SERIAL

    # log: info device + latensi per query
    log_dir = only_log("logs/*-precheck")
    assert f"Log (info device & latensi query): {Path('logs') / log_dir.name}" in text
    log = (log_dir / "android.log").read_text(encoding="utf-8")
    for line in ("device ro.product.brand = TECNO", "device ro.product.model = TECNO BG6",
                 "device ro.build.version.release = 13", "device ro.build.version.sdk = 33",
                 "device ro.tranos.version = hios13.6.0", f"device wm size = {WM_SIZE}",
                 "device wm density = Physical density: 320", "HiOS/Transsion terdeteksi",
                 "latensi query precheck: median", "latensi precheck exists: n=10"):
        assert line in log, line
    with (log_dir / "android-queries-precheck.csv").open(encoding="utf-8") as f:
        samples = list(csv.DictReader(f))
    assert samples and set(samples[0]) == {"t_server_ms", "op", "target", "ms"}
    probes = [s for s in samples if s["op"] == "exists" and "__flashbuy_probe__" in s["target"]]
    assert len(probes) == 10 and all(float(s["ms"]) >= LATENCY_S * 1000 * 0.9 for s in probes)
    assert {"shell", "start_url", "find_all", "info"} <= {s["op"] for s in samples}

    # precheck hanya membaca: tidak ada tap / pesanan, aplikasi tidak ditutup
    assert dev.app.kind("tap") == [] and dev.app.kind("order") == []
    no_forbidden_shell(dev)
    assert dev.app.screen == "product", "precheck kembali ke halaman produk"


def test_precheck_agent_dead_at_start_is_restarted_with_warning(tmp_path, device, term, prechecks):
    rc, dev = run_precheck(tmp_path, device, driver_kw={"alive": False})
    text = term.getvalue()
    assert rc == 0, text  # peringatan saja
    status, detail = table_rows(text)["agent uiautomator2"]
    assert status == "PERINGATAN"
    assert detail.startswith("mati lalu dihidupkan ulang - kemungkinan dibunuh HiOS")
    assert dev.driver.restarts == 1
    assert [i.name for i in prechecks[0].warnings] == ["agent uiautomator2"]
    log = (only_log("logs/*-precheck") / "android.log").read_text(encoding="utf-8")
    assert "WARN agent uiautomator2 mati; menghidupkan ulang" in log


def test_precheck_agent_cannot_restart_fails(tmp_path, device, term, prechecks):
    rc, dev = run_precheck(tmp_path, device, driver_cls=AgentWontRestartDriver, driver_kw={"alive": False})
    text = term.getvalue()
    assert rc == 1, text
    assert table_rows(text)["agent uiautomator2"] == ("GAGAL", "mati lalu dihidupkan ulang - gagal dihidupkan")
    assert prechecks[0].status == RunStatus.ERROR
    assert dev.app.kind("intent") == [], "berhenti sebelum membuka aplikasi"


@pytest.mark.parametrize(("scenario", "detail"), [
    ({"locked": True}, "layar terkunci - buka kunci HP"),
    ({"screen_on": False}, "layar tidak menyala (Asleep)"),
], ids=["terkunci", "mati"])
def test_precheck_screen_locked_or_off_fails(tmp_path, device, term, scenario, detail):
    rc, dev = run_precheck(tmp_path, device, **scenario)
    text = term.getvalue()
    assert rc == 1, text
    assert table_rows(text)["layar"] == ("GAGAL", detail)
    assert not [c for c in dev.shell_cmds() if FORBIDDEN_SHELL.search(c)], "alat tidak membuka kunci/menekan tombol"


def test_precheck_app_not_installed_fails(tmp_path, device, term, prechecks):
    rc, dev = run_precheck(tmp_path, device, installed=False)
    text = term.getvalue()
    assert rc == 1, text
    assert table_rows(text)["aplikasi Shopee"] == ("GAGAL", f"{PACKAGE} tidak terpasang")
    assert prechecks[0].status == RunStatus.ERROR
    assert dev.app.kind("intent") == [] and "start_url" not in dev.ops()


def test_precheck_slow_queries_warn_latency(tmp_path, device, term, prechecks):
    rc, _ = run_precheck(tmp_path, device, driver_cls=SlowQueryDriver)
    text = term.getvalue()
    assert rc == 0, text
    status, detail = table_rows(text)["latensi query"]
    assert status == "PERINGATAN"
    p95 = int(re.search(r"p95 (\d+) ms", detail).group(1))
    assert p95 >= SLOW_QUERY_S * 1000 and "> target 100 ms" in detail
    assert [i.name for i in prechecks[0].warnings] == ["latensi query"]
    log = (only_log("logs/*-precheck") / "android.log").read_text(encoding="utf-8")
    assert re.search(r"WARN \d+ query > 100 ms \(terlama exists", log)


@pytest.mark.parametrize(("scenario", "row", "detail"), [
    ({"wallet_unsupported": True}, "saldo ShopeePay", "halaman tidak bisa dibuka di aplikasi (am start gagal"),
    ({"stay_on": "0"}, "layar tetap menyala", "'Tetap aktif' mati dan timeout layar 60000 ms"),
], ids=["wallet_unsupported", "stay_on_off"])
def test_precheck_warnings_do_not_fail(tmp_path, device, term, prechecks, scenario, row, detail):
    rc, _ = run_precheck(tmp_path, device, **scenario)
    text = term.getvalue()
    assert rc == 0, text
    status, got = table_rows(text)[row]
    assert status == "PERINGATAN" and got.startswith(detail), got
    assert [i.name for i in prechecks[0].warnings] == [row]


@pytest.mark.xfail(strict=True, reason=(
    "BUG: precheck membandingkan saldo dengan harga yang tampil SEBELUM flash sale (harga normal Rp150.000 > "
    "max_item_price) -> 'saldo ShopeePay' GAGAL & exit 1, padahal alat tidak akan pernah membayar di atas "
    "max_item_price/max_total; saldo Rp110.000 cukup untuk harga flash Rp99.000 + ongkir Rp10.000. "
    "Logika sama di web_runner."))
def test_precheck_balance_enough_for_flash_price_is_not_failure(tmp_path, device, term):
    rc, _ = run_precheck(tmp_path, device, wallet_balance=110_000)
    text = term.getvalue()
    rows = table_rows(text)
    assert rows["saldo vs max_total"] == ("PERINGATAN", "saldo Rp110.000 < max_total Rp120.000")
    assert rows["saldo ShopeePay"][0] != "GAGAL", rows["saldo ShopeePay"]
    assert rc == 0, text


def test_precheck_login_required(tmp_path, device, term, prechecks):
    rc, dev = run_precheck(tmp_path, device, login_required=True)
    text = term.getvalue()
    assert rc == 1, text
    assert prechecks[0].status == RunStatus.LOGIN_REQUIRED
    rows = table_rows(text)
    assert rows["login"][0] == "GAGAL" and "aplikasi minta login" in rows["login"][1]
    assert "tombol Beli" not in rows, "berhenti di layar login"
    assert dev.app.screen == "login" and dev.app.kind("tap") == [], "alat tidak mencoba login sendiri"


# ------------------------------------------------------------------ run --only android (e2e, jam nyata)


@dataclass
class CliRun:
    rc: int
    text: str
    dev: Device
    open_at: float
    result: dict | None
    run_dir: Path | None

    def step_names(self) -> list[str]:
        return [s["name"] for s in self.result["steps"]]

    def buys(self) -> list[dict]:
        return self.dev.app.kind("buy")


def run_cli(tmp_path, device, term, *, live: bool = False, cfg: dict | None = None, open_in_s: float = OPEN_IN_S,
            **scenario) -> CliRun:
    open_at = time.time() + open_in_s
    dev = device(open_at, **scenario)
    argv = ["run", "--config", str(write_cfg(tmp_path, open_at, **(cfg or {}))), "--only", "android"]
    rc = cli.main(argv + (["--live"] if live else []))
    found = list(tmp_path.glob("logs/*-android/android-result.json"))
    assert len(found) <= 1
    result = json.loads(found[0].read_text(encoding="utf-8")) if found else None
    return CliRun(rc, term.getvalue(), dev, open_at, result, found[0].parent if found else None)


def test_run_android_dry_run_e2e(tmp_path, device, term, sync_calls, alarms):
    out = run_cli(tmp_path, device, term)
    assert out.rc == 0, out.text
    assert "Jalur android | Mode: DRY-RUN" in out.text and "selectors: default teks" in out.text
    assert "Status: DRYRUN_OK - " in out.text and f"Log: {Path('logs') / out.run_dir.name}" in out.text
    assert out.result["status"] == "DRYRUN_OK" and out.result["live"] is False
    # timesync awal sekali; resync T-2 menit dilewati (run dimulai < T-2 menit)
    assert sync_calls == [{"samples": timesync.DEFAULT_SAMPLES, "http_url": "https://shopee.co.id/"}]
    _subsequence(out.step_names(), ["precheck", "arm", "page_open", "armed", "t_minus_lead", "poll_start",
                                    "click_buy", "buy_ok", "checkout_loaded", "payment_ok", "price_guard_ok",
                                    "place_order_gate", "result"])
    assert "click_place_order" not in out.step_names()
    # tepat satu klik Beli, setelah slot buka & di dalam jendela polling; "Buat Pesanan" tidak disentuh
    assert len(out.buys()) == 1 and out.dev.app.kind("buy_disabled") == []
    t_ms = int(out.open_at * 1000)
    assert t_ms <= out.buys()[0]["t_server_ms"] <= t_ms + 8000
    assert out.dev.app.kind("order") == []
    assert "place_order" not in [e["detail"] for e in out.dev.app.kind("tap")]
    assert out.dev.app.screen == "checkout", "aplikasi dibiarkan di checkout (tidak ditutup)"
    no_forbidden_shell(out.dev)
    assert alarms == []
    # log run: device props, latensi query precheck & run, screenshot + dump status akhir
    log = (out.run_dir / "android.log").read_text(encoding="utf-8")
    assert "device ro.product.model = TECNO BG6" in log and f"device wm size = {WM_SIZE}" in log
    assert (out.run_dir / "android-queries-precheck.csv").exists()
    assert (out.run_dir / "android-queries-run.csv").exists()
    assert len(out.result["screenshots"]) == 1 and Path(out.result["screenshots"][0]).exists()
    assert list(out.run_dir.glob("android-*-DRYRUN_OK.xml"))


def test_run_android_live_e2e_stops_at_pin_and_leaves_app_open(tmp_path, device, term, alarms):
    out = run_cli(tmp_path, device, term, live=True, cfg={"expected_name": LIVE_NAME})
    assert out.rc == 0, out.text
    assert "LIVE - pesanan sungguhan akan dibuat" in out.text
    assert "Status: ORDER_PLACED_AWAIT_PIN - " in out.text
    assert "Aplikasi di HP dibiarkan terbuka apa adanya (alat tidak menutup aplikasi)" in out.text
    assert out.result["status"] == "ORDER_PLACED_AWAIT_PIN" and out.result["live"] is True
    assert len(out.buys()) == 1 and len(out.dev.app.kind("order")) == 1, "tepat satu pesanan"
    assert out.dev.app.screen == "pin", "layar PIN dibiarkan untuk diisi manual"
    assert "press_back" not in out.dev.ops()
    no_forbidden_shell(out.dev)  # tidak ada `input text` -> PIN tidak mungkin diketik alat
    _subsequence(out.step_names(), ["price_guard_ok", "place_order_gate", "click_place_order", "pin_screen"])
    assert alarms == ["ORDER_PLACED_AWAIT_PIN"]


def test_run_android_live_unknown_after_order_prints_maybe_ordered(tmp_path, device, term, alarms):
    out = run_cli(tmp_path, device, term, live=True, cfg={"expected_name": LIVE_NAME}, unknown_after_order=True)
    assert out.rc == 1, out.text
    assert f"Status: UNKNOWN_STATE - {MAYBE_ORDERED_MSG}" in out.text
    assert out.result["status"] == "UNKNOWN_STATE" and out.result["message"] == MAYBE_ORDERED_MSG
    assert out.result["detail"].startswith("UNKNOWN_STATE: layar tidak dikenali")
    assert f"Detail: {out.result['detail']}" in out.text
    assert "Aplikasi di HP dibiarkan terbuka apa adanya" in out.text
    assert len(out.dev.app.kind("order")) == 1 and out.dev.app.screen == "order_unknown"
    assert alarms == ["UNKNOWN_STATE"]


@pytest.mark.parametrize(("flag", "status"), [("captcha_after_buy", "CAPTCHA"),
                                               ("verification_after_buy", "VERIFICATION")])
def test_run_android_captcha_left_as_is_without_retry(tmp_path, device, term, alarms, flag, status):
    out = run_cli(tmp_path, device, term, **{flag: True})
    assert out.rc == 1, out.text
    assert f"Status: {status} - " in out.text
    assert "Captcha/verifikasi di HP dibiarkan apa adanya. Selesaikan manual; alat TIDAK akan retry." in out.text
    assert "Aplikasi di HP dibiarkan terbuka apa adanya" in out.text  # dry-run pun dibiarkan
    assert len(out.buys()) == 1, "tanpa retry otomatis"
    assert out.dev.ops().count("click") == 1 and "press_back" not in out.dev.ops()
    assert out.dev.app.screen == status.lower(), "layar captcha/verifikasi tidak disentuh"
    assert alarms == [status]


# ------------------------------------------------------------------ run: ditolak sebelum driver & timesync


@pytest.mark.parametrize("case", ["run_no_only", "precheck_no_only", "android_disabled", "live_no_expected_name",
                                  "live_blank_expected_name", "lead_out_of_range", "start_passed"])
def test_run_rejected_before_driver_and_timesync(tmp_path, device, term, sync_calls, case):
    open_at = time.time() + (-30 if case == "start_passed" else 3600)
    dev = device(open_at)
    cfg, argv, expect = {}, ["run", "--only", "android"], ""
    if case == "run_no_only":
        argv, expect = ["run"], "menyusul di tahap 4"
    elif case == "precheck_no_only":
        argv, expect = ["precheck"], "menyusul di tahap 4"
    elif case == "android_disabled":
        cfg, expect = {"android": {"enabled": False}, "web": {"enabled": True}}, "android.enabled = false di config"
    elif case == "live_no_expected_name":
        argv, expect = [*argv, "--live"], "mode --live ditolak: `expected_name` wajib diisi"
    elif case == "live_blank_expected_name":
        cfg, argv, expect = {"expected_name": "   "}, [*argv, "--live"], "`expected_name` wajib diisi"
    elif case == "lead_out_of_range":
        argv, expect = [*argv, "--lead-ms", "1500"], "--lead-ms harus 0..1000"
    elif case == "start_passed":
        expect = "sudah lewat"
    path = write_cfg(tmp_path, open_at, **cfg)
    rc = cli.main([argv[0], "--config", str(path), *argv[1:]])
    assert rc == 2
    assert expect in term.getvalue(), term.getvalue()
    assert dev.configs == [], "driver Android tidak boleh dibuat"
    assert sync_calls == [], "timesync tidak boleh jalan"
    assert dev.driver.calls == [] and not Path("logs").exists()


@pytest.mark.parametrize("argv", [["precheck", "--only", "android"], ["run", "--only", "android"],
                                  ["run", "--only", "android", "--live"], ["login", "--platform", "android"],
                                  ["calibrate", "--platform", "android"]],
                         ids=["precheck", "run", "run_live", "login", "calibrate"])
def test_driver_error_exits_2_with_message(tmp_path, monkeypatch, term, sync_calls, argv):
    def boom(cfg):
        raise DriverError(f"gagal konek ke device {cfg.android.serial}: device offline")

    monkeypatch.setattr(cli, "_android_driver", boom)
    path = write_cfg(tmp_path, time.time() + 3600, expected_name=LIVE_NAME)
    rc = cli.main([argv[0], "--config", str(path), *argv[1:]])
    text = term.getvalue()
    assert rc == 2, text
    assert f"Android: gagal konek ke device {SERIAL}: device offline" in text and "adb devices" in text
    assert sync_calls == [], "gagal konek -> berhenti sebelum timesync"
    assert not list(tmp_path.glob("logs/*/android-result.json"))


def test_real_driver_factory_wraps_connect_failure(tmp_path, monkeypatch, term):
    import uiautomator2

    def connect(serial=None):
        raise RuntimeError(f"device {serial!r} not found")

    monkeypatch.setattr(uiautomator2, "connect", connect)
    rc = cli.main(["login", "--config", str(write_cfg(tmp_path, time.time() + 3600)), "--platform", "android"])
    assert rc == 2
    assert f"Android: gagal konek ke device {SERIAL}: device '{SERIAL}' not found" in term.getvalue()


# ------------------------------------------------------------------ login --platform android


@pytest.mark.parametrize("login_required", [False, True], ids=["sudah_login", "perlu_login"])
def test_login_android_opens_app_via_intent(tmp_path, device, term, login_required):
    dev = device(time.time() + 3600, login_required=login_required)
    rc = cli.main(["login", "--config", str(write_cfg(tmp_path, dev.app.sc.open_at)), "--platform", "android"])
    text = term.getvalue()
    assert rc == 0, text
    assert [e["detail"] for e in dev.app.kind("intent")] == ["https://shopee.co.id/"]
    assert dev.driver.calls == [("start_url", "https://shopee.co.id/")], "hanya membuka aplikasi"
    assert dev.app.screen == ("login" if login_required else "product")
    assert dev.app.kind("tap") == []
    assert f"Device {SERIAL}: aplikasi {PACKAGE} dibuka." in text and "Login manual di HP" in text


@pytest.mark.parametrize("command", ["login", "calibrate"])
def test_login_and_calibrate_require_platform(tmp_path, command):
    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--config", str(write_cfg(tmp_path, time.time() + 3600))])
    assert exc.value.code == 2


# ------------------------------------------------------------------ calibrate --platform android

BUY_RID = "com.shopee.id:id/btn_buy_now"
CONFIRM_RID = "com.shopee.id:id/btn_sheet_confirm"
PLACE_ORDER_RID = "com.shopee.id:id/btn_place_order"
SHOPEEPAY_RID = "com.shopee.id:id/payment_shopeepay"
PRICE_RID = "com.shopee.id:id/tv_product_price"
MAIN_PRICE_BOUNDS = (20, 610, 400, 690)
WEB_SECTION = {"steps": {"buy_button": [{"role": "button", "name": "Beli Sekarang"}]},
               "urls": {"login_page": "/buyer/login"}, "calibrated": {"at": "2026-09-30T10:00:00+0700"}}


class CalibApp(FakeShopeeApp):
    """Seperti aplikasi asli: tombol penting & harga punya resource-id (dan sebagian content-desc)."""

    def _render(self):
        out = []
        for key, n in super()._render():
            if key == "buy" and n.text:
                n = replace(n, rid=BUY_RID, desc=n.text)
            elif key == "confirm":
                n = replace(n, rid=CONFIRM_RID)
            elif key == "place_order":
                n = replace(n, rid=PLACE_ORDER_RID)
            elif key == "pay:ShopeePay" and n.text:
                n = replace(n, rid=SHOPEEPAY_RID, desc=n.text)
            elif n.bounds == MAIN_PRICE_BOUNDS:
                n = replace(n, rid=PRICE_RID)
            out.append((key, n))
        return out


class ScriptedUser:
    """Pengguna palsu: tiap prompt dicocokkan dengan langkah skrip (awalan pesan), menjalankan aksinya
    (pindah layar = pengguna men-tap HP SENDIRI), lalu menjawab."""

    def __init__(self, script: list[tuple[str, Callable[[], None] | None, str]]):
        self.script = list(script)
        self.asked: list[str] = []

    def __call__(self, message: str) -> str:
        assert self.script, f"prompt tak terduga: {message!r}"
        prefix, action, answer = self.script.pop(0)
        self.asked.append(message)
        assert message.lstrip().startswith(prefix), f"prompt {message!r}, diharapkan {prefix!r}"
        if action is not None:
            action()
        return answer


def goto(app: FakeShopeeApp, screen: str) -> Callable[[], None]:
    def act() -> None:
        if screen == "checkout":
            app.checkout_items = [(app.sc.product_name, app.unit_price(), 1)]
            app.checkout_at = app.now()
        app.screen = screen

    return act


def kinds(cands: list[dict]) -> list[str]:
    return [next(iter(c)) for c in cands]


def assert_ordered(steps: dict[str, list[dict]]) -> None:
    for step, cands in steps.items():
        ranks = [KIND_RANK[k] for k in kinds(cands)]
        assert ranks == sorted(ranks), f"{step}: urutan kandidat {kinds(cands)}"


def test_calibrate_android_records_ordered_candidates_without_tapping(tmp_path, device, term):
    sel_path = tmp_path / "selectors.json"
    original = {"version": 1, "web": WEB_SECTION}
    sel_path.write_text(json.dumps(original), encoding="utf-8")
    dev = device(time.time() + 3600, app_cls=CalibApp)
    app = dev.app
    user = ScriptedUser([
        ("[buy_button]", goto(app, "product"), ""),
        ("[product_price]", None, ""),
        ("Elemen product_price tidak dikenali", None, "Rp150.000"),  # harga: tidak ada default -> diketik
        ("[sheet_marker]", goto(app, "sheet"), ""),
        ("[sheet_confirm]", None, ""),
        ("[place_order]", goto(app, "checkout"), ""),
        ("[payment_change]", None, ""),
        ("[payment_shopeepay]", goto(app, "payment_list"), ""),
        ("[payment_confirm]", None, "lewati"),
    ])
    cfg = cli._load(cli.build_parser().parse_args(
        ["calibrate", "--config", str(write_cfg(tmp_path, app.sc.open_at)), "--platform", "android"]))
    rc = cli.calibrate_android(cfg, dev.driver, sel_path, prompt=user)
    text = term.getvalue()
    assert rc == 0, text
    assert user.script == [], "semua langkah kalibrasi ditanyakan"

    data = json.loads(sel_path.read_text(encoding="utf-8"))
    assert data["web"] == WEB_SECTION and data["version"] == 1, "bagian web dipertahankan"
    backups = list(tmp_path.glob("selectors.json.bak-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text(encoding="utf-8")) == original
    assert f"Tersimpan: {sel_path} (bagian android) (backup: {backups[0]})" in text

    section = data["android"]
    steps = section["steps"]
    assert set(steps) == {"buy_button", "product_price", "sheet_marker", "sheet_confirm", "place_order",
                          "payment_change", "payment_shopeepay"}
    assert_ordered(steps)
    assert steps["buy_button"] == [{"resourceId": BUY_RID}, {"text": "Beli Sekarang"},
                                   {"textContains": "Beli Sekaran"}, {"description": "Beli Sekarang"}]
    assert steps["product_price"] == [{"resourceId": PRICE_RID}], "teks harga (berubah-ubah) tidak disimpan"
    assert steps["sheet_marker"] == [{"text": "Jumlah"}]
    assert steps["place_order"] == [{"resourceId": PLACE_ORDER_RID}, {"text": "Buat Pesanan"}]
    assert steps["payment_change"] == [{"text": "Metode Pembayaran"}, {"textContains": "Metode Pemba"}]
    assert steps["payment_shopeepay"] == [{"resourceId": SHOPEEPAY_RID}, {"text": "ShopeePay"},
                                          {"description": "ShopeePay"}]
    assert section["calibrated"]["device"] == SERIAL
    assert section["calibrated"]["skipped"] == ["variant_option", "payment_confirm"]
    assert section["markers"] == {} and section["urls"] == {}

    # kalibrasi tidak pernah men-tap / membuka / menekan apa pun: hanya membaca layar
    assert app.kind("tap") == [] and app.kind("intent") == [] and app.kind("order") == []
    assert set(dev.ops()) <= {"dump", "find_all"}, dev.ops()

    # dimuat ulang: hasil kalibrasi di depan default sejenis, default teks tetap jadi cadangan
    loaded = android_selectors.load(sel_path)
    assert loaded.steps["buy_button"] == [{"resourceId": BUY_RID}, {"text": "Beli Sekarang"},
                                          {"textContains": "Beli Sekaran"}, {"textContains": "Beli Sekarang"},
                                          {"description": "Beli Sekarang"}]
    assert loaded.candidates("place_order")[0] == Sel("resourceId", PLACE_ORDER_RID)
    assert loaded.steps["payment_confirm"] == android_selectors.ANDROID_DEFAULT_STEPS["payment_confirm"]
    assert_ordered(loaded.steps)


def test_calibrate_android_via_cli_main_with_variant_placeholder(tmp_path, monkeypatch, device, term):
    variant = "256GB Biru"
    dev = device(time.time() + 3600, variants=["128GB Hitam", variant])
    app = dev.app
    user = ScriptedUser([
        ("[buy_button]", goto(app, "product"), ""),
        ("[product_price]", None, "lewati"),
        ("[sheet_marker]", goto(app, "sheet"), ""),
        ("[variant_option]", None, ""),
        ("[sheet_confirm]", None, "skip"),
        ("[place_order]", goto(app, "checkout"), ""),
        ("[payment_change]", None, "s"),
        ("[payment_shopeepay]", goto(app, "payment_list"), ""),
        ("[payment_confirm]", None, "LEWATI"),
    ])
    monkeypatch.setitem(cli.calibrate_android.__kwdefaults__, "prompt", user)  # ganti input()
    path = write_cfg(tmp_path, app.sc.open_at, variant=variant)
    rc = cli.main(["calibrate", "--config", str(path), "--platform", "android"])
    text = term.getvalue()
    assert rc == 0, text
    assert "Kalibrasi Android." in text and user.script == []
    assert dev.configs and dev.configs[0].variant == variant
    data = json.loads((tmp_path / "selectors.json").read_text(encoding="utf-8"))
    assert set(data) == {"version", "android"} and not list(tmp_path.glob("selectors.json.bak-*"))
    steps = data["android"]["steps"]
    assert set(steps) == {"buy_button", "sheet_marker", "variant_option", "place_order", "payment_shopeepay"}
    assert steps["variant_option"] == [{"text": "{variant}"}], "teks variasi disimpan sebagai placeholder"
    assert variant not in json.dumps(steps)
    assert steps["buy_button"] == [{"text": "Beli Sekarang"}, {"textContains": "Beli Sekaran"}]
    assert data["android"]["calibrated"]["skipped"] == ["product_price", "sheet_confirm", "payment_change",
                                                         "payment_confirm"]
    assert_ordered(steps)
    assert app.kind("tap") == [] and app.kind("variant") == [], "variasi tidak dipilih oleh alat"
    assert set(dev.ops()) <= {"dump", "find_all"}


# ------------------------------------------------------------------ ringkasan hasil di konsol


@pytest.mark.xfail(strict=True, reason=(
    "BUG: cli.print_result mencetak result.message/detail dengan markup Rich aktif. Teks dari layar HP "
    "(mis. nama produk di pesan PRICE_GUARD keranjang '[iPhone ...; ...]') yang diawali huruf kecil di dalam "
    "kurung siku dianggap tag gaya dan HILANG dari konsol (dan '[/...' akan melempar MarkupError)."))
def test_run_android_price_guard_message_keeps_bracketed_product_names(tmp_path, device, term):
    out = run_cli(tmp_path, device, term, product_name="iPhone Uji Coba 128GB", go_cart=True,
                  cart_other_items=[("iPhone Casing Murah", 5_000, True)])
    assert out.rc == 1, out.text
    assert out.result["status"] == "PRICE_GUARD"
    assert "[iPhone Uji Coba 128GB; iPhone Casing Murah]" in out.result["message"]
    assert out.dev.app.kind("checkout") == [] and out.dev.app.kind("order") == []
    assert f"Status: PRICE_GUARD - {out.result['message']}" in out.text


@pytest.mark.xfail(strict=True, reason=(
    "BUG: RunLog mencetak baris WARN/ERROR ke console CLI dengan markup Rich aktif, sehingga awalan "
    "'[android]' (dan teks layar/HP lain dalam kurung siku berhuruf kecil) hilang dari konsol; "
    "file android.log tetap benar."))
def test_run_android_console_warn_lines_keep_platform_tag(tmp_path, device, term):
    out = run_cli(tmp_path, device, term, stay_on="0")
    assert out.rc == 0, out.text
    log = (out.run_dir / "android.log").read_text(encoding="utf-8")
    assert "[android] WARN precheck layar tetap menyala: PERINGATAN" in log
    warn_lines = [line for line in out.text.splitlines() if "WARN precheck layar tetap menyala" in line]
    assert warn_lines and all("[android] WARN" in line for line in warn_lines), warn_lines
