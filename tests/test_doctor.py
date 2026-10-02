"""Unit `flashbuy doctor`: setiap baris PASS / WARN / FAIL (config, timesync, precheck, versi Shopee vs kalibrasi,
umur selector, latensi Android + saran android.lead_ms, ruang disk, --beep). E2E CLI ada di test_android_cli
(jalur Android, FakeDriver) dan test_cli (jalur web, mock)."""

from __future__ import annotations

import time
from collections import namedtuple
from datetime import datetime, timedelta

import pytest
import yaml

from flashbuy import doctor
from flashbuy.doctor import FAIL, PASS, WARN, Check
from flashbuy.notifier import Notifier
from flashbuy.runner_base import PrecheckItem, PrecheckResult, RunStatus
from flashbuy.timesync import WIB, OffsetResult, SyncReport

NOW = time.time()
PRODUCT_URL = "https://shopee.co.id/Ponsel-Uji-i.1.2"


def _cfg(tmp_path, **over):
    data = {"product_url": PRODUCT_URL, "start_time": datetime.fromtimestamp(NOW + 86400, WIB).isoformat(),
            "max_item_price": 100_000, "max_total": 120_000, "expected_name": "Uji Coba", **over}
    data = {k: v for k, v in data.items() if v is not None}
    p = tmp_path / "target.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _rows(checks: list[Check]) -> dict[str, tuple[str, str]]:
    return {c.name: (c.level, c.detail) for c in checks}


# ------------------------------------------------------------------ config


def test_config_all_pass(tmp_path):
    cfg, checks = doctor.check_config(_cfg(tmp_path), now=NOW)
    rows = _rows(checks)
    assert cfg is not None
    assert list(rows) == ["config: expected_name", "config: max_item_price", "config: max_total",
                          "config: start_time"]
    assert {lvl for lvl, _ in rows.values()} == {PASS}, rows
    assert rows["config: max_item_price"][1] == "Rp100.000" and "24.0 jam lagi" in rows["config: start_time"][1]


@pytest.mark.parametrize(("over", "row", "level", "detail"), [
    ({"expected_name": None}, "config: expected_name", FAIL, "WAJIB untuk --live"),
    ({"expected_name": "  "}, "config: expected_name", FAIL, "WAJIB untuk --live"),
    ({"max_item_price": 0}, "config: max_item_price", FAIL, "greater than 0"),
    ({"max_total": 50_000}, "config: max_total", FAIL, "max_total harus >= max_item_price"),
    ({"start_time": "2099-01-01T00:00:00"}, "config: start_time", FAIL, "wajib pakai zona waktu"),
    ({"start_time": datetime.fromtimestamp(NOW - 60, WIB).isoformat()}, "config: start_time", FAIL, "sudah lewat"),
    ({"start_time": datetime.fromtimestamp(NOW + 30, WIB).isoformat()}, "config: start_time", FAIL,
     "terlalu dekat"),
    ({"start_time": datetime.fromtimestamp(NOW + 300, WIB).isoformat()}, "config: start_time", WARN,
     "< 10 menit"),
    ({"product_url": "https://example.com/x"}, "config: product_url", FAIL, "shopee.co.id"),
])
def test_config_rows(tmp_path, over, row, level, detail):
    _, checks = doctor.check_config(_cfg(tmp_path, **over), now=NOW)
    rows = _rows(checks)
    assert rows[row][0] == level and detail in rows[row][1], rows


@pytest.mark.parametrize(("content", "detail"), [(None, "file tidak ditemukan"), ("a: [", "YAML tidak valid"),
                                                 ("- 1\n", "mapping")])
def test_config_file_problems(tmp_path, content, detail):
    p = tmp_path / "target.yaml"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    cfg, checks = doctor.check_config(p, now=NOW)
    assert cfg is None and checks[0].level == FAIL and detail in checks[0].detail


# ------------------------------------------------------------------ timesync


