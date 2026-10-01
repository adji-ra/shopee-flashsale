"""Spesifikasi G (precheck Android): device terhubung, agent u2 < 1 s pada 3 query, layar menyala & tidak terkunci,
`svc power stayon usb` selama run (dikembalikan setelahnya), versi Shopee vs kalibrasi, alamat/saldo = alarm saja.

Dua tingkat:
- runner (tests/android_harness.run_android, waktu virtual): perilaku run lengkap + aturan keras (tanpa klik
  "Buat Pesanan" di dry-run/status stop, tanpa input PIN, aksi polling di jendela T-1 s..T+8 s berjarak >= 400 ms,
  aplikasi tidak pernah ditutup);
- CLI (`precheck/run --only android`, jam nyata, sedikit saja): tabel, exit code, alarm, pengaturan device.
"""

from __future__ import annotations

import io
import itertools
import re
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from flashbuy import android_runner, android_selectors, cli, notifier, timesync
from flashbuy.android_driver import AgentDead, FakeDriver, Node, Sel
from flashbuy.runner_base import RunStatus
from flashbuy.timesync import WIB, OffsetResult, SyncReport
from tests import android_harness
from tests.android_harness import assert_polling_rules, make_android, polling_actions, run_android
from tests.conftest import PRICE_DEFAULTS
from tests.fake_android import PRODUCT_URL, AppScenario, FakeShopeeApp

SERIAL = "FAKE123"
PING = Sel("text", "__flashbuy_ping__")  # query ping agent di precheck (_agent_healthy)
HARNESS_WM = "Physical size: 720x1612"  # wm size bawaan FakeDriver (harness)
# Operasi driver yang sah (tanpa input teks: PIN tidak mungkin diketik alat).
ALLOWED_OPS = {"exists", "info", "info_any", "find_all", "click", "current_app", "start_url", "swipe_refresh",
               "press_back", "webview", "screenshot", "dump", "shell", "agent_alive", "restart_agent",
               "last_toast", "clear_toast"}
# Perintah shell yang menutup/mematikan aplikasi atau mengetik/menekan tombol (termasuk PIN, buka kunci).
FORBIDDEN_SHELL = re.compile(r"force-stop|\bam\s+(kill|stop)|\bpm\s+clear|\binput\b|\bkill\b", re.I)
SETTINGS_WRITE = re.compile(r"^(svc power stayon|settings put)\b")


# ------------------------------------------------------------------ util tingkat runner


def _calls(out, op: str) -> list[str]:
    return [t for o, t in out.driver.calls if o == op]


def _pings(out) -> int:
    return _calls(out, "exists").count(str(PING))


def _log(out) -> str:
    return (out.log_dir / "android.log").read_text(encoding="utf-8")


def _settings(out) -> list[str]:
    """Perubahan pengaturan device yang sampai ke HP (svc power stayon / settings put)."""
    return [e["detail"] for e in out.kind("settings")]


def _safe(out, *, ordered: bool = False) -> None:
    """Aturan keras untuk setiap run: operasi sah saja, tanpa shell penutup app / input (PIN), "Buat Pesanan"
    hanya pada live sukses, aksi polling di jendela & berjarak >= 400 ms."""
    ops = {op for op, _ in out.driver.calls}
    assert ops <= ALLOWED_OPS, ops - ALLOWED_OPS
    bad = [t for t in _calls(out, "shell") if FORBIDDEN_SHELL.search(t)]
    assert bad == [], f"perintah shell terlarang: {bad}"
    assert len(out.kind("order")) == (1 if ordered else 0)
    assert out.runner.order_clicked is ordered
    assert ("click_place_order" in out.step_names()) is ordered
    if polling_actions(out):
        assert_polling_rules(out)


def _one_buy(out) -> None:
    assert len(out.kind("buy")) == 1 and out.kind("buy_disabled") == [], "tepat satu klik Beli"


def _use(monkeypatch, *, app_cls=None, driver_cls=None) -> None:
    """Subkelas aplikasi/driver palsu untuk run_android (dipasang di modul harness, bukan diedit)."""
    if app_cls is not None:
        monkeypatch.setattr(android_harness, "FakeShopeeApp", app_cls)
    if driver_cls is not None:
        monkeypatch.setattr(android_harness, "FakeDriver", driver_cls)


def _calibrated(tmp_path: Path, app_version: str, wm_size: str = HARNESS_WM):
    path = tmp_path / "selectors.json"
    android_selectors.save(path, {}, {"app_version": app_version, "wm_size": wm_size})
    return android_selectors.load(path)


# ------------------------------------------------------------------ driver/aplikasi palsu tambahan


