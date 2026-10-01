"""Integrasi CLI jalur Android: `precheck/run/login/calibrate` di atas FakeDriver + FakeShopeeApp.

CLI memakai ServerClock() = jam sistem, jadi device palsu di sini memakai jam NYATA (RealClock)
dengan latensi kecil (5 ms); latensi tetap FakeDriver yang besar (start_url 0,3 s, restart agent 2 s,
dump 0,4 s) dipotong supaya tes tetap cepat. `cli._android_driver` diganti pabrik FakeDriver,
`timesync.sync` diganti laporan palsu (tanpa jaringan), alarm tidak berbunyi.

Desain ronde 2 yang diuji di sini: `run` menolak start setelah T-70 s (kecuali --allow-local, khusus mock/tes:
semua run e2e di sini memakainya karena T hanya beberapa detik lagi); `--live --headless` butuh --allow-local;
precheck/arm tidak pernah melempar (hasil ERROR/CAPTCHA/... dilaporkan, alarm arm de-dup); saldo dibandingkan
dengan min(harga tampil, max_item_price); latensi info/exists dan find_all dilaporkan terpisah; pesan aplikasi
lewat overlay node maupun Toast Android (getLastToast); last_toast/clear_toast = operasi agent saja;
`cli._android_driver` membuat U2Driver(serial, package=...) yang memanggil jsonrpc langsung.
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
from types import SimpleNamespace

import pytest
import yaml
from rich.console import Console

from flashbuy import android_selectors, cli, notifier, timesync
from flashbuy.android_calibrate import ANDROID_CAL_STEPS, AndroidCalibrator
from flashbuy.android_driver import AgentDead, DriverError, FakeDriver, Node, Sel, U2Driver
from flashbuy.android_runner import AndroidRunner
from flashbuy.android_screen import RP_ANY_MATCH, parse_dump
from flashbuy.android_selectors import KIND_RANK
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus
from flashbuy.timesync import WIB, OffsetResult, SyncReport
from tests.conftest import LIVE_NAME, PRICE_DEFAULTS
from tests.fake_android import PACKAGE, PRODUCT_URL, AppScenario, FakeShopeeApp

SERIAL = "FAKE123"
LATENCY_S = 0.005
SLOW_QUERY_S = 0.15
SLOW_FIND_ALL_S = 0.35
OPEN_IN_S = 1.0  # start_time di depan: precheck + arm (~0,35 s) lalu benar-benar menunggu T-lead
WM_SIZE = "Physical size: 1080x2460"  # beda dari tata letak app palsu: resolusi dibaca, bukan diasumsikan
PROPS = {"ro.product.brand": "TECNO", "ro.product.manufacturer": "TECNO", "ro.product.model": "TECNO BG6",
         "ro.build.version.release": "13", "ro.build.version.sdk": "33", "ro.tranos.version": "hios13.6.0"}
# Perintah shell yang menutup/mematikan aplikasi atau mengetik/menekan tombol (termasuk PIN).
FORBIDDEN_SHELL = re.compile(r"force-stop|\bam\s+(kill|stop)|\bpm\s+clear|\binput\b|\bkill\b", re.I)
PRECHECK_ROWS = ["device", "agent uiautomator2", "layar", "layar tetap menyala", "aplikasi Shopee",
                 "versi vs kalibrasi", "login", "tombol Beli", "harga produk", "latensi query", "alamat default",
                 "saldo ShopeePay"]
# last_toast/clear_toast: status Toast di agent uiautomator2 saja (tidak menyentuh aplikasi/Shopee).
AGENT_OPS = {"last_toast", "clear_toast"}
# Operasi driver yang sah dipakai CLI/runner. Tidak ada input teks (PIN tidak mungkin diketik alat).
ALLOWED_OPS = {"exists", "info", "info_any", "find_all", "click", "current_app", "start_url", "swipe_refresh",
               "press_back", "webview", "screenshot", "dump", "shell", "agent_alive", "restart_agent"} | AGENT_OPS
# Klik yang harus langsung diikuti clear_toast: Beli & konfirmasi sheet (keduanya "Beli Sekarang"), Buat Pesanan.
TOAST_CLEARED_CLICKS = {"Beli Sekarang", "Buat Pesanan"}
VARIANTS = ["128GB Hitam", "256GB Biru"]
VARIANT_TOAST = "Silakan pilih variasi terlebih dahulu"


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


class SlowFindAllDriver(FakeDriver):
    """find_all (info_list: ~16 pencarian per elemen) 350 ms pada probe latensi; info/exists tetap cepat."""

    def find_all(self, sel: Sel) -> list:
        if sel == Sel("textMatches", RP_ANY_MATCH):
            time.sleep(SLOW_FIND_ALL_S)
        return super().find_all(sel)


class AgentWontRestartDriver(FakeDriver):
    """Agent uiautomator2 mati dan tidak bisa dihidupkan ulang."""

    def restart_agent(self) -> None:
        self._rpc("restart_agent")
        raise DriverError("uiautomator2 tidak bisa dijalankan ulang")


class AgentDiesOnFindAllDriver(FakeDriver):
    """Agent dibunuh (HiOS) tepat saat bacaan find_all pertama di tengah precheck."""

    def find_all(self, sel: Sel) -> list:
        self._rpc("find_all", sel)
        raise AgentDead("find_all: HTTPError: agent tidak menjawab")


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


class AlarmLog(list):
    """Daftar event alarm (list biasa untuk perbandingan) + pesan tiap alarm di `messages`."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []


@pytest.fixture(autouse=True)
def alarms(monkeypatch) -> AlarmLog:
    """Notifier CLI tanpa beep/webhook; mengembalikan daftar event alarm (diisi saat run)."""
    made: list[notifier.Notifier] = []
    events = AlarmLog()

    class QuietNotifier(notifier.Notifier):
        def __init__(self, webhook_url: str = "", **kw):
            super().__init__(webhook_url, beep=lambda f, d: None, post=lambda u, p: None, repeat=1, **kw)
            made.append(self)

        def alarm(self, event: str, message: str, **extra) -> None:
            events.append(event)
            events.messages.append(message)
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


def write_calibration(tmp_path: Path, app_version: str = "3.40.21", wm_size: str = WM_SIZE) -> Path:
    """selectors.json dengan metadata kalibrasi Android (versi Shopee & resolusi saat kalibrasi)."""
    path = tmp_path / "selectors.json"
    android_selectors.save(path, {}, {"app_version": app_version, "wm_size": wm_size})
    return path