def _off(src, off=0.0, unc=10.0, ok=True):
    if not ok:
        return OffsetResult(src, error="gagal")
    return OffsetResult(src, offset_ms=off, uncertainty_ms=unc, rtt_ms=20, samples=6, method="m")


@pytest.mark.parametrize(("report", "level", "detail"), [
    (SyncReport(ntp=_off("NTP", 5), shopee=_off("HTTP", 0)), PASS, "offset +0.0 ms ±10.0 ms"),
    (SyncReport(ntp=_off("NTP"), shopee=_off("HTTP", unc=150)), FAIL, "> batas ±100 ms"),
    (SyncReport(ntp=_off("NTP", unc=101), shopee=_off("HTTP", ok=False)), FAIL, "> batas ±100 ms"),
    (SyncReport(ntp=_off("NTP"), shopee=_off("HTTP", ok=False)), WARN, "header Date Shopee gagal"),
    (SyncReport(ntp=_off("NTP", 0), shopee=_off("HTTP", 250)), WARN, "Shopee vs NTP beda +250 ms"),
    (SyncReport(ntp=_off("NTP", ok=False), shopee=_off("HTTP", ok=False)), FAIL, "semua sumber waktu gagal"),
])
def test_timesync_rows(report, level, detail):
    c = doctor.check_timesync(report)
    assert c.level == level and detail in c.detail, c


# ------------------------------------------------------------------ precheck


@pytest.mark.parametrize(("pre", "level", "detail"), [
    (PrecheckResult("web", [PrecheckItem("login", True, "ok"), PrecheckItem("alamat", True)]), PASS, "2 cek OK"),
    (PrecheckResult("web", [PrecheckItem("saldo", None, "tidak terbaca")]), WARN, "saldo: tidak terbaca"),
    (PrecheckResult("web", [PrecheckItem("alamat", False, "tidak ada alamat utama")]), FAIL,
     "alamat: tidak ada alamat utama"),
    (PrecheckResult("android", [PrecheckItem("login", False, "belum login")], RunStatus.LOGIN_REQUIRED), FAIL,
     "LOGIN_REQUIRED: login: belum login"),
    (RuntimeError("agent mati"), FAIL, "RuntimeError: agent mati"),
])
def test_precheck_rows(pre, level, detail):
    c = doctor.check_precheck("web", pre)
    assert c.level == level and detail in c.detail, c


# ------------------------------------------------------------------ versi Shopee & umur selector


@pytest.mark.parametrize(("current", "calibrated", "level", "detail"), [
    ("3.40.21", {"app_version": "3.40.21"}, PASS, "3.40.21 = kalibrasi"),
    ("3.41.0", {"app_version": "3.40.21"}, FAIL, "versi berubah: kalibrasi 3.40.21, sekarang 3.41.0"),
    ("3.40.21", {}, WARN, "belum ada kalibrasi Android"),
    ("3.40.21", {"app_version": "", "at": "x"}, WARN, "kalibrasi tanpa versi tercatat"),
    ("", {"app_version": "3.40.21"}, WARN, "tidak terbaca"),
])
def test_app_version_rows(current, calibrated, level, detail):
    c = doctor.check_app_version(current, calibrated)
    assert c.level == level and detail in c.detail, c


def _stamp(days_ago: float) -> str:
    return datetime.fromtimestamp(NOW - days_ago * 86400).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


@pytest.mark.parametrize(("calibrated", "level", "detail"), [
    ({"at": _stamp(1)}, PASS, "1.0 hari lalu"),  # android memakai "at"
    ({"time": _stamp(2.9)}, PASS, "2.9 hari lalu"),  # web memakai "time"
    ({"at": _stamp(3.2)}, WARN, "> 3 hari"),
    ({"time": _stamp(10)}, WARN, "10.0 hari lalu"),
    ({}, WARN, "belum dikalibrasi"),
    ({"at": "kemarin"}, WARN, "tidak terbaca"),
])
def test_selector_age_rows(calibrated, level, detail):
    c = doctor.check_selector_age("android", calibrated, now=NOW)
    assert c.level == level and detail in c.detail, c