class SlowPingDriver(FakeDriver):
    """Ping agent lambat/tidak menjawab (agent dibekukan/dibunuh HiOS); query lain normal.

    `bad` = indeks ping (0, 1, 2, ... dihitung lintas restart) yang bermasalah; `forever` = semua ping bermasalah;
    `mode` "slow" (ping `slow_s` detik) atau "timeout" (AgentDead seperti U2Driver saat HTTP timeout)."""

    bad: frozenset[int] = frozenset()
    forever = False
    mode = "slow"
    slow_s = 1.2

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ping_no = 0

    def exists(self, sel: Sel) -> bool:
        if sel != PING:
            return super().exists(sel)
        i, self.ping_no = self.ping_no, self.ping_no + 1
        if not (self.forever or i in self.bad):
            return super().exists(sel)
        if self.mode == "timeout":
            self._rpc("exists", sel, latency=self.latency_s)
            raise AgentDead("exist: HTTPTimeoutError: agent tidak menjawab")
        self._rpc("exists", sel, latency=self.slow_s)
        return False


class NoStayOnEffectApp(FakeShopeeApp):
    """`svc power stayon usb` tidak berefek (ROM menolak); nilai stay_on_while_plugged_in tetap."""

    def shell(self, cmd: list[str]) -> str:
        if " ".join(cmd).startswith("svc power stayon"):
            self._event("svc_ignored", " ".join(cmd))
            return ""
        return super().shell(cmd)


class NoVersionApp(FakeShopeeApp):
    """`dumpsys package` tanpa versionName (aplikasi terpasang, versi tidak terbaca)."""

    def shell(self, cmd: list[str]) -> str:
        return "" if cmd[:2] == ["dumpsys", "package"] else super().shell(cmd)


class KeyguardApp(FakeShopeeApp):
    """HP terkunci: keyguard (com.android.systemui) di depan; elemen Shopee tidak terlihat dan tap/gestur mengenai
    keyguard. `unlock_at` = detik relatif T saat pengguna membuka kunci (None = tetap terkunci)."""

    unlock_at: float | None = None

    def locked_now(self) -> bool:
        return self.sc.locked and (self.unlock_at is None or self.now() < self.sc.open_at + self.unlock_at)

    @property
    def package(self) -> str:
        return "com.android.systemui" if self.locked_now() else super().package

    def activity(self) -> str:
        return "NotificationShade" if self.locked_now() else super().activity()

    def nodes(self):
        return [] if self.locked_now() else super().nodes()

    def active_nodes(self):
        return [] if self.locked_now() else super().active_nodes()

    def system_nodes(self):
        if self.locked_now():
            return [Node(text="Geser ke atas untuk membuka", bounds=(160, 1400, 560, 1440))]
        return super().system_nodes()

    def webview(self) -> bool:
        return False if self.locked_now() else super().webview()

    def on_tap(self, x: int, y: int) -> None:
        if self.locked_now():
            self._event("tap", "keyguard")
            return
        super().on_tap(x, y)

    def on_refresh(self) -> None:
        if not self.locked_now():
            super().on_refresh()

    def shell(self, cmd: list[str]) -> str:
        if cmd[:2] == ["dumpsys", "window"]:
            return f"    mDreamingLockscreen={str(self.locked_now()).lower()}\n"
        return super().shell(cmd)


# ------------------------------------------------------------------ agent uiautomator2: 3 query < 1 s


@pytest.mark.parametrize("bad", [None, 0, 1, 2], ids=["sehat", "ping1_lambat", "ping2_lambat", "ping3_lambat"])
def test_agent_three_pings_each_under_1s_slow_one_restarts_once(tmp_path, monkeypatch, bad):
    """Sehat = tepat 3 ping < 1 s. Satu ping lambat (ke-1/2/3) -> restart sekali, cek ulang 3 ping, PERINGATAN,
    run lanjut. (test_android_safety menguji agent MATI via /ping; di sini agent hidup tetapi lambat.)"""
    monkeypatch.setattr(SlowPingDriver, "bad", frozenset() if bad is None else frozenset({bad}))
    _use(monkeypatch, driver_cls=SlowPingDriver)
    out = run_android(tmp_path)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    log = _log(out)
    if bad is None:
        assert _pings(out) == 3 and out.driver.restarts == 0
        assert re.search(r"precheck agent uiautomator2: OK - menjawab 3 query: \d+, \d+, \d+ ms", log), log
    else:
        assert _pings(out) == bad + 1 + 3, "berhenti di ping lambat, lalu 3 ping setelah restart"
        assert out.driver.restarts == 1
        assert re.search(r"precheck agent uiautomator2: PERINGATAN - lambat \(1200 ms >= 1000 ms\), dihidupkan "
                         r"ulang -> menjawab 3 query: [\d, ]+ ms; kemungkinan dibunuh HiOS", log), log
        assert "WARN agent uiautomator2 lambat (1200 ms >= 1000 ms); menghidupkan ulang" in log
    assert out.events() == [], "peringatan agent bukan alarm"
    _one_buy(out)
    _safe(out)