def run_precheck(tmp_path, device, calibrated: bool = True, **kw) -> tuple[int, Device]:
    """precheck --only android. calibrated=True: selectors.json dari kalibrasi di versi Shopee yang sama."""
    if calibrated:
        write_calibration(tmp_path)
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
    # agent responsif: 3 query berturut-turut masing-masing < 1 s
    assert re.fullmatch(r"menjawab 3 query: \d+, \d+, \d+ ms", rows["agent uiautomator2"][1]), rows
    assert rows["layar tetap menyala"][1] == "stay_on_while_plugged_in=3 (USB sudah termasuk)"
    assert rows["aplikasi Shopee"][1] == f"{PACKAGE} 3.40.21"
    assert rows["versi vs kalibrasi"][1] == f"versi 3.40.21 = kalibrasi 3.40.21, {WM_SIZE}"
    assert "enabled=False" in rows["tombol Beli"][1]  # sebelum slot buka: tombol ada tapi belum aktif
    assert rows["harga produk"][1].startswith("harga Rp150.000 > maks Rp100.000")
    # hot path (info/exists, 10 probe + 3 info tombol Beli) dan find_all (lebih mahal) diukur terpisah
    assert re.fullmatch(r"info/exists median \d+ ms, p95 \d+ ms, maks \d+ ms \(13 query\); find_all \d+ ms",
                        rows["latensi query"][1]), rows["latensi query"]
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
                 "latensi query precheck: info/exists median",
                 "latensi precheck exists: n=13",  # 10 probe + 3 ping agent
                 "latensi precheck find_all: n="):
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
    assert detail.startswith("mati, dihidupkan ulang -> menjawab 3 query") and "kemungkinan dibunuh HiOS" in detail
    assert dev.driver.restarts == 1
    assert [i.name for i in prechecks[0].warnings] == ["agent uiautomator2"]
    log = (only_log("logs/*-precheck") / "android.log").read_text(encoding="utf-8")
    assert "WARN agent uiautomator2 mati; menghidupkan ulang" in log


def test_precheck_agent_cannot_restart_fails(tmp_path, device, term, prechecks):
    rc, dev = run_precheck(tmp_path, device, driver_cls=AgentWontRestartDriver, driver_kw={"alive": False})
    text = term.getvalue()
    assert rc == 1, text
    status, detail = table_rows(text)["agent uiautomator2"]
    assert status == "GAGAL" and detail.startswith("mati, dihidupkan ulang -> tetap gagal"), detail
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
    assert p95 >= SLOW_QUERY_S * 1000 and "> target 100/300 ms" in detail
    assert [i.name for i in prechecks[0].warnings] == ["latensi query"]
    log = (only_log("logs/*-precheck") / "android.log").read_text(encoding="utf-8")
    assert re.search(r"WARN \d+ query melewati target \(info/exists/klik 100 ms, find_all 300 ms\); "
                     r"terlama exists text='__flashbuy_(probe|ping)__'", log), log  # probe latensi / ping agent


def test_precheck_slow_find_all_warns_with_its_own_budget(tmp_path, device, term, prechecks):
    """find_all punya target sendiri (300 ms); info/exists cepat tetap di bawah 100 ms."""
    rc, _ = run_precheck(tmp_path, device, driver_cls=SlowFindAllDriver)
    text = term.getvalue()
    assert rc == 0, text
    status, detail = table_rows(text)["latensi query"]
    assert status == "PERINGATAN" and "> target 100/300 ms" in detail, detail
    hot_p95 = int(re.search(r"p95 (\d+) ms", detail).group(1))
    find_all_ms = int(re.search(r"find_all (\d+) ms", detail).group(1))
    assert hot_p95 < 100 <= SLOW_FIND_ALL_S * 1000 <= find_all_ms, detail
    assert [i.name for i in prechecks[0].warnings] == ["latensi query"]
    log = (only_log("logs/*-precheck") / "android.log").read_text(encoding="utf-8")
    assert re.search(r"WARN \d+ query melewati target .*; terlama find_all textMatches=", log), log


def test_precheck_agent_death_mid_precheck_is_reported_not_raised(tmp_path, device, term, prechecks):
    """precheck() tidak pernah melempar: DriverError/AgentDead di tengah precheck -> PrecheckResult ERROR,
    tabel tetap tercetak, exit 1 (bukan traceback / exit 2), log & CSV latensi tetap ditulis."""
    rc, dev = run_precheck(tmp_path, device, driver_cls=AgentDiesOnFindAllDriver)
    text = term.getvalue()
    assert rc == 1, text
    assert prechecks and prechecks[0].status == RunStatus.ERROR
    items = [(i.name, i.ok) for i in prechecks[0].items]
    assert items[0] == ("device", True), "info device yang sudah terbaca tetap dilaporkan"
    assert items[-1] == ("device", False), items
    failed = [i for i in prechecks[0].items if i.ok is False]
    assert len(failed) == 1 and failed[0].detail.startswith("koneksi device/agent gagal: find_all: HTTPError")
    assert table_rows(text)["device"] == ("GAGAL", failed[0].detail)  # tercetak di tabel
    assert "Traceback" not in text
    log_dir = only_log("logs/*-precheck")
    assert (log_dir / "android-queries-precheck.csv").exists()
    assert dev.app.kind("tap") == [] and dev.app.kind("order") == []
    no_forbidden_shell(dev)


@pytest.mark.parametrize(("scenario", "row", "detail"), [
    ({"wallet_unsupported": True}, "saldo ShopeePay", "ALARM: halaman tidak bisa dibuka di aplikasi (am start gagal"),
    # perintah precheck saja: hanya dilaporkan (run yang memasang svc power stayon usb)
    ({"stay_on": "0"}, "layar tetap menyala", "stay_on_while_plugged_in=0, timeout layar 60000 ms; saat run alat "
                                              "memasang `svc power stayon usb`"),
], ids=["wallet_unsupported", "stay_on_off"])
def test_precheck_warnings_do_not_fail(tmp_path, device, term, prechecks, scenario, row, detail):
    rc, _ = run_precheck(tmp_path, device, **scenario)
    text = term.getvalue()
    assert rc == 0, text
    status, got = table_rows(text)[row]
    assert status == "PERINGATAN" and got.startswith(detail), got
    assert [i.name for i in prechecks[0].warnings] == [row]


def test_precheck_wallet_page_asking_pin_is_not_typed(tmp_path, device, term, prechecks, alarms):
    """Halaman ShopeePay meminta PIN: alat tidak mengetik/men-tap kolom PIN; saldo tidak terbaca -> alarm saja,
    precheck tidak gagal (spesifikasi G), lalu kembali ke halaman produk."""
    rc, dev = run_precheck(tmp_path, device, wallet_pin=True)
    text = term.getvalue()
    assert rc == 0, text
    assert table_rows(text)["saldo ShopeePay"] == (
        "PERINGATAN", "ALARM: halaman meminta PIN; tidak dibaca (PIN tidak diketik alat)")
    assert alarms == ["precheck_saldo ShopeePay"]
    assert dev.app.kind("tap") == [] and "click" not in dev.ops(), "kolom PIN tidak disentuh"
    assert not [c for c in dev.shell_cmds() if re.search(r"\binput\b", c)], "tidak ada input teks"
    assert dev.app.screen == "product"


