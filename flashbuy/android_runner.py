"""Runner jalur Android (aplikasi Shopee lewat uiautomator2). Kontrak sama dengan web_runner.

Alur berbasis status: di setiap iterasi layar diklasifikasi, lalu aksi yang sesuai dijalankan.
  T-60 s   buka produk lewat intent VIEW (package com.shopee.id), pilih variasi lebih awal bila bisa
  T-lead   polling: klik Beli hanya jika tombol aktif DAN harga <= max_item_price (lapis 1)
           -> bottom sheet variasi/qty (langkah maju) -> checkout (atau keranjang -> lapis 2)
  checkout pastikan ShopeePay, lapis 3 (ongkir & total stabil, 1 baris, qty 1, nama, harga, total)
  akhir    dry-run: stop | live: klik "Buat Pesanan" -> layar PIN -> alarm (PIN diketik manual)

Kecepatan: hot path memakai query selector di device (info/find_all/click koordinat), bukan dump.
Setiap query diukur latensinya (TimedDriver) dan ditulis ke log setelah run (di luar hot path).
Tidak ada sleep tetap; semua penantian = kondisi + timeout. Semua waktu dari ServerClock
(jam yang sama dengan jendela polling), jadi tes bisa memakai jam palsu.

Aplikasi TIDAK pernah ditutup/di-force-stop oleh alat; captcha/verifikasi dibiarkan apa adanya.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from flashbuy import pricing
from flashbuy.android_driver import AndroidDriver, DriverError, Node, Sel, TimedDriver, U2Driver, node_matches
from flashbuy.android_screen import (
    ANY_TEXT_MATCH,
    PRICE_MATCH,
    cart_rows,
    checkout_count,
    checkout_snapshot,
    is_price,
    payment_value,
    pick_main_price,
)
from flashbuy.android_selectors import AndroidSelectors, union
from flashbuy.config import FLOW_TIMEOUT_S, ConfigError, TargetConfig, require_live_ready
from flashbuy.notifier import Notifier
from flashbuy.runner_base import (
    ALARM_STATUSES,
    STOP_ALL_STATUSES,
    PollingGate,
    PrecheckItem,
    PrecheckResult,
    RateLimiter,
    RunLog,
    RunResult,
    RunStatus,
    after_order_click,
    always_allow,
)
from flashbuy.timesync import ServerClock

NO_RESPONSE_S = 1.5  # klik Beli tanpa perubahan layar -> boleh klik ulang (lewat RateLimiter)
STALE_TOAST_S = 0.4  # toast/banner lama yang masih tampil diabaikan selama ini
FIRST_RELOAD_S = 0.5  # belum siap (tombol/harga) di T+0,5 s -> reload pertama
RELOAD_EVERY_S = 2.0  # reload berikutnya tiap 2 s (tetap lewat RateLimiter & jendela)
UNKNOWN_LIMIT_S = 1.5  # layar tak dikenali berturut-turut lebih lama dari ini -> UNKNOWN_STATE
PRICE_STABLE_TIMEOUT_S = 1.5  # total checkout harus stabil dalam waktu ini
PRICE_STABLE_GAP_S = 0.1  # nilai yang sama harus bertahan minimal selama ini
OPEN_TIMEOUT_S = 20.0  # tunggu halaman produk setelah intent
SETTLE_TIMEOUT_S = 2.0  # tunggu tombol Beli muncul lagi setelah reload
SELECT_WAIT_S = 3.0  # tunggu pilihan variasi/pembayaran terverifikasi
QUERY_WARN_MS = 100.0  # target latensi per query
PRECHECK_PROBES = 10  # jumlah query uji latensi saat precheck
KEEPALIVE_EVERY_S = 2.0  # cek agent uiautomator2 antara arm dan T
KEEPALIVE_STOP_BEFORE_S = 3.0  # berhenti cek agent T-3 s (supaya tidak bersaing dengan hot path)
MARKER_MAX_CHARS = 120  # penanda status hanya dicari di teks pendek (bukan deskripsi produk)
MAX_AGENT_RESTARTS = 2  # restart agent maksimal saat polling (HiOS bisa membunuhnya)
_NO_PRICE = "\u0000"

DEVICE_PROPS = (
    "ro.product.manufacturer", "ro.product.brand", "ro.product.model", "ro.product.device",
    "ro.build.version.release", "ro.build.version.sdk", "ro.build.display.id",
    "ro.tranos.version", "ro.tranos.type", "ro.os.version.release",
)
_SHOPEEPAY_RE = re.compile(r"(?i)^\s*shopeepay\b(?!\s*later)")
_PAYMENT_LABEL_RE = re.compile(r"(?i)metode pembayaran")


class Screen(StrEnum):
    PRODUCT = "PRODUCT"
    NOT_STARTED = "NOT_STARTED"
    VARIANT_REQUIRED = "VARIANT_REQUIRED"
    ERROR_TOAST = "ERROR_TOAST"
    SHEET = "SHEET"
    CART = "CART"
    CHECKOUT = "CHECKOUT"
    PIN_SCREEN = "PIN_SCREEN"
    SOLD_OUT = "SOLD_OUT"
    CAPTCHA = "CAPTCHA"
    VERIFICATION = "VERIFICATION"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    OTHER_APP = "OTHER_APP"
    LOADING = "LOADING"  # indikator loading (ProgressBar) tanpa teks yang dikenali
    UNKNOWN = "UNKNOWN"


TERMINAL: dict[Screen, RunStatus] = {
    Screen.CAPTCHA: RunStatus.CAPTCHA,
    Screen.VERIFICATION: RunStatus.VERIFICATION,
    Screen.LOGIN_REQUIRED: RunStatus.LOGIN_REQUIRED,
    Screen.SOLD_OUT: RunStatus.SOLD_OUT,
}


@dataclass
class Seen:
    screen: Screen
    evidence: str = ""
    node: Node | None = None


class _Aborted(Exception):
    pass


class _Stop(Exception):
    def __init__(self, status: RunStatus, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class AndroidRunner:
    name = "android"

    def __init__(self, cfg: TargetConfig, selectors: AndroidSelectors, *, log: RunLog, notifier: Notifier,
                 driver: AndroidDriver | None = None, before_place_order: Callable[[], bool] = always_allow,
                 limiter: RateLimiter | None = None, stop_event=None):
        self.cfg = cfg
        self.limits = cfg.limits
        self.sel = selectors
        self.log = log
        self.notifier = notifier
        self.before_place_order = before_place_order
        self.limiter = limiter or RateLimiter()
        self.stop_event = stop_event  # asyncio.Event / threading.Event (cukup .is_set())
        self.variant = cfg.variant
        self.package = cfg.android.package
        self._raw_driver = driver
        self.d: TimedDriver | None = None
        self.open_at: float | None = None
        self.clock: ServerClock | None = None
        # batas waktu internal (bisa diskalakan di tes)
        self.flow_timeout_s = FLOW_TIMEOUT_S
        self.unknown_limit_s = UNKNOWN_LIMIT_S
        self.price_stable_timeout_s = PRICE_STABLE_TIMEOUT_S
        self.open_timeout_s = OPEN_TIMEOUT_S
        self.order_clicked = False
        self._abort_reason: str | None = None
        self._arm_state: Seen | None = None
        self._tracking = False
        self._unknown_since: float | None = None
        self._blocked_price = _NO_PRICE
        self._price_blocked = False
        self._last_price: pricing.ProductPrice | None = None
        self._variant_ok: bool | None = None
        self._sticky: dict[str, Sel] = {}
        self._cand_cache: dict[str, list[Sel]] = {}
        self._markers = {name: selectors.marker_re(name) for name in selectors.markers}
        self._keepalive: asyncio.Task | None = None
        self._agent_restarts = 0
        self.device_info: dict[str, str] = {}

    # ------------------------------------------------------------------ waktu

    @property
    def _clock(self) -> ServerClock:
        return self.clock or self.log.clock  # sebelum attempt: jam RunLog (ikut resync)

    def _mono(self) -> float:
        return self._clock.clock.monotonic()

    # ------------------------------------------------------------------ lifecycle

    async def prepare(self) -> None:
        if self.d is not None:
            return
        raw = self._raw_driver
        if raw is None:
            raw = await asyncio.to_thread(U2Driver, self.cfg.android.serial)
        self.d = TimedDriver(raw, self._mono, lambda: self._clock.now_ms())
        self.log.info(f"device {self.d.serial or '(pertama)'} terhubung, package {self.package}")

    async def abort(self, reason: str = "") -> None:
        self._abort_reason = reason or "dibatalkan"

    async def close(self) -> None:
        """Tidak menutup aplikasi apa pun; hanya menghentikan cek agent."""
        self._stop_keepalive()

    def _checkpoint(self) -> None:
        if self._abort_reason is None and self.stop_event is not None and self.stop_event.is_set():
            self._abort_reason = "dihentikan oleh runner lain"
        if self._abort_reason is not None:
            raise _Aborted(self._abort_reason)

    # ------------------------------------------------------------------ util selector

    def _cands(self, step: str) -> list[Sel]:
        if step not in self._cand_cache:
            self._cand_cache[step] = self.sel.candidates(step, self.variant)
        return self._cand_cache[step]

    def _find(self, step: str) -> Node | None:
        """Kandidat pertama yang menemukan elemen (kandidat yang terakhir berhasil dicoba dulu)."""
        cands = self._cands(step)
        hit = self._sticky.get(step)
        order = [hit, *[c for c in cands if c != hit]] if hit in cands else cands
        for c in order:
            node = self.d.info(c)
            if node is not None:
                self._sticky[step] = c
                return node
        return None

    def _local(self, step: str, nodes: list[Node]) -> Node | None:
        """Cocokkan kandidat (jenis teks/desc) ke node snapshot, tanpa RPC."""
        for c in self._cands(step):
            if c.by in ("resourceId", "className"):
                continue
            for n in nodes:
                if node_matches(n, c):
                    return n
        return None

    def _marker(self, name: str, nodes: list[Node]) -> Node | None:
        rx = self._markers.get(name)
        if rx is None:
            return None
        return next((n for n in nodes if len(n.label) <= MARKER_MAX_CHARS and rx.fullmatch(n.label)), None)

    def _snapshot(self) -> list[Node]:
        """Semua node bertulisan di layar (1 RPC, bukan dump)."""
        return self.d.find_all(Sel("textMatches", ANY_TEXT_MATCH))

    def _watch_sel(self) -> Sel:
        """Satu query untuk polling: nominal harga + penanda bahaya/habis/belum mulai."""
        pats = [PRICE_MATCH] if not self._cands("product_price") else []
        for name in ("captcha", "verification", "sold_out", "not_started", "error_toast", "variant_required"):
            pats += self.sel.marker(name)
        return Sel("textMatches", union(pats))

    # ------------------------------------------------------------------ klasifikasi layar

    def _classify_nodes(self, nodes: list[Node], context: str) -> Seen:
        for name, screen in (("captcha", Screen.CAPTCHA), ("verification", Screen.VERIFICATION)):
            if n := self._marker(name, nodes):
                return Seen(screen, f"teks {n.label[:60]!r}", n)
        if n := self._marker("pin", nodes):
            return Seen(Screen.PIN_SCREEN, f"teks {n.label[:40]!r}", n)
        if n := self._local("place_order", nodes):
            return Seen(Screen.CHECKOUT, "tombol Buat Pesanan", n)
        if n := self._local("sheet_marker", nodes):
            return Seen(Screen.SHEET, f"teks {n.label[:30]!r}", n)
        if n := self._local("cart_marker", nodes):
            return Seen(Screen.CART, f"teks {n.label[:30]!r}", n)
        buy = self._local("buy_button", nodes)
        sold = self._marker("sold_out", nodes)
        # di halaman produk: label "Habis" lain (mis. chip variasi lain) bukan berarti produk ini habis
        if sold is not None and (context != "product" or buy is None or sold == buy
                                 or "berakhir" in sold.label.lower()):
            return Seen(Screen.SOLD_OUT, f"teks {sold.label[:60]!r}", sold)
        if n := self._marker("variant_required", nodes):
            return Seen(Screen.VARIANT_REQUIRED, f"teks {n.label[:60]!r}", n)
        if n := self._marker("error_toast", nodes):
            return Seen(Screen.ERROR_TOAST, f"teks {n.label[:60]!r}", n)
        if (n := self._marker("not_started", nodes)) and context == "after_buy":
            return Seen(Screen.NOT_STARTED, f"teks {n.label[:60]!r}", n)
        if buy is not None:
            return Seen(Screen.PRODUCT, "tombol Beli", buy)
        if n := self._marker("login", nodes):
            return Seen(Screen.LOGIN_REQUIRED, f"teks {n.label[:40]!r}", n)
        return Seen(Screen.UNKNOWN, f"{len(nodes)} teks tak dikenali")

    def _classify(self, context: str = "any") -> Seen:
        nodes = self._snapshot()
        seen = self._classify_nodes(nodes, context)
        if seen.screen == Screen.UNKNOWN:
            seen = self._explain_unknown(seen)
        return self._observe(seen)

    def _explain_unknown(self, seen: Seen) -> Seen:
        """Hanya saat layar tak dikenali: loading, resourceId kalibrasi, aplikasi aktif, content-desc, WebView."""
        if self.d.exists(Sel("className", "android.widget.ProgressBar")):
            return Seen(Screen.LOADING, "indikator loading")
        for step, screen in (("place_order", Screen.CHECKOUT), ("buy_button", Screen.PRODUCT)):
            for c in self._cands(step):
                if c.by == "resourceId" and (n := self.d.info(c)) is not None:
                    return Seen(screen, f"resourceId {c.value}", n)
        if self._tracking and (self._unknown_since is None or self._mono() - self._unknown_since < 0.3):
            return seen  # transisi singkat (animasi): diagnosa mahal ditunda
        app = self.d.current_app()
        if app.package and app.package != self.package:
            return Seen(Screen.OTHER_APP, f"aplikasi aktif {app.package}/{app.activity}")
        descs = [Node(text=n.desc, bounds=n.bounds) for n in self.d.find_all(Sel("descriptionMatches", ANY_TEXT_MATCH))]
        if descs:
            by_desc = self._classify_nodes(descs, "any")
            if by_desc.screen != Screen.UNKNOWN:
                by_desc.evidence = "content-desc " + by_desc.evidence
                return by_desc
        web = " + WebView" if self.d.webview_present() else ""
        return Seen(Screen.UNKNOWN, f"{seen.evidence}{web} ({app.package}/{app.activity})")

    def _observe(self, seen: Seen) -> Seen:
        """Jaring pengaman: UNKNOWN/OTHER_APP berturut-turut > unknown_limit_s -> UNKNOWN_STATE."""
        if not self._tracking:
            return seen
        now = self._mono()
        if seen.screen not in (Screen.UNKNOWN, Screen.OTHER_APP):
            self._unknown_since = None
        elif self._unknown_since is None:
            self._unknown_since = now
        elif now - self._unknown_since > self.unknown_limit_s:
            raise _Stop(RunStatus.UNKNOWN_STATE,
                        f"layar tidak dikenali > {self.unknown_limit_s:.1f} s ({seen.evidence or 'tanpa ciri'})")
        return seen

    def _wait_screen(self, targets: set[Screen], timeout_s: float, context: str = "any") -> Seen:
        end = self._mono() + timeout_s
        while True:
            self._checkpoint()
            seen = self._classify(context)
            if seen.screen in targets or seen.screen in TERMINAL:
                return seen
            if self._mono() >= end:
                return Seen(Screen.UNKNOWN, "timeout")

    # ------------------------------------------------------------------ precheck

    async def precheck(self) -> PrecheckResult:
        return await asyncio.to_thread(self._precheck_sync)

    def _shell(self, *cmd: str) -> str:
        try:
            return self.d.shell(list(cmd))
        except DriverError as e:
            self.log.warn(f"shell {' '.join(cmd)} gagal: {e}")
            return ""

    def _precheck_sync(self) -> PrecheckResult:
        res = PrecheckResult(self.name)
        add = res.items.append

        # perangkat: jangan asumsikan versi Android / resolusi -> baca & catat
        props = {p: self._shell("getprop", p).strip() for p in DEVICE_PROPS}
        self.device_info = {k: v for k, v in props.items() if v}
        size = self._shell("wm", "size").strip().replace("\n", "; ")
        density = self._shell("wm", "density").strip().replace("\n", "; ")
        self.device_info.update({"wm size": size, "wm density": density})
        for k, v in self.device_info.items():
            self.log.info(f"device {k} = {v}")
        brand = props.get("ro.product.brand") or props.get("ro.product.manufacturer") or "?"
        add(PrecheckItem("device", True, f"{brand} {props.get('ro.product.model', '')} Android "
                                         f"{props.get('ro.build.version.release') or '?'} (SDK "
                                         f"{props.get('ro.build.version.sdk') or '?'}), {size or 'ukuran ?'}"))
        hios = any(props.get(k) for k in ("ro.tranos.version", "ro.tranos.type")) or \
            brand.lower() in ("tecno", "infinix", "itel")
        if hios:
            self.log.info("HiOS/Transsion terdeteksi: agen uiautomator2 bisa dibunuh di latar; dipantau sampai T-3 s")

        # agent uiautomator2 (HiOS bisa membunuhnya)
        alive = self.d.agent_alive()
        if alive is None:
            add(PrecheckItem("agent uiautomator2", None, "status agent tidak bisa dicek"))
        elif alive:
            add(PrecheckItem("agent uiautomator2", True, "hidup"))
        else:
            self.log.warn("agent uiautomator2 mati; menghidupkan ulang")
            try:
                self.d.restart_agent()
                alive = self.d.agent_alive()
            except DriverError as e:
                alive = False
                self.log.warn(str(e))
            add(PrecheckItem("agent uiautomator2", None if alive else False,
                             "mati lalu dihidupkan ulang - " + ("kemungkinan dibunuh HiOS; matikan optimasi baterai "
                                                                "& izinkan aktivitas latar untuk shell/USB debugging"
                                                                if alive else "gagal dihidupkan")))
            if not alive:
                res.status = RunStatus.ERROR
                return res

        # layar menyala & tidak terkunci; tidak mati sebelum T
        power = self._shell("dumpsys", "power")
        window = self._shell("dumpsys", "window")
        awake = re.search(r"mWakefulness=(\w+)", power)
        locked = re.search(r"(mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen|"
                           r"mKeyguardShowing)=true", window)
        if awake and awake.group(1).lower() != "awake":
            add(PrecheckItem("layar", False, f"layar tidak menyala ({awake.group(1)})"))
        elif locked:
            add(PrecheckItem("layar", False, "layar terkunci - buka kunci HP"))
        else:
            add(PrecheckItem("layar", True if awake else None, "menyala & tidak terkunci" if awake else
                             "status layar tidak terbaca"))
        stay = self._shell("settings", "get", "global", "stay_on_while_plugged_in").strip()
        timeout = self._shell("settings", "get", "system", "screen_off_timeout").strip()
        if stay in ("", "0", "null") and (not timeout.isdigit() or int(timeout) < 15 * 60_000):
            add(PrecheckItem("layar tetap menyala", None,
                             f"'Tetap aktif' mati dan timeout layar {timeout or '?'} ms - layar bisa mati sebelum T; "
                             "aktifkan Opsi Pengembang > Tetap aktif"))

        # aplikasi Shopee
        path = self._shell("pm", "path", self.package)
        if "package:" not in path:
            add(PrecheckItem("aplikasi Shopee", False, f"{self.package} tidak terpasang"))
            res.status = RunStatus.ERROR
            return res
        ver = re.search(r"versionName=(\S+)", self._shell("dumpsys", "package", self.package))
        add(PrecheckItem("aplikasi Shopee", True, f"{self.package} {ver.group(1) if ver else '(versi ?)'}"))

        # halaman produk: login, tombol Beli, harga, latensi query
        try:
            self.d.start_url(self.cfg.product_url, self.package)
        except DriverError as e:
            add(PrecheckItem("buka produk", False, f"intent VIEW gagal: {e}"))
            return res
        seen = self._wait_screen({Screen.PRODUCT, Screen.NOT_STARTED, Screen.SHEET}, self.open_timeout_s, "product")
        if seen.screen == Screen.LOGIN_REQUIRED:
            add(PrecheckItem("login", False, f"aplikasi minta login ({seen.evidence})"))
            res.status = RunStatus.LOGIN_REQUIRED
            return res
        if seen.screen in (Screen.CAPTCHA, Screen.VERIFICATION):
            add(PrecheckItem("buka produk", False, f"{seen.screen}: {seen.evidence} - selesaikan manual"))
            res.status = TERMINAL[seen.screen]
            return res
        app = self.d.current_app()
        if app.package != self.package:
            add(PrecheckItem("buka produk", False, f"intent membuka {app.package}, bukan {self.package}"))
        else:
            add(PrecheckItem("login", True, "halaman produk terbuka tanpa diminta login"))
        buy = self._find("buy_button")
        add(PrecheckItem("tombol Beli", True if buy else None,
                         f"ditemukan ({buy.label!r}, enabled={buy.enabled})" if buy else
                         "tidak ditemukan - jalankan calibrate --platform android"))
        price_text = self._read_price()
        price = pricing.check_product_price(price_text, self.limits)
        add(PrecheckItem("harga produk", True if price.verdict != "unreadable" else None,
                         price.describe(self.limits) + " (sebelum flash sale bisa harga normal)"))
        add(self._latency_probe())

        # alamat & saldo (dibaca dari UI; tidak terbaca -> peringatan saja)
        add(self._read_page("alamat default", "address_page",
                            lambda t: (True, "alamat 'Utama' ditemukan") if re.search(r"\butama\b", t, re.I) else
                            (False, "belum ada alamat tersimpan") if re.search(r"belum ada alamat", t, re.I) else
                            (None, "tidak terbaca dari halaman alamat")))
        balance: list[int] = []

        def wallet(t: str):
            m = re.search(r"saldo[^\n]*\n?[^\n]*?(Rp\s?[\d.]+)", t, re.I)
            v = pricing.parse_price(m.group(1)) if m else None
            if v is None:
                return None, "saldo tidak terbaca"
            balance.append(v)
            need = price.value
            if need is None:
                return None, f"saldo {pricing.rupiah(v)}, harga produk tidak terbaca"
            return v >= need, f"saldo {pricing.rupiah(v)} vs harga {pricing.rupiah(need)} (ongkir belum termasuk)"

        add(self._read_page("saldo ShopeePay", "wallet_page", wallet))
        if balance and balance[0] < self.limits.max_total:
            add(PrecheckItem("saldo vs max_total", None, f"saldo {pricing.rupiah(balance[0])} < max_total "
                                                         f"{pricing.rupiah(self.limits.max_total)}"))
        try:  # kembali ke halaman produk
            self.d.start_url(self.cfg.product_url, self.package)
        except DriverError:
            pass
        self._write_latency("precheck")
        return res

    def _latency_probe(self) -> PrecheckItem:
        start = len(self.d.stats.samples)
        for _ in range(PRECHECK_PROBES):
            self.d.exists(Sel("text", "__flashbuy_probe__"))
        for c in self._cands("buy_button")[:1]:
            for _ in range(3):
                self.d.info(c)
        ms = sorted(s.ms for s in self.d.stats.samples[start:])
        p95 = ms[min(len(ms) - 1, int(round(0.95 * (len(ms) - 1))))]
        median = ms[len(ms) // 2]
        detail = f"median {median:.0f} ms, p95 {p95:.0f} ms, maks {ms[-1]:.0f} ms ({len(ms)} query)"
        self.log.info(f"latensi query precheck: {detail}")
        if p95 > QUERY_WARN_MS:
            return PrecheckItem("latensi query", None, f"{detail} > target {QUERY_WARN_MS:.0f} ms - "
                                                       "pakai kabel/port USB lain, tutup aplikasi lain")
        return PrecheckItem("latensi query", True, detail)

    def _read_page(self, name: str, url_key: str, judge) -> PrecheckItem:
        url = self.sel.urls.get(url_key)
        if not url:
            return PrecheckItem(name, None, "URL halaman tidak diset")
        try:
            self.d.start_url(url, self.package)
        except DriverError as e:
            return PrecheckItem(name, None, f"halaman tidak bisa dibuka di aplikasi ({e})")
        end = self._mono() + 5.0
        text = ""
        while self._mono() < end:
            text = "\n".join(n.label for n in sorted(self._snapshot(), key=lambda n: (n.bounds[1], n.bounds[0])))
            ok, detail = judge(text)
            if ok is not None:
                return PrecheckItem(name, ok, detail)
        ok, detail = judge(text)
        return PrecheckItem(name, ok, detail)

    # ------------------------------------------------------------------ arm (T-60 s)

    async def arm(self, open_at: float) -> None:
        self.open_at = open_at
        await asyncio.to_thread(self._arm_sync)
        self._keepalive = asyncio.ensure_future(self._keepalive_loop())

    def _arm_sync(self) -> None:
        self._ensure_agent("arm")
        self.d.start_url(self.cfg.product_url, self.package)
        self.log.mark("page_open")
        seen = self._wait_screen({Screen.PRODUCT, Screen.SHEET}, self.open_timeout_s, "product")
        if seen.screen in (Screen.LOGIN_REQUIRED, Screen.CAPTCHA, Screen.VERIFICATION):
            self._arm_state = seen
            self.log.warn(f"arm: {seen.screen} ({seen.evidence})")
            return
        if seen.screen != Screen.PRODUCT:
            self.log.warn(f"arm: halaman produk belum terdeteksi ({seen.screen} {seen.evidence}); "
                          "dicari lagi saat polling")
        self._preselect_variant()
        price = pricing.check_product_price(self._read_price(), self.limits)
        self.log.mark("armed", f"layar {seen.screen}, {price.describe(self.limits)}")

    def _ensure_agent(self, when: str) -> None:
        alive = self.d.agent_alive()
        if alive is False:
            self.log.warn(f"{when}: agent uiautomator2 mati (HiOS?), menghidupkan ulang")
            self.d.restart_agent()
            self.log.info(f"{when}: agent hidup lagi ({self.d.agent_alive()})")

    async def _keepalive_loop(self) -> None:
        """Antara arm dan T-3 s: cek agent tiap 2 s; hidupkan ulang bila dibunuh HiOS."""
        try:
            while self.open_at is not None and self._clock.now() < self.open_at - KEEPALIVE_STOP_BEFORE_S:
                await asyncio.sleep(KEEPALIVE_EVERY_S)
                if self._clock.now() >= self.open_at - KEEPALIVE_STOP_BEFORE_S:
                    break
                await asyncio.to_thread(self._ensure_agent, "keepalive")
        except asyncio.CancelledError:
            pass
        except DriverError as e:
            self.log.warn(f"keepalive: {e}")

    def _stop_keepalive(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None

    def _preselect_variant(self) -> None:
        """Pilih variasi lebih awal bila opsinya tampil langsung di halaman produk."""
        if not self.variant:
            return
        opt = self._find("variant_option")
        if opt is None:
            self.log.info(f"variasi {self.variant!r} belum tampil di halaman produk; dipilih di bottom sheet")
            return
        if opt.selected or opt.checked:
            self._variant_ok = True
            return
        if not opt.enabled:
            self.log.warn(f"variasi {self.variant!r} belum bisa dipilih")
            return
        self.d.click(opt)
        self.log.mark("variant_selected", self.variant)
        self._variant_ok = True
        seen = self._classify("product")
        if seen.screen == Screen.SHEET:  # opsi membuka bottom sheet -> tutup, pilih ulang setelah Beli
            self.d.press_back()
            self.log.info("pilih variasi membuka bottom sheet; ditutup, dipilih ulang setelah klik Beli")

    # ------------------------------------------------------------------ attempt

    async def attempt(self, clock: ServerClock, live: bool) -> RunResult:
        self.clock = clock
        self._stop_keepalive()
        if live:
            try:
                require_live_ready(self.cfg)
            except ConfigError as e:
                return await asyncio.to_thread(self._finish, RunStatus.ERROR, str(e), live)
        return await asyncio.to_thread(self._attempt_wrapped, live)

    def _attempt_wrapped(self, live: bool) -> RunResult:
        self._tracking = True
        self._unknown_since = None
        try:
            status, message = self._attempt(live)
        except _Aborted as e:
            status, message = RunStatus.ABORTED, str(e)
        except _Stop as e:
            status, message = e.status, e.message
        except DriverError as e:
            status, message = RunStatus.ERROR, f"driver: {e}"
        finally:
            self._tracking = False
        return self._finish(status, message, live)

    def _attempt(self, live: bool) -> tuple[RunStatus, str]:
        if self.open_at is None:
            raise RuntimeError("arm() belum dipanggil")
        self._checkpoint()
        if self._arm_state is not None:
            return TERMINAL[self._arm_state.screen], f"saat membuka produk: {self._arm_state.evidence}"
        clock = self.clock
        gate = PollingGate(clock, self.open_at, self.limiter)
        self.log.mark("poll_start")

        clicks = 0
        last_reload = None
        outcome = Seen(Screen.PRODUCT)
        while True:
            self._checkpoint()
            kind, info = self._wait_ready(gate, last_reload)
            if kind == "stop":
                return TERMINAL[info.screen], info.evidence
            if kind == "expired":
                return self._window_closed(clicks)
            if kind == "reload":
                if not gate.acquire_sync():
                    return self._window_closed(clicks)
                self._checkpoint()
                self._reload()
                last_reload = clock.now()
                self.log.mark("reload")
                self._blocked_price = _NO_PRICE
                self._wait_screen({Screen.PRODUCT}, SETTLE_TIMEOUT_S, "product")
                self._preselect_variant()  # variasi bisa hilang setelah reload -> pilih ulang
                continue
            btn, price = info
            t_ready = clock.now_ms()
            if not gate.acquire_sync():
                return self._window_closed(clicks)
            self._checkpoint()
            stale = outcome.screen in (Screen.NOT_STARTED, Screen.ERROR_TOAST, Screen.VARIANT_REQUIRED)
            t_click = clock.now_ms()
            self.d.click(btn)
            clicks += 1
            if clicks == 1:
                self.log.mark("buy_ready", price.describe(self.limits), t_ms=t_ready)
            self.log.mark("click_buy", f"#{clicks}", t_ms=t_click)
            outcome = self._after_buy(stale)
            st = outcome.screen
            if st in (Screen.CART, Screen.CHECKOUT):
                break
            if st in TERMINAL:
                return TERMINAL[st], outcome.evidence
            if st in (Screen.NOT_STARTED, Screen.ERROR_TOAST):
                self.log.mark("not_started", f"#{clicks} ({outcome.evidence})")
                continue
            if st == Screen.VARIANT_REQUIRED:
                if not self.variant:
                    return RunStatus.ERROR, "produk wajib pilih variasi; isi `variant` di target.yaml"
                continue
            if st == Screen.PIN_SCREEN:
                return RunStatus.ERROR, "layar PIN muncul tak terduga setelah klik Beli"
            self.log.mark("no_response", f"#{clicks} ({outcome.evidence or st})")

        # ---- klik Beli berhasil: langkah maju sekali-sekali, tanpa throttle, batas 30 s
        self.log.mark("buy_ok", str(outcome.screen))
        deadline = self._mono() + self.flow_timeout_s
        if outcome.screen == Screen.CART:
            self._cart_guard()
            btn = self._wait_find("cart_checkout", deadline)
            if btn is None:
                return RunStatus.ERROR, "tombol Checkout di keranjang tidak ditemukan"
            self._checkpoint()
            t_click = clock.now_ms()
            self.d.click(btn)
            self.log.mark("click_checkout", t_ms=t_click)
            outcome = self._wait_screen({Screen.CHECKOUT}, max(0.1, deadline - self._mono()))
        if outcome.screen != Screen.CHECKOUT:
            return self._unexpected(outcome, "halaman checkout")
        self.log.mark("checkout_loaded")

        ok, msg = self._ensure_shopeepay(deadline)
        if not ok:
            return RunStatus.ERROR, msg
        self.log.mark("payment_ok", msg)

        if self._wait_find("place_order", deadline) is None:
            return RunStatus.ERROR, "tombol 'Buat Pesanan' tidak ditemukan"
        self._checkout_guard()  # lapis 3: penentu akhir, dry-run maupun live
        place = self._find("place_order")  # posisi terbaru (layar bisa bergeser saat total dimuat)
        if place is None:
            return RunStatus.ERROR, "tombol 'Buat Pesanan' hilang setelah pengecekan harga"
        self._checkpoint()
        if not self.before_place_order():
            return RunStatus.ABORTED, "before_place_order() menolak (lock dipegang jalur lain)"
        self.log.mark("place_order_gate", "live" if live else "dry-run: berhenti di sini")
        if not live:
            return RunStatus.DRYRUN_OK, "sampai checkout dengan ShopeePay & harga lolos; 'Buat Pesanan' TIDAK diklik"

        t_click = clock.now_ms()
        self.order_clicked = True
        self.d.click(place)
        self.log.mark("click_place_order", t_ms=t_click)
        outcome = self._wait_screen({Screen.PIN_SCREEN}, max(0.1, deadline - self._mono()), "after_order")
        if outcome.screen != Screen.PIN_SCREEN:
            return self._unexpected(outcome, "layar PIN")
        self.log.mark("pin_screen")
        return RunStatus.ORDER_PLACED_AWAIT_PIN, "pesanan dibuat; masukkan PIN ShopeePay secara manual"

    def _window_closed(self, clicks: int) -> tuple[RunStatus, str]:
        if self._price_blocked and self._last_price is not None:
            hint = f"; variasi {self.variant!r} tidak ditemukan/terpilih" if self._variant_ok is False else ""
            return RunStatus.PRICE_GUARD, (f"jendela polling habis tanpa harga valid: "
                                           f"{self._last_price.describe(self.limits)} ({clicks} klik){hint}")
        return RunStatus.NOT_STARTED_TIMEOUT, f"slot tidak terbuka sampai T+8 s ({clicks} klik)"

    def _unexpected(self, seen: Seen, waiting_for: str) -> tuple[RunStatus, str]:
        if seen.screen in TERMINAL:
            return TERMINAL[seen.screen], seen.evidence
        if seen.screen == Screen.UNKNOWN and seen.evidence == "timeout":
            return RunStatus.TIMEOUT, f"{waiting_for} tidak muncul dalam batas waktu"
        return RunStatus.ERROR, f"menunggu {waiting_for}, dapat {seen.screen} {seen.evidence}".strip()

    def _wait_find(self, step: str, deadline: float) -> Node | None:
        while self._mono() < deadline:
            self._checkpoint()
            node = self._find(step)
            if node is not None:
                return node
            seen = self._classify("any")
            if seen.screen in TERMINAL:
                raise _Stop(TERMINAL[seen.screen], seen.evidence)
        return None

    def _reload(self) -> None:
        if self.cfg.android.reload == "intent":
            self.d.start_url(self.cfg.product_url, self.package)
        else:
            self.d.swipe_refresh()

    # ------------------------------------------------------------------ lapis 1: produk

    def _read_price(self) -> str | None:
        if self._cands("product_price"):
            node = self._find("product_price")
            return node.label if node is not None else None
        node = pick_main_price(self.d.find_all(Sel("textMatches", PRICE_MATCH)))
        return node.label if node is not None else None

    def _wait_ready(self, gate: PollingGate, last_reload: float | None):
        """Tunggu tombol Beli aktif + harga valid.

        Return ("ready", (node, ProductPrice)) | ("expired", None) | ("reload", None) | ("stop", Seen).
        """
        watch = self._watch_sel()
        while True:
            self._checkpoint()
            if gate.expired():
                return "expired", None
            try:
                btn = self._find("buy_button")
                if btn is None:
                    seen = self._classify("product")
                    if seen.screen in TERMINAL:
                        return "stop", seen
                    if seen.screen == Screen.PRODUCT:
                        btn = seen.node
                else:
                    self._observe(Seen(Screen.PRODUCT, "tombol Beli", btn))
                if btn is not None:
                    found = self.d.find_all(watch)
                    seen = self._classify_nodes([btn, *found], "product")
                    if seen.screen in TERMINAL:
                        return "stop", seen
                    ready = btn.enabled and not self._marker("not_started", [btn])
                    if ready:
                        price_text = self._price_from(found)
                        price = pricing.check_product_price(price_text, self.limits)
                        self._last_price = price
                        self._price_blocked = price.verdict != "ok"
                        if price.verdict == "ok":
                            self._blocked_price = _NO_PRICE
                            return "ready", (btn, price)
                        if price_text != self._blocked_price:  # catat sekali per teks harga
                            self._blocked_price = price_text
                            self.log.mark("price_not_yet" if price.verdict == "high" else "price_unreadable",
                                          price.describe(self.limits))
                    else:
                        self._price_blocked = False
            except DriverError as e:
                self._recover_agent(e)
            now = gate.clock.now()
            if now >= gate.open_at + FIRST_RELOAD_S and (last_reload is None or now - last_reload >= RELOAD_EVERY_S):
                return "reload", None

    def _price_from(self, found: list[Node]) -> str | None:
        if self._cands("product_price"):
            return self._read_price()
        node = pick_main_price(n for n in found if is_price(n))
        return node.label if node is not None else None

    def _recover_agent(self, err: DriverError) -> None:
        """Query gagal saat polling: agent mati (HiOS)? hidupkan ulang, maksimal MAX_AGENT_RESTARTS kali."""
        self.log.warn(f"query gagal saat polling: {err}")
        if self.d.agent_alive() is False and self._agent_restarts < MAX_AGENT_RESTARTS:
            self._agent_restarts += 1
            self.log.warn(f"agent uiautomator2 mati saat polling (HiOS?); restart #{self._agent_restarts}")
            self.d.restart_agent()
            return
        raise err

    def _after_buy(self, stale: bool) -> Seen:
        t0 = self._mono()
        seen_clear = not stale
        sheet_done = False
        while True:
            self._checkpoint()
            seen = self._classify("after_buy")
            st = seen.screen
            elapsed = self._mono() - t0
            if st in (Screen.CART, Screen.CHECKOUT, Screen.PIN_SCREEN) or st in TERMINAL:
                return seen
            if st == Screen.SHEET:
                if not sheet_done:
                    self._handle_sheet()
                    sheet_done = True
                    t0 = self._mono()
                    seen_clear = False  # toast lama di sheet diabaikan sebentar
                elif elapsed > NO_RESPONSE_S:
                    return Seen(Screen.PRODUCT, "sheet tidak bereaksi")  # klik ulang lewat RateLimiter
                continue
            if st in (Screen.NOT_STARTED, Screen.ERROR_TOAST, Screen.VARIANT_REQUIRED):
                if seen_clear or elapsed > STALE_TOAST_S:
                    return seen
                continue
            if st == Screen.LOADING:  # server lambat saat flash sale: tunggu, bukan "tidak bereaksi"
                if elapsed > self.flow_timeout_s:
                    return Seen(Screen.UNKNOWN, "timeout")
                continue
            seen_clear = True
            if elapsed > NO_RESPONSE_S:
                return Seen(Screen.PRODUCT, "tidak ada reaksi")

    def _handle_sheet(self) -> None:
        """Bottom sheet variasi/kuantitas: pilih variasi, cek harga yang tampil, konfirmasi (sekali)."""
        if self.variant:
            opt = self._find("variant_option")
            if opt is None:
                self._variant_ok = False
                raise _Stop(RunStatus.ERROR, f"variasi {self.variant!r} tidak ditemukan di pilihan variasi")
            if not (opt.selected or opt.checked):
                if not opt.enabled:
                    raise _Stop(RunStatus.SOLD_OUT, f"variasi {self.variant!r} tidak bisa dipilih (habis?)")
                self.d.click(opt)
                self.log.mark("variant_selected", self.variant)
            self._variant_ok = True
        nodes = self._snapshot()
        main = pick_main_price(nodes)
        price = pricing.check_product_price(main.label if main else None, self.limits)
        if price.verdict != "ok":  # hanya informasi; penentu harga = lapis 3 di checkout (fail-closed)
            self.log.warn(f"pilihan variasi: {price.describe(self.limits)}; diputuskan di checkout (lapis 3)")
        confirm = self._local("sheet_confirm", nodes) or self._find("sheet_confirm")
        if confirm is None:
            raise _Stop(RunStatus.ERROR, "tombol konfirmasi di pilihan variasi tidak ditemukan")
        t_click = self.clock.now_ms()
        self.d.click(confirm)
        self.log.mark("click_sheet_confirm", price.describe(self.limits), t_ms=t_click)

    # ------------------------------------------------------------------ lapis 2: keranjang

    def _cart_guard(self) -> None:
        target = self.cfg.expected_name
        rows, counts = self._read_cart()
        if not rows:
            if counts and set(counts) != {1}:
                raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: tombol Checkout menunjukkan {counts} item, "
                                                   "baris item tidak terbaca")
            self.log.warn("keranjang: baris item tidak terbaca; diputuskan di checkout (lapis 3)")
            return
        verdict = pricing.check_cart([r for r, _ in rows], target)
        if verdict.to_uncheck:
            for i in verdict.to_uncheck:
                self._checkpoint()
                row, box = rows[i]
                self.d.click(box)
                self.log.mark("cart_uncheck", row.text.splitlines()[0][:50])
            rows, counts = self._read_cart()
            verdict = pricing.check_cart([r for r, _ in rows], target)
        if not verdict.ok:
            names = "; ".join(r.text.splitlines()[0][:40] for r, _ in rows if r.checked)
            raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: {verdict.reason} [{names}]")
        if counts and set(counts) != {1}:
            raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: tombol Checkout menunjukkan {counts} item")
        self.log.mark("cart_ok", verdict.reason)

    def _read_cart(self):
        nodes = self._snapshot()
        boxes = self.d.find_all(Sel("className", "android.widget.CheckBox"))
        return cart_rows(boxes, nodes), checkout_count(nodes)

    # ------------------------------------------------------------------ checkout

    def _ensure_shopeepay(self, deadline: float) -> tuple[bool, str]:
        nodes = self._snapshot()
        value = payment_value(nodes, _PAYMENT_LABEL_RE)
        if value and _SHOPEEPAY_RE.match(value):
            return True, "ShopeePay sudah terpilih"
        row = self._local("payment_change", nodes) or self._find("payment_change")
        if row is None:
            return False, f"baris 'Metode Pembayaran' tidak ditemukan (metode tampil: {value!r})"
        t_click = self.clock.now_ms()
        self.d.click(row)
        self.log.mark("payment_change", f"sebelumnya {value!r}", t_ms=t_click)
        end = min(deadline, self._mono() + SELECT_WAIT_S)
        opt = None
        while self._mono() < end and opt is None:
            self._checkpoint()
            opt = self._shopeepay_option()
        if opt is None:
            return False, "opsi ShopeePay tidak ditemukan di daftar metode pembayaran"
        t_click = self.clock.now_ms()
        self.d.click(opt)
        self.log.mark("select_shopeepay", t_ms=t_click)
        confirm = self._find("payment_confirm")
        if confirm is not None:
            self.d.click(confirm)
            self.log.mark("payment_confirm")
        while self._mono() < end:
            self._checkpoint()
            nodes = self._snapshot()
            seen = self._classify_nodes(nodes, "any")
            if seen.screen in TERMINAL:
                raise _Stop(TERMINAL[seen.screen], seen.evidence)
            if seen.screen == Screen.CHECKOUT:
                value = payment_value(nodes, _PAYMENT_LABEL_RE)
                if value and _SHOPEEPAY_RE.match(value):
                    return True, "ShopeePay dipilih (sebelumnya metode lain)"
        return False, f"ShopeePay tidak terverifikasi terpilih (metode tampil: {value!r}); jalankan calibrate ulang"

    def _shopeepay_option(self) -> Node | None:
        node = self._find("payment_shopeepay")
        return node if node is not None and _SHOPEEPAY_RE.match(node.label) else None

    def _checkout_guard(self) -> pricing.CheckoutVerdict:
        """Lapis 3: tunggu ongkir & total terbaca dan stabil (>= 100 ms), lalu cek isi pesanan."""
        end = self._mono() + self.price_stable_timeout_s
        prev = None
        since = 0.0
        snap = None
        shipping = total = None
        while True:
            self._checkpoint()
            nodes = self._snapshot()
            seen = self._classify_nodes(nodes, "any")
            if seen.screen in TERMINAL:
                raise _Stop(TERMINAL[seen.screen], seen.evidence)
            snap = checkout_snapshot(nodes)
            shipping, total = pricing.read_total(snap)
            key = (shipping, total) if shipping is not None and total is not None else None
            now = self._mono()
            if key is not None and key == prev and now - since >= PRICE_STABLE_GAP_S:
                break
            if key != prev:
                prev, since = key, now
            if now >= end:
                raise _Stop(RunStatus.PRICE_GUARD,
                            f"total checkout tidak stabil/terbaca dalam {self.price_stable_timeout_s:.1f} s "
                            f"(ongkir {pricing.rupiah(shipping)}, total {pricing.rupiah(total)})")
        verdict = pricing.check_checkout(snap, self.limits)
        self.log.info(f"checkout terbaca: {verdict.summary()}")
        if not verdict.ok:
            raise _Stop(RunStatus.PRICE_GUARD, f"{'; '.join(verdict.reasons)} | {verdict.summary()}")
        self.log.mark("price_guard_ok", verdict.summary())
        return verdict

    # ------------------------------------------------------------------ akhir

    def _write_latency(self, label: str) -> None:
        stats = self.d.stats
        if not stats.samples:
            return
        for line in stats.summary():
            self.log.info(f"latensi {label} {line}")
        slow = stats.slow(QUERY_WARN_MS)
        if slow:
            worst = max(slow, key=lambda s: s.ms)
            self.log.warn(f"{len(slow)} query > {QUERY_WARN_MS:.0f} ms (terlama {worst.op} {worst.target} "
                          f"{worst.ms:.0f} ms)")
        stats.write_csv(self.log.run_dir / f"android-queries-{label}.csv")
        stats.samples.clear()

    def _finish(self, status: RunStatus, message: str, live: bool) -> RunResult:
        detail = ""
        if self.order_clicked:
            status, message, detail = after_order_click(status, message)
        self.log.mark("result", f"{status}: {message}" + (f" ({detail})" if detail else ""))
        result = RunResult(self.name, status, message, live, steps=list(self.log.steps), detail=detail)
        if status in STOP_ALL_STATUSES and self.stop_event is not None:
            self.stop_event.set()
        if status in ALARM_STATUSES or self.order_clicked:
            self.notifier.alarm(str(status), message, platform=self.name)
        if self.d is not None:
            self._final_diagnostics(status, result)
            self._write_latency("run")
        self.log.write_result(result)
        return result

    def _final_diagnostics(self, status: RunStatus, result: RunResult) -> None:
        """Di luar hot path: aplikasi aktif, screenshot, dump hierarki (diagnosa)."""
        try:
            app = self.d.current_app()
            self.log.info(f"layar akhir: {app.package}/{app.activity}")
        except DriverError as e:
            self.log.warn(f"current_app gagal: {e}")
        path = self.log.screenshot_path(str(status))
        try:
            if self.d.screenshot(path):
                result.screenshots.append(path)
        except DriverError as e:
            self.log.warn(f"screenshot gagal: {e}")
        try:
            dump_path = Path(str(path).removesuffix(".png") + ".xml")
            dump_path.write_text(self.d.dump(), encoding="utf-8")
            self.log.info(f"dump hierarki: {dump_path.name}")
        except DriverError as e:
            self.log.warn(f"dump gagal: {e}")