def test_agent_ping_980_ms_is_healthy_without_restart(tmp_path, monkeypatch):
    """Run penuh: ping 980 ms (< 1 s) = sehat; tanpa restart, tepat 3 ping, run lanjut normal."""
    monkeypatch.setattr(SlowPingDriver, "bad", frozenset({0}))
    monkeypatch.setattr(SlowPingDriver, "slow_s", 0.98)
    _use(monkeypatch, driver_cls=SlowPingDriver)
    out = run_android(tmp_path)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert out.driver.restarts == 0 and _pings(out) == 3
    assert re.search(r"precheck agent uiautomator2: OK - menjawab 3 query: 98\d, \d+, \d+ ms", _log(out))
    _one_buy(out)
    _safe(out)


@pytest.mark.parametrize(("dt", "alive", "healthy"), [
    (0.999, True, True), (1.0, True, False), (1.001, True, False), (0.999, None, True),
], ids=["999ms", "1000ms", "1001ms", "ping_tak_bisa_dicek"])
def test_agent_ping_limit_is_strictly_below_1_s(tmp_path, dt, alive, healthy):
    """Batas spesifikasi G: tiap query HARUS < 1 s; tepat 1000 ms = lambat. Jam monotonic runner diganti (selisih
    persis `dt` per query) agar `>=` vs `>` bisa dibedakan tanpa bergantung pada jam palsu. /ping yang tidak bisa
    dicek (None) tidak menggagalkan agent bila 3 query menjawab cepat."""
    runner, app, driver, *_ = make_android(tmp_path)
    driver.alive = alive
    runner.d = driver
    ts = itertools.chain.from_iterable((0.0, dt) for _ in range(3))
    runner._mono = lambda: next(ts)
    ok, why = runner._agent_healthy()
    assert ok is healthy, why
    pings = [t for op, t in driver.calls if op == "exists"]
    if healthy:
        assert pings == [str(PING)] * 3
        assert why == "menjawab 3 query: 999, 999, 999 ms" + ("" if alive else " (status /ping tidak bisa dicek)")
    else:
        assert pings == [str(PING)], "berhenti pada query lambat pertama"
        assert why == f"lambat ({dt * 1000:.0f} ms >= 1000 ms)"
    assert driver.restarts == 0 and app.events == [], "cek agent hanya membaca (restart diputuskan pemanggil)"
    assert {op for op, _ in driver.calls} <= {"agent_alive", "exists"}


@pytest.mark.parametrize("mode", ["slow", "timeout"])
def test_agent_still_bad_after_restart_stops_before_opening_product(tmp_path, monkeypatch, mode):
    """Agent tetap lambat / tidak menjawab setelah restart: GAGAL, status ERROR, alarm, run berhenti sebelum
    intent produk (tanpa klik, tanpa polling) dan tanpa mengubah pengaturan layar."""
    monkeypatch.setattr(SlowPingDriver, "forever", True)
    monkeypatch.setattr(SlowPingDriver, "mode", mode)
    _use(monkeypatch, driver_cls=SlowPingDriver)
    out = run_android(tmp_path, live=True, stay_on="0", runner_attrs={"hold_screen_on": True})
    assert out.result.status == RunStatus.ERROR, out.result.message
    why = r"lambat \(1200 ms >= 1000 ms\)" if mode == "slow" else r"tidak menjawab \(exist: HTTPTimeoutError: .*\)"
    assert re.fullmatch(rf"pre-check: agent uiautomator2: {why}, dihidupkan ulang -> tetap gagal \({why}\)",
                        out.result.message), out.result.message
    assert out.driver.restarts == 1, "restart sekali saja"
    assert _pings(out) == 2, "satu ping gagal sebelum dan sesudah restart"
    assert out.step_names() == ["precheck"]
    assert out.kind("intent") == [] and _calls(out, "start_url") == [], "berhenti sebelum membuka produk"
    assert out.kind("tap") == [] and polling_actions(out) == []
    assert out.events() == ["precheck"]
    assert out.notifier.events[0]["message"].startswith("pre-check gagal: agent uiautomator2: ")
    # urutan cek precheck tidak dipatok: yang wajib, nilai stay_on semula tidak tertinggal berubah
    assert _settings(out) in ([], ["svc power stayon usb", "svc power stayon false"]) and out.app.sc.stay_on == "0"
    _safe(out)


