"""Runner jalur Web (Playwright, profil Chrome persisten, headed).

Tidak ada stealth plugin, patch navigator.webdriver, atau spoof UA/fingerprint. Semua aksi
berupa klik UI biasa. Hot path (tunggu tombol aktif -> klik) tanpa screenshot/tracing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from playwright.async_api import BrowserContext, Locator, Page, Playwright, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from flashbuy import selector_store
from flashbuy.config import FLOW_TIMEOUT_S, TargetConfig, WebConfig
from flashbuy.guards import ENABLED_JS, TERMINAL, WAIT_ENABLED_JS, Classification, Guard, PageState
from flashbuy.notifier import Notifier
from flashbuy.runner_base import (
    NEEDS_USER_STATUSES,
    STOP_ALL_STATUSES,
    PollingGate,
    PrecheckItem,
    PrecheckResult,
    RateLimiter,
    RunLog,
    RunResult,
    RunStatus,
    always_allow,
)
from flashbuy.selector_store import SelectorSet
from flashbuy.timesync import ServerClock

POLL_S = 0.02  # interval cek status halaman (lokal, tanpa request ke server)
NO_RESPONSE_S = 1.5  # klik Beli tanpa reaksi & tanpa navigasi -> boleh klik ulang
STALE_TOAST_S = 0.4  # toast lama yang masih tampil diabaikan selama ini
RELOAD_AFTER_S = 2.0  # tombol masih nonaktif T+2 s -> reload (terhitung aksi polling)
RELOAD_EVERY_S = 2.0
SELECT_WAIT_S = 3.0

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

_RP_RE = re.compile(r"Rp\s?([\d.]+)")


def parse_rupiah(text: str) -> int | None:
    m = _RP_RE.search(text or "")
    return int(m.group(1).replace(".", "")) if m else None


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


class WebRunner:
    name = "web"

    def __init__(self, cfg: TargetConfig, selectors: SelectorSet, *, log: RunLog, notifier: Notifier,
                 headless: bool = False, before_place_order: Callable[[], bool] = always_allow,
                 limiter: RateLimiter | None = None, stop_event: asyncio.Event | None = None):
        self.cfg = cfg
        self.sel = selectors
        self.guard = Guard(selectors)
        self.log = log
        self.notifier = notifier
        self.headless = headless
        self.before_place_order = before_place_order
        self.limiter = limiter or RateLimiter()
        self.stop_event = stop_event
        self.variant = cfg.variant
        self.open_at: float | None = None
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._buy: Locator | None = None
        self._arm_state: Classification | None = None
        self._abort_reason: str | None = None
        self._nav_pending = False
        self.order_clicked = False  # "Buat Pesanan" sudah diklik (pesanan mungkin sudah dibuat)
        self.flow_timeout_s = FLOW_TIMEOUT_S  # bisa diskalakan di tes

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
        self.log.info(f"browser siap (channel={self.cfg.web.channel or 'chromium'}, headless={self.headless})")

    def _on_request(self, req) -> None:
        if req.is_navigation_request() and req.frame == self.page.main_frame:
            self._nav_pending = True

    def _on_nav_done(self, req) -> None:
        if req.is_navigation_request() and req.frame == self.page.main_frame:
            self._nav_pending = False

    async def abort(self, reason: str = "") -> None:
        self._abort_reason = reason or "dibatalkan"

    async def close(self) -> None:
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

    def _check_abort(self) -> None:
        if self._abort_reason is None and self.stop_event is not None and self.stop_event.is_set():
            self._abort_reason = "dihentikan oleh runner lain"
        if self._abort_reason is not None:
            raise _Aborted(self._abort_reason)

    # ------------------------------------------------------------------ util

    def _url(self, key: str) -> str:
        return self.cfg.origin + self.sel.urls[key]

    async def _classify(self, with_buy: bool = False) -> Classification:
        return await self.guard.classify(self.page, self._buy if with_buy else None)

    async def _resolve(self, step: str) -> Locator | None:
        found = await selector_store.resolve(self.page, self.sel.candidates(step), self.variant)
        return found[0] if found else None

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
        page = self.page
        await page.goto(self._url("address_page"), wait_until="domcontentloaded")
        if (await self._classify()).state == PageState.LOGIN_REQUIRED:
            res.items.append(PrecheckItem("login", False, "sesi tidak login (diarahkan ke halaman login)"))
            res.status = RunStatus.LOGIN_REQUIRED
            return res
        res.items.append(PrecheckItem("login", True, "sesi aktif"))

        text = await self._body_text()
        if re.search(r"\butama\b", text, re.I):
            res.items.append(PrecheckItem("alamat default", True, "alamat 'Utama' ditemukan"))
        elif re.search(r"belum ada alamat", text, re.I):
            res.items.append(PrecheckItem("alamat default", False, "belum ada alamat tersimpan"))
        else:
            res.items.append(PrecheckItem("alamat default", None, "tidak terbaca dari halaman alamat"))

        await page.goto(self._url("wallet_page"), wait_until="domcontentloaded")
        text = await self._body_text()
        m = re.search(r"saldo[^\n]*\n?[^\n]*?(Rp\s?[\d.]+)", text, re.I)
        balance = parse_rupiah(m.group(1)) if m else None

        await page.goto(self.cfg.product_url, wait_until="domcontentloaded")
        c = await self._classify()
        if c.state == PageState.LOGIN_REQUIRED:
            res.items.append(PrecheckItem("login", False, "halaman produk minta login"))
            res.status = RunStatus.LOGIN_REQUIRED
            return res
        text = await self._body_text()
        fs = re.search(r"flash sale", text, re.I)
        price = parse_rupiah(text[fs.end():]) if fs else None
        ms = re.search(r"(ongkos kirim|ongkir)[^\n]*?(Rp\s?[\d.]+)", text, re.I)
        shipping = parse_rupiah(ms.group(2)) if ms else None

        if balance is None:
            res.items.append(PrecheckItem("saldo ShopeePay", None, "saldo tidak terbaca"))
        elif price is None:
            res.items.append(PrecheckItem("saldo ShopeePay", None,
                                          f"saldo Rp{balance:,} terbaca, harga produk tidak terbaca"))
        else:
            need = price + (shipping or 0)
            note = "" if shipping is not None else " (ongkir tidak terbaca)"
            res.items.append(PrecheckItem("saldo ShopeePay", balance >= need,
                                          f"saldo Rp{balance:,} vs harga+ongkir Rp{need:,}{note}"))
        buy = await self._resolve("buy_button")
        res.items.append(PrecheckItem("tombol Beli", True if buy else None,
                                      "ditemukan" if buy else "tidak ditemukan - jalankan calibrate"))
        return res

    # ------------------------------------------------------------------ arm (T-60 s)

    async def arm(self, open_at: float) -> None:
        self.open_at = open_at
        await self.page.goto(self.cfg.product_url, wait_until="domcontentloaded")
        self.log.mark("page_open")
        self._buy = await self._resolve("buy_button")
        c = await self._classify(with_buy=True)
        if c.state in (PageState.LOGIN_REQUIRED, PageState.CAPTCHA, PageState.VERIFICATION):
            self._arm_state = c
            self.log.warn(f"arm: {c.state} ({c.evidence})")
            return
        if self._buy is None:
            self.log.warn("arm: tombol Beli belum ditemukan, akan dicari ulang saat polling")
        await self._select_variant()
        await self._ensure_qty_one_product()
        if self._buy is not None:
            try:
                await self._buy.scroll_into_view_if_needed(timeout=3000)
            except PlaywrightError:
                pass
        self.log.mark("armed", f"status {c.state}")

    async def _select_variant(self) -> bool:
        if not self.variant:
            return True
        opt = await self._resolve("variant_option")
        if opt is None:
            self.log.warn(f"variasi '{self.variant}' tidak ditemukan")
            return False
        if await self._is_selected(opt):
            return True
        try:
            if not await opt.evaluate(ENABLED_JS):
                self.log.warn(f"variasi '{self.variant}' belum bisa dipilih")
                return False
            await opt.click(timeout=3000)
        except PlaywrightError as e:
            self.log.warn(f"gagal memilih variasi: {e.message.splitlines()[0]}")
            return False
        self.log.mark("variant_selected", self.variant)
        return True

    async def _ensure_qty_one_product(self) -> None:
        qty = await self._resolve("quantity_input")
        if qty is None:
            return
        try:
            value = await qty.input_value(timeout=2000)
            if value.strip() != "1":
                await qty.fill("1")
                self.log.mark("qty_set", f"{value} -> 1")
        except PlaywrightError:
            pass

    # ------------------------------------------------------------------ attempt

    async def attempt(self, clock: ServerClock, live: bool) -> RunResult:
        self.clock = clock
        try:
            status, message = await self._attempt(clock, live)
        except _Aborted as e:
            status, message = RunStatus.ABORTED, str(e)
        except _Stop as e:
            status, message = e.status, e.message
        except PlaywrightTimeout as e:
            status, message = RunStatus.TIMEOUT, e.message.splitlines()[0]
        except PlaywrightError as e:
            status, message = RunStatus.ERROR, e.message.splitlines()[0]
        return await self._finish(status, message, live)

    async def _attempt(self, clock: ServerClock, live: bool) -> tuple[RunStatus, str]:
        if self.open_at is None:
            raise RuntimeError("arm() belum dipanggil")
        if self._arm_state is not None:
            return TERMINAL[self._arm_state.state], f"saat membuka halaman: {self._arm_state.evidence}"
        gate = PollingGate(clock, self.open_at, self.limiter)
        loop = asyncio.get_running_loop()
        self.log.mark("poll_start")

        clicks = 0
        last_reload = None
        outcome: Classification | None = None
        while True:
            self._check_abort()
            ready = await self._wait_buy_enabled(gate, last_reload)
            if isinstance(ready, Classification):
                return TERMINAL[ready.state], ready.evidence
            if ready == "expired":
                return RunStatus.NOT_STARTED_TIMEOUT, f"slot tidak terbuka sampai T+8 s ({clicks} klik)"
            if ready == "reload":
                if not await gate.acquire():
                    return RunStatus.NOT_STARTED_TIMEOUT, "jendela polling habis (reload)"
                self._check_abort()
                await self.page.reload(wait_until="domcontentloaded")
                last_reload = clock.now()
                self.log.mark("reload")
                self._buy = await self._resolve("buy_button")
                await self._select_variant()
                continue
            t_enabled = clock.now_ms()
            if not await gate.acquire():
                return RunStatus.NOT_STARTED_TIMEOUT, f"jendela polling habis ({clicks} klik)"
            self._check_abort()
            stale = outcome is not None and outcome.state in (PageState.NOT_STARTED, PageState.VARIANT_REQUIRED)
            t_click = clock.now_ms()
            await self._buy.click(force=True, timeout=2000)
            clicks += 1
            if clicks == 1:
                self.log.mark("buy_enabled", t_ms=t_enabled)
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
                if not await self._select_variant():
                    return RunStatus.ERROR, f"variasi '{self.variant}' tidak bisa dipilih"
                continue
            if st == PageState.PIN_SCREEN:
                return RunStatus.ERROR, "layar PIN muncul tak terduga setelah klik Beli"
            self.log.mark("no_response", f"#{clicks} ({outcome.evidence or st})")

        # ---- klik Beli berhasil: langkah maju sekali-sekali, tanpa throttle, batas 30 s
        self.log.mark("buy_ok", str(outcome.state))
        deadline = loop.time() + self.flow_timeout_s
        if outcome.state == PageState.CART:
            await self._wait_loaded(deadline)
            await self._check_qty_cart()
            btn = await self._wait_resolve("cart_checkout", deadline)
            if btn is None:
                return RunStatus.ERROR, "tombol Checkout di keranjang tidak ditemukan"
            self._check_abort()
            t_click = clock.now_ms()
            await btn.click(timeout=5000)  # Playwright ikut menunggu navigasi commit
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
        self._check_abort()
        if not self.before_place_order():
            return RunStatus.ABORTED, "before_place_order() menolak (lock dipegang jalur lain)"
        self.log.mark("place_order_gate", "live" if live else "dry-run: berhenti di sini")
        if not live:
            return RunStatus.DRYRUN_OK, "sampai checkout dengan ShopeePay; 'Buat Pesanan' TIDAK diklik"

        t_click = clock.now_ms()
        self.order_clicked = True
        await place.click(timeout=5000)
        self.log.mark("click_place_order", t_ms=t_click)
        outcome = await self._wait_for({PageState.PIN_SCREEN}, deadline)
        if outcome.state != PageState.PIN_SCREEN:
            return self._unexpected(outcome, "layar PIN")
        self.log.mark("pin_screen")
        return RunStatus.ORDER_PLACED_AWAIT_PIN, "pesanan dibuat; masukkan PIN ShopeePay secara manual"

    def _unexpected(self, c: Classification, waiting_for: str) -> tuple[RunStatus, str]:
        if c.state in TERMINAL:
            return TERMINAL[c.state], c.evidence
        if c.state == PageState.UNKNOWN and c.evidence == "timeout":
            return RunStatus.TIMEOUT, f"{waiting_for} tidak muncul dalam {self.flow_timeout_s:.0f} s"
        return RunStatus.ERROR, f"menunggu {waiting_for}, dapat {c.state} {c.evidence}".strip()

    async def _wait_buy_enabled(self, gate: PollingGate, last_reload: float | None
                                ) -> str | Classification:
        """Tunggu tombol Beli aktif tanpa mengirim request. Return 'ready' | 'expired' | 'reload'
        | Classification terminal."""
        while True:
            self._check_abort()
            if gate.expired():
                return "expired"
            if self._buy is None:
                self._buy = await self._resolve("buy_button")
            handle = None
            if self._buy is not None:
                try:
                    handle = await self._buy.element_handle(timeout=200)
                except PlaywrightError:
                    handle = None
            if handle is not None:
                try:
                    if await handle.evaluate(WAIT_ENABLED_JS, 250):
                        if await handle.evaluate("el => el.isConnected"):
                            return "ready"
                        continue  # elemen diganti; resolve ulang
                except PlaywrightError:
                    pass
                finally:
                    await handle.dispose()
            else:
                await asyncio.sleep(0.05)
            c = await self._classify()
            if c.state in TERMINAL:
                return c
            now = gate.clock.now()
            if now >= gate.open_at + RELOAD_AFTER_S and (
                    last_reload is None or now - last_reload >= RELOAD_EVERY_S):
                return "reload"

    async def _wait_after_buy(self, loop: asyncio.AbstractEventLoop, stale: bool) -> Classification:
        t0 = loop.time()
        seen_clear = not stale
        while True:
            self._check_abort()
            c = await self._classify()
            st = c.state
            elapsed = loop.time() - t0
            if st in (PageState.CART, PageState.CHECKOUT, PageState.PIN_SCREEN) or st in TERMINAL:
                return c
            if st in (PageState.NOT_STARTED, PageState.VARIANT_REQUIRED):
                if seen_clear or elapsed > STALE_TOAST_S:
                    return c
            else:
                seen_clear = True
            if self._nav_pending:
                if elapsed > self.flow_timeout_s:
                    return Classification(PageState.UNKNOWN, "timeout")
            elif elapsed > NO_RESPONSE_S:
                return Classification(PageState.UNKNOWN, "tidak ada reaksi")
            await asyncio.sleep(POLL_S)

    async def _wait_for(self, targets: set[PageState], deadline: float) -> Classification:
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            self._check_abort()
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
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            self._check_abort()
            found = await self._resolve(step)
            if found is not None:
                return found
            await asyncio.sleep(POLL_S)
        return None

    async def _check_qty_cart(self) -> None:
        qty = await self._resolve("quantity_input")
        if qty is None:
            self.log.info("kuantitas di keranjang tidak terbaca")
            return
        value = (await qty.input_value(timeout=2000)).strip()
        if value != "1":
            raise _Stop(RunStatus.ERROR, f"kuantitas di keranjang {value}, bukan 1")

    async def _ensure_shopeepay(self, deadline: float) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()
        opt_deadline = min(deadline, loop.time() + SELECT_WAIT_S)
        opt = await self._wait_resolve("payment_shopeepay", opt_deadline)
        if opt is None and self.sel.candidates("payment_change"):
            change = await self._resolve("payment_change")
            if change is not None:
                t_click = self.clock.now_ms()
                await change.click(timeout=5000)
                self.log.mark("payment_change", t_ms=t_click)
                opt = await self._wait_resolve("payment_shopeepay", deadline)
        if opt is None:
            return False, "opsi ShopeePay tidak ditemukan di checkout"
        if await self._is_selected(opt):
            return True, "ShopeePay sudah terpilih"
        t_click = self.clock.now_ms()
        await opt.click(timeout=5000)
        self.log.mark("select_shopeepay", t_ms=t_click)
        end = min(deadline, loop.time() + SELECT_WAIT_S)
        while loop.time() < end:
            if await self._is_selected(opt):
                return True, "ShopeePay dipilih (sebelumnya metode lain)"
            await asyncio.sleep(POLL_S)
        return False, "ShopeePay tidak terverifikasi terpilih; jalankan calibrate ulang"

    # ------------------------------------------------------------------ akhir

    async def _finish(self, status: RunStatus, message: str, live: bool) -> RunResult:
        if self.order_clicked and status != RunStatus.ORDER_PLACED_AWAIT_PIN:
            message = f"'Buat Pesanan' SUDAH diklik, cek status pesanan secara manual! ({message})"
        self.log.mark("result", f"{status}: {message}")
        result = RunResult(self.name, status, message, live, steps=list(self.log.steps))
        if status in STOP_ALL_STATUSES and self.stop_event is not None:
            self.stop_event.set()
        if status in NEEDS_USER_STATUSES or self.order_clicked:
            self.notifier.alarm(str(status), message, platform=self.name)
        if self.page is not None:  # screenshot hanya di status akhir (di luar hot path)
            path = self.log.screenshot_path(str(status))
            try:
                await self.page.screenshot(path=str(path), timeout=5000)
                result.screenshots.append(path)
            except PlaywrightError as e:
                self.log.warn(f"screenshot gagal: {e.message.splitlines()[0]}")
        self.log.write_result(result)
        return result
