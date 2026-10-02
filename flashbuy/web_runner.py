"""Runner jalur Web (Playwright, profil Chrome persisten, headed).

Tidak ada stealth plugin, patch navigator.webdriver, atau spoof UA/fingerprint. Semua aksi
berupa klik UI biasa. Hot path (tunggu tombol aktif + harga valid -> klik) tanpa screenshot/tracing.

Pengaman harga (fail-closed, lihat flashbuy.pricing):
  lapis 1  halaman produk: klik Beli hanya jika tombol aktif DAN harga tampil <= max_item_price
  lapis 2  keranjang: hanya item target yang tercentang
  lapis 3  checkout: 1 baris, qty 1, nama, harga satuan & total stabil <= batas; tepat sebelum
           before_place_order(), di dry-run maupun live
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from playwright.async_api import BrowserContext, Frame, Locator, Page, Playwright, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from flashbuy import pricing, selector_store
from flashbuy.config import FLOW_TIMEOUT_S, ConfigError, TargetConfig, WebConfig, require_live_ready
from flashbuy.control import CANCEL_MESSAGE
from flashbuy.guards import ENABLED_JS, MIN_IFRAME_PX, TERMINAL, Classification, Guard, PageState
from flashbuy.notifier import Notifier
from flashbuy.rehearsal import (
    STOP_STATUSES,
    PlaceOrderForbidden,
    RehearsalReport,
    RehearsalStep,
    cart_line,
    forbidden_gate,
    guard_text,
)
from flashbuy.runner_base import (
    ALARM_STATUSES,
    MAYBE_ORDERED_MSG,
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
from flashbuy.selector_store import SelectorSet
from flashbuy.timesync import ServerClock
from flashbuy.web_js import (
    CART_BOX_JS,
    CART_JS,
    CHECKOUT_JS,
    PRICE_TEXT_JS,
    PRODUCT_TITLE_JS,
    SHIPPING_LABEL,
    TOTAL_LABEL,
    WAIT_READY_JS,
)

POLL_S = 0.02  # interval cek status halaman (lokal, tanpa request ke server)
NO_RESPONSE_S = 1.5  # klik Beli tanpa reaksi & tanpa navigasi -> boleh klik ulang
STALE_TOAST_S = 0.4  # toast lama yang masih tampil diabaikan selama ini
FIRST_RELOAD_S = 0.5  # belum siap (tombol/harga) di T+0,5 s -> reload pertama
RELOAD_EVERY_S = 2.0  # reload berikutnya tiap 2 s (tetap lewat RateLimiter & jendela)
SELECT_WAIT_S = 3.0
UNKNOWN_LIMIT_S = 1.5  # status UNKNOWN berturut-turut lebih lama dari ini -> UNKNOWN_STATE
PRICE_STABLE_TIMEOUT_S = 1.5  # total checkout harus stabil dalam waktu ini
PRICE_STABLE_GAP_S = 0.1  # jarak dua pembacaan total
_NO_PRICE = "\u0000"  # penanda "belum ada harga yang ditolak"

SELECTED_JS = r"""el => {
  for (let e = el, i = 0; e && i < 3; e = e.parentElement, i++) {
    for (const a of ['aria-checked', 'aria-pressed', 'aria-selected'])
      if (e.getAttribute(a) === 'true') return true;
    if (e.matches('input:checked') || e.querySelector(':scope > input:checked')) return true;
    if (i < 2 && /(^|[\s_-])(selected|active|checked)([\s_-]|$)/i.test(
        typeof e.className === 'string' ? e.className : '')) return true;
  }
  return false;
}"""


class _Aborted(Exception):
    pass


class _Stop(Exception):
    def __init__(self, status: RunStatus, message: str):
        super().__init__(message)
        self.status, self.message = status, message


async def launch_context(pw: Playwright, web: WebConfig, *, headless: bool = False) -> BrowserContext:
    """Profil persisten apa adanya. Sengaja TANPA argumen anti-deteksi apa pun."""
    web.profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"user_data_dir": str(web.profile_dir), "headless": headless}
    if web.channel:
        kwargs["channel"] = web.channel
    if not headless:
        kwargs["no_viewport"] = True
    return await pw.chromium.launch_persistent_context(**kwargs)


async def _block_media(route: Route) -> None:
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


def _cand(cand: dict) -> str:
    """Kandidat selector web untuk laporan, mis. role=button name='Beli Sekarang'."""
    return " ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in cand.items())


def _first_line(e: PlaywrightError) -> str:
    return e.message.splitlines()[0] if e.message else type(e).__name__


class WebRunner:
    name = "web"

    def __init__(self, cfg: TargetConfig, selectors: SelectorSet, *, log: RunLog, notifier: Notifier,
                 headless: bool = False, before_place_order: Callable[[], bool] = always_allow,
                 limiter: RateLimiter | None = None, stop_event: asyncio.Event | None = None):
        self.cfg = cfg
        self.limits = cfg.limits
        self.sel = selectors
        self.guard = Guard(selectors)
        self.log = log
        self.notifier = notifier
        self.headless = headless
        self.before_place_order = before_place_order
        self.limiter = limiter or RateLimiter()
        self.stop_event = stop_event  # stop global (orchestrator: StopView)
        self.cancel_event = None  # orchestrator: jalur lain memenangkan lock -> berhenti polling
        self.variant = cfg.variant
        self.open_at: float | None = None
        # batas waktu internal (bisa diskalakan di tes)
        self.flow_timeout_s = FLOW_TIMEOUT_S
        self.unknown_limit_s = UNKNOWN_LIMIT_S
        self.price_stable_timeout_s = PRICE_STABLE_TIMEOUT_S
        self.order_clicked = False  # "Buat Pesanan" sudah diklik (pesanan mungkin sudah dibuat)
        self.rehearsal = False  # mode rehearsal: "Buat Pesanan" tidak pernah diklik (fungsi kliknya melempar)
        self._live = False  # attempt live: satu-satunya keadaan klik "Buat Pesanan" diizinkan
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._buy: Locator | None = None
        self._arm_state: Classification | None = None
        self._abort_reason: str | None = None
        self._event_hit: Classification | None = None  # captcha/verifikasi dari event frame
        self._nav_pending = False
        self._tracking = False
        self._unknown_since: float | None = None
        self._blocked_price = _NO_PRICE
        self._price_blocked = False
        self._last_price: pricing.ProductPrice | None = None
        self._product_title = ""
        self._variant_ok: bool | None = None
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle

    async def prepare(self) -> None:
        if self.context is not None:
            return
        self._pw = await async_playwright().start()
        self.context = await launch_context(self._pw, self.cfg.web, headless=self.headless)
        self._closed = asyncio.Event()
        self.context.on("close", lambda _ctx: self._closed.set())
        if self.cfg.web.block_media:
            await self.context.route("**/*", _block_media)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(10_000)
        self.page.on("request", self._on_request)
        self.page.on("requestfailed", self._on_nav_done)
        self.page.on("domcontentloaded", lambda _p: setattr(self, "_nav_pending", False))
        # Deteksi captcha/verifikasi berbasis event, bukan hanya setelah tiap langkah.
        self.page.on("framenavigated", self._on_frame)
        self.page.on("frameattached", self._on_frame)
        self.log.info(f"browser siap (channel={self.cfg.web.channel or 'chromium'}, headless={self.headless})")

    def _on_request(self, req) -> None:
        if req.is_navigation_request() and req.frame == self.page.main_frame:
            self._nav_pending = True

    def _on_nav_done(self, req) -> None:
        if req.is_navigation_request() and req.frame == self.page.main_frame:
            self._nav_pending = False

    def _on_frame(self, frame: Frame) -> None:
        if self._event_hit is not None:
            return
        url = frame.url
        state = self.guard.url_state(url)
        if state is None:
            return
        if frame == self.page.main_frame:
            self._event_hit = Classification(state, f"navigasi ke {url}")
            self.log.warn(f"event: {state} - {url}")
            self._event_stop()
        else:
            task = asyncio.ensure_future(self._check_iframe(frame, state, url))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)

    async def _check_iframe(self, frame: Frame, state: PageState, url: str) -> None:
        try:
            el = await frame.frame_element()
            visible = await el.is_visible()
            box = await el.bounding_box()
        except PlaywrightError:
            return
        if visible and box and box["width"] >= MIN_IFRAME_PX and box["height"] >= MIN_IFRAME_PX:
            if self._event_hit is None:
                self._event_hit = Classification(state, f"iframe {url[:100]}")
                self.log.warn(f"event: {state} - iframe {url[:100]}")
                self._event_stop()
        else:
            self.log.info(f"iframe cocok pola tapi kecil/tersembunyi, diabaikan: {url[:100]}")

    def _event_stop(self) -> None:
        """Captcha/verifikasi dari event halaman: stop global SAAT ITU JUGA (jalur lain berhenti sebelum tap
        berikutnya), walaupun jalur ini sedang diam (menunggu arm atau T-lead) dan baru melaporkan di checkpoint."""
        if self._event_hit is not None and TERMINAL[self._event_hit.state] in STOP_ALL_STATUSES \
                and self.stop_event is not None:
            self.stop_event.set()

    async def abort(self, reason: str = "") -> None:
        self._abort_reason = reason or "dibatalkan"

    async def close(self) -> None:
        for task in list(self._bg):
            task.cancel()
        try:
            if self.context is not None:
                await self.context.close()
        except PlaywrightError:
            pass
        if self._pw is not None:
            await self._pw.stop()
        self.context = self.page = self._pw = None

    async def wait_closed(self) -> None:
        """Tunggu pengguna menutup jendela browser (untuk PIN/captcha manual)."""
        if self.context is not None:
            await self._closed.wait()

    def _checkpoint(self) -> None:
        """Dipanggil di setiap iterasi loop dan sebelum SETIAP klik/navigasi: abort pengguna, captcha dari event
        halaman ini, stop global dari jalur lain. Setelah alat mengklik "Buat Pesanan", stop global tidak
        menghentikan penantian layar PIN (pesanan sudah dikirim)."""
        if self._abort_reason is not None:
            raise _Aborted(self._abort_reason)
        if self._event_hit is not None:
            raise _Stop(TERMINAL[self._event_hit.state], self._event_hit.evidence)
        if not self.order_clicked and self.stop_event is not None and self.stop_event.is_set():
            raise _Aborted(getattr(self.stop_event, "reason", "") or "dihentikan oleh runner lain")

    def _poll_checkpoint(self) -> None:
        """Fase polling (klik Beli, reload): juga berhenti bila jalur lain sudah memenangkan lock."""
        self._checkpoint()
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise _Aborted(CANCEL_MESSAGE)

    async def _click(self, loc: Locator, **kw) -> None:
        self._checkpoint()
        await loc.click(**kw)

    async def _goto(self, url: str) -> None:
        self._checkpoint()
        await self.page.goto(url, wait_until="domcontentloaded")

    # ------------------------------------------------------------------ util

    def _url(self, key: str) -> str:
        return self.cfg.origin + self.sel.urls[key]

    async def _classify(self, with_buy: bool = False) -> Classification:
        c = await self.guard.classify(self.page, self._buy if with_buy else None)
        return self._observe(c)

    def _observe(self, c: Classification) -> Classification:
        """Jaring pengaman: UNKNOWN berturut-turut > unknown_limit_s -> UNKNOWN_STATE."""
        if not self._tracking:
            return c
        now = asyncio.get_running_loop().time()
        if c.state != PageState.UNKNOWN:
            self._unknown_since = None
        elif self._unknown_since is None:
            self._unknown_since = now
        elif now - self._unknown_since > self.unknown_limit_s:
            raise _Stop(RunStatus.UNKNOWN_STATE,
                        f"halaman tidak dikenali > {self.unknown_limit_s:.1f} s ({c.evidence or 'tanpa ciri'})")
        return c

    async def _resolve(self, step: str) -> Locator | None:
        found = await selector_store.resolve(self.page, self.sel.candidates(step), self.variant)
        return found[0] if found else None

    async def _resolve_cand(self, step: str) -> tuple[Locator, dict] | None:
        """Seperti _resolve, plus kandidat selector yang cocok (laporan rehearsal)."""
        return await selector_store.resolve(self.page, self.sel.candidates(step), self.variant)

    async def _body_text(self) -> str:
        try:
            return await self.page.evaluate("() => document.body ? document.body.innerText : ''")
        except PlaywrightError:
            return ""

    async def _is_selected(self, loc: Locator) -> bool:
        try:
            return bool(await loc.evaluate(SELECTED_JS, timeout=2000))
        except PlaywrightError:
            return False

    # ------------------------------------------------------------------ precheck

    async def precheck(self) -> PrecheckResult:
        res = PrecheckResult(self.name)
        try:
            await self._precheck(res)
        except _Aborted as e:
            res.status = RunStatus.ABORTED
            res.items.append(PrecheckItem("precheck", False, f"dibatalkan: {e}"))
        except _Stop as e:
            res.status = e.status
            res.items.append(PrecheckItem("precheck", False, f"{e.status}: {e.message}"))
        if res.status in STOP_ALL_STATUSES and self.stop_event is not None:
            self.stop_event.set()
        return res

    async def _precheck(self, res: PrecheckResult) -> None:
        page = self.page
        await self._goto(self._url("address_page"))
        if (await self._classify()).state == PageState.LOGIN_REQUIRED:
            res.items.append(PrecheckItem("login", False, "sesi tidak login (diarahkan ke halaman login)"))
            res.status = RunStatus.LOGIN_REQUIRED
            return
        res.items.append(PrecheckItem("login", True, "sesi aktif"))

        text = await self._body_text()
        if re.search(r"\butama\b", text, re.I):
            res.items.append(PrecheckItem("alamat default", True, "alamat 'Utama' ditemukan"))
        elif re.search(r"belum ada alamat", text, re.I):
            res.items.append(PrecheckItem("alamat default", False, "belum ada alamat tersimpan"))
        else:
            res.items.append(PrecheckItem("alamat default", None, "tidak terbaca dari halaman alamat"))

        await self._goto(self._url("wallet_page"))
        text = await self._body_text()
        m = re.search(r"saldo[^\n]*\n?[^\n]*?(Rp\s?[\d.]+)", text, re.I)
        balance = pricing.parse_price(m.group(1)) if m else None

        await self._goto(self.cfg.product_url)
        if (await self._classify()).state == PageState.LOGIN_REQUIRED:
            res.items.append(PrecheckItem("login", False, "halaman produk minta login"))
            res.status = RunStatus.LOGIN_REQUIRED
            return
        text = await self._body_text()
        price_text = await page.evaluate(PRICE_TEXT_JS, self.sel.css("product_price"))
        price = pricing.parse_price(price_text)
        ms = re.search(r"(ongkos kirim|ongkir)[^\n]*?(Rp\s?[\d.]+)", text, re.I)
        shipping = pricing.parse_price(ms.group(2)) if ms else None

        if balance is None:
            res.items.append(PrecheckItem("saldo ShopeePay", None, "saldo tidak terbaca"))
        elif price is None:
            res.items.append(PrecheckItem("saldo ShopeePay", None,
                                          f"saldo {pricing.rupiah(balance)} terbaca, harga produk tidak terbaca "
                                          f"({price_text!r})"))
        else:
            # sebelum flash sale halaman bisa menampilkan harga normal; alat tidak membayar > max_item_price
            unit = min(price, self.limits.max_item_price)
            need = unit + (shipping or 0)
            note = "" if shipping is not None else " (ongkir tidak terbaca)"
            if unit < price:
                note += f" (harga tampil {pricing.rupiah(price)} dibatasi max_item_price)"
            res.items.append(PrecheckItem("saldo ShopeePay", balance >= need,
                                          f"saldo {pricing.rupiah(balance)} vs harga+ongkir "
                                          f"{pricing.rupiah(need)}{note}"))
        if balance is not None and balance < self.limits.max_total:
            res.items.append(PrecheckItem("saldo vs max_total", None,
                                          f"saldo {pricing.rupiah(balance)} < max_total "
                                          f"{pricing.rupiah(self.limits.max_total)}"))
        buy = await self._resolve("buy_button")
        res.items.append(PrecheckItem("tombol Beli", True if buy else None,
                                      "ditemukan" if buy else "tidak ditemukan - jalankan calibrate"))

    # ------------------------------------------------------------------ arm (T-60 s)

    async def arm(self, open_at: float) -> None:
        self.open_at = open_at
        try:
            await self._arm()
        except (_Aborted, _Stop) as e:  # stop global saat arm: attempt langsung berhenti tanpa aksi
            self.log.warn(f"arm dihentikan: {e}")
            if isinstance(e, _Stop) and self._event_hit is not None and self._arm_state is None:
                # captcha/verifikasi dari event halaman (navigasi/iframe) saat arm: hasil diminta SEKARANG
                self._arm_state = self._event_hit
                self._event_stop()

    @property
    def arm_blocked(self) -> bool:
        """arm (T-60 s) berakhir di login/captcha/verifikasi: attempt() langsung mengembalikan hasilnya."""
        return self._arm_state is not None

    async def _arm(self) -> None:
        await self._goto(self.cfg.product_url)
        self.log.mark("page_open")
        self._buy = await self._resolve("buy_button")
        c = await self._classify(with_buy=True)
        if c.state in (PageState.LOGIN_REQUIRED, PageState.CAPTCHA, PageState.VERIFICATION):
            self._arm_state = c
            self.log.warn(f"arm: {c.state} ({c.evidence})")
            if TERMINAL[c.state] in STOP_ALL_STATUSES and self.stop_event is not None:
                self.stop_event.set()  # langsung: jalur lain berhenti sekarang, bukan saat T-lead
            return
        try:
            self._product_title = await self.page.evaluate(PRODUCT_TITLE_JS)
        except PlaywrightError:
            self._product_title = ""
        if self._buy is None:
            self.log.warn("arm: tombol Beli belum ditemukan, akan dicari ulang saat polling")
        await self._select_variant()
        await self._ensure_qty_one_product()
        if self._buy is not None:
            try:
                await self._buy.scroll_into_view_if_needed(timeout=3000)
            except PlaywrightError:
                pass
        self.log.mark("armed", f"status {c.state}, produk {self._product_title[:40]!r}")

    async def _select_variant(self, wait_s: float = 0.0, gate: PollingGate | None = None) -> bool | None:
        """Pilih variasi bila belum terpilih. Di fase polling (`gate`) klik chip = aksi polling: lewat RateLimiter
        & jendela T-1..T+8 s (None = jendela habis, tidak diklik)."""
        if not self.variant:
            return True
        loop = asyncio.get_running_loop()
        opt = await self._wait_resolve("variant_option", loop.time() + wait_s) if wait_s else \
            await self._resolve("variant_option")
        if opt is None:
            self.log.warn(f"variasi '{self.variant}' tidak ditemukan")
            self._variant_ok = False
            return False
        if await self._is_selected(opt):
            self._variant_ok = True
            return True
        try:
            if not await opt.evaluate(ENABLED_JS):
                self.log.warn(f"variasi '{self.variant}' belum bisa dipilih")
                return False
            if gate is not None and not await gate.acquire():
                return None
            await self._click(opt, timeout=3000)
        except PlaywrightError as e:
            self.log.warn(f"gagal memilih variasi: {_first_line(e)}")
            return False
        self.log.mark("variant_selected", self.variant)
        self._variant_ok = True
        return True

    async def _ensure_qty_one_product(self) -> None:
        qty = await self._resolve("quantity_input")
        if qty is None:
            return
        try:
            value = await qty.input_value(timeout=2000)
            if value.strip() != "1":
                self._checkpoint()
                await qty.fill("1")
                self.log.mark("qty_set", f"{value} -> 1")
        except PlaywrightError:
            pass

    # ------------------------------------------------------------------ attempt

    async def attempt(self, clock: ServerClock, live: bool) -> RunResult:
        self.clock = clock
        self._live = live and not self.rehearsal
        if live:
            try:
                require_live_ready(self.cfg)
            except ConfigError as e:
                return await self._finish(RunStatus.ERROR, str(e), live)
        self._tracking = True
        self._unknown_since = None
        try:
            status, message = await self._attempt(clock, live)
        except _Aborted as e:
            status, message = RunStatus.ABORTED, str(e)
        except _Stop as e:
            status, message = e.status, e.message
        except PlaywrightTimeout as e:
            status, message = RunStatus.TIMEOUT, _first_line(e)
        except PlaywrightError as e:
            status, message = RunStatus.ERROR, _first_line(e)
        finally:
            self._tracking = False
        return await self._finish(status, message, live)

    async def _attempt(self, clock: ServerClock, live: bool) -> tuple[RunStatus, str]:
        if self.open_at is None:
            raise RuntimeError("arm() belum dipanggil")
        # hasil arm dulu: arm sendiri yang men-set stop global saat captcha/verifikasi
        if self._arm_state is not None:
            return TERMINAL[self._arm_state.state], f"saat membuka halaman: {self._arm_state.evidence}"
        self._checkpoint()
        gate = PollingGate(clock, self.open_at, self.limiter)
        loop = asyncio.get_running_loop()
        self.log.mark("poll_start")

        clicks = 0
        last_reload = None
        outcome: Classification | None = None
        while True:
            self._poll_checkpoint()
            kind, info = await self._wait_ready(gate, last_reload)
            if kind == "stop":
                return TERMINAL[info.state], info.evidence
            if kind == "expired":
                return self._window_closed(clicks)
            if kind == "reload":
                if not await gate.acquire():
                    return self._window_closed(clicks)
                self._poll_checkpoint()
                await self.page.reload(wait_until="domcontentloaded")
                last_reload = clock.now()
                self.log.mark("reload")
                self._blocked_price = _NO_PRICE
                self._buy = await self._wait_resolve("buy_button", loop.time() + 2.0)
                await self._select_variant(wait_s=2.0, gate=gate)  # variasi lepas setelah reload -> pilih ulang
                continue
            price: pricing.ProductPrice = info
            t_ready = clock.now_ms()
            if not await gate.acquire():
                return self._window_closed(clicks)
            self._poll_checkpoint()
            stale = outcome is not None and outcome.state in (PageState.NOT_STARTED, PageState.VARIANT_REQUIRED)
            t_click = clock.now_ms()
            await self._click(self._buy, force=True, timeout=2000)
            clicks += 1
            if clicks == 1:
                self.log.mark("buy_ready", price.describe(self.limits), t_ms=t_ready)
            self.log.mark("click_buy", f"#{clicks}", t_ms=t_click)
            outcome = await self._wait_after_buy(loop, stale)
            st = outcome.state
            if st in (PageState.CART, PageState.CHECKOUT):
                break
            if st in TERMINAL:
                return TERMINAL[st], outcome.evidence
            if st == PageState.NOT_STARTED:
                self.log.mark("not_started", f"#{clicks}")
                continue
            if st == PageState.VARIANT_REQUIRED:
                if not self.variant:
                    return RunStatus.ERROR, "produk wajib pilih variasi; isi `variant` di target.yaml"
                picked = await self._select_variant(gate=gate)
                if picked is None:
                    return self._window_closed(clicks)
                if not picked:
                    return RunStatus.ERROR, f"variasi '{self.variant}' tidak bisa dipilih"
                continue
            if st == PageState.PIN_SCREEN:
                # PIN tanpa klik "Buat Pesanan" dari alat: pesanan mungkin sudah terbuat -> jalur lain berhenti
                # SEKARANG (gate menolak), hasil UNKNOWN_STATE + pesan wajib "Pesanan MUNGKIN sudah terbuat"
                self.order_clicked = True
                if self.stop_event is not None:
                    self.stop_event.set()
                return RunStatus.UNKNOWN_STATE, "layar PIN muncul setelah klik Beli"
            self.log.mark("no_response", f"#{clicks} ({outcome.evidence or st})")

        # ---- klik Beli berhasil: langkah maju sekali-sekali, tanpa throttle, batas 30 s
        self.log.mark("buy_ok", str(outcome.state))
        deadline = loop.time() + self.flow_timeout_s
        if outcome.state == PageState.CART:
            await self._wait_loaded(deadline)
            await self._cart_guard()
            btn = await self._wait_resolve("cart_checkout", deadline)
            if btn is None:
                return RunStatus.ERROR, "tombol Checkout di keranjang tidak ditemukan"
            t_click = clock.now_ms()
            await self._click(btn, timeout=5000)  # Playwright ikut menunggu navigasi commit
            self.log.mark("click_checkout", t_ms=t_click)
            outcome = await self._wait_for({PageState.CHECKOUT}, deadline)
        if outcome.state != PageState.CHECKOUT:
            return self._unexpected(outcome, "halaman checkout")
        await self._wait_loaded(deadline)
        self.log.mark("checkout_loaded")

        ok, msg = await self._ensure_shopeepay(deadline)
        if not ok:
            return RunStatus.ERROR, msg
        self.log.mark("payment_ok", msg)

        place = await self._wait_resolve("place_order", deadline)
        if place is None:
            return RunStatus.ERROR, "tombol 'Buat Pesanan' tidak ditemukan"
        await self._checkout_guard()  # lapis 3: penentu akhir, dry-run maupun live
        self._checkpoint()
        if not self.before_place_order():
            return RunStatus.ABORTED, "before_place_order() menolak (lock dipegang jalur lain)"
        self.log.mark("place_order_gate", "live" if live else "dry-run: berhenti di sini")
        if not live:
            return RunStatus.DRYRUN_OK, "sampai checkout dengan ShopeePay & harga lolos; 'Buat Pesanan' TIDAK diklik"

        self._checkpoint()  # stop global terakhir sebelum klik yang mengikat
        t_click = clock.now_ms()
        await self._click_place_order(place)
        self.log.mark("click_place_order", t_ms=t_click)
        outcome = await self._wait_for({PageState.PIN_SCREEN}, deadline)
        if outcome.state != PageState.PIN_SCREEN:
            return self._unexpected(outcome, "layar PIN")
        self.log.mark("pin_screen")
        return RunStatus.ORDER_PLACED_AWAIT_PIN, "pesanan dibuat; masukkan PIN ShopeePay secara manual"

    async def _click_place_order(self, place: Locator) -> None:
        """SATU-SATUNYA klik "Buat Pesanan". Dilarang di level fungsi pada rehearsal & dry-run (melempar sebelum
        apa pun dikirim)."""
        if self.rehearsal or not self._live:
            mode = "rehearsal" if self.rehearsal else "dry-run"
            raise PlaceOrderForbidden(f"klik 'Buat Pesanan' dilarang di mode {mode}")
        self.order_clicked = True  # sejak sini stop global tidak menghentikan penantian layar PIN
        await place.click(timeout=5000)

    def _window_closed(self, clicks: int) -> tuple[RunStatus, str]:
        """Jendela polling habis: PRICE_GUARD bila terakhir terhalang harga, selain itu NOT_STARTED_TIMEOUT."""
        if self._price_blocked and self._last_price is not None:
            hint = f"; variasi {self.variant!r} tidak ditemukan/terpilih" if self._variant_ok is False else ""
            return RunStatus.PRICE_GUARD, (f"jendela polling habis tanpa harga valid: "
                                           f"{self._last_price.describe(self.limits)} ({clicks} klik){hint}")
        return RunStatus.NOT_STARTED_TIMEOUT, f"slot tidak terbuka sampai T+8 s ({clicks} klik)"

    def _unexpected(self, c: Classification, waiting_for: str) -> tuple[RunStatus, str]:
        if c.state in TERMINAL:
            return TERMINAL[c.state], c.evidence
        if c.state == PageState.UNKNOWN and c.evidence == "timeout":
            return RunStatus.TIMEOUT, f"{waiting_for} tidak muncul dalam {self.flow_timeout_s:.0f} s"
        return RunStatus.ERROR, f"menunggu {waiting_for}, dapat {c.state} {c.evidence}".strip()

    # ------------------------------------------------------------------ lapis 1: produk

    async def _wait_ready(self, gate: PollingGate, last_reload: float | None):
        """Tunggu tombol Beli aktif + harga valid, tanpa mengirim request.

        Return ("ready", ProductPrice) | ("expired", None) | ("reload", None) | ("stop", Classification).
        """
        while True:
            self._poll_checkpoint()
            if gate.expired():
                return "expired", None
            if self._buy is None:
                self._buy = await self._resolve("buy_button")
            snap = None
            if self._buy is not None:
                handle = None
                try:
                    handle = await self._buy.element_handle(timeout=200)
                    snap = await handle.evaluate(WAIT_READY_JS, {
                        "timeoutMs": 250, "lastPrice": self._blocked_price,
                        "priceCss": self.sel.css("product_price")})
                except PlaywrightError:
                    snap = None
                finally:
                    if handle is not None:
                        await handle.dispose()
            if snap is not None:
                self._observe(Classification(PageState.PRODUCT_WAITING, "tombol Beli"))
                if not snap["connected"]:
                    self._buy = None  # elemen diganti; resolve ulang
                    continue
                if snap["enabled"]:
                    price = pricing.check_product_price(snap["price"], self.limits)
                    self._last_price = price
                    self._price_blocked = price.verdict != "ok"
                    if price.verdict == "ok":
                        self._blocked_price = _NO_PRICE
                        return "ready", price
                    if snap["price"] != self._blocked_price:  # catat sekali per teks harga
                        self._blocked_price = snap["price"]
                        self.log.mark("price_not_yet" if price.verdict == "high" else "price_unreadable",
                                      price.describe(self.limits))
                else:
                    self._price_blocked = False
            else:
                await asyncio.sleep(0.05)
            c = await self._classify(with_buy=True)
            if c.state in TERMINAL:
                return "stop", c
            now = gate.clock.now()
            if now >= gate.open_at + FIRST_RELOAD_S and (
                    last_reload is None or now - last_reload >= RELOAD_EVERY_S):
                return "reload", None

    async def _wait_after_buy(self, loop: asyncio.AbstractEventLoop, stale: bool) -> Classification:
        t0 = loop.time()
        seen_clear = not stale
        while True:
            self._checkpoint()
            c = await self._classify(with_buy=True)
            st = c.state
            elapsed = loop.time() - t0
            if st in (PageState.CART, PageState.CHECKOUT, PageState.PIN_SCREEN) or st in TERMINAL:
                return c
            if st in (PageState.NOT_STARTED, PageState.VARIANT_REQUIRED):
                if seen_clear or elapsed > STALE_TOAST_S:
                    return c
            else:
                seen_clear = True
            if st == PageState.UNKNOWN:
                # halaman tak dikenal (bisa tantangan yang tidak terbaca): jangan klik ulang/reload di atasnya;
                # jaring UNKNOWN_STATE (_observe) yang memutuskan
                if elapsed > self.flow_timeout_s:
                    return Classification(PageState.UNKNOWN, "timeout")
                await asyncio.sleep(POLL_S)
                continue
            if self._nav_pending:
                if elapsed > self.flow_timeout_s:
                    return Classification(PageState.UNKNOWN, "timeout")
            elif elapsed > NO_RESPONSE_S:
                return Classification(PageState.PRODUCT_ACTIVE, "tidak ada reaksi")
            await asyncio.sleep(POLL_S)

    async def _wait_for(self, targets: set[PageState], deadline: float) -> Classification:
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            self._checkpoint()
            c = await self._classify()
            if c.state in targets or c.state in TERMINAL:
                return c
            await asyncio.sleep(POLL_S)
        return Classification(PageState.UNKNOWN, "timeout")

    async def _wait_loaded(self, deadline: float) -> None:
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=remaining * 1000)
        except PlaywrightTimeout:
            pass

    async def _wait_resolve(self, step: str, deadline: float) -> Locator | None:
        found = await self._wait_resolve_cand(step, deadline)
        return found[0] if found else None

    async def _wait_resolve_cand(self, step: str, deadline: float) -> tuple[Locator, dict] | None:
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            self._checkpoint()
            found = await self._resolve_cand(step)
            if found is not None:
                return found
            await asyncio.sleep(POLL_S)
        return None

    # ------------------------------------------------------------------ lapis 2: keranjang

    async def _read_cart(self) -> tuple[list[pricing.CartRow], list[int]]:
        data = await self.page.evaluate(CART_JS, {"rowCss": self.sel.layout.get("cart_row", [])})
        rows = [pricing.CartRow(r["text"], bool(r["checked"]),
                                r["qty"] if r["qty"] is not None else pricing.parse_qty(r["text"]))
                for r in data["rows"]]
        counts = [int(re.search(r"\d+", c).group()) for c in data["counts"]]
        return rows, counts

    async def _cart_guard(self) -> None:
        target = self.cfg.expected_name or self._product_title[:25] or None
        rows, counts = await self._read_cart()
        if not rows:
            if counts and set(counts) != {1}:
                raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: tombol Checkout menunjukkan {counts} item, "
                                                   "baris item tidak terbaca")
            self.log.warn("keranjang: baris item tidak terbaca; diputuskan di checkout (lapis 3)")
            return
        verdict = pricing.check_cart(rows, target, strict=bool(self.cfg.expected_name))
        if verdict.to_uncheck:
            for i in verdict.to_uncheck:
                self._checkpoint()
                box = (await self.page.evaluate_handle(CART_BOX_JS, i)).as_element()
                if box is None:
                    break
                await self._click(box, timeout=3000)
                self.log.mark("cart_uncheck", rows[i].text.splitlines()[0][:50])
            await asyncio.sleep(0.2)
            rows, counts = await self._read_cart()
            verdict = pricing.check_cart(rows, target, strict=bool(self.cfg.expected_name))
        if not verdict.ok:
            names = "; ".join(r.text.splitlines()[0][:40] for r in rows if r.checked)
            raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: {verdict.reason} [{names}]")
        if counts and set(counts) != {1}:
            raise _Stop(RunStatus.PRICE_GUARD, f"keranjang: tombol Checkout menunjukkan {counts} item")
        self.log.mark("cart_ok", verdict.reason)

    # ------------------------------------------------------------------ lapis 3: checkout

    async def _checkout_guard(self, enforce: bool = True) -> pricing.CheckoutVerdict:
        cfg = {"rowCss": self.sel.layout.get("checkout_row", []),
               "totalCss": self.sel.layout.get("checkout_total", []),
               "shippingCss": self.sel.layout.get("checkout_shipping", []),
               "totalLabel": TOTAL_LABEL, "shippingLabel": SHIPPING_LABEL}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.price_stable_timeout_s
        prev_total: int | None = None
        shipping = total = None
        while True:
            self._checkpoint()
            try:
                data = await self.page.evaluate(CHECKOUT_JS, cfg)
                snap = pricing.CheckoutSnapshot(data["rows"], data["totals"], data["shippings"], data["pageText"])
                shipping, total = pricing.read_total(snap)
            except PlaywrightError:
                snap = None
            if snap is not None and shipping is not None and total is not None and total == prev_total:
                break
            prev_total = total if shipping is not None else None
            if loop.time() >= deadline:
                raise _Stop(RunStatus.PRICE_GUARD,
                            f"total checkout tidak stabil/terbaca dalam {self.price_stable_timeout_s:.1f} s "
                            f"(ongkir {pricing.rupiah(shipping)}, total {pricing.rupiah(total)})")
            await asyncio.sleep(PRICE_STABLE_GAP_S)
        verdict = pricing.check_checkout(snap, self.limits)
        self.log.info(f"checkout terbaca: {verdict.summary()}")
        if not verdict.ok:
            if not enforce:  # rehearsal: dievaluasi & dilaporkan, tidak menghentikan
                return verdict
            raise _Stop(RunStatus.PRICE_GUARD, f"{'; '.join(verdict.reasons)} | {verdict.summary()}")
        self.log.mark("price_guard_ok", verdict.summary())
        return verdict

    async def _ensure_shopeepay(self, deadline: float) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()
        opt_deadline = min(deadline, loop.time() + SELECT_WAIT_S)
        opt = await self._wait_resolve("payment_shopeepay", opt_deadline)
        if opt is None and self.sel.candidates("payment_change"):
            change = await self._resolve("payment_change")
            if change is not None:
                t_click = self.clock.now_ms()
                await self._click(change, timeout=5000)
                self.log.mark("payment_change", t_ms=t_click)
                opt = await self._wait_resolve("payment_shopeepay", deadline)
        if opt is None:
            return False, "opsi ShopeePay tidak ditemukan di checkout"
        if await self._is_selected(opt):
            return True, "ShopeePay sudah terpilih"
        t_click = self.clock.now_ms()
        await self._click(opt, timeout=5000)
        self.log.mark("select_shopeepay", t_ms=t_click)
        end = min(deadline, loop.time() + SELECT_WAIT_S)
        while loop.time() < end:
            if await self._is_selected(opt):
                return True, "ShopeePay dipilih (sebelumnya metode lain)"
            await asyncio.sleep(POLL_S)
        return False, "ShopeePay tidak terverifikasi terpilih; jalankan calibrate ulang"

    # ------------------------------------------------------------------ rehearsal

    async def rehearse(self, clock: ServerClock) -> RehearsalReport:
        """Rehearsal sekali jalan di produk target asli (lihat flashbuy.rehearsal): buka produk -> variasi -> klik
        Beli (lapis 1 hanya dievaluasi) -> keranjang (lapis 2 dievaluasi) -> Checkout -> ShopeePay -> baca checkout
        (lapis 3 dievaluasi) -> cari tombol "Buat Pesanan" TANPA klik -> STOP. Klik "Buat Pesanan" dilarang di
        fungsi kliknya; before_place_order diganti fungsi yang melempar."""
        self.rehearsal, self._live = True, False
        self.before_place_order = forbidden_gate
        self.clock = clock
        rep = RehearsalReport(self.name)
        self._tracking, self._unknown_since = True, None
        try:
            await self._rehearse(rep, clock)
        except _Stop as e:
            rep.stop(str(e.status), e.message)
        except _Aborted as e:
            rep.stop("ABORTED", str(e))
        except PlaywrightError as e:
            rep.stop("ERROR", f"browser: {_first_line(e)}")
        finally:
            self._tracking = False
        if self.order_clicked and not rep.message.startswith(MAYBE_ORDERED_MSG):  # PIN tanpa klik "Buat Pesanan"
            rep.stop("UNKNOWN_STATE", f"{MAYBE_ORDERED_MSG} ({rep.message})")
        if rep.status in STOP_STATUSES:  # captcha/verifikasi/login/UNKNOWN_STATE/PIN: stop + alarm, tanpa retry
            if rep.status != "LOGIN_REQUIRED" and self.stop_event is not None:
                self.stop_event.set()
            self.notifier.alarm(rep.status, f"rehearsal web: {rep.message}", platform=self.name)
        elif rep.status in ("OK", "GAGAL") and rep.clicked_buy:  # keranjang hanya berubah bila Beli diklik
            rep.cart_items = await self._rehearse_cart()
        self.log.mark("result", f"rehearsal {rep.status}: {rep.message or 'semua langkah OK'}")
        self.log.info(cart_line(rep.cart_items))
        rep.write(self.log.run_dir)
        return rep

    async def _rh(self, rep: RehearsalReport, name: str, t0: float, ok: bool, *, selector: str = "",
                  values: dict | None = None, detail: str = "") -> None:
        """Catat satu langkah rehearsal: latensi (sebelum screenshot), screenshot, log."""
        ms = (asyncio.get_running_loop().time() - t0) * 1000
        shot = ""
        if self.page is not None:
            path = self.log.screenshot_path(f"rehearsal-{len(rep.steps) + 1}-{name}")
            try:
                await self.page.screenshot(path=str(path), timeout=5000)
                shot = str(path)
            except PlaywrightError as e:
                self.log.warn(f"screenshot gagal: {_first_line(e)}")
        rep.add(RehearsalStep(name, ok, selector, round(ms, 1), values or {}, shot, detail))
        text = f"rehearsal {name}: {'OK' if ok else 'GAGAL'}" + (f" [{selector}]" if selector else "")
        (self.log.info if ok else self.log.warn)(text + (f" - {detail}" if detail else ""))

    async def _rehearse(self, rep: RehearsalReport, clock: ServerClock) -> None:
        loop = asyncio.get_running_loop()
        # 1. buka produk
        t0 = loop.time()
        await self._goto(self.cfg.product_url)
        found = await self._resolve_cand("buy_button")
        self._buy = found[0] if found else None
        c = await self._classify(with_buy=True)
        if c.state in (PageState.LOGIN_REQUIRED, PageState.CAPTCHA, PageState.VERIFICATION):
            raise _Stop(TERMINAL[c.state], f"saat membuka produk: {c.evidence}")
        try:
            self._product_title = await self.page.evaluate(PRODUCT_TITLE_JS) or ""
        except PlaywrightError:
            self._product_title = ""
        values = {"url": self.page.url, "judul": self._product_title}
        if self.cfg.expected_name:
            values["nama cocok expected_name"] = pricing.name_matches(self._product_title, self.cfg.expected_name)
        await self._rh(rep, "buka produk", t0, True, values=values)
        # 2. tombol Beli
        t0 = loop.time()
        if found is None:
            await self._rh(rep, "tombol Beli", t0, False, detail="tidak ditemukan - jalankan calibrate")
            return
        try:
            label, enabled = (await self._buy.inner_text(timeout=2000)).strip(), await self._buy.evaluate(ENABLED_JS)
        except PlaywrightError:
            label, enabled = "", None
        await self._rh(rep, "tombol Beli", t0, True, selector=_cand(found[1]), values={"teks": label, "aktif": enabled})
        # 3. harga produk: lapis 1 dievaluasi, TIDAK ditegakkan (harga normal saat rehearsal)
        t0 = loop.time()
        price = pricing.check_product_price(await self.page.evaluate(PRICE_TEXT_JS, self.sel.css("product_price")),
                                            self.limits)
        rep.guards["lapis 1 harga produk"] = guard_text(price.verdict == "ok", price.describe(self.limits))
        await self._rh(rep, "baca harga produk", t0, price.verdict != "unreadable",
                       selector=", ".join(self.sel.css("product_price")) or "heuristik (nominal terbesar)",
                       values={"harga": price.value, "teks": price.text})
        # 4. variasi & kuantitas
        if self.variant:
            t0 = loop.time()
            opt = await self._resolve_cand("variant_option")
            picked = await self._select_variant(wait_s=2.0)
            await self._rh(rep, "pilih variasi", t0, bool(picked), selector=_cand(opt[1]) if opt else "",
                           values={"variasi": self.variant},
                           detail="" if picked else "variasi tidak ditemukan / tidak bisa dipilih")
            if not picked:
                return
        await self._ensure_qty_one_product()
        # 5. klik Beli sekali (lewat RateLimiter jalur ini)
        t0 = loop.time()
        await clock.wait_until_async(self.limiter.reserve(clock.now()))
        await self._click(self._buy, force=True, timeout=2000)
        outcome = await self._wait_after_buy(loop, stale=False)
        st = outcome.state
        if st == PageState.PIN_SCREEN:  # tanpa klik "Buat Pesanan": pesanan mungkin ada -> stop semua, alarm
            self.order_clicked = True
            raise _Stop(RunStatus.UNKNOWN_STATE, f"{MAYBE_ORDERED_MSG} (layar PIN muncul setelah klik Beli)")
        if st in TERMINAL:
            raise _Stop(TERMINAL[st], outcome.evidence)
        ok = st in (PageState.CART, PageState.CHECKOUT)
        await self._rh(rep, "klik Beli", t0, ok, selector=_cand(found[1]), values={"hasil": str(st)},
                       detail="" if ok else f"{st} {outcome.evidence}".strip())
        if not ok:
            return
        deadline = loop.time() + self.flow_timeout_s
        # 6. keranjang (web): lapis 2 dievaluasi, lalu klik Checkout
        if st == PageState.CART:
            t0 = loop.time()
            await self._wait_loaded(deadline)
            rows, counts = await self._read_cart()
            values = {"item": [r.text.splitlines()[0][:60] for r in rows],
                      "tercentang": sum(r.checked for r in rows), "tombol Checkout": counts}
            try:
                await self._cart_guard()
                rep.guards["lapis 2 keranjang"] = guard_text(True, "1 item tercentang, kuantitas 1")
            except _Stop as e:
                if e.status != RunStatus.PRICE_GUARD:
                    raise
                rep.guards["lapis 2 keranjang"] = guard_text(False, e.message)
            await self._rh(rep, "baca keranjang", t0, bool(rows), values=values,
                           detail="" if rows else "baris keranjang tidak terbaca")
            t0 = loop.time()
            btn = await self._wait_resolve_cand("cart_checkout", deadline)
            if btn is None:
                await self._rh(rep, "klik Checkout", t0, False, detail="tombol Checkout tidak ditemukan")
                return
            await self._click(btn[0], timeout=5000)
            outcome = await self._wait_for({PageState.CHECKOUT}, deadline)
            if outcome.state in TERMINAL:
                raise _Stop(TERMINAL[outcome.state], outcome.evidence)
            ok = outcome.state == PageState.CHECKOUT
            await self._rh(rep, "klik Checkout", t0, ok, selector=_cand(btn[1]),
                           detail="" if ok else f"{outcome.state} {outcome.evidence}".strip())
            if not ok:
                return
        # 7. checkout + ShopeePay
        t0 = loop.time()
        await self._wait_loaded(deadline)
        await self._rh(rep, "halaman checkout", t0, True, values={"url": self.page.url})
        t0 = loop.time()
        ok, msg = await self._ensure_shopeepay(deadline)
        pay = await self._resolve_cand("payment_shopeepay")
        await self._rh(rep, "ShopeePay", t0, ok, selector=_cand(pay[1]) if pay else "", detail=msg)
        # 8. baca checkout: lapis 3 dievaluasi, tidak ditegakkan
        t0 = loop.time()
        try:
            verdict = await self._checkout_guard(enforce=False)
        except _Stop as e:
            if e.status != RunStatus.PRICE_GUARD:
                raise
            rep.guards["lapis 3 checkout"] = guard_text(False, e.message)
            await self._rh(rep, "baca checkout", t0, False, detail=e.message)
        else:
            v = verdict.values
            rep.guards["lapis 3 checkout"] = guard_text(
                verdict.ok, ("; ".join(verdict.reasons) + " | " if verdict.reasons else "") + verdict.summary())
            missing = [r for r in verdict.reasons if "terbaca" in r]
            await self._rh(rep, "baca checkout", t0, not missing, values={
                "nama cocok": v.get("name_ok"), "variasi cocok": v.get("variant_ok"), "qty": v.get("qty"),
                "harga": v.get("item_price"), "ongkir": v.get("shipping"), "total": v.get("total")},
                detail="; ".join(missing))
        # 9. tombol "Buat Pesanan": hanya DICARI, tidak pernah diklik
        t0 = loop.time()
        place = await self._wait_resolve_cand("place_order", deadline)
        await self._rh(rep, "tombol Buat Pesanan (tidak diklik)", t0, place is not None,
                       selector=_cand(place[1]) if place else "",
                       detail="ditemukan; rehearsal berhenti di sini" if place else
                       "tidak ditemukan - jalankan calibrate")

    async def _rehearse_cart(self) -> list[str] | None:
        """Isi keranjang di akhir rehearsal (dilaporkan untuk dihapus manual; alat tidak menghapusnya)."""
        try:
            await self._goto(self._url("cart_page"))
            await self._wait_loaded(asyncio.get_running_loop().time() + 5.0)
            rows, _ = await self._read_cart()
        except (PlaywrightError, _Stop, _Aborted, KeyError) as e:
            self.log.warn(f"isi keranjang tidak terbaca: {e}")
            return None
        return [r.text.splitlines()[0][:60] + (f" (qty {r.qty})" if r.qty not in (None, 1) else "") for r in rows]

    # ------------------------------------------------------------------ akhir

    async def _finish(self, status: RunStatus, message: str, live: bool) -> RunResult:
        detail = ""
        if self.order_clicked:
            status, message, detail = after_order_click(status, message)
        self.log.mark("result", f"{status}: {message}" + (f" ({detail})" if detail else ""))
        result = RunResult(self.name, status, message, live, steps=list(self.log.steps), detail=detail)
        if status in STOP_ALL_STATUSES and self.stop_event is not None:
            self.stop_event.set()
        if status in ALARM_STATUSES or self.order_clicked:
            self.notifier.alarm(str(status), message, platform=self.name)
        if self.page is not None:  # di luar hot path: URL/judul + screenshot status akhir
            try:
                self.log.info(f"halaman akhir: url={self.page.url} judul={await self.page.title()!r}")
            except PlaywrightError:
                pass
            path = self.log.screenshot_path(str(status))
            try:
                await self.page.screenshot(path=str(path), timeout=5000)
                result.screenshots.append(path)
            except PlaywrightError as e:
                self.log.warn(f"screenshot gagal: {_first_line(e)}")
        self.log.write_result(result)
        return result