# ------------------------------------------------------------------ svc power stayon usb selama run


@pytest.mark.parametrize(("live", "scenario", "status"), [
    (False, {}, RunStatus.DRYRUN_OK),
    (True, {}, RunStatus.ORDER_PLACED_AWAIT_PIN),
    (False, {"captcha_after_buy": True}, RunStatus.CAPTCHA),
    (True, {"cfg": {"expected_name": None}}, RunStatus.ERROR),
    (False, {"shipping": 30_000}, RunStatus.PRICE_GUARD),  # total Rp129.000 > max_total Rp120.000
    (False, {"installed": False}, RunStatus.ERROR),  # precheck berhenti SETELAH svc dipasang
], ids=["dry", "live_pin", "captcha", "error_live_tanpa_nama", "price_guard", "precheck_tidak_terpasang"])
def test_stay_on_usb_during_run_restored_after_any_result(tmp_path, live, scenario, status):
    out = run_android(tmp_path, live=live, stay_on="0", runner_attrs={"hold_screen_on": True}, **scenario)
    assert out.result.status == status, out.result.message
    assert _settings(out) == ["svc power stayon usb", "svc power stayon false"]
    assert out.app.sc.stay_on == "0", "nilai semula dikembalikan"
    # dipasang saat precheck (sebelum buka produk T-60 s), dikembalikan paling akhir (setelah diagnosa akhir)
    svc_at = out.kind("settings")[0]["t_server_ms"]
    assert all(svc_at < e["t_server_ms"] for e in out.kind("buy") + out.kind("intent"))
    assert out.driver.calls[-1] == ("shell", "svc power stayon false")
    log = _log(out)
    assert "precheck layar tetap menyala: OK - svc power stayon usb selama run (semula 0)" in log
    assert "layar tetap menyala dikembalikan: svc power stayon false" in log
    if status == RunStatus.CAPTCHA:
        assert out.app.screen == "captcha" and out.events() == ["CAPTCHA"], "captcha dibiarkan, tanpa retry"
        _one_buy(out)
    _safe(out, ordered=status == RunStatus.ORDER_PLACED_AWAIT_PIN)


def test_stay_on_restored_after_abort_by_other_runner(tmp_path):
    """ABORTED (runner lain berhenti di T-2 s, setelah arm): pengaturan tetap dikembalikan, di sini semula 1 (AC
    saja) -> `settings put ... 1`; tanpa klik/polling. (test_android_runner::test_stop_event_from_other_runner_aborts
    menguji ABORTED-nya, tanpa hold_screen_on.)"""
    stop = threading.Event()

    async def during(runner, app):
        orig = app.nodes

        def nodes():
            if app.now() >= app.sc.open_at - 2.0:
                stop.set()
            return orig()

        app.nodes = nodes

    out = run_android(tmp_path, stay_on="1", during=during,
                      runner_attrs={"hold_screen_on": True, "stop_event": stop})
    assert out.result.status == RunStatus.ABORTED, out.result.message
    assert "runner lain" in out.result.message and "arm" in out.step_names()
    assert _settings(out) == ["svc power stayon usb", "settings put global stay_on_while_plugged_in 1"]
    assert out.app.sc.stay_on == "1" and out.driver.calls[-1] == ("shell", _settings(out)[-1])
    assert out.kind("tap") == [] and polling_actions(out) == [] and out.events() == []
    _safe(out)


@pytest.mark.parametrize(("stay", "settings", "row"), [
    ("1", ["svc power stayon usb", "settings put global stay_on_while_plugged_in 1"],
     "OK - svc power stayon usb selama run (semula 1)"),  # AC saja -> dikembalikan persis
    ("3", [], "OK - stay_on_while_plugged_in=3 (USB sudah termasuk)"),  # USB sudah aktif: tidak disentuh
    ("7", [], "OK - stay_on_while_plugged_in=7 (USB sudah termasuk)"),
], ids=["ac_saja", "usb_ac", "semua"])
def test_stay_on_restores_original_value_or_leaves_it(tmp_path, stay, settings, row):
    out = run_android(tmp_path, stay_on=stay, runner_attrs={"hold_screen_on": True})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert _settings(out) == settings
    assert out.app.sc.stay_on == stay
    assert f"precheck layar tetap menyala: {row}" in _log(out)
    if not settings:
        assert not [t for t in _calls(out, "shell") if SETTINGS_WRITE.match(t)], "tanpa perintah ubah pengaturan"
    _one_buy(out)
    _safe(out)


