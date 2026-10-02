"""`flashbuy doctor --config target.yaml`: cek akhir H-1. Setiap baris PASS / WARN / FAIL.

Tidak ada baris yang menambah barang ke keranjang atau checkout: precheck hanya membuka halaman alamat, saldo,
dan produk (web) atau intent ke halaman yang sama (Android), lalu membaca layar.
"""

from __future__ import annotations

import math
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from flashbuy import pricing
from flashbuy.config import POLL_WINDOW_AFTER_S, TargetConfig
from flashbuy.runner_base import PrecheckResult

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
TIMESYNC_FAIL_MS = 100.0  # ketidakpastian offset > 100 ms -> FAIL
TIMESYNC_DISAGREE_WARN_MS = 100.0  # Shopee vs NTP beda > 100 ms -> WARN
SELECTOR_MAX_AGE_DAYS = 3  # kalibrasi lebih dari 3 hari lalu -> WARN
LATENCY_WARN_MS = 100.0  # p95 query Android > 100 ms -> WARN
LATENCY_FAIL_MS = 500.0  # p95 > 500 ms: agent hampir tidak bisa dipakai -> FAIL
DISK_FAIL_MB = 200
DISK_WARN_MB = 1024
LATE_START_S = 70  # sama dengan cli.LATE_START_S


@dataclass
class Check:
    name: str
    level: str  # PASS / WARN / FAIL
    detail: str


def worst(checks: list[Check]) -> str:
    levels = {c.level for c in checks}
    return FAIL if FAIL in levels else WARN if WARN in levels else PASS


# --------------------------------------------------------------------------- config


def check_config(path: str | Path, *, allow_local: bool = False, now: float | None = None
                 ) -> tuple[TargetConfig | None, list[Check]]:
    """Validasi target.yaml per kunci penting. Config tidak valid -> (None, baris FAIL)."""
    now = time.time() if now is None else now
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [Check("config", FAIL, f"file tidak ditemukan: {path}")]
    except yaml.YAMLError as e:
        return None, [Check("config", FAIL, f"YAML tidak valid: {e}")]
    if not isinstance(raw, dict):
        return None, [Check("config", FAIL, "isi harus mapping YAML")]
    errors: dict[str, list[str]] = {}
    cfg = None
    try:
        cfg = TargetConfig.model_validate(raw, context={"allow_local": allow_local})
    except ValidationError as e:
        for err in e.errors():
            key = str(err["loc"][0]) if err["loc"] else "(root)"
            if key == "(root)" and "max_total" in err["msg"]:
                key = "max_total"
            errors.setdefault(key, []).append(err["msg"])
    checks: list[Check] = []

    name = raw.get("expected_name")
    if "expected_name" in errors:
        checks.append(Check("config: expected_name", FAIL, "; ".join(errors.pop("expected_name"))))
    elif not (isinstance(name, str) and name.strip()):
        checks.append(Check("config: expected_name", FAIL, "kosong - WAJIB untuk --live (potongan nama produk)"))
    else:
        checks.append(Check("config: expected_name", PASS, repr(name.strip())))

    for key in ("max_item_price", "max_total"):
        if key in errors:
            checks.append(Check(f"config: {key}", FAIL, "; ".join(errors.pop(key))))
        else:
            checks.append(Check(f"config: {key}", PASS, pricing.rupiah(int(raw[key]))))

    if "start_time" in errors:
        checks.append(Check("config: start_time", FAIL, "; ".join(errors.pop("start_time"))))
    elif cfg is not None or "start_time" in raw:
        start = cfg.start_epoch if cfg is not None else datetime.fromisoformat(str(raw["start_time"])).timestamp()
        left = start - now
        when = f"{datetime.fromtimestamp(start).astimezone().isoformat()} (T{-left:+.0f} s)"
        if left < -POLL_WINDOW_AFTER_S:
            checks.append(Check("config: start_time", FAIL, f"sudah lewat: {when}"))
        elif left < LATE_START_S:
            checks.append(Check("config: start_time", FAIL, f"terlalu dekat: run harus mulai <= T-{LATE_START_S} s; "
                                                            f"{when}"))
        elif left < 600:
            checks.append(Check("config: start_time", WARN, f"< 10 menit lagi (precheck T-10 mnt terlewat): {when}"))
        else:
            hours = left / 3600
            checks.append(Check("config: start_time", PASS, f"{when}, {hours:.1f} jam lagi, ber-zona waktu"))
    for key, msgs in errors.items():
        checks.append(Check(f"config: {key}", FAIL, "; ".join(msgs)))
    return cfg, checks


