"""Runner jalur Android (aplikasi Shopee lewat uiautomator2). Kontrak sama dengan web_runner.

Alur berbasis status: di setiap iterasi layar diklasifikasi, lalu aksi yang sesuai dijalankan.
  T-60 s   buka produk lewat intent VIEW (package com.shopee.id), pilih variasi lebih awal bila bisa
  T-lead   polling: klik Beli hanya jika tombol aktif DAN harga <= max_item_price (lapis 1)
           -> bottom sheet variasi/qty (langkah maju) -> checkout (atau keranjang -> lapis 2)
  checkout pastikan ShopeePay, lapis 3 (ongkir & total stabil, 1 baris, qty 1, nama, variasi, harga, total)
  akhir    dry-run: stop | live: klik "Buat Pesanan" -> layar PIN -> alarm (PIN diketik manual)

Kecepatan (lihat android_driver): hot path hanya memakai `info`/`exists` (satu pencarian pohon di device),
klasifikasi = rantai `exists` berprioritas dengan regex gabungan; `find_all` (info_list: ~16 pencarian
per elemen cocok) hanya untuk bacaan sempit di langkah maju (sheet, checkout, keranjang) dan untuk menilai
tombol "Habis" polos setelah `exists`-nya kena (bukan di iterasi polling). Latensi tiap
query diukur (TimedDriver) dan ditulis ke log setelah run. Tidak ada sleep tetap: kondisi + timeout.
Semua waktu dari ServerClock (jam yang sama dengan jendela polling), jadi tes memakai jam palsu.

Keamanan klik: setiap aksi polling (Beli, konfirmasi ulang di sheet, reload, pilih ulang variasi setelah
reload) lewat PollingGate (>= 425 ms, T-1..T+8 s); reload menggeser slot berikutnya dari saat reload SELESAI;
tombol Beli diverifikasi ulang tepat sebelum diklik bila sempat menunggu slot, chip variasi selalu dibaca
ulang setelah slot. Captcha/verifikasi (teks & content-desc) dicek sebelum konfirmasi sheet. Aplikasi TIDAK
pernah ditutup/di-force-stop; captcha, verifikasi, dan layar PIN dibiarkan apa adanya.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from flashbuy import pricing
from flashbuy.android_driver import (
    AgentDead,
    AndroidDriver,
    AppInfo,
    DriverError,
    Node,
    Sel,
    TimedDriver,
    U2Driver,
    node_matches,
)
from flashbuy.android_screen import (
    ANY_TEXT_MATCH,
    ORDER_COUNT_MATCH,
    PENDING_MATCH,
    QTY_MATCH,
    RP_ANY_MATCH,
    RP_SHORT_MATCH,
    SHIPPING_LABEL_MATCH,
    SP,
    TOTAL_LABEL_MATCH,
    cart_rows,
    checkout_count,
    checkout_snapshot,
    is_shopeepay,
    payment_value,
)
from flashbuy.android_selectors import AndroidSelectors, text_regex, union
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

NO_RESPONSE_S = 1.5  # halaman produk TIDAK berubah sekian lama setelah klik -> boleh klik ulang (lewat gate)
STALE_TOAST_S = 0.4  # toast/banner lama yang masih tampil diabaikan selama ini
FIRST_RELOAD_S = 0.5  # belum siap (tombol/harga) di T+0,5 s -> reload pertama
RELOAD_EVERY_S = 2.0  # reload berikutnya tiap 2 s (tetap lewat RateLimiter & jendela)
UNKNOWN_LIMIT_S = 1.5  # layar tak dikenali berturut-turut lebih lama dari ini -> UNKNOWN_STATE
LOADING_LIMIT_S = 10.0  # indikator loading tanpa layar dikenali berturut-turut lebih lama dari ini -> UNKNOWN_STATE
PRICE_STABLE_TIMEOUT_S = 1.5  # total checkout harus stabil dalam waktu ini
PRICE_STABLE_GAP_S = 0.1  # nilai yang sama harus bertahan minimal selama ini
POST_CHANGE_STABLE_S = 1.0  # setelah ganti metode bayar/uncheck keranjang: server menghitung ulang total
OPEN_TIMEOUT_S = 20.0  # tunggu halaman produk setelah intent
SETTLE_TIMEOUT_S = 2.0  # tunggu tombol Beli muncul lagi setelah reload
SELECT_WAIT_S = 3.0  # tunggu pilihan variasi/pembayaran terverifikasi
CART_SETTLE_S = 2.0  # keranjang: tunggu baris terbaca / centang terbarui setelah uncheck
VARIANT_VERIFY_S = 0.6  # tunggu chip variasi terlihat terpilih setelah diklik
REVERIFY_AFTER_WAIT_S = 0.05  # menunggu slot lebih lama dari ini -> cek ulang tombol sebelum klik
QUERY_WARN_MS = 100.0  # target latensi per query info/exists
FIND_ALL_WARN_MS = 300.0  # find_all (info_list) memang lebih mahal; di atas ini diberi peringatan
PRECHECK_PROBES = 10  # jumlah query uji latensi saat precheck
AGENT_PING_QUERIES = 3  # agent sehat = 3 query berturut-turut masing-masing < AGENT_PING_MAX_S
AGENT_PING_MAX_S = 1.0
KEEPALIVE_EVERY_S = 2.0  # cek agent uiautomator2 antara arm dan T
KEEPALIVE_STOP_BEFORE_S = 3.0  # berhenti cek agent T-3 s (supaya tidak bersaing dengan hot path)
MARKER_MAX_CHARS = 120  # penanda status hanya dicari di teks pendek (bukan deskripsi produk)
MAX_AGENT_RESTARTS = 2  # restart agent maksimal saat polling (HiOS bisa membunuhnya)
GUARD_FRESH_S = 0.5  # cek pra-klik (content-desc bahaya, sheet sebelum klik pertama) dianggap segar selama ini
SHEET_CLOSE_S = 1.0  # sheet yang ditutup dengan back harus hilang dalam waktu ini
UNKNOWN_DIAG_AFTER_S = 0.3  # diagnosa mahal (aplikasi aktif, content-desc) hanya saat UNKNOWN bertahan
_NO_PRICE = "\u0000"

DEVICE_PROPS = (
    "ro.product.manufacturer", "ro.product.brand", "ro.product.model", "ro.product.device",
    "ro.build.version.release", "ro.build.version.sdk", "ro.build.display.id",
    "ro.tranos.version", "ro.tranos.type", "ro.os.version.release",
)
PAYMENT_LABEL_MATCH = r"(?is)\s*metode pembayaran\b.*"
# nama metode yang mungkin tampil di baris "Metode Pembayaran" (nilai dibaca agar bisa dibandingkan)
PAYMENT_METHOD_MATCH = (r"(?is).*(shopeepay|spaylater|\bcod\b|cek dulu|bayar di tempat|transfer|bank|virtual account|"
                        r"kartu|alfamart|indomaret|gopay|ovo|\bdana\b|akulaku|kredivo).*")
BARE_SOLD_OUT = ("habis",)  # "Habis" polos: hanya dihitung bila berupa tombol (bukan lencana chip variasi)
BOTTOM_BAR_FRACTION = 0.85  # pusat elemen di bawah 85% tinggi layar = area tombol bawah
_PS_COMPETITORS = re.compile(r"io\.appium\S*|com\.github\.uiautomator\S*", re.I)


VARIANT_LINE_MATCH = rf"(?is){SP}*variasi\b.*"  # baris "Variasi: ..." di checkout (dibaca lapis 3)
_LOOSE_GAP = r"[\W_]*"  # tanda baca/spasi apa pun (Java & Python, ASCII)


def _loose_match(text: str | None) -> str | None:
    """Regex device: teks yang memuat `text` dengan tanda baca/spasi bebas di antara huruf & angka."""
    chars = pricing.squash(text or "")
    return rf"(?is).*{_LOOSE_GAP.join(re.escape(c) for c in chars)}.*" if chars else None


# Dialog sistem saat aplikasi crash / tidak merespons (ANR): jendela package "android" di atas Shopee, jadi
# dicari tanpa batas package (info_any). Teks Indonesia & Inggris.
SYSTEM_DIALOG_MATCH = (r"(?is)(?=.{1,120}$).*(tidak merespons|tidak menanggapi|isn.t responding|not responding|"
                       r"telah berhenti|terus berhenti|berhenti bekerja|has stopped|keeps stopping).*")
# Activity Shopee yang namanya menunjukkan verifikasi/captcha (bila tak ada elemen dikenal di layar).
VERIFY_ACTIVITY_RE = re.compile(r"captcha|verif|challenge|anti.?fraud|risk|security|otp", re.I)
# Dialog/aplikasi sistem yang dikenal: bukan verifikasi; di depan = Shopee keluar dari foreground -> UNKNOWN.
KNOWN_SYSTEM_PACKAGES = frozenset({
    "android", "com.android.systemui", "com.android.permissioncontroller", "com.google.android.permissioncontroller",
    "com.android.packageinstaller", "com.google.android.packageinstaller", "com.android.incallui",
    "com.android.server.telecom", "com.android.phone", "com.android.dialer", "com.google.android.dialer",
    "com.android.settings",
})
# keyboard, launcher (home), dan aplikasi sistem HiOS/Transsion (Phone Master, pop-up asisten, ...)
_SYSTEM_PACKAGE_RE = re.compile(r"inputmethod|keyboard|launcher|\.home$|^com\.transsion\.", re.I)
FOREGROUND_CACHE_S = 0.5  # aplikasi/activity di depan (adb dumpsys, mahal) dibaca ulang paling cepat tiap 0,5 s

DANGER = ("captcha", "verification", "pin")
MESSAGES = ("sold_out", "variant_required", "error_toast", "not_started")
# Query penanda di device: hanya teks pendek (bukan paragraf deskripsi produk) dan bukan "Habis" polos (lencana
# chip variasi lain). Elemen cocok PERTAMA yang dikembalikan, jadi teks yang diabaikan klien tidak boleh
# menutupi penanda sesungguhnya di belakangnya. "Habis" polos dicari terpisah (_bare_sold_out).
_MARKER_PREFIX = (rf"(?=[\s\S]{{1,{MARKER_MAX_CHARS}}}$)"
                  rf"(?!(?i:{SP}*(?:{'|'.join(map(re.escape, BARE_SOLD_OUT))}){SP}*)$)")


class Screen(StrEnum):
    """Status layar (spesifikasi F) + tiga sub-status: pesan di halaman produk (VARIANT_REQUIRED, ERROR_TOAST)
    dan LOADING (indikator loading tanpa teks dikenali; ditunggu, bukan dihitung UNKNOWN)."""

    PRODUCT_WAITING = "PRODUCT_WAITING"  # halaman produk, tombol Beli ada tetapi belum aktif
    PRODUCT_ACTIVE = "PRODUCT_ACTIVE"  # halaman produk, tombol Beli aktif
    VARIANT_SHEET = "VARIANT_SHEET"  # bottom sheet variasi/kuantitas
    CART = "CART"
    CHECKOUT = "CHECKOUT"
    PIN_SCREEN = "PIN_SCREEN"
    NOT_STARTED = "NOT_STARTED"
    SOLD_OUT = "SOLD_OUT"
    CAPTCHA = "CAPTCHA"
    VERIFICATION = "VERIFICATION"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    UNKNOWN = "UNKNOWN"  # termasuk: aplikasi keluar dari foreground, crash, dialog ANR
    VARIANT_REQUIRED = "VARIANT_REQUIRED"
    ERROR_TOAST = "ERROR_TOAST"
    LOADING = "LOADING"


PRODUCT = (Screen.PRODUCT_WAITING, Screen.PRODUCT_ACTIVE)


TERMINAL: dict[Screen, RunStatus] = {
    Screen.CAPTCHA: RunStatus.CAPTCHA,
    Screen.VERIFICATION: RunStatus.VERIFICATION,
    Screen.LOGIN_REQUIRED: RunStatus.LOGIN_REQUIRED,
    Screen.SOLD_OUT: RunStatus.SOLD_OUT,
}
_PROGRESS = (Screen.VARIANT_SHEET, Screen.CART, Screen.CHECKOUT)  # klik Beli ternyata sudah berhasil


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
        self.limits = pricing.Limits(cfg.max_item_price, cfg.max_total, cfg.expected_name, cfg.variant)
        self.sel = selectors
        self.log = log
        self.notifier = notifier
        self.before_place_order = before_place_order
        self.limiter = limiter or RateLimiter()
        self.stop_event = stop_event  # asyncio.Event / threading.Event
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
        self._arm_failure: tuple[RunStatus, str] | None = None
        self._tracking = False
        self._unknown_since: float | None = None
        self._blocked_price = _NO_PRICE
        self._price_blocked = False
        self._last_price: pricing.ProductPrice | None = None
        self._variant_ok: bool | None = None
        self._sticky: dict[str, Sel] = {}
        self._cand_cache: dict[str, list[Sel]] = {}
        self._union_cache: dict[tuple, Sel | None] = {}
        self._markers = {name: selectors.marker_re(name) for name in selectors.markers}
        self._ended = tuple(p for p in selectors.marker("sold_out") if "berakhir" in p.lower())
        self._guard_at = float("-inf")
        self._fg: tuple[float, AppInfo | None, str, bool] | None = None  # (waktu, app di depan, error, WebView)
        self._dialog_logged = ""
        self.hold_screen_on = False  # run: svc power stayon usb selama run (diset CLI), dikembalikan di close()
        self._stay_restore: str | None = None  # nilai stay_on_while_plugged_in sebelum run
        self.home_packages: set[str] = set()  # launcher device (dibaca saat precheck)
        self._recheck_variant = False
        self._variant_on_page = False
        self._variant_unreadable = False
        self.loading_limit_s = LOADING_LIMIT_S
        self._loading_since: float | None = None
        self._keepalive: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._alarmed: set[str] = set()
        self._agent_restarts = 0
        self._screen_h = 0
        self.device_info: dict[str, str] = {}

    # ------------------------------------------------------------------ waktu & sinyal

    @property
    def _clock(self) -> ServerClock:
        return self.clock or self.log.clock  # sebelum attempt: jam RunLog (ikut resync)

    def _mono(self) -> float:
        return self._clock.clock.monotonic()

    def _signal_stop(self) -> None:
        """Hentikan runner lain (STOP_ALL). asyncio.Event di-set lewat loop-nya (thread-safe)."""
        ev = self.stop_event
        if ev is None:
            return
        if isinstance(ev, asyncio.Event) and self._loop is not None and \
                threading.current_thread() is not threading.main_thread():
            self._loop.call_soon_threadsafe(ev.set)
        else:
            ev.set()

    def _alarm(self, event: str, message: str) -> None:
        if event in self._alarmed:
            return
        self._alarmed.add(event)
        self.notifier.alarm(event, message, platform=self.name)

    async def _in_thread(self, fn, *args):
        """Jalankan bagian sinkron di thread; bila dibatalkan (Ctrl+C), thread ikut berhenti di checkpoint."""
        self._loop = asyncio.get_running_loop()
        try:
            return await asyncio.to_thread(fn, *args)
        except asyncio.CancelledError:
            self._abort_reason = self._abort_reason or "dibatalkan"
            raise

    # ------------------------------------------------------------------ lifecycle

    async def prepare(self) -> None:
        if self.d is not None:
            return
        raw = self._raw_driver
        if raw is None:
            raw = await asyncio.to_thread(lambda: U2Driver(self.cfg.android.serial, package=self.package))
        self.d = TimedDriver(raw, self._mono, lambda: self._clock.now_ms())
        try:
            self._screen_h = (await asyncio.to_thread(self.d.window_size))[1]
        except DriverError as e:
            self.log.warn(f"ukuran layar tidak terbaca: {e}")
        implicit = getattr(raw, "no_implicit_restart", None)
        self.log.info(f"device {self.d.serial or '(pertama)'} terhubung, package {self.package}, tinggi layar "
                      f"{self._screen_h or '?'} px" + ("" if implicit is None else
                                                      f", restart implisit u2 {'mati' if implicit else 'AKTIF'}"))

    async def abort(self, reason: str = "") -> None:
        self._abort_reason = reason or "dibatalkan"

    async def close(self) -> None:
        """Tidak menutup aplikasi apa pun; hanya menghentikan cek agent dan mengembalikan pengaturan layar."""
        self._stop_keepalive()
        if self._stay_restore is not None:
            await asyncio.to_thread(self._restore_stay_awake)

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

    def _find(self, step: str, sticky: bool = True) -> Node | None:
        """Kandidat pertama yang menemukan elemen (info, jalur A). Kandidat yang terakhir berhasil dicoba dulu."""
        cands = self._cands(step)
        hit = self._sticky.get(step) if sticky else None
        order = [hit, *[c for c in cands if c != hit]] if hit in cands else cands
        for c in order:
            node = self.d.info(c)
            if node is not None:
                if sticky:
                    self._sticky[step] = c
                return node
        return None

    def _local(self, step: str, nodes: list[Node]) -> Node | None:
        """Cocokkan kandidat ke node hasil query (tanpa RPC); kandidat pertama yang cocok menang."""
        for c in self._cands(step):
            if c.by == "className":
                continue
            for n in nodes:
                if node_matches(n, c):
                    return n
        return None

    def _pick(self, step: str, nodes: list[Node]) -> Node | None:
        """Seperti _local, tetapi bila beberapa node cocok pilih yang paling bawah di layar (tombol bawah /
        elemen sheet yang menutupi halaman di belakangnya)."""
        for c in self._cands(step):
            if c.by == "className":
                continue
            hits = [n for n in nodes if node_matches(n, c)]
            if hits:
                return max(enumerate(hits), key=lambda t: (t[1].bounds[3], t[0]))[1]
        return None

    def _patterns(self, steps: tuple[str, ...] = (), markers: tuple[str, ...] = ()) -> list[str]:
        pats = [r for step in steps for c in self._cands(step) if (r := text_regex(c)) is not None]
        return pats + [p for name in markers for p in self.sel.marker(name)]

    def _union(self, steps: tuple[str, ...] = (), markers: tuple[str, ...] = (), extra: tuple[str, ...] = (),
               short: bool = False, by: str = "textMatches") -> Sel | None:
        """Satu selector regex gabungan. short=True: query penanda (teks pendek, bukan "Habis" polos)."""
        key = (steps, markers, extra, short, by)
        if key not in self._union_cache:
            pats = [*self._patterns(steps, markers), *extra]
            rx = union(pats) if pats else None
            if rx is not None and short:
                rx = f"{_MARKER_PREFIX}(?:{rx})"
            self._union_cache[key] = Sel(by, rx) if rx is not None else None
        return self._union_cache[key]

    def _rid_cands(self, *steps: str) -> list[Sel]:
        return [c for s in steps for c in self._cands(s) if c.by in ("resourceId", "resourceIdMatches")]

    def _first(self, steps: tuple[str, ...] = (), markers: tuple[str, ...] = ()) -> Node | None:
        """Elemen pertama yang cocok salah satu pola (1 query info). Penanda: hanya teks pendek."""
        sel = self._union(steps, markers, short=bool(markers) and not steps)
        return self.d.info(sel) if sel is not None else None

    def _danger(self, buy: Node | None, context: str, by: str = "textMatches") -> Seen | None:
        """Captcha / verifikasi / PIN / "Flash Sale telah berakhir" dalam 1 query (teks pendek saja).
        Banner berakhir yang kebetulan lebih awal di pohon tidak boleh menutupi captcha: dicek ulang."""
        def query(sel: Sel | None) -> Node | None:
            n = self.d.info(sel)
            # node cocok lewat content-desc: nilai penanda dari desc (label = text bila text tidak kosong)
            return replace(n, text=n.desc) if n is not None and by != "textMatches" else n

        n = query(self._union(markers=DANGER, extra=self._ended, short=True, by=by))
        if n is None:
            return None
        seen = self._message_screen(n, buy, context)
        if seen is not None and seen.screen == Screen.SOLD_OUT:
            n2 = query(self._union(markers=DANGER, short=True, by=by))
            if n2 is not None and (seen2 := self._message_screen(n2, buy, context)) is not None:
                return seen2
        return seen

    def _bare_sold_out(self, context: str) -> Seen | None:
        """Tombol "Habis" polos (bukan lencana chip variasi lain). exists dulu (murah); bila ada, find_all menilai
        semua kecocokan."""
        sel = Sel("textMatches", rf"(?i){SP}*(?:{'|'.join(map(re.escape, BARE_SOLD_OUT))}){SP}*")
        if not self.d.exists(sel):
            return None
        for n in self.d.find_all(sel):
            if self._sold_counts(n, None, context):
                return Seen(Screen.SOLD_OUT, f"tombol {n.label!r}", n)
        return None

    def _has(self, *steps: str) -> Node | None:
        """Ada elemen langkah ini? Kandidat teks digabung jadi satu info; resourceId hasil kalibrasi dicek terpisah."""
        for c in self._rid_cands(*steps):
            if (n := self.d.info(c)) is not None:
                return n
        return self._first(steps)

    def _rid_regex(self, step: str) -> re.Pattern | None:
        pats = [re.escape(c.value) if c.by == "resourceId" else c.value for c in self._rid_cands(step)]
        return re.compile("|".join(f"(?:{p})" for p in pats)) if pats else None

    def _marker_of(self, node: Node, *names: str) -> str | None:
        if len(node.label) > MARKER_MAX_CHARS:
            return None
        for name in names:
            rx = self._markers.get(name)
            if rx is not None and rx.fullmatch(node.label):
                return name
        return None

    def _marker(self, name: str, nodes: list[Node]) -> Node | None:
        return next((n for n in nodes if self._marker_of(n, name)), None)

    def _snapshot(self) -> list[Node]:
        """Semua node bertulisan di jendela aktif (mahal: hanya precheck/keranjang/diagnosa, bukan hot path)."""
        return self.d.find_all(Sel("textMatches", ANY_TEXT_MATCH))

    # ------------------------------------------------------------------ klasifikasi layar

    def _sold_counts(self, sold: Node, buy: Node | None, context: str) -> bool:
        """"Habis" polos hanya berarti habis bila berupa tombol (bisa diklik / di bar bawah), bukan lencana chip
        variasi lain. Frasa kuat ("Stok habis", "Flash Sale telah berakhir") cukup, kecuali di halaman produk
        yang tombol Beli-nya masih ada (bisa milik variasi lain)."""
        if sold.label.strip().lower() in BARE_SOLD_OUT:
            bottom = self._screen_h and sold.center[1] >= BOTTOM_BAR_FRACTION * self._screen_h
            return sold.clickable or bool(bottom)
        return context != "product" or buy is None or "berakhir" in sold.label.lower()

    def _message_screen(self, node: Node, buy: Node | None, context: str) -> Seen | None:
        name = self._marker_of(node, "captcha", "verification", "pin", "sold_out", "variant_required",
                               "error_toast", "not_started")
        if name is None:
            return None
        screen = {"captcha": Screen.CAPTCHA, "verification": Screen.VERIFICATION, "pin": Screen.PIN_SCREEN,
                  "sold_out": Screen.SOLD_OUT, "variant_required": Screen.VARIANT_REQUIRED,
                  "error_toast": Screen.ERROR_TOAST, "not_started": Screen.NOT_STARTED}[name]
        if screen == Screen.SOLD_OUT and not self._sold_counts(node, buy, context):
            return None
        return Seen(screen, f"teks {node.label[:60]!r}", node)

    def _classify_nodes(self, nodes: list[Node], context: str) -> Seen:
        """Klasifikasi dari node yang sudah dibaca (hasil find_all bacaan langkah maju)."""
        buy = self._local("buy_button", nodes)
        for n in nodes:
            if self._marker_of(n, "captcha", "verification"):
                return self._message_screen(n, buy, context)
        if n := self._marker("pin", nodes):
            return Seen(Screen.PIN_SCREEN, f"teks {n.label[:40]!r}", n)
        if n := self._local("place_order", nodes):
            return Seen(Screen.CHECKOUT, "tombol Buat Pesanan", n)
        if n := self._local("cart_marker", nodes):
            return Seen(Screen.CART, f"teks {n.label[:30]!r}", n)
        for n in nodes:
            seen = self._message_screen(n, buy, context)
            if seen is not None and seen.screen != Screen.NOT_STARTED:
                return seen
        if n := self._local("sheet_marker", nodes):
            return Seen(Screen.VARIANT_SHEET, f"teks {n.label[:30]!r}", n)
        if buy is not None:
            return self._product(buy)
        if n := self._marker("not_started", nodes):
            return Seen(Screen.NOT_STARTED, f"teks {n.label[:60]!r}", n)
        if n := self._marker("login", nodes):
            return Seen(Screen.LOGIN_REQUIRED, f"teks {n.label[:40]!r}", n)
        return Seen(Screen.UNKNOWN, f"{len(nodes)} teks tak dikenali")

    def _toast_screen(self, context: str) -> Seen | None:
        """Toast Android (jendela terpisah, tidak ada di pohon node) lewat getLastToast."""
        text = self.d.last_toast()
        if not text:
            return None
        seen = self._message_screen(Node(text=text), None, context)
        if seen is not None:
            seen.evidence = f"toast {text[:60]!r}"
        return seen

    def _classify(self, context: str = "any") -> Seen:
        """Rantai query berprioritas (info/exists, masing-masing satu pencarian), berhenti di yang pertama cocok."""
        t_obs = self._mono()  # waktu layar diamati (sebelum diagnosa lambat)
        if (seen := self._danger(None, context)) is not None:
            return self._observe(seen, t_obs)
        if context == "after_order" and (n := self._has("pin_screen")) is not None:
            return self._observe(Seen(Screen.PIN_SCREEN, f"resourceId {n.rid}", n), t_obs)
        anchor = self._anchor()
        if anchor is not None and anchor.screen in (Screen.CHECKOUT, Screen.CART):
            return self._observe(anchor, t_obs)
        buy = self._find("buy_button") if anchor is None else None
        msg = self._first(markers=MESSAGES)
        msg_seen = self._message_screen(msg, buy, context) if msg is not None else None
        # pesan (toast/dialog) diprioritaskan di atas SHEET: sheet yang tetap terbuka dengan "Silakan pilih
        # variasi"/"Stok habis"/"belum dimulai" harus terbaca sebagai pesan itu
        if msg_seen is not None and msg_seen.screen != Screen.NOT_STARTED:
            return self._observe(msg_seen, t_obs)
        if context in ("after_buy", "after_order") and (toast := self._toast_screen(context)) is not None:
            return self._observe(toast, t_obs)
        if anchor is not None:  # SHEET
            if msg_seen is not None and context == "after_buy":
                return self._observe(msg_seen, t_obs)
            return self._observe(anchor, t_obs)
        if context == "after_buy" and msg_seen is not None:  # "belum dimulai" di halaman produk
            return self._observe(msg_seen, t_obs)
        if buy is not None:
            return self._observe(self._product(buy), t_obs)
        if msg_seen is not None:  # tombol "Ingatkan Saya" / hitung mundur: belum mulai, bukan tak dikenal
            return self._observe(msg_seen, t_obs)
        if (seen := self._danger(None, context, by="descriptionMatches")) is not None:  # mis. captcha WebView
            seen.evidence = f"content-desc {seen.evidence}"
            return self._observe(seen, t_obs)
        if (n := self._first(markers=("login",))) is not None:
            return self._observe(Seen(Screen.LOGIN_REQUIRED, f"teks {n.label[:40]!r}", n), t_obs)
        if (seen := self._bare_sold_out(context)) is not None:
            return self._observe(seen, t_obs)
        if self.d.exists(Sel("className", "android.widget.ProgressBar")):
            return self._observe(Seen(Screen.LOADING, "indikator loading"), t_obs)
        if context != "after_order" and (n := self._has("pin_screen")) is not None:  # PIN tanpa teks
            return self._observe(Seen(Screen.PIN_SCREEN, f"resourceId {n.rid}", n), t_obs)
        if (seen := self._foreground_screen()) is not None:
            return self._observe(seen, t_obs)
        return self._observe(Seen(Screen.UNKNOWN, "tidak ada elemen yang dikenali"), t_obs)

    def _product(self, buy: Node) -> Seen:
        """PRODUCT_ACTIVE: tombol Beli aktif (bukan 'Ingatkan Saya'); selain itu PRODUCT_WAITING."""
        active = buy.enabled and not self._marker_of(buy, "not_started")
        return Seen(Screen.PRODUCT_ACTIVE if active else Screen.PRODUCT_WAITING, "tombol Beli", buy)

    def _foreground(self) -> tuple[AppInfo | None, str, bool]:
        """(aplikasi/activity di depan, error, ada WebView Shopee). Cache FOREGROUND_CACHE_S: adb dumpsys mahal;
        hanya dipanggil di ujung rantai klasifikasi (layar tak dikenali), bukan di hot path."""
        now = self._mono()
        if self._fg is None or now - self._fg[0] >= FOREGROUND_CACHE_S:
            try:
                app = self.d.current_app()
                self._fg = (now, app, "", app.package == self.package and self.d.webview_present())
            except DriverError as e:
                self._fg = (now, None, str(e), False)
        return self._fg[1], self._fg[2], self._fg[3]

    def _foreground_screen(self) -> Seen | None:
        """Tidak ada elemen Shopee yang dikenali: putuskan dari package/activity di depan.

        - Shopee + activity bernama verifikasi/captcha, atau WebView tanpa elemen dikenal -> CAPTCHA/VERIFICATION
        - launcher, dialog/aplikasi sistem yang dikenal (crash, ANR, telepon, keyboard, Phone Master), atau tidak
          terbaca -> UNKNOWN (Shopee keluar dari foreground; jaring 1,5 s)
        - aplikasi/activity asing lain -> VERIFICATION (mis. verifikasi di browser/Play Services)
        """
        app, err, webview = self._foreground()
        if app is None or not app.package:
            return Seen(Screen.UNKNOWN, f"aplikasi di depan tidak terbaca ({err or 'kosong'})")
        where = f"{app.package}/{app.activity}"
        if app.package == self.package:
            if VERIFY_ACTIVITY_RE.search(app.activity):
                screen = Screen.CAPTCHA if "captcha" in app.activity.lower() else Screen.VERIFICATION
                return Seen(screen, f"activity verifikasi {where}")
            if webview:
                return Seen(Screen.VERIFICATION, f"WebView tanpa elemen Shopee yang dikenal ({where}); "
                                                 "kemungkinan halaman verifikasi")
            return None
        if app.package in KNOWN_SYSTEM_PACKAGES or app.package in self.home_packages or \
                _SYSTEM_PACKAGE_RE.search(app.package):
            return Seen(Screen.UNKNOWN, f"Shopee keluar dari foreground: {where}")
        return Seen(Screen.VERIFICATION, f"aplikasi/activity asing di depan: {where}")

    def _system_dialog(self) -> Seen | None:
        """Dialog crash/ANR (jendela sistem) di atas Shopee: elemen Shopee masih terbaca di bawahnya, jadi tap
        akan mengenai dialog ('Tutup aplikasi'). -> UNKNOWN (tanpa klik)."""
        n = self.d.info_any(Sel("textMatches", SYSTEM_DIALOG_MATCH))
        return Seen(Screen.UNKNOWN, f"dialog sistem {n.label[:60]!r} (crash/ANR)", n) if n is not None else None

    def _anchor(self) -> Seen | None:
        """CHECKOUT / CART / SHEET dalam satu query gabungan (layar-layar ini tidak tampil bersamaan)."""
        steps = (("place_order", Screen.CHECKOUT), ("cart_marker", Screen.CART), ("sheet_marker", Screen.VARIANT_SHEET))
        for step, screen in steps:
            for c in self._rid_cands(step):
                if (n := self.d.info(c)) is not None:
                    return Seen(screen, f"resourceId {c.value}", n)
        n = self._first(tuple(step for step, _ in steps))
        if n is None:
            return None
        for step, screen in steps:
            if self._local(step, [n]) is not None:
                return Seen(screen, "tombol Buat Pesanan" if screen == Screen.CHECKOUT else f"teks {n.label[:30]!r}",
                            n)
        return None

    def _diagnose_unknown(self, seen: Seen) -> str:
        """Diagnosa mahal (adb dumpsys, content-desc, WebView) hanya saat akan memutuskan UNKNOWN_STATE."""
        parts = [seen.evidence]
        try:
            app = self.d.current_app()
            parts.append(f"aplikasi aktif {app.package}/{app.activity}")
            if app.package and app.package != self.package:
                parts.append("(aplikasi lain di depan)")
            if self.d.webview_present():
                parts.append("WebView")
            descs = self.d.find_all(Sel("descriptionMatches", ANY_TEXT_MATCH))
            if descs:
                parts.append("content-desc: " + "; ".join(n.desc[:30] for n in descs[:5]))
        except DriverError as e:
            parts.append(f"diagnosa gagal: {e}")
        return ", ".join(p for p in parts if p)

    def _observe(self, seen: Seen, t_obs: float | None = None) -> Seen:
        """Jaring pengaman: UNKNOWN berturut-turut > unknown_limit_s -> UNKNOWN_STATE (diukur dari waktu
        pengamatan, bukan setelah diagnosa)."""
        if not self._tracking:
            return seen
        now = self._mono() if t_obs is None else t_obs
        if seen.screen != Screen.LOADING:
            self._loading_since = None
        elif self._loading_since is None:
            self._loading_since = now
        elif now - self._loading_since > self.loading_limit_s:  # server lambat ditunggu, tetapi tidak selamanya
            raise _Stop(RunStatus.UNKNOWN_STATE,
                        f"indikator loading > {self.loading_limit_s:.0f} s ({self._diagnose_unknown(seen)})")
        if seen.screen != Screen.UNKNOWN:
            self._unknown_since = None
        elif self._unknown_since is None:
            self._unknown_since = now
        elif now - self._unknown_since > self.unknown_limit_s:
            raise _Stop(RunStatus.UNKNOWN_STATE,
                        f"layar tidak dikenali > {self.unknown_limit_s:.1f} s ({self._diagnose_unknown(seen)})")
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
        return await self._in_thread(self._precheck_guarded)

    def _shell(self, *cmd: str) -> str:
        try:
            return self.d.shell(list(cmd))
        except DriverError as e:
            self.log.warn(f"shell {' '.join(cmd)} gagal: {e}")
            return ""

    def _precheck_guarded(self) -> PrecheckResult:
        res = PrecheckResult(self.name)
        try:
            self._precheck_sync(res)
        except _Aborted as e:
            res.status = RunStatus.ABORTED
            res.items.append(PrecheckItem("precheck", False, f"dibatalkan: {e}"))
        except _Stop as e:
            res.status = e.status
            res.items.append(PrecheckItem("precheck", False, f"{e.status}: {e.message}"))
        except DriverError as e:
            res.status = RunStatus.ERROR
            res.items.append(PrecheckItem("device", False, f"koneksi device/agent gagal: {e}"))
        if res.status in STOP_ALL_STATUSES:
            self._signal_stop()
        if self.d is not None:
            self._write_latency("precheck")
        return res

    def _precheck_sync(self, res: PrecheckResult) -> None:
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

        # agent uiautomator2 (HiOS bisa membunuhnya): hidup (/ping + RPC) DAN responsif (3 query < 1 s)
        ok, why = self._agent_healthy()
        if ok:
            add(PrecheckItem("agent uiautomator2", True, why))
        else:
            self.log.warn(f"agent uiautomator2 {why}; menghidupkan ulang")
            try:
                self.d.restart_agent()
                ok, why2 = self._agent_healthy()
            except DriverError as e:
                ok, why2 = False, str(e)
            add(PrecheckItem("agent uiautomator2", None if ok else False,
                             f"{why}, dihidupkan ulang -> " + (f"{why2}; kemungkinan dibunuh HiOS: matikan optimasi "
                                                               "baterai & kunci aplikasi ATX/uiautomator di recent apps"
                                                               if ok else f"tetap gagal ({why2})")))
            if not ok:
                res.status = RunStatus.ERROR
                return

        # klien UiAutomation lain (Appium, atx lama, ...) bisa merebut/merusak sesi agent
        rivals = sorted({m.group(0) for m in _PS_COMPETITORS.finditer(self._shell("ps", "-A"))})
        if rivals:
            add(PrecheckItem("klien UiAutomation lain", None, f"terdeteksi {', '.join(rivals)} - tutup Appium/"
                                                               "scrcpy/PC suite lain sebelum run"))

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
        add(self._stay_awake())

        # aplikasi Shopee
        path = self._shell("pm", "path", self.package)
        if "package:" not in path:
            add(PrecheckItem("aplikasi Shopee", False, f"{self.package} tidak terpasang"))
            res.status = RunStatus.ERROR
            return
        ver = re.search(r"versionName=(\S+)", self._shell("dumpsys", "package", self.package))
        self.device_info["versionName"] = ver.group(1) if ver else ""
        add(PrecheckItem("aplikasi Shopee", True, f"{self.package} {ver.group(1) if ver else '(versi ?)'}"))
        add(self._calibration_match(ver.group(1) if ver else "", size))
        home = self._shell("cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN",
                           "-c", "android.intent.category.HOME").strip().splitlines()
        if home and "/" in home[-1]:
            self.home_packages.add(home[-1].split("/")[0].strip())

        # halaman produk: login, tombol Beli, harga, latensi query
        try:
            self.d.start_url(self.cfg.product_url, self.package)
        except DriverError as e:
            add(PrecheckItem("buka produk", False, f"intent VIEW gagal: {e}"))
            return
        seen = self._wait_screen({*PRODUCT, Screen.NOT_STARTED, Screen.VARIANT_SHEET}, self.open_timeout_s, "product")
        if seen.screen == Screen.LOGIN_REQUIRED:
            add(PrecheckItem("login", False, f"aplikasi minta login ({seen.evidence})"))
            res.status = RunStatus.LOGIN_REQUIRED
            return
        if seen.screen in (Screen.CAPTCHA, Screen.VERIFICATION):
            add(PrecheckItem("buka produk", False, f"{seen.screen}: {seen.evidence} - selesaikan manual"))
            res.status = TERMINAL[seen.screen]
            return
        app = self.d.current_app()
        if app.package != self.package:
            add(PrecheckItem("buka produk", False, f"intent membuka {app.package}, bukan {self.package}"))
        else:
            add(PrecheckItem("login", True, "halaman produk terbuka tanpa diminta login"))
        buy = self._find("buy_button")
        add(PrecheckItem("tombol Beli", True if buy else None,
                         f"ditemukan ({buy.label!r}, enabled={buy.enabled})" if buy else
                         "tidak ditemukan (sebelum flash sale bisa 'Ingatkan Saya') - cek/kalibrasi"))
        price = pricing.check_product_price(self._read_price(), self.limits)
        add(PrecheckItem("harga produk", True if price.verdict != "unreadable" else None,
                         price.describe(self.limits) + " (sebelum flash sale bisa harga normal)"))
        add(self._latency_probe())

        # alamat & saldo (dibaca dari UI). Tidak terbaca / kurang -> alarm saja, run tidak dihentikan.
        # Captcha/verifikasi di halaman ini = STOP (tidak ditimpa intent berikutnya).
        add(self._read_page("alamat default", "address_page",
                            lambda t: (True, "alamat 'Utama' ditemukan") if re.search(r"\butama\b", t, re.I) else
                            (False, "belum ada alamat tersimpan") if re.search(r"belum ada alamat", t, re.I) else
                            (None, "tidak terbaca dari halaman alamat")))

        def wallet(t: str):
            m = re.search(r"saldo[^\n]*\n?[^\n]*?(Rp\s?[\d.]+)", t, re.I)
            v = pricing.parse_price(m.group(1)) if m else None
            if v is None:
                return None, "saldo tidak terbaca"
            # Sebelum T halaman bisa menampilkan harga normal; alat tidak membayar > max_item_price per unit.
            need = min(price.value or self.limits.max_item_price, self.limits.max_item_price)
            if v < need:
                return False, f"saldo {pricing.rupiah(v)} < harga {pricing.rupiah(need)}"
            if v < self.limits.max_total:
                return None, (f"saldo {pricing.rupiah(v)} >= harga {pricing.rupiah(need)} tetapi < max_total "
                              f"{pricing.rupiah(self.limits.max_total)} (ongkir/biaya bisa membuatnya kurang)")
            return True, f"saldo {pricing.rupiah(v)} >= max_total {pricing.rupiah(self.limits.max_total)}"

        add(self._read_page("saldo ShopeePay", "wallet_page", wallet))
        try:
            self.d.start_url(self.cfg.product_url, self.package)  # kembali ke halaman produk
        except DriverError as e:
            self.log.warn(f"kembali ke halaman produk gagal: {e}")

    def _calibration_match(self, version: str, size: str) -> PrecheckItem:
        """Versi Shopee & resolusi saat kalibrasi vs sekarang. Versi berbeda = PERINGATAN KERAS (+ alarm):
        teks/resourceId bisa berubah antarversi."""
        cal = self.sel.calibrated or {}
        cal_ver, cal_size = cal.get("app_version", ""), cal.get("wm_size", "")
        if not cal_ver:
            return PrecheckItem("versi vs kalibrasi", None, "belum ada kalibrasi Android (memakai default teks); "
                                                          "jalankan calibrate --platform android")
        if version and version != cal_ver:
            msg = (f"PERINGATAN KERAS: versi Shopee {version} berbeda dari saat kalibrasi ({cal_ver}); selector bisa "
                   "tidak cocok - kalibrasi ulang lalu dry-run, atau matikan auto-update Shopee")
            self.log.warn(msg)
            self._alarm("precheck_versi", msg)
            return PrecheckItem("versi vs kalibrasi", None, msg)
        if cal_size and size and cal_size != size:
            return PrecheckItem("versi vs kalibrasi", None, f"versi sama ({cal_ver}), tetapi resolusi berubah: "
                                                          f"kalibrasi {cal_size!r}, sekarang {size!r}")
        return PrecheckItem("versi vs kalibrasi", True if version else None,
                            f"versi {version or '?'} = kalibrasi {cal_ver}" + (f", {cal_size}" if cal_size else ""))

    def _stay_awake(self) -> PrecheckItem:
        """Run: `svc power stayon usb` (layar tidak mati selama kabel USB terpasang); nilai lama dikembalikan di
        close(). Perintah precheck saja: hanya dilaporkan."""
        stay = self._shell("settings", "get", "global", "stay_on_while_plugged_in").strip()
        timeout = self._shell("settings", "get", "system", "screen_off_timeout").strip()
        usb = stay.isdigit() and int(stay) & 2
        if usb:
            return PrecheckItem("layar tetap menyala", True, f"stay_on_while_plugged_in={stay} (USB sudah termasuk)")
        if not self.hold_screen_on:
            short = not timeout.isdigit() or int(timeout) < 15 * 60_000
            return PrecheckItem("layar tetap menyala", None if short else True,
                                f"stay_on_while_plugged_in={stay or '?'}, timeout layar {timeout or '?'} ms; saat run "
                                "alat memasang `svc power stayon usb` dan mengembalikannya setelah selesai")
        self._shell("svc", "power", "stayon", "usb")
        now = self._shell("settings", "get", "global", "stay_on_while_plugged_in").strip()
        if not (now.isdigit() and int(now) & 2):
            return PrecheckItem("layar tetap menyala", None,
                                f"`svc power stayon usb` tidak berefek (nilai {now or '?'}); "
                                "aktifkan Opsi Pengembang > Tetap aktif")
        self._stay_restore = stay if stay.isdigit() else "0"
        self.log.info(f"svc power stayon usb (stay_on_while_plugged_in {stay or '?'} -> {now}); "
                      "dikembalikan setelah run")
        return PrecheckItem("layar tetap menyala", True, f"svc power stayon usb selama run (semula {stay or '?'})")

    def _restore_stay_awake(self) -> None:
        prev, self._stay_restore = self._stay_restore, None
        if prev is None or self.d is None:
            return
        cmd = ("svc", "power", "stayon", "false") if prev == "0" else \
            ("settings", "put", "global", "stay_on_while_plugged_in", prev)
        self._shell(*cmd)
        self.log.info(f"layar tetap menyala dikembalikan: {' '.join(cmd)}")

    def _agent_healthy(self) -> tuple[bool, str]:
        """Agent hidup dan menjawab AGENT_PING_QUERIES query berturut-turut, masing-masing < AGENT_PING_MAX_S
        (agent yang dibunuh/dibekukan HiOS bisa masih menerima koneksi tetapi lambat)."""
        alive = self.d.agent_alive()
        if alive is False:
            return False, "mati"
        times = []
        for _ in range(AGENT_PING_QUERIES):
            t0 = self._mono()
            try:
                self.d.exists(Sel("text", "__flashbuy_ping__"))
            except DriverError as e:
                return False, f"tidak menjawab ({e})"
            times.append(self._mono() - t0)
            if times[-1] >= AGENT_PING_MAX_S:
                return False, f"lambat ({times[-1] * 1000:.0f} ms >= {AGENT_PING_MAX_S * 1000:.0f} ms)"
        detail = f"menjawab {len(times)} query: " + ", ".join(f"{t * 1000:.0f}" for t in times) + " ms"
        return True, detail + ("" if alive else " (status /ping tidak bisa dicek)")

    def _latency_probe(self) -> PrecheckItem:
        """Latensi query hot path (exists/info) dan bacaan find_all (lebih mahal di device)."""
        start = len(self.d.stats.samples)
        for _ in range(PRECHECK_PROBES):
            self.d.exists(Sel("text", "__flashbuy_probe__"))
        for c in self._cands("buy_button")[:1]:
            for _ in range(3):
                self.d.info(c)
        hot = sorted(s.ms for s in self.d.stats.samples[start:])
        mark = len(self.d.stats.samples)
        self.d.find_all(Sel("textMatches", RP_ANY_MATCH))
        fa = [s.ms for s in self.d.stats.samples[mark:]]
        p95 = hot[min(len(hot) - 1, int(round(0.95 * (len(hot) - 1))))]
        detail = (f"info/exists median {hot[len(hot) // 2]:.0f} ms, p95 {p95:.0f} ms, maks {hot[-1]:.0f} ms "
                  f"({len(hot)} query); find_all {fa[0]:.0f} ms")
        self.log.info(f"latensi query precheck: {detail}")
        if p95 > QUERY_WARN_MS or fa[0] > FIND_ALL_WARN_MS:
            return PrecheckItem("latensi query", None, f"{detail} > target {QUERY_WARN_MS:.0f}/{FIND_ALL_WARN_MS:.0f}"
                                                       " ms - pakai kabel/port USB lain, tutup aplikasi lain")
        return PrecheckItem("latensi query", True, detail)

    def _read_page(self, name: str, url_key: str, judge) -> PrecheckItem:
        item = self._read_page_raw(name, url_key, judge)
        if item.ok is not True:  # spesifikasi G: hanya alarm (PERINGATAN), tidak menghentikan run
            self._alarm(f"precheck_{name}", f"pre-check {name}: {item.detail}")
            item = PrecheckItem(item.name, None, f"ALARM: {item.detail}")
        return item

    def _read_page_raw(self, name: str, url_key: str, judge) -> PrecheckItem:
        url = self.sel.urls.get(url_key)
        if not url:
            return PrecheckItem(name, None, "URL halaman tidak diset")
        try:
            self.d.start_url(url, self.package)
        except DriverError as e:
            return PrecheckItem(name, None, f"halaman tidak bisa dibuka di aplikasi ({e})")
        end = self._mono() + 5.0
        text = ""
        while True:
            nodes = self._snapshot()
            seen = self._classify_nodes(nodes, "any")
            if seen.screen in (Screen.CAPTCHA, Screen.VERIFICATION, Screen.LOGIN_REQUIRED):
                raise _Stop(TERMINAL[seen.screen], f"{seen.screen} di halaman {url_key}: {seen.evidence} - "
                                                   "selesaikan manual")
            if seen.screen == Screen.PIN_SCREEN:  # halaman saldo minta PIN: alat tidak mengetik PIN
                return PrecheckItem(name, None, "halaman meminta PIN; tidak dibaca (PIN tidak diketik alat)")
            text = "\n".join(n.label for n in sorted(nodes, key=lambda n: (n.bounds[1], n.bounds[0])))
            ok, detail = judge(text)
            if ok is not None or self._mono() >= end:
                return PrecheckItem(name, ok, detail)

    # ------------------------------------------------------------------ arm (T-60 s)

    async def arm(self, open_at: float) -> None:
        self.open_at = open_at
        await self._in_thread(self._arm_guarded)
        if self._arm_state is None and self._arm_failure is None:
            self._keepalive = asyncio.ensure_future(self._keepalive_loop())

    def _arm_guarded(self) -> None:
        try:
            self._arm_sync()
        except _Aborted as e:
            self._arm_failure = (RunStatus.ABORTED, str(e))
        except _Stop as e:
            self._arm_failure = (e.status, e.message)
        except DriverError as e:
            self._arm_failure = (RunStatus.ERROR, f"saat membuka produk (T-60 s): {e}")
        if self._arm_failure is not None:
            self.log.warn(f"arm gagal: {self._arm_failure[0]} {self._arm_failure[1]}")
            self._alarm(str(self._arm_failure[0]), self._arm_failure[1])

    def _arm_sync(self) -> None:
        self._ensure_agent("arm")
        self.d.start_url(self.cfg.product_url, self.package)
        self.log.mark("page_open")
        seen = self._wait_screen({*PRODUCT, Screen.NOT_STARTED, Screen.VARIANT_SHEET}, self.open_timeout_s, "product")
        if seen.screen in (Screen.LOGIN_REQUIRED, Screen.CAPTCHA, Screen.VERIFICATION):
            self._arm_state = seen
            self.log.warn(f"arm: {seen.screen} ({seen.evidence})")
            # langsung: alarm + hentikan runner lain (jangan tunggu sampai T-lead)
            if TERMINAL[seen.screen] in STOP_ALL_STATUSES:
                self._signal_stop()
            self._alarm(str(TERMINAL[seen.screen]), f"saat membuka produk (T-60 s): {seen.evidence}")
            return
        if seen.screen not in (*PRODUCT, Screen.NOT_STARTED):
            self.log.warn(f"arm: halaman produk belum terdeteksi ({seen.screen} {seen.evidence}); "
                          "dicari lagi saat polling")
        if seen.screen in PRODUCT:
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
        """Pilih variasi lebih awal (T-60 s) bila opsinya tampil langsung di halaman produk. Saat polling hanya
        dipilih ulang setelah reload yang melepasnya (_reselect_variant, lewat gate); selain itu variasi dipilih
        di bottom sheet setelah klik Beli."""
        if not self.variant:
            return
        opt = self._find("variant_option", sticky=False)
        if opt is None:
            self.log.info(f"variasi {self.variant!r} belum tampil di halaman produk; dipilih di bottom sheet")
            return
        if opt.selected or opt.checked:
            self._variant_ok = self._variant_on_page = True
            return
        if not opt.enabled:
            self.log.warn(f"variasi {self.variant!r} belum bisa dipilih")
            return
        self.d.click(opt)
        self.log.mark("variant_selected", self.variant)
        self._variant_ok = True
        if self._has("sheet_marker") is not None:  # opsi membuka bottom sheet -> tutup, pilih ulang setelah Beli
            self.d.press_back()
            self.log.info("pilih variasi membuka bottom sheet; ditutup, dipilih ulang setelah klik Beli")
            return
        # dicek ulang setelah tiap reload saat polling. Status terpilih chip tak terbaca: dipilih ulang hanya
        # setelah reload intent (lihat _variant_after_reload)
        self._variant_on_page = True
        end = self._mono() + VARIANT_VERIFY_S
        readable = False
        while not readable and self._mono() < end:
            chip = self._find("variant_option", sticky=False)
            readable = chip is not None and (chip.selected or chip.checked)
        self._variant_unreadable = not readable
        if not readable:
            self.log.info(f"status terpilih variasi {self.variant!r} tidak terbaca; setelah reload intent dipilih "
                          "ulang sekali, setelah swipe tidak")

    # ------------------------------------------------------------------ attempt

    async def attempt(self, clock: ServerClock, live: bool) -> RunResult:
        self.clock = clock
        self._stop_keepalive()
        if live:
            try:
                require_live_ready(self.cfg)
            except ConfigError as e:
                return await self._in_thread(self._finish, RunStatus.ERROR, str(e), live)
        return await self._in_thread(self._attempt_wrapped, live)

    def _attempt_wrapped(self, live: bool) -> RunResult:
        self._tracking = True
        self._unknown_since = self._loading_since = None
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
        # hasil arm dulu: arm sendiri yang men-set stop_event bersama saat captcha/verifikasi
        if self._arm_failure is not None:
            return self._arm_failure
        if self._arm_state is not None:
            return TERMINAL[self._arm_state.screen], f"saat membuka produk: {self._arm_state.evidence}"
        self._checkpoint()
        clock = self.clock
        gate = PollingGate(clock, self.open_at, self.limiter)
        self.log.mark("poll_start")

        clicks = 0
        last_reload = None
        outcome = Seen(Screen.PRODUCT_WAITING)
        while True:
            self._checkpoint()
            kind, info = self._wait_ready(gate, last_reload, clicks)
            if kind == "stop":
                return TERMINAL[info.screen], info.evidence
            if kind == "expired":
                return self._window_closed(clicks)
            if kind == "progress" and info.screen in (Screen.CART, Screen.CHECKOUT):
                outcome = info
                break
            if kind == "reload":
                if not gate.acquire_sync():
                    return self._window_closed(clicks)
                self._checkpoint()
                last_reload = clock.now()  # jadwal reload (T+0,5 s lalu tiap 2 s) dihitung dari AWAL reload
                self._reload()
                self.limiter.touch(clock.now())  # efek reload ada di akhir gestur/intent: slot berikutnya dari sini
                self.log.mark("reload")
                self._blocked_price = _NO_PRICE
                self._recheck_variant = self._variant_on_page
                seen = self._wait_screen({*PRODUCT, Screen.NOT_STARTED}, SETTLE_TIMEOUT_S, "product")
                if seen.screen in TERMINAL:
                    return TERMINAL[seen.screen], seen.evidence
                continue
            if kind == "variant":  # variasi lepas setelah reload: pilih ulang (aksi polling, lewat gate)
                if not gate.acquire_sync():
                    return self._window_closed(clicks)
                self._checkpoint()
                self._reselect_variant()
                self.limiter.touch(clock.now())  # seperti reload: slot berikutnya dihitung dari akhir aksi ini
                continue
            stale = outcome.screen in (Screen.NOT_STARTED, Screen.ERROR_TOAST, Screen.VARIANT_REQUIRED)
            if kind in ("sheet", "progress"):  # sheet masih terbuka: konfirmasi ulang = aksi polling
                if not gate.acquire_sync():
                    return self._window_closed(clicks)
                self._checkpoint()
                clicks += 1
                self._handle_sheet(retry=True)
                outcome = self._after_buy(stale, confirm_done=True)
            else:
                btn, price = info
                t_ready = clock.now_ms()
                t_wait = self._mono()
                if not gate.acquire_sync():
                    return self._window_closed(clicks)
                waited = self._mono() - t_wait > REVERIFY_AFTER_WAIT_S
                self._checkpoint()
                # toast lama bukan reaksi klik ini: dibersihkan SEBELUM klik (toast reaksi klik tetap terbaca) dan
                # sebelum cek ulang tombol, supaya jarak cek ulang -> klik sesingkat mungkin
                self.d.clear_toast()
                if waited:
                    # sempat menunggu slot: layar bisa sudah berubah (mis. checkout) -> cek ulang tombolnya
                    fresh = self._find("buy_button")
                    if fresh is None or fresh.bounds != btn.bounds:
                        self.log.mark("buy_recheck", "tombol Beli berubah/hilang saat menunggu slot; tidak diklik")
                        continue
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
            if st == Screen.PIN_SCREEN:
                # PIN tanpa klik "Buat Pesanan" dari alat: pesanan mungkin sudah terbuat (mis. tap tak sengaja)
                self.order_clicked = True
                return RunStatus.UNKNOWN_STATE, f"layar PIN muncul setelah klik Beli ({outcome.evidence})"
            if st in (Screen.NOT_STARTED, Screen.ERROR_TOAST):
                self.log.mark("not_started", f"#{clicks} ({outcome.evidence})")
                continue
            if st == Screen.VARIANT_REQUIRED:
                if not self.variant:
                    return RunStatus.ERROR, "produk wajib pilih variasi; isi `variant` di target.yaml"
                continue
            self.log.mark("no_response", f"#{clicks} ({outcome.evidence or st})")

        # ---- klik Beli berhasil: langkah maju sekali-sekali, tanpa throttle, batas 30 s
        self.log.mark("buy_ok", str(outcome.screen))
        deadline = self._mono() + self.flow_timeout_s
        changed = False
        if outcome.screen == Screen.CART:
            changed = self._cart_guard()
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

        ok, msg, switched = self._ensure_shopeepay(deadline)
        if not ok:
            return RunStatus.ERROR, msg
        self.log.mark("payment_ok", msg)

        if self._wait_find("place_order", deadline) is None:
            return RunStatus.ERROR, "tombol 'Buat Pesanan' tidak ditemukan"
        self._checkout_guard(changed or switched)  # lapis 3: penentu akhir, dry-run maupun live
        self._guard_at = float("-inf")
        while self._guard(1):  # captcha content-desc -> stop; dialog sistem -> tunggu (jaring UNKNOWN 1,5 s)
            self._checkpoint()
        place = self._find("place_order")  # posisi terbaru (layar bisa bergeser saat total dimuat)
        if place is None:
            return RunStatus.ERROR, "tombol 'Buat Pesanan' hilang setelah pengecekan harga"
        self._checkpoint()
        if not self.before_place_order():
            return RunStatus.ABORTED, "before_place_order() menolak (lock dipegang jalur lain)"
        self.log.mark("place_order_gate", "live" if live else "dry-run: berhenti di sini")
        if not live:
            return RunStatus.DRYRUN_OK, "sampai checkout dengan ShopeePay & harga lolos; 'Buat Pesanan' TIDAK diklik"

        self.d.clear_toast()
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
            self.d.start_url(self.cfg.product_url, self.package, wait=False)  # tanpa -W: jangan blok polling
        else:
            self.d.swipe_refresh()

    # ------------------------------------------------------------------ lapis 1: produk

    def _read_price(self) -> str | None:
        """Harga produk: selector kalibrasi, atau teks Rp pendek PERTAMA yang terlihat (1 query info).
        Format rusak ("Rp99rb") sengaja ikut terbaca -> 'tidak terbaca', bukan diganti nominal lain."""
        if self._cands("product_price"):
            node = self._find("product_price")
        else:
            node = self.d.info(Sel("textMatches", RP_SHORT_MATCH))
        return node.label if node is not None else None

    def _wait_ready(self, gate: PollingGate, last_reload: float | None, clicks: int):
        """Tunggu tombol Beli aktif + harga valid. Per iterasi: info tombol Beli, satu query bahaya
        (captcha/verifikasi/PIN/sale berakhir), satu query harga; setelah ada klik, satu query penanda bottom
        sheet. Cek pra-klik (_guard: content-desc bahaya, sheet sebelum klik pertama) bila sudah > 0,5 s.

        Return ("ready", (node, ProductPrice)) | ("sheet", None) | ("progress", Seen) | ("expired", None)
        | ("reload", None) | ("variant", Node) | ("stop", Seen).
        """
        while True:
            self._checkpoint()
            if gate.expired():
                return "expired", None
            reloadable = True
            try:
                btn = self._find("buy_button")
                if btn is not None and clicks and self._has("sheet_marker") is not None:
                    return "sheet", None  # klik sebelumnya membuka sheet: konfirmasi lewat gate
                if btn is None:
                    seen = self._classify("product")
                    if seen.screen in TERMINAL:
                        return "stop", seen
                    if seen.screen == Screen.VARIANT_SHEET and not clicks:
                        # sheet terbuka sebelum klik Beli pertama (lapis 1 belum lolos): tutup, jangan konfirmasi
                        self._close_sheet()
                        continue
                    if seen.screen in _PROGRESS:
                        return ("sheet", None) if seen.screen == Screen.VARIANT_SHEET else ("progress", seen)
                    if seen.screen == Screen.PIN_SCREEN:
                        self.order_clicked = True
                        raise _Stop(RunStatus.UNKNOWN_STATE, f"layar PIN muncul saat polling ({seen.evidence})")
                    # layar tak dikenal / aplikasi lain / loading: jangan reload/klik, biarkan jaring UNKNOWN
                    reloadable = seen.screen in (*PRODUCT, Screen.NOT_STARTED)
                    if seen.screen in PRODUCT:
                        btn = seen.node
                if btn is not None:
                    if (seen := self._danger(btn, "product")) is not None:
                        return "stop", self._no_pin(seen)
                    if btn.label.strip().lower() in BARE_SOLD_OUT:
                        return "stop", Seen(Screen.SOLD_OUT, f"tombol {btn.label!r}", btn)
                    if self._guard(clicks):
                        continue  # dialog sistem tampil / sheet sebelum klik pertama baru ditutup: baca ulang
                    self._observe(self._product(btn))
                    ready = btn.enabled and not self._marker_of(btn, "not_started")
                    if ready and self._recheck_variant:
                        opt = self._variant_after_reload(last_reload)
                        if opt == "wait":
                            continue  # chip variasi belum tampil setelah reload: jangan baca harga/klik dulu
                        if opt is not None:
                            return "variant", opt
                    if ready:
                        price_text = self._read_price()
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
            except AgentDead as e:
                self._recover_agent(e, gate)
                reloadable = False
            now = gate.clock.now()
            if reloadable and now >= gate.open_at + FIRST_RELOAD_S and (
                    last_reload is None or now - last_reload >= RELOAD_EVERY_S):
                return "reload", None

    def _no_pin(self, seen: Seen) -> Seen:
        """PIN saat polling (alat belum mengklik "Buat Pesanan"): pesanan mungkin sudah terbuat."""
        if seen.screen == Screen.PIN_SCREEN:
            self.order_clicked = True
            raise _Stop(RunStatus.UNKNOWN_STATE, f"layar PIN muncul saat polling ({seen.evidence})")
        return seen

    def _guard(self, clicks: int) -> bool:
        """Cek yang tidak perlu tiap iterasi (dijalankan bila hasil terakhir > GUARD_FRESH_S; dipaksa segera
        setelah klik Beli/konfirmasi, tepat sebelum konfirmasi sheet & "Buat Pesanan", dan di awal lapis 3):
        - penanda bahaya di content-desc (mis. captcha di WebView) -> stop;
        - dialog sistem crash/ANR di atas Shopee -> UNKNOWN (jaring 1,5 s), tanpa klik selama dialog tampil;
        - sebelum klik Beli pertama: bottom sheet yang sudah terbuka (info jalur A tetap menemukan 'Beli
          Sekarang' di belakang/di dalam sheet; klik akan mendarat di konfirmasi sheet tanpa pemilihan variasi).
        True = jangan bertindak di iterasi ini (dialog sistem tampil / sheet baru ditutup)."""
        if self._mono() - self._guard_at < GUARD_FRESH_S:
            return False
        if (seen := self._danger(None, "product", by="descriptionMatches")) is not None:
            seen = self._no_pin(seen)
            raise _Stop(TERMINAL[seen.screen], f"content-desc {seen.evidence}")
        if (seen := self._system_dialog()) is not None:
            self._observe(seen)  # UNKNOWN berturut-turut > 1,5 s -> UNKNOWN_STATE
            if seen.evidence != self._dialog_logged:
                self._dialog_logged = seen.evidence
                self.log.warn(f"{seen.evidence}: tidak ada klik selama dialog tampil")
            self._guard_at = float("-inf")  # dicek lagi tiap iterasi sampai hilang
            return True
        if not clicks and self._has("sheet_marker") is not None:
            self._close_sheet()
            return True
        self._guard_at = self._mono()
        return False

    def _close_sheet(self) -> None:
        """Tutup bottom sheet yang terbuka sebelum klik Beli pertama (back), lalu pastikan sudah hilang."""
        self.d.press_back()
        self.log.warn("bottom sheet terbuka sebelum klik Beli; ditutup (back), tidak dikonfirmasi")
        end = self._mono() + SHEET_CLOSE_S
        while self._has("sheet_marker") is not None:
            self._checkpoint()
            if self._mono() >= end:
                raise _Stop(RunStatus.ERROR, "bottom sheet terbuka sebelum klik Beli dan tidak tertutup dengan back")
        self._guard_at = float("-inf")

    def _reselect_variant(self) -> None:
        """Klik ulang chip variasi (slot gate sudah dipakai). Chip dibaca ulang (koordinat lama tidak dipakai),
        bahaya dicek dulu; bila chip ternyata membuka bottom sheet, sheet ditutup dan variasi dipilih di sheet
        setelah klik Beli (tidak dicek ulang lagi)."""
        if (seen := self._danger(None, "product")) is not None:
            seen = self._no_pin(seen)
            raise _Stop(TERMINAL[seen.screen], seen.evidence)
        opt = self._find("variant_option", sticky=False)
        if opt is None or opt.selected or opt.checked or not opt.enabled:
            return
        if self._variant_unreadable and self.cfg.android.reload != "intent":
            return
        self.d.click(opt)
        self.log.mark("variant_selected", f"{self.variant} (ulang setelah reload)")
        if self._has("sheet_marker") is not None:
            self._variant_on_page = False
            self._close_sheet()
        self._guard_at = float("-inf")

    def _variant_after_reload(self, last_reload: float | None) -> Node | str | None:
        """Reload bisa mengembalikan halaman ke variasi bawaan: harga lapis 1 lalu milik variasi lain. Variasi
        yang dipilih di halaman produk saat arm diperiksa sekali per reload:
        - chip belum tampil -> "wait" (harga belum dibaca, Beli belum diklik) sampai SETTLE_TIMEOUT_S sejak reload;
        - status terpilih terbaca & belum terpilih -> chip (dipilih ulang lewat gate, aksi polling);
        - status terpilih TIDAK terbaca -> dipilih ulang hanya setelah reload intent (halaman dibuka ulang =
          variasi bawaan); setelah swipe tidak (tap bisa jadi toggle yang melepas pilihan). Lapis 3 penentu."""
        opt = self._find("variant_option", sticky=False)
        if opt is None:
            settling = last_reload is not None and self._clock.now() - last_reload < SETTLE_TIMEOUT_S
            if settling:
                return "wait"
            self._recheck_variant = False
            return None
        self._recheck_variant = False
        if opt.selected or opt.checked or not opt.enabled:
            return None
        if self._variant_unreadable and self.cfg.android.reload != "intent":
            return None
        return opt

    def _recover_agent(self, err: DriverError, gate: PollingGate) -> None:
        """Agent mati saat polling (HiOS): hidupkan ulang secara eksplisit (tercatat), maks MAX_AGENT_RESTARTS."""
        self.log.warn(f"agent uiautomator2 tidak menjawab saat polling (HiOS?): {err}")
        if self._agent_restarts >= MAX_AGENT_RESTARTS or gate.expired():
            raise err
        self._agent_restarts += 1
        self.log.warn(f"restart agent #{self._agent_restarts}")
        self.d.restart_agent()

    def _after_buy(self, stale: bool, confirm_done: bool = False) -> Seen:
        """Tunggu reaksi klik Beli/konfirmasi. 'Tidak bereaksi' hanya bila halaman produk TIDAK berubah
        selama NO_RESPONSE_S; layar tak dikenal/loading ditunggu (jaring UNKNOWN / batas 30 s), bukan diklik ulang."""
        t_click = self._mono()
        t_ref = t_click
        seen_clear = not stale
        sheet_done = confirm_done
        self._guard_at = float("-inf")  # content-desc bahaya dicek segera setelah klik (bukan sebelum: kecepatan)
        while True:
            self._checkpoint()
            if self._guard(1):
                continue  # dialog sistem di atas aplikasi: tunggu (jaring UNKNOWN), jangan klasifikasi/tindak
            seen = self._classify("after_buy")
            st = seen.screen
            now = self._mono()
            if st in (Screen.CART, Screen.CHECKOUT, Screen.PIN_SCREEN) or st in TERMINAL:
                return seen
            if st == Screen.VARIANT_SHEET:
                if not sheet_done:
                    if not self._handle_sheet():
                        continue  # konfirmasi ditahan dialog sistem: coba lagi saat dialog hilang
                    sheet_done = True
                    t_ref = self._mono()
                    seen_clear = False  # toast lama di sheet diabaikan sebentar
                elif now - t_ref > NO_RESPONSE_S:
                    return Seen(Screen.VARIANT_SHEET, "sheet tidak bereaksi")
                continue
            if st in (Screen.NOT_STARTED, Screen.ERROR_TOAST, Screen.VARIANT_REQUIRED):
                if seen_clear or now - t_ref > STALE_TOAST_S:
                    return seen
                continue
            if now - t_click > self.flow_timeout_s:
                return Seen(Screen.UNKNOWN, "timeout")
            if st == Screen.LOADING:  # server lambat saat flash sale: tunggu, bukan "tidak bereaksi"
                t_ref = now
                continue
            if st == Screen.UNKNOWN:
                continue  # jaring UNKNOWN (_observe) yang memutuskan; jangan klik ulang di atasnya
            seen_clear = True
            if now - t_ref > NO_RESPONSE_S:
                return Seen(st, "tidak ada reaksi")

    def _sheet_nodes(self, step: str) -> list[Node]:
        sel = self._union((step,))
        return self.d.find_all(sel) if sel is not None else []

    def _handle_sheet(self, retry: bool = False) -> bool:
        """Bottom sheet variasi/kuantitas: pilih variasi (diverifikasi), lalu konfirmasi sekali.

        Elemen diambil dari find_all sempit (jendela aktif = sheet) dan dipilih yang paling bawah, supaya
        tidak men-tap elemen halaman produk di belakang sheet (tap di luar sheet bisa menutupnya)."""
        waited = False  # sempat memilih variasi: layar bisa berubah sejak klasifikasi terakhir
        if self.variant:
            opt = self._pick("variant_option", self._sheet_nodes("variant_option")) \
                or self._find("variant_option", sticky=False)
            if opt is None:
                self._variant_ok = False
                raise _Stop(RunStatus.ERROR, f"variasi {self.variant!r} tidak ditemukan di pilihan variasi")
            if not (opt.selected or opt.checked):
                if not opt.enabled:
                    raise _Stop(RunStatus.SOLD_OUT, f"variasi {self.variant!r} tidak bisa dipilih (habis?)")
                self.d.click(opt)
                waited = True
                self.log.mark("variant_selected", self.variant)
                end = self._mono() + VARIANT_VERIFY_S
                while True:
                    chip = self._pick("variant_option", self._sheet_nodes("variant_option"))
                    if chip is not None and (chip.selected or chip.checked):
                        break
                    if self._mono() >= end:
                        self.log.warn(f"variasi {self.variant!r} tidak terlihat terpilih (status chip tidak terbaca); "
                                      "diverifikasi di checkout (lapis 3)")
                        break
            self._variant_ok = True
        confirm = self._pick("sheet_confirm", self._sheet_nodes("sheet_confirm")) or self._find("sheet_confirm")
        if confirm is None:
            raise _Stop(RunStatus.ERROR, "tombol konfirmasi di pilihan variasi tidak ditemukan")
        # konfirmasi = langkah yang mengikat: captcha/verifikasi (teks bila sempat memilih variasi; content-desc
        # selalu) dicek tepat sebelumnya
        if waited and (seen := self._danger(None, "after_buy")) is not None:
            seen = self._no_pin(seen)
            raise _Stop(TERMINAL[seen.screen], seen.evidence)
        self._guard_at = float("-inf")
        if self._guard(1):
            return False  # dialog sistem di atas sheet: konfirmasi ditahan
        self.d.clear_toast()
        t_click = self.clock.now_ms()
        self.d.click(confirm)
        self.log.mark("click_sheet_confirm", "ulang (lewat gate)" if retry else "", t_ms=t_click)
        return True

    # ------------------------------------------------------------------ lapis 2: keranjang

    def _still_there(self, steps: str | tuple[str, ...], nodes: list[Node]) -> None:
        """Bacaan langkah maju (find_all sempit) tidak memuat penanda layarnya: Shopee crash / keluar dari
        foreground / dialog asing? Klasifikasi penuh (paket & activity di depan) + jaring UNKNOWN 1,5 s."""
        steps = (steps,) if isinstance(steps, str) else steps
        if any(self._local(step, nodes) is not None for step in steps):
            return
        seen = self._classify("any")
        if seen.screen in TERMINAL:
            raise _Stop(TERMINAL[seen.screen], seen.evidence)

    def _cart_guard(self) -> bool:
        """Lapis 2. Return True bila keranjang diubah (uncheck) -> total checkout perlu waktu dihitung ulang."""
        target = self.cfg.expected_name
        strict = bool(target)
        end = self._mono() + CART_SETTLE_S
        rows, counts = self._read_cart()
        while not rows and self._mono() < end:  # tunggu baris keranjang terbaca
            self._checkpoint()
            rows, counts = self._read_cart()
        if not rows:
            if counts and set(counts) != {1}:
                raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: tombol Checkout menunjukkan {counts} item, "
                                                   "baris item tidak terbaca")
            self.log.warn("keranjang: baris item tidak terbaca; diputuskan di checkout (lapis 3)")
            return False
        verdict = pricing.check_cart([r for r, _ in rows], target, strict)
        changed = False
        if verdict.to_uncheck:
            for i in verdict.to_uncheck:
                self._checkpoint()
                row, box = rows[i]
                self.d.click(box)
                self.log.mark("cart_uncheck", row.text.splitlines()[0][:50])
            changed = True
            end = self._mono() + CART_SETTLE_S
            while True:  # centang & "Checkout (N)" diperbarui setelah round-trip server
                self._checkpoint()
                rows, counts = self._read_cart()
                verdict = pricing.check_cart([r for r, _ in rows], target, strict)
                if (verdict.ok and (not counts or set(counts) == {1})) or self._mono() >= end:
                    break
        if not verdict.ok:
            names = "; ".join(r.text.splitlines()[0][:40] for r, _ in rows if r.checked)
            raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: {verdict.reason} [{names}]")
        if counts and set(counts) != {1}:
            raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: tombol Checkout menunjukkan {counts} item")
        self.log.mark("cart_ok", verdict.reason)
        return changed

    def _read_cart(self):
        # langkah maju: baca semua teks agar nama item ikut terbaca
        nodes = self._snapshot()
        seen = self._classify_nodes(nodes, "any")
        if seen.screen in TERMINAL:
            raise _Stop(TERMINAL[seen.screen], seen.evidence)
        self._still_there("cart_marker", nodes)
        boxes = self.d.find_all(Sel("className", "android.widget.CheckBox"))
        return cart_rows(boxes, nodes), checkout_count(nodes)

    # ------------------------------------------------------------------ checkout

    def _payment_read(self) -> tuple[list[Node], str | None]:
        nodes = self.d.find_all(self._union(("place_order", "payment_shopeepay", "payment_confirm"),
                                            ("captcha", "verification", "pin"),
                                            (PAYMENT_LABEL_MATCH, PAYMENT_METHOD_MATCH)))
        seen = self._classify_nodes(nodes, "any")
        if seen.screen in TERMINAL:
            raise _Stop(TERMINAL[seen.screen], seen.evidence)
        self._still_there(("place_order", "payment_shopeepay"), nodes)
        return nodes, payment_value(nodes)

    def _ensure_shopeepay(self, deadline: float) -> tuple[bool, str, bool]:
        """(ok, pesan, diganti). Metode terpilih dibaca dari baris "Metode Pembayaran"/radio tercentang."""
        nodes, value = self._payment_read()
        if is_shopeepay(value):
            radios = self.d.find_all(Sel("className", "android.widget.RadioButton"))
            if not radios or is_shopeepay(payment_value([*nodes, *radios])):
                return True, "ShopeePay sudah terpilih", False
            value = payment_value([*nodes, *radios])
        row = self._local("payment_change", nodes) or self._find("payment_change")
        if row is None:
            return False, f"baris 'Metode Pembayaran' tidak ditemukan (metode tampil: {value!r})", False
        t_click = self.clock.now_ms()
        self.d.click(row)
        self.log.mark("payment_change", f"sebelumnya {value!r}", t_ms=t_click)
        end = min(deadline, self._mono() + SELECT_WAIT_S)
        opt = None
        while self._mono() < end and opt is None:
            self._checkpoint()
            if (seen := self._danger(None, "any")) is not None and seen.screen in TERMINAL:
                raise _Stop(TERMINAL[seen.screen], seen.evidence)
            node = self._find("payment_shopeepay")
            opt = node if node is not None and is_shopeepay(node.label) else None
        if opt is None:
            return False, "opsi ShopeePay tidak ditemukan di daftar metode pembayaran", True
        t_click = self.clock.now_ms()
        self.d.click(opt)
        self.log.mark("select_shopeepay", t_ms=t_click)
        confirm = self._find("payment_confirm")
        if confirm is not None:
            self.d.click(confirm)
            self.log.mark("payment_confirm")
        while self._mono() < end:
            self._checkpoint()
            nodes, value = self._payment_read()
            if self._local("place_order", nodes) is not None and is_shopeepay(value):
                return True, "ShopeePay dipilih (sebelumnya metode lain)", True
        return False, f"ShopeePay tidak terverifikasi terpilih (metode tampil: {value!r}); jalankan calibrate ulang", \
            True

    def _checkout_read(self) -> list[Node]:
        # nama & variasi diambil longgar (tanda baca/spasi bebas, seperti _squash di pricing): "Variasi: 256GB,
        # Biru" tetap terbaca untuk variant "256GB Biru"; keputusan tetap di pricing.check_checkout
        loose = tuple(p for p in map(_loose_match, (self.cfg.expected_name, self.variant)) if p)
        sel = self._union(("place_order",), ("captcha", "verification", "pin", "sold_out"),
                          (RP_ANY_MATCH, QTY_MATCH, TOTAL_LABEL_MATCH, SHIPPING_LABEL_MATCH, ORDER_COUNT_MATCH,
                           PENDING_MATCH, VARIANT_LINE_MATCH, *loose))
        return self.d.find_all(sel)

    def _checkout_guard(self, changed: bool = False) -> pricing.CheckoutVerdict:
        """Lapis 3: tunggu ongkir & total terbaca dan stabil, lalu cek isi pesanan (fail-closed).

        Stabil = nilai sama bertahan >= 100 ms; setelah ganti metode bayar / uncheck keranjang >= 1 s
        (server menghitung ulang total & promo metode)."""
        gap = POST_CHANGE_STABLE_S if changed else PRICE_STABLE_GAP_S
        timeout = self.price_stable_timeout_s + (POST_CHANGE_STABLE_S if changed else 0.0)
        end = self._mono() + timeout
        prev = None
        since = 0.0
        snap = None
        shipping = total = None
        total_rid, shipping_rid = self._rid_regex("checkout_total"), self._rid_regex("checkout_shipping")
        self._guard_at = float("-inf")  # minimal sekali cek content-desc bahaya di checkout sebelum memesan
        while True:
            self._checkpoint()
            if self._guard(1):
                continue
            t_read = self._mono()
            nodes = self._checkout_read()
            seen = self._classify_nodes(nodes, "any")
            if seen.screen in TERMINAL:
                raise _Stop(TERMINAL[seen.screen], seen.evidence)
            self._still_there("place_order", nodes)
            snap = checkout_snapshot(nodes, total_rid, shipping_rid)
            shipping, total = pricing.read_total(snap)
            key = (shipping, total, tuple(snap.rows)) if shipping is not None and total is not None else None
            if key is not None and key == prev and t_read - since >= gap:
                break
            if key != prev:
                prev, since = key, t_read
            if self._mono() >= end:
                raise _Stop(RunStatus.PRICE_GUARD,
                            f"total checkout tidak stabil/terbaca dalam {timeout:.1f} s "
                            f"(ongkir {pricing.rupiah(shipping)}, total {pricing.rupiah(total)})")
        verdict = pricing.check_checkout(snap, self.limits)
        if verdict.values.get("name_ok") is False or verdict.values.get("variant_ok") is False:
            verdict = self._recheck_identity(snap, total_rid, shipping_rid) or verdict
        self.log.info(f"checkout terbaca: {verdict.summary()}")
        if not verdict.ok:
            raise _Stop(RunStatus.PRICE_GUARD, f"{'; '.join(verdict.reasons)} | {verdict.summary()}")
        self.log.mark("price_guard_ok", verdict.summary())
        return verdict

    def _recheck_identity(self, snap: pricing.CheckoutSnapshot, total_rid, shipping_rid
                          ) -> pricing.CheckoutVerdict | None:
        """Nama/variasi tidak terlihat di bacaan sempit: bacaan sempit hanya memuat teks yang cocok pola, jadi
        header toko bisa tak terbaca dan kolom produk lalu diambil dari harga (nama yang di kiri harga ikut
        terbuang). Baca SEKALI semua teks layar (mahal, hanya di jalur gagal ini), susun ulang baris produk, dan
        nilai ulang dengan total/ongkir yang sudah stabil. Tetap fail-closed: verdict baru harus lolos semua cek."""
        nodes = self._snapshot()
        seen = self._classify_nodes(nodes, "any")
        if seen.screen in TERMINAL:
            raise _Stop(TERMINAL[seen.screen], seen.evidence)
        full = checkout_snapshot(nodes, total_rid, shipping_rid)
        verdict = pricing.check_checkout(pricing.CheckoutSnapshot(rows=full.rows, totals=snap.totals,
                                                                  shippings=snap.shippings,
                                                                  page_text=full.page_text), self.limits)
        self.log.info(f"nama/variasi dibaca ulang dari semua teks layar: {verdict.summary()}")
        return verdict if verdict.ok else None

    # ------------------------------------------------------------------ akhir

    def _write_latency(self, label: str) -> None:
        stats = self.d.stats
        if not stats.samples:
            return
        for line in stats.summary():
            self.log.info(f"latensi {label} {line}")
        slow = [s for s in stats.samples if s.ms > (FIND_ALL_WARN_MS if s.op == "find_all" else QUERY_WARN_MS)
                and s.op in ("exists", "info", "click", "find_all")]
        if slow:
            worst = max(slow, key=lambda s: s.ms)
            self.log.warn(f"{len(slow)} query melewati target (info/exists/klik {QUERY_WARN_MS:.0f} ms, find_all "
                          f"{FIND_ALL_WARN_MS:.0f} ms); terlama {worst.op} {worst.target} {worst.ms:.0f} ms")
        stats.write_csv(self.log.run_dir / f"android-queries-{label}.csv")
        stats.samples.clear()

    def _finish(self, status: RunStatus, message: str, live: bool) -> RunResult:
        detail = ""
        if self.order_clicked:
            status, message, detail = after_order_click(status, message)
        self.log.mark("result", f"{status}: {message}" + (f" ({detail})" if detail else ""))
        result = RunResult(self.name, status, message, live, steps=list(self.log.steps), detail=detail)
        if status in STOP_ALL_STATUSES:
            self._signal_stop()
        if status in ALARM_STATUSES or self.order_clicked:
            self._alarm(str(status), message)
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