def test_stay_on_command_without_effect_is_warning_and_not_restored(tmp_path, monkeypatch):
    _use(monkeypatch, app_cls=NoStayOnEffectApp)
    out = run_android(tmp_path, stay_on="0", runner_attrs={"hold_screen_on": True})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert [e["detail"] for e in out.kind("svc_ignored")] == ["svc power stayon usb"], "tidak ada 'restore'"
    assert _settings(out) == [], "juga tidak lewat `settings put`"
    assert "precheck layar tetap menyala: PERINGATAN - `svc power stayon usb` tidak berefek (nilai 0)" in _log(out)
    assert out.events() == []
    _one_buy(out)
    _safe(out)


# ------------------------------------------------------------------ versi Shopee vs kalibrasi


@pytest.mark.parametrize(("case", "row", "alarm"), [
    ("sama", "OK - versi 3.40.21 = kalibrasi 3.40.21, Physical size: 720x1612", False),
    ("berubah", "PERINGATAN - PERINGATAN KERAS: versi Shopee 3.40.21 berbeda dari saat kalibrasi (3.39.0); "
                "selector bisa tidak cocok", True),
    ("tanpa_kalibrasi", "PERINGATAN - belum ada kalibrasi Android (memakai default teks)", False),
    ("resolusi_berubah", "PERINGATAN - versi sama (3.40.21), tetapi resolusi berubah: kalibrasi "
                         "'Physical size: 1080x2460', sekarang 'Physical size: 720x1612'", False),
    ("versi_tak_terbaca", "PERINGATAN - PERINGATAN KERAS: versi Shopee tidak terbaca, tidak bisa dibandingkan "
                          "dengan kalibrasi (3.40.21)", True),
])
def test_app_version_vs_calibration_warns_but_never_stops_run(tmp_path, monkeypatch, case, row, alarm):
    """Versi berbeda = PERINGATAN KERAS + alarm `precheck_versi`; versi tak terbaca tidak boleh OK. Tidak pernah
    menghentikan run."""
    sel = {"sama": lambda: _calibrated(tmp_path, "3.40.21"),
           "berubah": lambda: _calibrated(tmp_path, "3.39.0"),
           "tanpa_kalibrasi": lambda: None,
           "resolusi_berubah": lambda: _calibrated(tmp_path, "3.40.21", "Physical size: 1080x2460"),
           "versi_tak_terbaca": lambda: _calibrated(tmp_path, "3.40.21")}[case]()
    if case == "versi_tak_terbaca":
        _use(monkeypatch, app_cls=NoVersionApp)
    out = run_android(tmp_path, selectors=sel)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message  # tidak pernah menghentikan run
    assert f"precheck versi vs kalibrasi: {row}" in _log(out)
    assert out.events() == (["precheck_versi"] if alarm else [])
    if case == "berubah":
        assert out.notifier.events[0]["message"].startswith("PERINGATAN KERAS: versi Shopee 3.40.21 berbeda")
        assert out.runner.device_info["versionName"] == "3.40.21"
    if case == "versi_tak_terbaca":  # tidak pernah dilaporkan "sama": tidak bisa dibandingkan = PERINGATAN KERAS
        assert out.runner.device_info["versionName"] == ""
        assert out.notifier.events[0]["message"].startswith("PERINGATAN KERAS: versi Shopee tidak terbaca")
    _one_buy(out)
    _safe(out)


# ------------------------------------------------------------------ alamat / saldo: alarm saja


@pytest.mark.parametrize(("live", "scenario", "alarms", "lines"), [
    (False, {"address_utama": False}, ["precheck_alamat default"],
     ["precheck alamat default: PERINGATAN - ALARM: belum ada alamat tersimpan"]),
    (False, {"wallet_balance": None}, ["precheck_saldo ShopeePay"],
     ["precheck saldo ShopeePay: PERINGATAN - ALARM: saldo tidak terbaca"]),
    (True, {"address_utama": False, "wallet_balance": None}, ["precheck_alamat default", "precheck_saldo ShopeePay",
                                                              "ORDER_PLACED_AWAIT_PIN"],
     ["precheck alamat default: PERINGATAN - ALARM: belum ada alamat tersimpan",
      "precheck saldo ShopeePay: PERINGATAN - ALARM: saldo tidak terbaca"]),
], ids=["alamat_kosong", "saldo_tak_terbaca", "keduanya_live"])
def test_address_or_wallet_unreadable_alarms_only_run_continues(tmp_path, live, scenario, alarms, lines):
    out = run_android(tmp_path, live=live, open_in_s=20, **scenario)
    status = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == status, out.result.message
    assert out.events() == alarms, "alarm per item; bukan alarm 'precheck' gagal (run tidak dihentikan)"
    log = _log(out)
    for line in lines:
        assert line in log, line
    _one_buy(out)
    _safe(out, ordered=live)
    if live:
        assert out.app.screen == "pin" and "press_back" not in [op for op, _ in out.driver.calls]