# --------------------------------------------------------------------------- timesync


def check_timesync(report) -> Check:
    p = report.primary
    if p is None:
        return Check("timesync", FAIL, "semua sumber waktu gagal (NTP & header Date Shopee)")
    detail = f"offset {p.offset_ms:+.1f} ms ±{p.uncertainty_ms:.1f} ms ({p.source}, RTT {p.rtt_ms:.0f} ms)"
    if p.uncertainty_ms > TIMESYNC_FAIL_MS:
        return Check("timesync", FAIL, f"{detail} > batas ±{TIMESYNC_FAIL_MS:.0f} ms")
    d = report.disagreement_ms
    if p is not report.shopee:
        return Check("timesync", WARN, f"{detail}; header Date Shopee gagal, memakai NTP")
    if d is not None and abs(d) > TIMESYNC_DISAGREE_WARN_MS:
        return Check("timesync", WARN, f"{detail}; Shopee vs NTP beda {d:+.0f} ms")
    return Check("timesync", PASS, detail)


# --------------------------------------------------------------------------- precheck & selector


def check_precheck(platform: str, pre: PrecheckResult | BaseException) -> Check:
    name = f"precheck {platform}"
    if isinstance(pre, BaseException):
        return Check(name, FAIL, f"{type(pre).__name__}: {pre}")
    failed = [f"{i.name}: {i.detail}" for i in pre.items if i.ok is False]
    warned = [f"{i.name}: {i.detail}" for i in pre.items if i.ok is None]
    if not pre.ok:
        status = f"{pre.status}: " if pre.status is not None else ""
        return Check(name, FAIL, status + "; ".join(failed or warned))
    if warned:
        return Check(name, WARN, "; ".join(warned))
    return Check(name, PASS, f"{len(pre.items)} cek OK")


def check_app_version(current: str, calibrated: dict) -> Check:
    name = "versi Shopee vs kalibrasi"
    cal = calibrated.get("app_version", "")
    if not calibrated:
        return Check(name, WARN, f"belum ada kalibrasi Android (sekarang {current or '?'}); jalankan "
                                 "calibrate --platform android")
    if not cal:
        return Check(name, WARN, f"kalibrasi tanpa versi tercatat (sekarang {current or '?'}); kalibrasi ulang")
    if not current:
        return Check(name, WARN, f"versi Shopee sekarang tidak terbaca (kalibrasi {cal})")
    if current != cal:
        return Check(name, FAIL, f"versi berubah: kalibrasi {cal}, sekarang {current} - kalibrasi ulang lalu "
                                 "dry-run, dan matikan auto-update Shopee sampai hari H")
    return Check(name, PASS, f"{current} = kalibrasi")


def check_selector_age(platform: str, calibrated: dict, *, now: float | None = None) -> Check:
    name = f"umur selector {platform}"
    now = time.time() if now is None else now
    stamp = calibrated.get("at") or calibrated.get("time")
    if not stamp:
        return Check(name, WARN, "belum dikalibrasi (memakai default teks)")
    try:
        at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return Check(name, WARN, f"waktu kalibrasi tidak terbaca: {stamp!r}")
    age = timedelta(seconds=now - at.timestamp())
    days = age.total_seconds() / 86400
    text = f"dikalibrasi {stamp} ({days:.1f} hari lalu)"
    if age > timedelta(days=SELECTOR_MAX_AGE_DAYS):
        return Check(name, WARN, f"{text} > {SELECTOR_MAX_AGE_DAYS} hari - kalibrasi ulang & dry-run")
    return Check(name, PASS, text)