def test_precheck_balance_enough_for_flash_price_is_not_failure(tmp_path, device, term, prechecks):
    # sebelum slot buka halaman menampilkan harga normal Rp150.000 (> max_item_price); alat tidak pernah membayar
    # > max_item_price per unit, jadi saldo dibandingkan dengan min(harga tampil, max_item_price) = Rp100.000
    rc, _ = run_precheck(tmp_path, device, wallet_balance=110_000)
    text = term.getvalue()
    rows = table_rows(text)
    assert "saldo vs max_total" not in rows, "saldo vs max_total = bagian baris 'saldo ShopeePay'"
    assert rows["saldo ShopeePay"] == ("PERINGATAN", "ALARM: saldo Rp110.000 >= harga Rp100.000 tetapi < max_total "
                                                     "Rp120.000 (ongkir/biaya bisa membuatnya kurang)")
    assert [i.name for i in prechecks[0].warnings] == ["saldo ShopeePay"]
    assert rc == 0, text


@pytest.mark.parametrize(("normal_price", "balance", "status", "detail"), [
    (150_000, 99_999, "PERINGATAN", "ALARM: saldo Rp99.999 < harga Rp100.000"),  # acuan = max_item_price
    (150_000, 120_000, "OK", "saldo Rp120.000 >= max_total Rp120.000"),
    (80_000, 79_999, "PERINGATAN", "ALARM: saldo Rp79.999 < harga Rp80.000"),  # harga tampil < max_item_price
    (80_000, 80_000, "PERINGATAN", "ALARM: saldo Rp80.000 >= harga Rp80.000 tetapi < max_total Rp120.000"),
], ids=["kurang_dari_maks_item", "cukup_maks_total", "kurang_dari_harga_tampil", "sama_harga_tampil"])
def test_precheck_balance_vs_min_displayed_price_and_max_item(tmp_path, device, term, prechecks, alarms,
                                                               normal_price, balance, status, detail):
    """Android: saldo < min(harga tampil, max_item_price) atau < max_total -> ALARM + PERINGATAN (run tidak
    dihentikan, exit 0); else OK tanpa alarm.

    (PERINGATAN = penilai halaman saldo mengembalikan None -> halaman dibaca ulang sampai 5 s jam nyata; kasus
    PERINGATAN sengaja sedikit supaya tes tetap cepat.)"""
    rc, dev = run_precheck(tmp_path, device, normal_price=normal_price, wallet_balance=balance)
    text = term.getvalue()
    got_status, got_detail = table_rows(text)["saldo ShopeePay"]
    assert got_status == status and got_detail.startswith(detail), (got_status, got_detail)
    assert rc == 0, text  # spesifikasi G: alamat/saldo hanya alarm, run tidak dihentikan
    assert prechecks[0].status is None, "saldo kurang = alarm, bukan status fatal"
    assert alarms == (["precheck_saldo ShopeePay"] if status != "OK" else []), alarms
    assert dev.app.kind("tap") == [] and dev.app.kind("order") == [], "precheck hanya membaca"


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
    rc: int | None
    text: str
    dev: Device
    open_at: float
    result: dict | None
    run_dir: Path | None
    error: BaseException | None = None  # exception dari cli.main (hanya bila run_cli(catch=True))

    def step_names(self) -> list[str]:
        return [s["name"] for s in self.result["steps"]]

    def step(self, name: str) -> dict:
        return next(s for s in self.result["steps"] if s["name"] == name)

    def buys(self) -> list[dict]:
        return self.dev.app.kind("buy")


def run_cli(tmp_path, device, term, *, live: bool = False, cfg: dict | None = None, open_in_s: float = OPEN_IN_S,
            catch: bool = False, **scenario) -> CliRun:
    open_at = time.time() + open_in_s
    dev = device(open_at, **scenario)
    # T beberapa detik lagi (jam nyata): --allow-local (opsi tersembunyi khusus mock/tes) melewati penolakan
    # "Terlambat" (run harus mulai <= T-70 s); penolakan itu sendiri diuji di test_run_late_start_*
    argv = ["run", "--config", str(write_cfg(tmp_path, open_at, **(cfg or {}))), "--only", "android",
            "--allow-local"]
    rc, error = None, None
    try:
        rc = cli.main(argv + (["--live"] if live else []))
    except Exception as e:  # noqa: BLE001 - hanya dengan catch=True (tes yang memeriksa keamanan saat konsol gagal)
        if not catch:
            raise
        error = e
    found = list(tmp_path.glob("logs/*-android/android-result.json"))
    assert len(found) <= 1
    result = json.loads(found[0].read_text(encoding="utf-8")) if found else None
    return CliRun(rc, term.getvalue(), dev, open_at, result, found[0].parent if found else None, error)


def assert_safe_ops(dev: Device) -> None:
    """Hanya operasi driver yang sah; tanpa perintah shell penutup aplikasi / input teks."""
    assert set(dev.ops()) <= ALLOWED_OPS, set(dev.ops()) - ALLOWED_OPS
    no_forbidden_shell(dev)


def assert_toast_cleared_before_clicks(dev: Device) -> int:
    """clear_toast (operasi agent) tepat sebelum setiap klik Beli / konfirmasi sheet / Buat Pesanan."""
    calls = dev.driver.calls
    idx = [i for i, (op, target) in enumerate(calls) if op == "click" and target in TOAST_CLEARED_CLICKS]
    assert idx, "tidak ada klik Beli/konfirmasi/Buat Pesanan"
    for i in idx:
        j = i - 1
        if calls[i][1] == "Beli Sekarang" and calls[j][0] == "info_any":  # cek dialog crash/ANR tepat sebelum klik
            j -= 1
        if calls[j] == ("info", "text='Beli Sekarang'"):  # cek ulang tombol setelah menunggu slot
            j -= 1
        assert calls[j] == ("clear_toast", ""), calls[i - 4:i + 1]
    return len(idx)