# ------------------------------------------------------------------ layar terkunci (tingkat run)


@pytest.mark.parametrize(("unlock_at", "screen_on", "why"), [
    (None, True, "layar terkunci - buka kunci HP"),
    (-10.0, True, "layar terkunci - buka kunci HP"),
    (None, False, "layar tidak menyala (Asleep)"),  # layar mati (terkunci) sepanjang run
], ids=["tetap_terkunci", "dibuka_sebelum_arm", "layar_mati"])
def test_locked_screen_precheck_alarms_tool_never_unlocks(tmp_path, monkeypatch, unlock_at, screen_on, why):
    """Precheck: layar terkunci / mati = GAGAL -> alarm 'precheck'. Alat tidak pernah menyalakan layar, membuka
    kunci, mengetik, atau tap selama terkunci. Kasus `dibuka_sebelum_arm`: pengguna membuka kunci di T-10 s (setelah
    precheck, sebelum arm T-4 s).
    Tetap terkunci saat T: keyguard (systemui) di depan = Shopee keluar dari foreground -> UNKNOWN > 1,5 s ->
    UNKNOWN_STATE tanpa tap/reload. (test_android_cli::test_precheck_screen_locked_or_off_fails menguji baris tabel
    precheck saja.)

    DESAIN, belum ditetapkan spesifikasi G: layar terkunci saat precheck = GAGAL tanpa status, jadi run hanya
    ber-alarm lalu LANJUT (android_runner._precheck_sync, cek "layar"). Bila orkestrator memutuskan run harus
    berhenti di sini, kedua cabang status di bawah (UNKNOWN_STATE / DRYRUN_OK) perlu disesuaikan; asersi keamanan
    (tanpa tap selama terkunci, tanpa buka kunci, tanpa "Buat Pesanan") tetap berlaku."""
    monkeypatch.setattr(KeyguardApp, "unlock_at", unlock_at)
    _use(monkeypatch, app_cls=KeyguardApp)
    stop = threading.Event()
    out = run_android(tmp_path, open_in_s=40, locked=True, screen_on=screen_on,
                      runner_attrs={"open_timeout_s": 2.0, "stop_event": stop})
    log = _log(out)
    assert f"precheck layar: GAGAL - {why}" in log
    # halaman alamat/saldo tertutup keyguard -> alarm per item; lalu alarm 'precheck' (item GAGAL, tanpa status)
    pre_alarms = ["precheck_alamat default", "precheck_saldo ShopeePay", "precheck"]
    assert out.events()[:3] == pre_alarms
    assert out.notifier.events[2]["message"].startswith(f"pre-check gagal: layar: {why}")
    assert not [t for t in _calls(out, "shell") if FORBIDDEN_SHELL.search(t)], "alat tidak membuka kunci/WAKEUP"
    if unlock_at is not None:
        unlock_ms = int((out.open_at + unlock_at) * 1000)
        assert all(e["t_server_ms"] >= unlock_ms for e in out.kind("tap")), "tanpa tap selama terkunci"
    if unlock_at is None:
        assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
        assert "Shopee keluar dari foreground: com.android.systemui/NotificationShade" in out.result.message
        assert out.events() == [*pre_alarms, "UNKNOWN_STATE"] and stop.is_set()
        assert out.kind("tap") == [] and polling_actions(out) == [], "tanpa klik/reload selama terkunci"
        assert out.result.screenshots and list(out.log_dir.glob("android-*-UNKNOWN_STATE.xml"))
        _safe(out)
    else:  # DESAIN (lihat docstring): run lanjut setelah alarm precheck
        assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
        assert out.events() == pre_alarms and not stop.is_set()
        _one_buy(out)
        _safe(out)


# ================================================================== CLI (jam nyata)


class RealClock:
    """Jam nyata untuk FakeDriver/FakeShopeeApp (sama dengan jam CLI); sleep dipotong `cap` detik."""

    def __init__(self, cap: float = 0.02):
        self.cap = cap

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.perf_counter()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(min(seconds, self.cap))


@dataclass
class Device:
    app: FakeShopeeApp
    driver: FakeDriver
    configs: list = field(default_factory=list)

    def ops(self) -> list[str]:
        return [op for op, _ in self.driver.calls]

    def shell_cmds(self) -> list[str]:
        return [t for op, t in self.driver.calls if op == "shell"]