def suggest_android_lead(p95_ms: float) -> int:
    """Satu iterasi polling Android = ~3 query (tombol Beli, penanda bahaya, harga): lead >= 3 x p95 + 50 ms,
    dibulatkan ke atas kelipatan 50, batas 150..1000 ms."""
    raw = 3 * p95_ms + 50
    return int(min(1000, max(150, math.ceil(raw / 50) * 50)))


def check_latency(latency: dict, configured_lead: int) -> Check:
    name = "latensi query Android"
    if not latency:
        return Check(name, WARN, "tidak terukur (precheck Android tidak sampai uji latensi)")
    p95 = latency["p95"]
    lead = suggest_android_lead(p95)
    detail = (f"median {latency['median']:.0f} ms, p95 {p95:.0f} ms, maks {latency['max']:.0f} ms "
              f"({latency['n']} query); saran android.lead_ms = {lead} (config {configured_lead})")
    if p95 > LATENCY_FAIL_MS:
        return Check(name, FAIL, f"{detail}; p95 > {LATENCY_FAIL_MS:.0f} ms - cek kabel/port USB, agent u2")
    if p95 > LATENCY_WARN_MS:
        return Check(name, WARN, f"{detail}; p95 > {LATENCY_WARN_MS:.0f} ms - kabel/port USB lain, tutup aplikasi")
    if configured_lead < lead:
        return Check(name, WARN, f"{detail}; android.lead_ms lebih kecil dari saran")
    return Check(name, PASS, detail)


def check_tap_mode(tap_mode: str, latency: dict) -> Check:
    """Mode ketuk Android + estimasi latensi satu ketukan kedua mode, dari query exists/info dengan selector Beli
    yang sama saat precheck (doctor tidak pernah mengetuk)."""
    name = "mode ketuk Android"
    if "tap_selector" in latency:
        est = (f"estimasi ketuk: selector ~{latency['tap_selector']:.0f} ms (1 RPC: cari+ketuk di HP), coord "
               f"~{latency['tap_coord']:.0f} ms (baca + tap koordinat)")
    else:
        est = "estimasi ketuk tidak terukur (precheck Android tidak sampai uji latensi)"
    if tap_mode == "coord":
        return Check(name, WARN, f"tap_mode=coord; {est}; celah: bila layar berganti di antara baca & ketuk, tap "
                                 "mendarat di elemen baru (mis. 'Buat Pesanan') - disarankan selector")
    return Check(name, PASS if "tap_selector" in latency else WARN, f"tap_mode=selector; {est}")


def check_disk(path: Path) -> Check:
    probe = path if path.exists() else Path.cwd()
    free_mb = shutil.disk_usage(probe).free / 2**20
    detail = f"{free_mb:,.0f} MB kosong di {probe.resolve()}"
    if free_mb < DISK_FAIL_MB:
        return Check("ruang disk log", FAIL, f"{detail} < {DISK_FAIL_MB} MB")
    if free_mb < DISK_WARN_MB:
        return Check("ruang disk log", WARN, f"{detail} < {DISK_WARN_MB} MB")
    return Check("ruang disk log", PASS, detail)


def check_beep(notifier, sleep: Callable[[float], None] = time.sleep) -> Check:
    """Bunyikan pola pendek lalu pola mendesak (yang dipakai saat pesanan terbuat); webhook ikut diuji."""
    notifier.alarm("doctor_beep", "uji alarm flashbuy: pola pendek (hasil lain)", pattern="short")
    notifier.join(10)
    sleep(1.0)
    notifier.alarm("doctor_beep", "uji alarm flashbuy: pola mendesak (pesanan terbuat)", pattern="urgent")
    notifier.join(20)
    errors = [e["webhook_error"] for e in notifier.events if "webhook_error" in e]
    hook = ("tidak diset" if not notifier.webhook_url else
            f"gagal: {errors[0]}" if errors else "terkirim 2x")
    level = WARN if errors else PASS
    return Check("alarm (--beep)", level, f"pola pendek & mendesak dibunyikan; webhook {hook}")