def run_csv(out: CliRun) -> list[dict]:
    with (out.run_dir / "android-queries-run.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def hook_arm(monkeypatch, before: Callable[[AndroidRunner], None]) -> None:
    """Jalankan `before(runner)` tepat saat arm (T-60 s) dimulai, sebelum intent membuka produk."""
    orig = AndroidRunner._arm_sync

    def arm_sync(self: AndroidRunner) -> None:
        before(self)
        return orig(self)

    monkeypatch.setattr(AndroidRunner, "_arm_sync", arm_sync)


class ArmScreenApp(FakeShopeeApp):
    """Intent saat arm (T-60 s) mendarat di layar lain (captcha/verifikasi/login), padahal precheck normal."""

    arm_screen: str | None = None
    arming = False

    def on_intent(self, url: str, package: str) -> None:
        super().on_intent(url, package)
        if self.arming and self.arm_screen:
            self.screen = self.arm_screen


class ArmFailDriver(FakeDriver):
    """Intent VIEW gagal saat arm (precheck sebelumnya normal)."""

    arming = False

    def start_url(self, url: str, package: str, wait: bool = True) -> None:
        if self.arming:
            self._rpc("start_url", url)
            raise DriverError("am start gagal: Error: Activity not started, unable to resolve Intent")
        super().start_url(url, package, wait)


class AsyncToastApp(FakeShopeeApp):
    """Toast Android ASINKRON (seperti HP): tercatat di getLastToast `toast_delay_s` setelah tap, jadi
    clearLastToast runner tepat setelah klik tidak menghapus reaksi klik itu. Tidak pernah menjadi node."""

    toast_delay_s = 0.06

    def __init__(self, sc, clock):
        self._pending: tuple[str, float] | None = None
        self._recorded: str | None = None
        super().__init__(sc, clock)

    def _flush(self) -> None:
        if self._pending is not None and self.now() >= self._pending[1]:
            self._recorded, self._pending = self._pending[0], None

    def _show_toast(self, text: str) -> None:
        self.toast = (text, self.now() + self.toast_delay_s + 1.5)
        if self.sc.toast_mode == "toast":
            self._pending = (text, self.now() + self.toast_delay_s)

    @property
    def last_toast(self) -> str | None:
        self._flush()
        return self._recorded

    @last_toast.setter
    def last_toast(self, value: str | None) -> None:
        self._flush()
        self._recorded = value


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
    assert_safe_ops(out.dev)
    assert alarms == []
    # klik Beli & konfirmasi sheet tepat didahului clear_toast (operasi agent, tidak menyentuh aplikasi)
    assert assert_toast_cleared_before_clicks(out.dev) == 2
    # hot path polling (poll_start .. klik Beli): hanya query satu objek (info/exists; info_any = cek dialog sistem
    # crash/ANR tiap <= 0,5 s) + clear_toast sebelum klik, tanpa find_all/baca toast/dump
    t_poll, t_click = out.step("poll_start")["t_server_ms"], out.step("click_buy")["t_server_ms"]
    hot = {r["op"] for r in run_csv(out) if t_poll <= int(r["t_server_ms"]) <= t_click}
    assert hot and hot <= {"info", "info_any", "exists", "clear_toast"}, hot
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
    assert_safe_ops(out.dev)  # tidak ada `input text` -> PIN tidak mungkin diketik alat
    _subsequence(out.step_names(), ["price_guard_ok", "place_order_gate", "click_place_order", "pin_screen"])
    assert alarms == ["ORDER_PLACED_AWAIT_PIN"]
    # Beli, konfirmasi sheet, Buat Pesanan: masing-masing langsung diikuti clear_toast
    assert assert_toast_cleared_before_clicks(out.dev) == 3
    # setelah klik "Buat Pesanan" tidak ada klik/tombol/intent lagi (layar PIN tidak disentuh)
    calls = out.dev.driver.calls
    after = calls[calls.index(("click", "Buat Pesanan")) + 1:]
    assert not {"click", "press_back", "start_url", "swipe_refresh"} & {op for op, _ in after}, after


def test_run_android_live_unknown_after_order_prints_maybe_ordered(tmp_path, device, term, alarms):
    out = run_cli(tmp_path, device, term, live=True, cfg={"expected_name": LIVE_NAME}, unknown_after_order=True)
    assert out.rc == 1, out.text
    assert f"Status: UNKNOWN_STATE - {MAYBE_ORDERED_MSG}" in out.text
    assert out.result["status"] == "UNKNOWN_STATE" and out.result["message"] == MAYBE_ORDERED_MSG
    # pesan diagnosa baru: dihitung dari waktu pengamatan, diagnosa (aplikasi aktif, WebView, content-desc)
    assert re.fullmatch(r"UNKNOWN_STATE: layar tidak dikenali > \d+\.\d s \(tidak ada elemen yang dikenali, "
                        rf"aplikasi aktif {re.escape(PACKAGE)}/\S+\)", out.result["detail"]), out.result["detail"]
    assert f"Detail: {out.result['detail']}" in out.text
    assert "Aplikasi di HP dibiarkan terbuka apa adanya" in out.text
    assert len(out.dev.app.kind("order")) == 1 and out.dev.app.screen == "order_unknown"
    assert alarms == ["UNKNOWN_STATE"]
    # aplikasi/activity & WebView (klasifikasi spesifikasi F) dibaca ulang paling cepat tiap 0,5 s selama UNKNOWN;
    # diagnosa mahal (content-desc semua elemen) hanya SEKALI, saat naik ke UNKNOWN_STATE
    calls = out.dev.driver.calls
    after_order = [op for op, _ in calls[calls.index(("click", "Buat Pesanan")):]]
    assert 1 <= after_order.count("webview") <= 5 and after_order.count("current_app") <= 6, after_order
    assert len([1 for op, t in calls if op == "find_all" and t.startswith("descriptionMatches")]) == 1
    after = calls[calls.index(("click", "Buat Pesanan")) + 1:]
    assert not {"click", "press_back", "start_url", "swipe_refresh"} & {op for op, _ in after}, "tanpa retry"
    assert_safe_ops(out.dev)


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
    assert alarms == [status], "alarm sekali (tidak dobel)"
    # setelah klik Beli: tidak ada aksi ke aplikasi lagi (tanpa klik, back, reload, intent)
    ops = out.dev.ops()
    assert not {"click", "press_back", "start_url", "swipe_refresh"} & set(ops[ops.index("click") + 1:])
    assert_safe_ops(out.dev)


@pytest.mark.parametrize("toast", ["node", "toast"], ids=["overlay_node", "android_toast"])
def test_run_android_variant_required_message_via_both_channels_is_error(tmp_path, device, term, alarms, toast):
    """Pesan "Silakan pilih variasi" sebagai overlay in-app (node) ATAU Toast Android (hanya lewat getLastToast):
    CLI berhenti ERROR (isi `variant`), tanpa klik ulang Beli/konfirmasi, tanpa checkout."""
    kw = {"toast_mode": "node"} if toast == "node" else {"app_cls": AsyncToastApp}
    out = run_cli(tmp_path, device, term, variants=VARIANTS, variant_required=True, **kw)
    assert out.rc == 1, out.text
    assert "Status: ERROR - produk wajib pilih variasi; isi `variant` di target.yaml" in out.text
    assert len(out.buys()) == 1 and len(out.dev.app.kind("confirm")) == 1, "tanpa klik ulang"
    assert "no_response" not in out.step_names()
    assert out.dev.app.kind("variant") == [], "variasi tidak dipilih alat tanpa config `variant`"
    assert out.dev.app.kind("checkout") == [] and out.dev.app.kind("order") == []
    if toast == "toast":
        assert out.dev.app.sc.toast_mode == "toast"
        assert out.dev.app.last_toast == VARIANT_TOAST, "pesan hanya ada di kanal getLastToast"
        assert "last_toast" in out.dev.ops()
    assert_toast_cleared_before_clicks(out.dev)
    assert_safe_ops(out.dev)
    assert alarms == []


@pytest.mark.parametrize(("screen", "status"), [("captcha", "CAPTCHA"), ("verification", "VERIFICATION"),
                                                 ("login", "LOGIN_REQUIRED")])
def test_run_android_terminal_screen_at_arm_alarms_immediately_once(tmp_path, monkeypatch, device, term, alarms,
                                                                     screen, status):
    """Captcha/verifikasi/login muncul saat buka produk T-60 s (precheck normal): alarm SAAT ITU (sebelum T),
    hasil akhir dari attempt() tanpa alarm kedua, tanpa polling/klik, layar dibiarkan apa adanya."""
    hook_arm(monkeypatch, lambda r: setattr(r.d.inner.app, "arming", True))
    monkeypatch.setattr(ArmScreenApp, "arm_screen", screen)
    out = run_cli(tmp_path, device, term, app_cls=ArmScreenApp)
    assert out.rc == 1, out.text
    assert out.result["status"] == status
    assert out.result["message"].startswith("saat membuka produk: teks "), out.result["message"]
    assert f"Status: {status} - saat membuka produk" in out.text
    assert alarms == [status], "alarm sekali (arm), tidak diulang hasil akhir"
    # alarm berasal dari arm (pesan "(T-60 s)"), dibunyikan sebelum menunggu T-lead
    assert alarms.messages[0].startswith("saat membuka produk (T-60 s): teks "), alarms.messages
    log = (out.run_dir / "android.log").read_text(encoding="utf-8")
    assert log.index(f"WARN arm: {status}") < log.index("STEP t_minus_lead")
    assert "poll_start" not in out.step_names() and "armed" not in out.step_names()
    assert out.buys() == [] and out.dev.app.kind("tap") == [] and "click" not in out.dev.ops()
    assert out.dev.app.screen == screen, "layar dibiarkan apa adanya"
    ops = out.dev.ops()
    last_intent = len(ops) - 1 - ops[::-1].index("start_url")
    assert not {"press_back", "start_url", "swipe_refresh"} & set(ops[last_intent + 1:])
    assert "Aplikasi di HP dibiarkan terbuka apa adanya" in out.text
    captcha_note = "Captcha/verifikasi di HP dibiarkan apa adanya. Selesaikan manual; alat TIDAK akan retry."
    assert (captcha_note in out.text) == (status != "LOGIN_REQUIRED")
    assert_safe_ops(out.dev)


def test_run_android_arm_driver_error_is_returned_by_attempt(tmp_path, monkeypatch, device, term, alarms):
    """arm() tidak melempar: intent gagal di T-60 s -> disimpan, attempt() mengembalikan ERROR (exit 1)."""
    hook_arm(monkeypatch, lambda r: setattr(r.d.inner, "arming", True))
    out = run_cli(tmp_path, device, term, driver_cls=ArmFailDriver)
    assert out.rc == 1, out.text
    assert out.result["status"] == "ERROR"
    assert out.result["message"].startswith("saat membuka produk (T-60 s): am start gagal"), out.result["message"]
    assert "Status: ERROR - saat membuka produk (T-60 s): am start gagal" in out.text
    assert "Traceback" not in out.text
    assert "poll_start" not in out.step_names() and out.buys() == [] and "click" not in out.dev.ops()
    assert alarms == ["ERROR"], "alarm arm sekali, tidak diulang hasil akhir"
    log = (out.run_dir / "android.log").read_text(encoding="utf-8")
    assert "WARN arm gagal: ERROR saat membuka produk (T-60 s)" in log
    assert_safe_ops(out.dev)


def test_run_android_precheck_error_stops_before_opening_product(tmp_path, device, term, alarms):
    out = run_cli(tmp_path, device, term, driver_cls=AgentWontRestartDriver, driver_kw={"alive": False})
    assert out.rc == 1, out.text
    assert out.result["status"] == "ERROR"
    assert out.result["message"].startswith("pre-check: agent uiautomator2: mati, dihidupkan ulang -> tetap gagal")
    assert "arm" not in out.step_names() and out.dev.app.kind("intent") == []
    assert out.buys() == [] and "click" not in out.dev.ops()
    assert alarms == ["precheck"]


# ------------------------------------------------------------------ run: ditolak sebelum driver & timesync


@pytest.mark.parametrize("case", ["run_no_only", "precheck_no_only", "android_disabled", "live_no_expected_name",
                                  "live_blank_expected_name", "lead_out_of_range", "start_passed", "late_start",
                                  "live_late_start", "live_headless"])
def test_run_rejected_before_driver_and_timesync(tmp_path, device, term, sync_calls, case):
    open_at = time.time() + {"start_passed": -30, "late_start": 5, "live_late_start": 30}.get(case, 3600)
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
    elif case == "late_start":  # T beberapa detik lagi tanpa --allow-local: precheck/arm akan jatuh di jendela polling
        expect = f"Terlambat: run harus dimulai paling lambat T-{cli.LATE_START_S} s"
    elif case == "live_late_start":
        cfg, argv, expect = {"expected_name": LIVE_NAME}, [*argv, "--live"], "Terlambat"
    elif case == "live_headless":
        cfg, argv, expect = {"expected_name": LIVE_NAME}, [*argv, "--live", "--headless"], \
            "--live tidak boleh --headless"
    path = write_cfg(tmp_path, open_at, **cfg)
    rc = cli.main([argv[0], "--config", str(path), *argv[1:]])
    assert rc == 2
    assert expect in term.getvalue(), term.getvalue()
    assert dev.configs == [], "driver Android tidak boleh dibuat"
    assert sync_calls == [], "timesync tidak boleh jalan"
    assert dev.driver.calls == [] and not Path("logs").exists()


@pytest.mark.parametrize(("start_in_s", "extra", "late"), [
    (cli.LATE_START_S - 5, [], True),
    (cli.LATE_START_S + 5, [], False),
    (5, ["--allow-local"], False),  # mock/tes saja
    (cli.LATE_START_S + 5, ["--live", "--headless", "--allow-local"], False),
], ids=["T-65s_ditolak", "T-75s_lolos", "allow_local", "live_headless_allow_local"])
def test_run_late_start_boundary(tmp_path, monkeypatch, term, sync_calls, start_in_s, extra, late):
    """Batas T-70 s: lolos pemeriksaan = sampai ke pembuatan driver (di sini sengaja gagal konek -> exit 2)."""
    reached = []

    def factory(cfg):
        reached.append(cfg)
        raise DriverError(f"gagal konek ke device {cfg.android.serial}: berhenti di tes")

    monkeypatch.setattr(cli, "_android_driver", factory)
    path = write_cfg(tmp_path, time.time() + start_in_s, expected_name=LIVE_NAME)
    rc = cli.main(["run", "--config", str(path), "--only", "android", *extra])
    text = term.getvalue()
    assert rc == 2, text
    assert ("Terlambat" in text) is late, text
    assert (reached == []) is late
    assert sync_calls == [], "timesync tidak jalan (ditolak / gagal konek dulu)"
    assert not Path("logs").exists()


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


CUSTOM_PACKAGE = "com.shopee.id.uji"  # android.package dari config harus sampai ke U2Driver


class _FakeJsonRpc:
    """`d.jsonrpc.<method>(*params, http_timeout=...)` uiautomator2: dicatat, jawaban kalengan."""

    def __init__(self, dev: FakeU2Device):
        self._dev = dev

    def __getattr__(self, method: str):
        def call(*params, http_timeout=None):
            self._dev.rpc.append((method, params, http_timeout))
            return {"exist": True, "deviceInfo": {}}.get(method)

        return call


class FakeU2Device:
    """uiautomator2.Device palsu sebatas yang dipakai U2Driver: jsonrpc, d(**kw).selector, shell."""

    def __init__(self, serial: str | None):
        self.serial = serial or ""
        self.rpc: list[tuple[str, tuple, float | None]] = []
        self.shells: list[list[str]] = []
        self.jsonrpc = _FakeJsonRpc(self)
        self.jsonrpc_call = None
        # atribut internal yang dibaca _disable_implicit_restart (koneksi nyata)
        self._dev, self._device_server_port, self._debug = object(), 9008, False

    def __call__(self, **kw):
        return SimpleNamespace(selector=dict(kw))

    def shell(self, cmd, timeout=None):
        self.shells.append(list(cmd))
        return SimpleNamespace(output="Starting: Intent { act=android.intent.action.VIEW }\nStatus: ok\n")


@pytest.fixture
def u2_devices(monkeypatch) -> list[FakeU2Device]:
    import uiautomator2

    made: list[FakeU2Device] = []

    def connect(serial=None):
        made.append(FakeU2Device(serial))
        return made[-1]

    monkeypatch.setattr(uiautomator2, "connect", connect)
    return made


def test_real_driver_factory_builds_package_scoped_u2driver(tmp_path, u2_devices):
    path = write_cfg(tmp_path, time.time() + 3600, android={"package": CUSTOM_PACKAGE})
    cfg = cli._load(cli.build_parser().parse_args(["login", "--config", str(path), "--platform", "android"]))
    drv = cli._android_driver(cfg)
    assert isinstance(drv, U2Driver)
    assert [d.serial for d in u2_devices] == [SERIAL] and drv.serial == SERIAL
    assert drv.package == CUSTOM_PACKAGE, "query dibatasi ke aplikasi Shopee dari config"
    # restart implisit u2 (berdetik-detik, diam-diam) dimatikan untuk koneksi nyata
    assert drv.no_implicit_restart is True and callable(u2_devices[0].jsonrpc_call)
    # query = jsonrpc langsung dengan selector ber-packageName dan timeout per panggilan
    assert drv.exists(Sel("text", "Beli Sekarang")) is True
    assert u2_devices[0].rpc == [("exist", ({"text": "Beli Sekarang", "packageName": CUSTOM_PACKAGE},),
                                  drv.rpc_timeout_s)]
    assert drv.rpc_timeout_s < 300, "bukan timeout bawaan u2 (300 s)"


def test_login_android_with_real_driver_only_opens_app_intent(tmp_path, term, u2_devices):
    path = write_cfg(tmp_path, time.time() + 3600, android={"package": CUSTOM_PACKAGE})
    rc = cli.main(["login", "--config", str(path), "--platform", "android"])
    text = term.getvalue()
    assert rc == 0, text
    (dev,) = u2_devices
    assert dev.shells == [["am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", "https://shopee.co.id/",
                           "-p", CUSTOM_PACKAGE]]
    assert dev.rpc == [], "login tidak membaca/men-tap layar (alat tidak mengetik apa pun)"
    assert f"Device {SERIAL}: aplikasi {CUSTOM_PACKAGE} dibuka." in text


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
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[product_price]", None, ""),
        ("Nomor elemen", None, "Rp150.000"),  # harga: tidak ada default -> cari teks yang diketik
        ("Nomor elemen", None, "1"),
        ("[sheet_marker]", goto(app, "sheet"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[sheet_confirm]", None, "lewati"),  # dikalibrasi di tes ..._same_text_as_buy_button_behind_sheet
        ("[place_order]", goto(app, "checkout"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[payment_change]", None, ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[payment_shopeepay]", goto(app, "payment_list"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
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
    assert f"Tersimpan: {sel_path} (bagian android; Shopee 3.40.21, {WM_SIZE}) (backup: {backups[0]})" in text
    # daftar kandidat bernomor: teks / resourceId / bounds
    assert f"1. teks='Beli Sekarang' desc='Beli Sekarang' id={BUY_RID} bounds=[360,1500][720,1612] klik" in text

    section = data["android"]
    steps = section["steps"]
    assert set(steps) == {"buy_button", "product_price", "sheet_marker", "place_order", "payment_change",
                          "payment_shopeepay"}
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
    # versi Shopee & resolusi saat kalibrasi (precheck: PERINGATAN KERAS bila versi berubah)
    assert section["calibrated"]["app_version"] == "3.40.21" and section["calibrated"]["wm_size"] == WM_SIZE
    assert section["calibrated"]["skipped"] == ["variant_option", "sheet_confirm", "payment_confirm"]
    assert section["markers"] == {} and section["urls"] == {}

    # kalibrasi tidak pernah men-tap / membuka / menekan apa pun: hanya membaca layar (+ versi app & resolusi)
    assert app.kind("tap") == [] and app.kind("intent") == [] and app.kind("order") == []
    assert set(dev.ops()) <= {"dump", "find_all", "shell"}, dev.ops()
    assert dev.shell_cmds() == [f"dumpsys package {PACKAGE}", "wm size"]
    # "Buat Pesanan" hanya dari dump: tidak ada query device ke elemen itu
    assert not [t for op, t in dev.driver.calls if op == "find_all" and ("Buat" in t or PLACE_ORDER_RID in t)]

    # dimuat ulang: hasil kalibrasi di depan default sejenis, default teks tetap jadi cadangan
    loaded = android_selectors.load(sel_path)
    # default buy_button ronde 2 = [text, description] (tanpa textContains); duplikat dibuang
    assert loaded.steps["buy_button"] == [{"resourceId": BUY_RID}, {"text": "Beli Sekarang"},
                                          {"textContains": "Beli Sekaran"}, {"description": "Beli Sekarang"}]
    assert loaded.candidates("place_order")[0] == Sel("resourceId", PLACE_ORDER_RID)
    assert loaded.steps["payment_confirm"] == android_selectors.ANDROID_DEFAULT_STEPS["payment_confirm"]
    assert loaded.steps["sheet_confirm"] == [{"text": "Beli Sekarang"}, {"text": "Konfirmasi"}], "default baru"
    assert_ordered(loaded.steps)


def test_calibrate_android_sheet_confirm_same_text_as_buy_button_behind_sheet(tmp_path, device, term):
    sel_path = tmp_path / "selectors.json"
    dev = device(time.time() + 3600, app_cls=CalibApp)
    app = dev.app
    user = ScriptedUser([
        ("[buy_button]", goto(app, "product"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[product_price]", None, "lewati"),
        ("[sheet_marker]", goto(app, "sheet"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[sheet_confirm]", None, ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[place_order]", goto(app, "checkout"), "lewati"),
        ("[payment_change]", None, "lewati"),
        ("[payment_shopeepay]", None, "lewati"),
        ("[payment_confirm]", None, "lewati"),
    ])
    cfg = cli._load(cli.build_parser().parse_args(
        ["calibrate", "--config", str(write_cfg(tmp_path, app.sc.open_at)), "--platform", "android"]))
    rc = cli.calibrate_android(cfg, dev.driver, sel_path, prompt=user)
    assert rc == 0 and user.script == []
    steps = json.loads(sel_path.read_text(encoding="utf-8"))["android"]["steps"]
    # yang direkam = tombol konfirmasi DI sheet (jendela aktif, nomor 1 di daftar), bukan tombol Beli di
    # belakangnya; teks 'Beli Sekarang' tidak unik di semua jendela -> hanya resource-id yang unik disimpan
    assert steps["sheet_confirm"] == [{"resourceId": CONFIRM_RID}]
    assert "1. teks='Beli Sekarang' id=" + CONFIRM_RID in term.getvalue()
    assert "sheet_confirm: text='Beli Sekarang' tidak unik (2 elemen di dump), dibuang" in term.getvalue()
    assert app.kind("tap") == [] and set(dev.ops()) <= {"dump", "find_all", "shell"}


def test_calibrate_android_via_cli_main_with_variant_placeholder(tmp_path, monkeypatch, device, term):
    variant = "256GB Biru"
    dev = device(time.time() + 3600, variants=["128GB Hitam", variant])
    app = dev.app
    user = ScriptedUser([
        ("[buy_button]", goto(app, "product"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[product_price]", None, "lewati"),
        ("[sheet_marker]", goto(app, "sheet"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[variant_option]", None, ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[sheet_confirm]", None, "skip"),
        ("[place_order]", goto(app, "checkout"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
        ("[payment_change]", None, "s"),
        ("[payment_shopeepay]", goto(app, "payment_list"), ""),
        ("Nomor elemen", None, ""),  # Enter = kandidat no. 1
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
    assert set(dev.ops()) <= {"dump", "find_all", "shell"}


class MetaFailsDriver(FakeDriver):
    """`dumpsys package` gagal (adb putus sesaat) tepat setelah pengguna menyelesaikan semua langkah kalibrasi."""

    def shell(self, cmd: list[str]) -> str:
        if cmd[:2] == ["dumpsys", "package"]:
            raise DriverError("adb: device offline")
        return super().shell(cmd)


def test_calibrate_android_keeps_results_when_app_version_unreadable(tmp_path, device, term):
    """Regresi: galat adb saat membaca versi Shopee/resolusi (sesudah semua langkah) tidak membuang hasil
    kalibrasi; versi disimpan kosong + peringatan (precheck lalu memberi PERINGATAN KERAS)."""
    sel_path = tmp_path / "selectors.json"
    dev = device(time.time() + 3600, driver_cls=MetaFailsDriver)
    app = dev.app
    user = ScriptedUser([
        ("[buy_button]", goto(app, "product"), ""),
        ("Nomor elemen", None, ""),
        *[(f"[{key}]", None, "lewati") for key in ("product_price", "sheet_marker", "sheet_confirm", "place_order",
                                                   "payment_change", "payment_shopeepay", "payment_confirm")],
    ])
    cfg = cli._load(cli.build_parser().parse_args(
        ["calibrate", "--config", str(write_cfg(tmp_path, app.sc.open_at)), "--platform", "android"]))
    rc = cli.calibrate_android(cfg, dev.driver, sel_path, prompt=user)
    text = term.getvalue()
    assert rc == 0 and user.script == [], text
    assert "! versi Shopee tidak terbaca (adb: device offline); disimpan kosong" in text
    assert f"Tersimpan: {sel_path} (bagian android; Shopee ?, {WM_SIZE})" in text
    section = json.loads(sel_path.read_text(encoding="utf-8"))["android"]
    assert section["steps"]["buy_button"][0] == {"text": "Beli Sekarang"}
    assert section["calibrated"]["app_version"] == "" and section["calibrated"]["wm_size"] == WM_SIZE


def test_calibrate_variant_s_lists_only_its_chip_and_typing_s_searches_not_skips(tmp_path, device, term):
    """Variasi 'S' (ukuran): kata kunci dicocokkan per kata dan tidak ke awalan resourceId 'com.shopee.id:id/'
    (dulu: hampir semua elemen terdaftar + 15 query peringkat). Mengetik 'S' di prompt nomor = cari teks itu,
    bukan 'lewati'."""
    class RidApp(FakeShopeeApp):
        def _render(self):  # setiap elemen punya resourceId (ber-awalan package)
            return [(k, replace(n, rid=n.rid or f"{PACKAGE}:id/n{i}")) for i, (k, n) in enumerate(super()._render())]

    dev = device(time.time() + 3600, app_cls=RidApp, variants=["S", "M", "L"])
    dev.app.screen = "sheet"
    answers = iter(["S", "1"])
    said: list[str] = []
    cal = AndroidCalibrator(dev.driver, android_selectors.defaults(), variant="S", prompt=lambda m: next(answers),
                            say=said.append)
    step = next(s for s in ANDROID_CAL_STEPS if s.key == "variant_option")
    nodes = parse_dump(dev.driver.dump())
    before = len(dev.driver.calls)
    listed = cal._listed(step, nodes)
    assert [n.text for n in listed] == ["S"], [n.text for n in listed]
    assert [op for op, _ in dev.driver.calls[before:]] == [], "satu kandidat: tanpa query peringkat"
    chosen = cal._choose(step, nodes)
    assert chosen is not None and chosen != "skip" and "S" in chosen.text.upper(), chosen
    assert next(answers, None) is None, "kedua jawaban dipakai ('S' = cari, '1' = pilih)"


@pytest.mark.parametrize("variant, chip", [
    ("XL (Hitam)", "XL (Hitam) - sisa 3"), ("Paket A (2 pcs)", "Paket A (2 pcs)\nRp99.000"), ("L+", "L+ (stok 2)"),
    ("S", "S"),
])
def test_calibrate_variant_keyword_with_punctuation_still_listed(variant, chip):
    """Kata kunci variasi dicocokkan per kata tanpa \\b (yang tidak pernah cocok bila variasi diawali/diakhiri tanda
    baca); 'S' tetap tidak cocok ke 'ShopeePay'/'Stok'."""
    step = next(s for s in ANDROID_CAL_STEPS if s.key == "variant_option")
    nodes = [Node(text=chip, bounds=(20, 1090, 250, 1140), clickable=True),
             Node(text="ShopeePay", bounds=(20, 200, 300, 240)), Node(text="Stok: 25", bounds=(20, 300, 300, 340))]
    cal = AndroidCalibrator(SimpleNamespace(find_all=lambda sel: []), android_selectors.defaults(), variant=variant,
                            prompt=lambda m: "", say=lambda m: None)
    assert [n.text for n in cal._listed(step, nodes)] == [chip]


# ------------------------------------------------------------------ ringkasan hasil di konsol

BRACKET_CART = {"product_name": "iPhone Uji Coba 128GB", "go_cart": True,
                "cart_other_items": [("iPhone Casing Murah", 5_000, True)]}
BRACKET_NAMES = "[iPhone Uji Coba 128GB; iPhone Casing Murah]"  # "[i..." = bentuk tag gaya Rich
CLOSING_TAG_ITEM = "[/promo] Casing Murah"  # "[/..." = tag penutup Rich -> MarkupError bila tidak di-escape


def timeline_detail(text: str, step: str) -> str:
    """Sel Detail baris `step` di tabel Timeline (3 kolom)."""
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"[│┃|]", line)]
        if len(cells) == 5 and cells[1] == step:
            return cells[3]
    raise AssertionError(f"baris timeline {step!r} tidak ada")


def test_run_android_price_guard_message_keeps_bracketed_product_names(tmp_path, device, term, alarms):
    # pesan & detail hasil di-escape (print_result) dan baris log konsol = Text tanpa markup (RunLog)
    out = run_cli(tmp_path, device, term, **BRACKET_CART)
    assert out.rc == 1, out.text
    assert out.result["status"] == "PRICE_GUARD"
    assert BRACKET_NAMES in out.result["message"]
    assert out.dev.app.kind("checkout") == [] and out.dev.app.kind("order") == []
    assert f"Status: PRICE_GUARD - {out.result['message']}" in out.text
    result_lines = [ln for ln in out.text.splitlines() if "[android] STEP result - PRICE_GUARD: keranjang" in ln]
    assert result_lines and BRACKET_NAMES in result_lines[0], result_lines
    assert alarms == ["PRICE_GUARD"]
    assert_safe_ops(out.dev)


def test_run_android_console_warn_lines_keep_platform_tag(tmp_path, device, term):
    out = run_cli(tmp_path, device, term)  # tanpa selectors.json kalibrasi -> peringatan "versi vs kalibrasi"
    assert out.rc == 0, out.text
    log = (out.run_dir / "android.log").read_text(encoding="utf-8")
    assert "[android] WARN precheck versi vs kalibrasi: PERINGATAN" in log
    warn_lines = [line for line in out.text.splitlines() if "WARN precheck versi vs kalibrasi" in line]
    assert warn_lines and all("[android] WARN" in line for line in warn_lines), warn_lines
    # semua baris RunLog di konsol (STEP/INFO/WARN) mempertahankan awalan [android]
    step_lines = [line for line in out.text.splitlines() if " STEP " in line]
    assert step_lines and all("[android] STEP" in line for line in step_lines), step_lines


@pytest.mark.parametrize("case", ["tag_lowercase", "closing_tag_live"])
def test_run_android_timeline_keeps_bracketed_screen_text(tmp_path, device, term, case):
    if case == "tag_lowercase":
        out = run_cli(tmp_path, device, term, **BRACKET_CART)
        assert out.rc == 1, out.text
        assert BRACKET_NAMES in timeline_detail(out.text, "result")
    else:
        out = run_cli(tmp_path, device, term, live=True, cfg={"expected_name": LIVE_NAME}, go_cart=True,
                      cart_other_items=[(CLOSING_TAG_ITEM, 5_000, True)], unknown_after_order=True)
        assert out.rc == 1, out.text
        assert f"Status: UNKNOWN_STATE - {MAYBE_ORDERED_MSG}" in out.text
        assert CLOSING_TAG_ITEM in timeline_detail(out.text, "cart_uncheck")


def test_run_android_live_maybe_ordered_safety_holds_even_if_console_rendering_fails(tmp_path, device, term,
                                                                                     alarms):
    """Teks layar '[/...' (nama item keranjang yang di-uncheck) tidak merusak ringkasan konsol, dan keselamatan
    tetap: satu pesanan, alarm sekali, hasil + pesan wajib tersimpan & tampil di ringkasan dan baris log,
    aplikasi dibiarkan terbuka, tanpa retry."""
    out = run_cli(tmp_path, device, term, live=True, cfg={"expected_name": LIVE_NAME}, go_cart=True,
                  cart_other_items=[(CLOSING_TAG_ITEM, 5_000, True)], unknown_after_order=True, catch=True)
    assert out.error is None and out.rc == 1, out.error or out.text
    assert f"Status: UNKNOWN_STATE - {MAYBE_ORDERED_MSG}" in out.text
    assert out.result["status"] == "UNKNOWN_STATE" and out.result["message"] == MAYBE_ORDERED_MSG
    assert [e["detail"] for e in out.dev.app.kind("cart_toggle")] == [CLOSING_TAG_ITEM], "item lain di-uncheck"
    assert len(out.buys()) == 1 and len(out.dev.app.kind("order")) == 1, "tepat satu pesanan, tanpa retry"
    assert out.dev.app.screen == "order_unknown", "aplikasi tidak ditutup / disentuh lagi"
    assert alarms == ["UNKNOWN_STATE"]
    # pesan wajib tetap sampai ke konsol lewat baris log RunLog (Text tanpa markup) & app dibiarkan terbuka
    assert any("[android] STEP result - UNKNOWN_STATE: " + MAYBE_ORDERED_MSG in ln for ln in out.text.splitlines())
    assert "Aplikasi di HP dibiarkan terbuka apa adanya" in out.text
    calls = out.dev.driver.calls
    after = calls[calls.index(("click", "Buat Pesanan")) + 1:]
    assert not {"click", "press_back", "start_url", "swipe_refresh"} & {op for op, _ in after}
    assert_safe_ops(out.dev)