@pytest.fixture
def cwd(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)  # logs/ dan selectors.json default relatif ke cwd
    return tmp_path


@pytest.fixture
def term(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=300, color_system=None))
    return buf


@pytest.fixture
def sync_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_sync(**kw) -> SyncReport:
        calls.append(kw)
        ok = lambda src, off: OffsetResult(src, offset_ms=off, uncertainty_ms=5, rtt_ms=10,  # noqa: E731
                                           samples=6, method="m")
        return SyncReport(ntp=ok("NTP", 1.0), shopee=ok("HTTP", 0.0))

    monkeypatch.setattr(timesync, "sync", fake_sync)
    return calls


@pytest.fixture
def alarms(monkeypatch) -> list[tuple[str, str]]:
    """(event, pesan) setiap alarm Notifier CLI (tanpa beep/webhook)."""
    events: list[tuple[str, str]] = []

    class QuietNotifier(notifier.Notifier):
        def __init__(self, webhook_url: str = "", **kw):
            super().__init__(webhook_url, beep=lambda f, d: None, post=lambda u, p: None, repeat=1, **kw)

        def alarm(self, event: str, message: str, **extra) -> None:
            events.append((event, message))
            super().alarm(event, message, **extra)

    monkeypatch.setattr(notifier, "Notifier", QuietNotifier)
    return events


@pytest.fixture
def prechecks(monkeypatch) -> list:
    seen = []
    orig = cli._print_precheck

    def spy(pre, platform):
        seen.append(pre)
        orig(pre, platform)

    monkeypatch.setattr(cli, "_print_precheck", spy)
    return seen


@pytest.fixture
def device(monkeypatch) -> Callable[..., Device]:
    def install(open_at: float, *, driver_cls=FakeDriver, **scenario) -> Device:
        clock = RealClock()
        app = FakeShopeeApp(AppScenario(open_at=open_at, **scenario), clock)
        driver = driver_cls(app, clock, latency_s=0.005, serial=SERIAL, wm_size=HARNESS_WM,
                            props={"ro.product.brand": "TECNO", "ro.product.model": "TECNO BG6",
                                   "ro.build.version.release": "13", "ro.build.version.sdk": "33",
                                   "ro.tranos.version": "hios13.6.0"})
        dev = Device(app, driver)

        def factory(cfg):
            dev.configs.append(cfg)
            return driver

        monkeypatch.setattr(cli, "_android_driver", factory)
        return dev

    return install


def write_cfg(tmp_path: Path, open_at: float, **top) -> Path:
    data = {"product_url": PRODUCT_URL, "start_time": datetime.fromtimestamp(open_at, WIB).isoformat(),
            **PRICE_DEFAULTS, "web": {"enabled": False}, "android": {"serial": SERIAL}, **top}
    path = tmp_path / "target.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def table_rows(text: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"[│┃|]", line)]
        if len(cells) == 5 and cells[1] and cells[1] != "Cek":
            rows[cells[1]] = (cells[2], cells[3])
    return rows


def cli_precheck(tmp_path: Path, device, *, calibration: str | None = "3.40.21", **scenario) -> tuple[int, Device]:
    if calibration is not None:
        android_selectors.save(tmp_path / "selectors.json", {}, {"app_version": calibration, "wm_size": HARNESS_WM})
    dev = device(time.time() + 3600, **scenario)
    rc = cli.main(["precheck", "--config", str(write_cfg(tmp_path, dev.app.sc.open_at)), "--only", "android"])
    return rc, dev


def cli_safe(dev: Device, *, taps: bool = False) -> None:
    bad = [c for c in dev.shell_cmds() if FORBIDDEN_SHELL.search(c)]
    assert bad == [], f"perintah shell terlarang: {bad}"
    assert set(dev.ops()) <= ALLOWED_OPS, set(dev.ops()) - ALLOWED_OPS
    assert dev.app.kind("order") == [], "precheck/dry-run tidak pernah 'Buat Pesanan'"
    if not taps:
        assert dev.app.kind("tap") == [], "precheck hanya membaca"


# ------------------------------------------------------------------ CLI: svc power stayon usb


@pytest.mark.parametrize("stay", ["0", "1"])
def test_cli_precheck_command_never_changes_stay_on(cwd, device, term, stay):
    """Perintah `precheck` hanya membaca: tidak mengirim `svc`/`settings put` (baris tabelnya sudah diuji di
    test_android_cli::test_precheck_warnings_do_not_fail[stay_on_off])."""
    rc, dev = cli_precheck(cwd, device, stay_on=stay)
    assert rc == 0, term.getvalue()
    assert dev.app.kind("settings") == [] and dev.app.sc.stay_on == stay
    assert not [c for c in dev.shell_cmds() if SETTINGS_WRITE.match(c)]
    cli_safe(dev)


