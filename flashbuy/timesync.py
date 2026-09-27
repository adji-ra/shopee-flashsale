"""Sinkronisasi waktu: offset jam lokal terhadap NTP dan terhadap header HTTP `Date` Shopee.

Konvensi: offset = waktu_server - waktu_lokal (detik). Positif = jam lokal ketinggalan.

Header `Date` hanya beresolusi 1 detik, jadi "median + koreksi RTT/2" biasa masih bergalat
hingga ±500 ms. Untuk itu setiap sampel dipakai sebagai batasan interval:

    server mencap Date di suatu saat lokal tau dalam [t0, t3], dan waktu server saat itu
    ada di [D, D+1)  =>  offset berada di [D - t3, D + 1 - t0]

Sampel berikutnya dijadwalkan supaya pergantian detik server (menurut estimasi saat ini)
jatuh tepat di tengah request, sehingga tiap sampel membelah dua interval (bisection).
Presisi akhir kira-kira sebesar RTT. Estimasi = titik tengah interval (setara koreksi RTT/2).
Dua sampel verifikasi tepat sebelum/sesudah pergantian detik yang diprediksi memastikan
estimasinya konsisten; jika bertentangan (jitter/CDN beda jam), dipakai median estimasi
per sampel dengan ketidakpastian ±500 ms.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import math
import os
import socket
import ssl
import statistics
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import unquote, urlsplit

DEFAULT_NTP_HOST = "id.pool.ntp.org"
DEFAULT_HTTP_URL = "https://shopee.co.id/"
DEFAULT_SAMPLES = 5
USER_AGENT = "flashbuy-timesync/0.1"

WIB = timezone(timedelta(hours=7))
_NTP_EPOCH_DELTA = 2_208_988_800  # detik antara 1900-01-01 dan 1970-01-01


class TimeSyncError(RuntimeError):
    pass


# --------------------------------------------------------------------------- jam


class Clock(Protocol):
    def time(self) -> float:
        """Jam dinding lokal, detik epoch."""

    def monotonic(self) -> float:
        """Jam monotonik beresolusi tinggi, detik."""

    def sleep(self, seconds: float) -> None: ...

    async def asleep(self, seconds: float) -> None: ...


class SystemClock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.perf_counter()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    async def asleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def to_epoch(t: datetime | float) -> float:
    if isinstance(t, datetime):
        if t.tzinfo is None:
            raise ValueError("datetime harus punya zona waktu (mis. +07:00)")
        return t.timestamp()
    return float(t)


class ServerClock:
    """Waktu server terkoreksi.

    Dijangkar ke jam monotonik saat dibuat, sehingga tidak ikut melompat jika Windows
    menyetel ulang jam dinding di tengah run.
    """

    def __init__(self, offset_s: float = 0.0, clock: Clock | None = None,
                 spin_threshold_s: float = 0.015):
        self.clock = clock or SystemClock()
        self.offset_s = offset_s
        # Sleep OS tidak presisi (Windows lama ~15.6 ms); sisa waktu di bawah ambang ini di-busy-wait.
        self.spin_threshold_s = spin_threshold_s
        self._anchor_wall = self.clock.time()
        self._anchor_mono = self.clock.monotonic()

    def now(self) -> float:
        return self._anchor_wall + (self.clock.monotonic() - self._anchor_mono) + self.offset_s

    def now_ms(self) -> int:
        return int(self.now() * 1000)

    def now_iso(self) -> str:
        return format_ts(self.now())

    def wait_until(self, t: datetime | float, lead_ms: float = 0.0, *,
                   max_chunk_s: float = 1.0) -> float:
        """Blok sampai waktu server >= t - lead_ms.

        Sleep kasar sampai tersisa `spin_threshold_s`, lalu busy-wait (presisi ~1 ms).
        Mengembalikan keterlambatan terhadap tenggat dalam ms (0 jika tenggat sudah lewat
        sebelum dipanggil tetap dilaporkan apa adanya, bisa besar).
        """
        deadline = to_epoch(t) - lead_ms / 1000.0
        while (remaining := deadline - self.now()) > self.spin_threshold_s:
            self.clock.sleep(min(remaining - self.spin_threshold_s, max_chunk_s))
        while (now := self.now()) < deadline:
            self.clock.sleep(0)  # lepas GIL sebentar agar thread lain tetap jalan
        return (now - deadline) * 1000.0

    async def wait_until_async(self, t: datetime | float, lead_ms: float = 0.0, *,
                               max_chunk_s: float = 1.0) -> float:
        """Versi asyncio. Busy-wait terakhir (<= spin_threshold_s) memblok event loop sesaat."""
        deadline = to_epoch(t) - lead_ms / 1000.0
        while (remaining := deadline - self.now()) > self.spin_threshold_s:
            await self.clock.asleep(min(remaining - self.spin_threshold_s, max_chunk_s))
        while (now := self.now()) < deadline:
            self.clock.sleep(0)
        return (now - deadline) * 1000.0


def sleep_until(clock: Clock, wall_target: float, spin_threshold_s: float = 0.015) -> None:
    """Tidur sampai jam dinding lokal = target; ~15 ms terakhir busy-wait."""
    while (remaining := wall_target - clock.time()) > spin_threshold_s:
        clock.sleep(remaining - spin_threshold_s)
    while clock.time() < wall_target:
        clock.sleep(0)


def format_ts(epoch_s: float, tz: timezone | None = None) -> str:
    """ISO 8601 dengan milidetik, default zona WIB (+07:00)."""
    tz = tz or WIB
    return datetime.fromtimestamp(epoch_s, tz).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- hasil


@dataclass
class OffsetResult:
    source: str
    offset_ms: float | None = None
    uncertainty_ms: float | None = None  # setengah lebar interval / sebaran
    rtt_ms: float | None = None  # median RTT sampel
    samples: int = 0
    method: str = ""
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.offset_ms is not None


@dataclass
class SyncReport:
    ntp: OffsetResult
    shopee: OffsetResult

    @property
    def primary(self) -> OffsetResult | None:
        """Header Shopee jadi acuan utama; NTP hanya cadangan."""
        if self.shopee.ok:
            return self.shopee
        if self.ntp.ok:
            return self.ntp
        return None

    @property
    def offset_s(self) -> float:
        p = self.primary
        if p is None:
            raise TimeSyncError("tidak ada sumber waktu yang berhasil (NTP & Shopee gagal)")
        return p.offset_ms / 1000.0

    @property
    def disagreement_ms(self) -> float | None:
        if self.ntp.ok and self.shopee.ok:
            return self.shopee.offset_ms - self.ntp.offset_ms
        return None


# --------------------------------------------------------------------------- NTP


def _to_ntp(epoch_s: float) -> bytes:
    ntp = epoch_s + _NTP_EPOCH_DELTA
    sec = int(ntp)
    frac = int((ntp - sec) * 2**32) & 0xFFFFFFFF
    return struct.pack("!II", sec, frac)


def _from_ntp(data: bytes, idx: int) -> float:
    sec, frac = struct.unpack("!II", data[idx:idx + 8])
    return sec - _NTP_EPOCH_DELTA + frac / 2**32


def build_ntp_request(t0: float) -> bytes:
    # LI=0, VN=4, Mode=3 (client); transmit timestamp = t0 agar bisa dicocokkan di respons.
    return b"\x23" + b"\x00" * 39 + _to_ntp(t0)


def parse_ntp_response(data: bytes, request: bytes, t0: float, t3: float) -> tuple[float, float]:
    """Kembalikan (offset_s, delay_s) sesuai RFC 5905."""
    if len(data) < 48:
        raise TimeSyncError(f"paket NTP terlalu pendek ({len(data)} byte)")
    li, mode, stratum = data[0] >> 6, data[0] & 0x07, data[1]
    if mode != 4:
        raise TimeSyncError(f"mode NTP tak terduga: {mode}")
    if stratum == 0:
        raise TimeSyncError("server NTP mengirim kiss-o'-death")
    if li == 3:
        raise TimeSyncError("server NTP belum tersinkron (LI=3)")
    if data[24:32] != request[40:48]:
        raise TimeSyncError("originate timestamp tidak cocok (respons basi/palsu)")
    t1 = _from_ntp(data, 32)  # server terima
    t2 = _from_ntp(data, 40)  # server kirim
    offset = ((t1 - t0) + (t2 - t3)) / 2
    delay = (t3 - t0) - (t2 - t1)
    return offset, delay


class UdpNtpTransport:
    """DNS di-resolve sekali di awal agar tidak masuk ke jendela pengukuran."""

    def __init__(self, host: str, timeout: float = 2.0):
        infos = socket.getaddrinfo(host, 123, type=socket.SOCK_DGRAM)
        if not infos:
            raise TimeSyncError(f"gagal resolve {host}")
        self._family, _, _, _, self._addr = infos[0]
        self._timeout = timeout

    def __call__(self, packet: bytes) -> bytes:
        with socket.socket(self._family, socket.SOCK_DGRAM) as s:
            s.settimeout(self._timeout)
            s.sendto(packet, self._addr)
            data, _ = s.recvfrom(512)
            return data


def measure_ntp(host: str = DEFAULT_NTP_HOST, samples: int = DEFAULT_SAMPLES, *,
                clock: Clock | None = None,
                transport: Callable[[bytes], bytes] | None = None,
                gap_s: float = 0.2) -> OffsetResult:
    clock = clock or SystemClock()
    res = OffsetResult(source=f"NTP {host}", method="median RFC 5905")
    try:
        transport = transport or UdpNtpTransport(host)
    except (OSError, TimeSyncError) as e:
        res.error = f"{type(e).__name__}: {e}"
        return res

    offsets: list[float] = []
    delays: list[float] = []
    errors: list[str] = []
    for i in range(samples):
        if i:
            clock.sleep(gap_s)
        try:
            t0 = clock.time()
            req = build_ntp_request(t0)
            data = transport(req)
            t3 = clock.time()
            off, delay = parse_ntp_response(data, req, t0, t3)
        except (OSError, TimeSyncError) as e:
            errors.append(f"{type(e).__name__}: {e}")
            continue
        offsets.append(off)
        delays.append(delay)

    res.samples = len(offsets)
    if not offsets:
        res.error = errors[-1] if errors else "tidak ada sampel"
        return res
    if errors:
        res.warnings.append(f"{len(errors)} sampel gagal: {errors[-1]}")
    res.offset_ms = statistics.median(offsets) * 1000
    res.rtt_ms = statistics.median(delays) * 1000
    spread = (max(offsets) - min(offsets)) / 2 * 1000
    res.uncertainty_ms = max(spread, res.rtt_ms / 2)
    return res


# --------------------------------------------------------------------------- HTTP Date


class HttpDateProbe:
    """Ambil header `Date` lewat koneksi keep-alive (handshake TLS tidak masuk ke sampel).

    Hanya request HEAD biasa ke halaman publik dengan User-Agent jujur; tidak ada API privat.
    Menghormati HTTPS_PROXY bila diset.
    """

    def __init__(self, url: str = DEFAULT_HTTP_URL, timeout: float = 5.0):
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"URL harus http(s): {url}")
        self.https = parts.scheme == "https"
        self.host = parts.hostname
        self.port = parts.port or (443 if self.https else 80)
        self.path = parts.path or "/"
        self.timeout = timeout
        self.method = "HEAD"
        self._conn: http.client.HTTPConnection | None = None

    def _connect(self) -> http.client.HTTPConnection:
        if not self.https:  # hanya untuk mock lokal di tes
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            conn.connect()
            return conn
        ctx = ssl.create_default_context()
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy and not _bypass_proxy(self.host):
            p = urlsplit(proxy)
            conn = http.client.HTTPSConnection(p.hostname, p.port or 8080,
                                               timeout=self.timeout, context=ctx)
            headers = {}
            if p.username:
                cred = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
                headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
            conn.set_tunnel(self.host, self.port, headers=headers)
        else:
            conn = http.client.HTTPSConnection(self.host, self.port,
                                               timeout=self.timeout, context=ctx)
        conn.connect()
        return conn

    def warmup(self) -> None:
        if self._conn is None:
            self._conn = self._connect()

    def __call__(self) -> str:
        # HEAD dulu (tanpa body); jika server menolak HEAD / tanpa Date, pindah ke GET permanen.
        for attempt in (1, 2, 3):
            try:
                self.warmup()
                assert self._conn is not None
                self._conn.request(self.method, self.path, headers={
                    "User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "keep-alive"})
                resp = self._conn.getresponse()
                resp.read()
                date = resp.getheader("Date")
                if resp.will_close:
                    self.close()
                if date:
                    return date
                if self.method == "HEAD":
                    self.method = "GET"
                    continue
                raise TimeSyncError(f"respons HTTP {resp.status} tanpa header Date")
            except (OSError, http.client.HTTPException):
                self.close()
                if attempt == 3:
                    raise
        raise TimeSyncError("server tidak mengirim header Date")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _bypass_proxy(host: str) -> bool:
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    for item in (x.strip() for x in no_proxy.split(",")):
        if item and (host == item.lstrip("*.") or host.endswith("." + item.lstrip("*."))):
            return True
    return False


def parse_http_date(value: str) -> int:
    dt = parsedate_to_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def measure_http_date(url: str = DEFAULT_HTTP_URL, samples: int = DEFAULT_SAMPLES, *,
                      clock: Clock | None = None,
                      probe: Callable[[], str] | None = None) -> OffsetResult:
    clock = clock or SystemClock()
    res = OffsetResult(source=f"HTTP Date {urlsplit(url).hostname}",
                       method="bisection pergantian detik")
    own_probe = probe is None
    probe = probe or HttpDateProbe(url)

    lo, hi = -math.inf, math.inf
    rtts: list[float] = []
    naive: list[float] = []  # D + 0.5 - titik_tengah_lokal, untuk fallback
    conflict = False

    def take() -> None:
        nonlocal lo, hi, conflict
        t0 = clock.time()
        d = parse_http_date(probe())
        t3 = clock.time()
        rtts.append(t3 - t0)
        naive.append(d + 0.5 - (t0 + t3) / 2)
        new_lo, new_hi = max(lo, d - t3), min(hi, d + 1 - t0)
        if new_lo > new_hi:
            conflict = True  # jangan persempit dengan data yang bertentangan
        else:
            lo, hi = new_lo, new_hi

    def at_phase(phase: float) -> None:
        # Tunggu sampai titik tengah request jatuh `phase` detik setelah pergantian
        # detik server menurut estimasi tengah interval.
        if not (math.isfinite(lo) and math.isfinite(hi)) or conflict:
            return
        rtt = statistics.median(rtts)
        mid = (lo + hi) / 2
        k = math.ceil(clock.time() + mid + rtt / 2 - phase + 0.05)
        sleep_until(clock, k + phase - mid - rtt / 2)

    try:
        # Handshake TCP/TLS di luar pengukuran. Urutan: 1 sampel kasar, `samples` sampel
        # bisection, 2 sampel verifikasi.
        if hasattr(probe, "warmup"):
            probe.warmup()
        take()
        for _ in range(samples):
            at_phase(0.0)
            take()
        # Verifikasi: bisection hanya bertanya "di atas/bawah tengah", jadi tidak bisa
        # mendeteksi server yang jamnya di luar interval. Dua sampel tepat sebelum & sesudah
        # pergantian detik yang diprediksi akan bertentangan jika estimasinya salah.
        for sign in (-1, 1):
            margin = (hi - lo) / 2 + statistics.median(rtts) + 0.01 if math.isfinite(hi - lo) else 0
            at_phase(sign * margin)
            take()
    except (OSError, http.client.HTTPException, TimeSyncError, ValueError) as e:
        res.error = f"{type(e).__name__}: {e}"
    finally:
        if own_probe:
            probe.close()  # type: ignore[attr-defined]

    res.samples = len(rtts)
    if res.samples == 0:
        res.error = res.error or "tidak ada sampel"
        return res
    if res.error:
        res.warnings.append(f"berhenti setelah {res.samples} sampel: {res.error}")
        res.error = None
    res.rtt_ms = statistics.median(rtts) * 1000

    if conflict or not (math.isfinite(lo) and math.isfinite(hi)):
        res.method = "median Date + koreksi RTT/2 (fallback)"
        res.warnings.append("interval antarsampel bertentangan; presisi turun ke ±500 ms")
        res.offset_ms = statistics.median(naive) * 1000
        res.uncertainty_ms = 500.0
    else:
        res.offset_ms = (lo + hi) / 2 * 1000
        res.uncertainty_ms = (hi - lo) / 2 * 1000
    return res


# --------------------------------------------------------------------------- gabungan


def sync(*, samples: int = DEFAULT_SAMPLES, ntp_host: str = DEFAULT_NTP_HOST,
         http_url: str = DEFAULT_HTTP_URL, clock: Clock | None = None,
         ntp_transport: Callable[[bytes], bytes] | None = None,
         http_probe: Callable[[], str] | None = None) -> SyncReport:
    clock = clock or SystemClock()
    ntp = measure_ntp(ntp_host, samples, clock=clock, transport=ntp_transport)
    shopee = measure_http_date(http_url, samples, clock=clock, probe=http_probe)
    report = SyncReport(ntp=ntp, shopee=shopee)
    if (d := report.disagreement_ms) is not None and abs(d) > shopee.uncertainty_ms + 50:
        shopee.warnings.append(f"berbeda {d:+.0f} ms dari NTP (di luar ketidakpastian)")
    return report


def server_clock_from(report: SyncReport, clock: Clock | None = None) -> ServerClock:
    return ServerClock(report.offset_s, clock)