# ------------------------------------------------------------------ latensi Android


@pytest.mark.parametrize(("p95", "lead"), [(5, 150), (40, 200), (60, 250), (100, 350), (400, 1000)])
def test_suggest_android_lead(p95, lead):
    assert doctor.suggest_android_lead(p95) == lead


@pytest.mark.parametrize(("p95", "configured", "level", "detail"), [
    (20, 300, PASS, "saran android.lead_ms = 150 (config 300)"),
    (60, 200, WARN, "lebih kecil dari saran"),
    (150, 1000, WARN, "p95 > 100 ms"),
    (600, 1000, FAIL, "p95 > 500 ms"),
])
def test_latency_rows(p95, configured, level, detail):
    lat = {"median": p95 / 2, "p95": p95, "max": p95 * 1.2, "n": 30}
    c = doctor.check_latency(lat, configured)
    assert c.level == level and detail in c.detail, c


def test_latency_not_measured_is_warning():
    assert doctor.check_latency({}, 300).level == WARN


# ------------------------------------------------------------------ mode ketuk


@pytest.mark.parametrize(("mode", "latency", "level", "detail"), [
    ("selector", {"tap_selector": 12, "tap_coord": 25}, PASS, "selector ~12 ms (1 RPC: cari+ketuk di HP), coord ~25"),
    ("coord", {"tap_selector": 12, "tap_coord": 25}, WARN, "celah: bila layar berganti"),
    ("selector", {}, WARN, "tidak terukur"),
])
def test_tap_mode_rows(mode, latency, level, detail):
    c = doctor.check_tap_mode(mode, latency)
    assert c.level == level and detail in c.detail and f"tap_mode={mode}" in c.detail, c


# ------------------------------------------------------------------ disk


@pytest.mark.parametrize(("free_mb", "level"), [(5000, PASS), (500, WARN), (100, FAIL)])
def test_disk_rows(tmp_path, monkeypatch, free_mb, level):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: usage(0, 0, free_mb * 2**20))
    c = doctor.check_disk(tmp_path / "logs")  # belum ada: diukur di cwd
    assert c.level == level and f"{free_mb:,} MB kosong" in c.detail, c


# ------------------------------------------------------------------ --beep


@pytest.mark.parametrize(("webhook", "fail", "level", "detail"), [
    ("", False, PASS, "webhook tidak diset"),
    ("http://hook", False, PASS, "webhook terkirim 2x"),
    ("http://hook", True, WARN, "webhook gagal"),
])
def test_beep_row_plays_short_then_urgent(webhook, fail, level, detail):
    beeps, posts = [], []

    def post(url, payload):
        if fail:
            raise OSError("koneksi ditolak")
        posts.append(payload)

    n = Notifier(webhook, beep=lambda f, d: beeps.append((f, d)), post=post, repeat=2)
    c = doctor.check_beep(n, sleep=lambda s: None)
    assert c.level == level and detail in c.detail, c
    assert [e["pattern"] for e in n.events if e["level"] == "alarm"] == ["short", "urgent"]
    assert len(beeps) == len(n.beeps("short")) + len(n.beeps("urgent"))
    if webhook and not fail:
        assert [p["pattern"] for p in posts] == ["short", "urgent"]


def test_worst():
    mk = lambda lvl: Check("x", lvl, "")  # noqa: E731
    assert doctor.worst([mk(PASS), mk(WARN)]) == WARN
    assert doctor.worst([mk(PASS), mk(FAIL), mk(WARN)]) == FAIL
    assert doctor.worst([mk(PASS)]) == PASS


def test_stamp_helper_matches_calibration_format():
    # format yang ditulis kalibrasi (android "at", web "time") harus bisa dibaca check_selector_age
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    assert doctor.check_selector_age("web", {"time": stamp}).level == PASS
    assert timedelta(days=doctor.SELECTOR_MAX_AGE_DAYS) == timedelta(days=3)