def test_cli_run_sets_stay_on_usb_and_restores_after_run(cwd, device, term, sync_calls, alarms):
    open_at = time.time() + 1.0
    dev = device(open_at, stay_on="0")
    rc = cli.main(["run", "--config", str(write_cfg(cwd, open_at)), "--only", "android", "--allow-local"])
    text = term.getvalue()
    assert rc == 0, text
    assert "Status: DRYRUN_OK - " in text
    assert [e["detail"] for e in dev.app.kind("settings")] == ["svc power stayon usb", "svc power stayon false"]
    assert dev.app.sc.stay_on == "0"
    assert dev.driver.calls[-1] == ("shell", "svc power stayon false"), "dikembalikan paling akhir"
    svc_at = dev.app.kind("settings")[0]["t_server_ms"]
    assert svc_at < dev.app.kind("buy")[0]["t_server_ms"], "dipasang saat precheck, sebelum polling"
    log = next(cwd.glob("logs/*-android/android.log")).read_text(encoding="utf-8")
    assert "precheck layar tetap menyala: OK - svc power stayon usb selama run (semula 0)" in log
    assert "layar tetap menyala dikembalikan: svc power stayon false" in log
    assert len(dev.app.kind("buy")) == 1 and alarms == []
    t_ms = int(open_at * 1000)
    assert t_ms - 1000 <= dev.app.kind("buy")[0]["t_server_ms"] <= t_ms + 8000
    cli_safe(dev, taps=True)


def test_cli_run_ctrl_c_while_waiting_for_t_restores_stay_on(cwd, device, term, sync_calls, alarms, monkeypatch):
    """Ctrl+C (SIGINT sungguhan ke proses; asyncio membatalkan task utama) setelah arm, saat menunggu T: finally di
    cli.run_android tetap memanggil runner.close() -> `svc power stayon false` (nilai semula 0); exit 130; tanpa
    klik apa pun. (test_cli_run_sets_stay_on_usb_and_restores_after_run = jalur selesai normal.)"""
    orig_arm = android_runner.AndroidRunner.arm

    async def arm(self, open_at):
        await orig_arm(self, open_at)
        signal.raise_signal(signal.SIGINT)  # pengguna menekan Ctrl+C di terminal

    monkeypatch.setattr(android_runner.AndroidRunner, "arm", arm)
    open_at = time.time() + 5.0  # arm (T-60 s) segera setelah precheck; Ctrl+C jauh sebelum T
    dev = device(open_at, stay_on="0")
    rc = cli.main(["run", "--config", str(write_cfg(cwd, open_at)), "--only", "android", "--allow-local"])
    text = term.getvalue()
    assert rc == 130 and "Dibatalkan." in text, text
    assert time.time() < open_at, "dibatalkan sebelum T"
    assert [e["detail"] for e in dev.app.kind("settings")] == ["svc power stayon usb", "svc power stayon false"]
    assert dev.app.sc.stay_on == "0" and dev.driver.calls[-1] == ("shell", "svc power stayon false")
    log = next(cwd.glob("logs/*-android/android.log")).read_text(encoding="utf-8")
    assert "layar tetap menyala dikembalikan: svc power stayon false" in log
    assert dev.app.kind("tap") == [] and dev.app.kind("buy") == [] and alarms == []
    cli_safe(dev)


# ------------------------------------------------------------------ CLI: versi Shopee vs kalibrasi


def test_cli_precheck_app_version_changed_strong_warning_and_alarm(cwd, device, term, prechecks, alarms):
    """CLI: versi Shopee != versi kalibrasi -> PERINGATAN KERAS di tabel + alarm `precheck_versi`, exit 0 (bukan
    kegagalan). Tingkat runner: test_app_version_vs_calibration_warns_but_never_stops_run."""
    rc, dev = cli_precheck(cwd, device, calibration="3.39.0")
    text = term.getvalue()
    assert rc == 0, text
    detail = ("PERINGATAN KERAS: versi Shopee 3.40.21 berbeda dari saat kalibrasi (3.39.0); selector bisa tidak "
              "cocok - kalibrasi ulang lalu dry-run, atau matikan auto-update Shopee")
    assert table_rows(text)["versi vs kalibrasi"] == ("PERINGATAN", detail)
    assert prechecks[0].status is None and [i.name for i in prechecks[0].warnings] == ["versi vs kalibrasi"]
    assert alarms == [("precheck_versi", detail)]
    cli_safe(dev)
