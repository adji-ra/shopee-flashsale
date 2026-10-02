"""Mock Shopee: server HTTP lokal dengan jam server yang bisa digeser dan skenario per tes.

- Header `Date` mengikuti jam mock (untuk uji timesync end-to-end).
- Waktu buka slot dicek di sisi server (POST /api/buy).
- Semua request (kecuali /__admin) dicatat dengan timestamp jam mock.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from tests.mock_shopee import pages

PRODUCT_PATH = "/Ponsel-Uji-Coba-128GB-i.1001.2002"
_PRODUCT_RE = re.compile(r"^/[^/]+-i\.(\d+)\.(\d+)$")


@dataclass
class Scenario:
    name: str = "normal"
    clock_offset_ms: int = 0  # jam mock = jam lokal + offset (juga header Date)
    open_at: float | None = None  # epoch jam mock; None = sudah dibuka
    sale_skew_ms: int = 0  # server penjualan baru menerima X ms setelah open_at
    button_delay_ms: int = 0  # tombol Beli aktif X ms setelah open_at
    sold_out: bool = False
    captcha_after_buy: bool = False
    verification_after_buy: bool = False
    login_expired: bool = False
    payment_default: str = "shopeepay"  # shopeepay | cod | bank
    variants: list[str] = field(default_factory=list)  # tidak kosong = variasi wajib
    checkout_latency_ms: int = 0
    buy_latency_ms: int = 0  # jawaban POST /api/buy ditunda X ms (server sibuk)
    address: str | None = "Jl. Contoh Raya No. 1, Kebayoran Baru, Jakarta Selatan"
    balance: int | None = 1_250_000
    product_name: str = "Ponsel Uji Coba 128GB"
    description: str = pages.DEFAULT_DESCRIPTION
    price: int = 99_000  # harga flash
    variant_prices: dict[str, int] = field(default_factory=dict)  # harga flash per variasi
    original_price: int = 1_499_000  # harga normal
    shipping: int = 12_000
    service_fee: int = 0
    # --- harga & UI halaman produk
    buy_active_before_open: bool = False  # tombol aktif + harga normal sebelum slot dibuka
    flash_available: bool = True  # False: stok flash habis -> harga normal, tombol tetap aktif
    live_update: bool = True  # False: UI tidak berubah live; status baru terlihat setelah reload
    verification_on_reload: bool = False  # GET produk setelah slot buka -> /verify/traffic
    # --- setelah klik Beli
    captcha_redirect_after_buy: bool = False  # redirect penuh ke /verify/captcha
    captcha_iframe_after_buy: bool = False  # overlay iframe captcha (bukan dialog)
    unknown_page_after_buy: bool = False  # redirect ke halaman asing
    pin_after_buy: bool = False  # klik Beli langsung berujung layar PIN (pesanan mungkin sudah terbuat)
    # --- keranjang & checkout
    cart_other_items: list[dict] = field(default_factory=list)  # {"name","price","checked"}
    cart_uncheck_fails: bool = False
    checkout_qty: int | None = None
    checkout_name: str | None = None
    price_format_broken: bool = False
    shipping_delay_ms: int = 0


PRESETS: dict[str, dict] = {
    "normal": {},
    "sold_out": {"sold_out": True},
    "captcha": {"captcha_after_buy": True},
    "verification": {"verification_after_buy": True},
    "login_expired": {"login_expired": True},
    "payment_not_shopeepay": {"payment_default": "cod"},
    "variant_required": {"variants": ["64GB Putih", "128GB Hitam", "256GB Biru"],
                         "variant_prices": {"64GB Putih": 89_000, "128GB Hitam": 99_000, "256GB Biru": 129_000}},
    "normal_price_before_open": {"buy_active_before_open": True},
    "flash_sold_out_normal_price": {"flash_available": False},
    "static_ui": {"live_update": False},
    "cart_other_checked": {"cart_other_items": [
        {"name": "Kabel Data USB-C 1m", "price": 25_000, "checked": True},
        {"name": "Casing HP Bening", "price": 15_000, "checked": False}]},
    "captcha_redirect": {"captcha_redirect_after_buy": True},
    "captcha_iframe": {"captcha_iframe_after_buy": True},
    "unknown_page": {"unknown_page_after_buy": True},
    "pin_after_buy": {"pin_after_buy": True},
}


class MockShopee:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.lock = threading.RLock()
        self.scenario = Scenario()
        self.requests: list[dict] = []
        self.sessions: dict[str, dict] = {}  # sid checkout -> data
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(self))
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    # ---- lifecycle
    def start(self) -> MockShopee:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def product_url(self) -> str:
        return self.base_url + PRODUCT_PATH

    # ---- jam & skenario
    def now(self) -> float:
        return time.time() + self.scenario.clock_offset_ms / 1000.0

    def reset(self) -> None:
        with self.lock:
            self.scenario = Scenario()
            self.requests.clear()
            self.sessions.clear()

    def apply(self, spec: dict) -> Scenario:
        """spec: {"name": preset, "open_in_ms": N, <field>: nilai, ...}"""
        with self.lock:
            spec = dict(spec)
            name = spec.pop("name", None)
            if name is not None:
                if name not in PRESETS:
                    raise ValueError(f"skenario tidak dikenal: {name}")
                self.scenario = Scenario(name=name, **PRESETS[name])
            open_in_ms = spec.pop("open_in_ms", None)
            for k, v in spec.items():
                if not hasattr(self.scenario, k):
                    raise ValueError(f"field skenario tidak dikenal: {k}")
                setattr(self.scenario, k, v)
            if open_in_ms is not None:
                self.scenario.open_at = self.now() + open_in_ms / 1000.0
            return self.scenario

    def record(self, **entry) -> None:
        with self.lock:
            self.requests.append({"t_server_ms": int(self.now() * 1000), **entry})

    # ---- aturan
    def sale_state(self) -> str:
        s = self.scenario
        if s.open_at is None:
            return "open"
        return "open" if self.now() >= s.open_at + s.sale_skew_ms / 1000.0 else "not_started"

    def ui_flash_now(self) -> bool:
        """Tampilan halaman menganggap flash sale sudah mulai (tombol aktif) — dipakai saat render."""
        s = self.scenario
        if s.open_at is None:
            return True
        return self.now() >= s.open_at + s.button_delay_ms / 1000.0

    def flash_price(self, variant: str | None) -> int:
        return self.scenario.variant_prices.get(variant or "", self.scenario.price)


class MockAdmin:
    """Klien endpoint admin (dipakai tes untuk memilih skenario & membaca log)."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read() or b"null")

    def reset(self) -> None:
        self._call("POST", "/__admin/reset", {})

    def scenario(self, **spec) -> dict:
        return self._call("POST", "/__admin/scenario", spec)

    def log(self, kind: str | None = None) -> list[dict]:
        entries = self._call("GET", "/__admin/log")
        return [e for e in entries if kind is None or e["kind"] == kind]

    def state(self) -> dict:
        return self._call("GET", "/__admin/state")


def _rupiah(n: int) -> str:
    return "Rp" + f"{n:,}".replace(",", ".")


def _make_handler(mock: MockShopee):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MockShopee/1.0"

        def log_message(self, *args) -> None:  # senyap
            pass

        def date_time_string(self, timestamp=None) -> str:
            return formatdate(mock.now() if timestamp is None else timestamp, usegmt=True)

        # ---- util respons
        def _send(self, status: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8",
                  headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _html(self, html: str, status: int = 200) -> None:
            self._send(status, html.encode())

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _redirect(self, location: str) -> None:
            self._send(302, b"", headers={"Location": location})

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            if self.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(raw)
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

        def _login_redirect(self) -> None:
            self._redirect("/buyer/login?next=" + quote(self.path, safe=""))

        # ---- dispatch
        def do_HEAD(self) -> None:
            self._send(200)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
            if path.startswith("/__admin/"):
                return self._admin(method, path)
            body = self._body() if method == "POST" else {}
            kind = _kind(method, path)
            mock.record(method=method, path=path, query=query, kind=kind, body=body,
                        t_wall_ms=int(time.time() * 1000))
            s = mock.scenario
            try:
                if path == "/favicon.ico":
                    return self._send(204)
                if path == "/":
                    return self._html(pages.home(mock.product_url))
                if path == "/buyer/login":
                    if method == "POST":
                        s.login_expired = False
                        return self._redirect(body.get("next") or "/")
                    return self._html(pages.login(query.get("next", "/")))
                if path == "/verify/traffic":
                    return self._html(pages.verification())
                if path == "/verify/captcha":
                    return self._html(pages.captcha_page())
                if path == "/captcha/frame":
                    return self._html(pages.captcha_frame())
                if path == "/promo/kejutan":
                    return self._html(pages.unknown_page())
                # halaman yang butuh login
                if s.login_expired:
                    if path.startswith("/api/"):
                        return self._json({"error": "login", "redirect": "/buyer/login"}, 401)
                    return self._login_redirect()
                if _PRODUCT_RE.match(path):
                    if s.verification_on_reload and s.open_at is not None and mock.now() >= s.open_at:
                        return self._redirect("/verify/traffic")
                    return self._product()
                if path == "/api/buy" and method == "POST":
                    return self._buy(body)
                if path == "/cart":
                    return self._cart(query.get("sid", ""))
                if path == "/checkout":
                    if s.checkout_latency_ms:
                        time.sleep(s.checkout_latency_ms / 1000.0)
                    return self._checkout(query.get("sid", ""), query.get("items", "t"))
                if path == "/api/order" and method == "POST":
                    return self._order(body)
                if path == "/pin":
                    return self._html(pages.pin())
                if path == "/order/done":
                    return self._html(pages.simple("Pesanan dibuat", "Pesanan COD berhasil dibuat."))
                if path == "/user/account/address":
                    return self._html(pages.address(s.address))
                if path == "/user/shopeepay":
                    return self._html(pages.wallet(_rupiah(s.balance) if s.balance is not None else None))
                return self._html(pages.simple("Tidak ditemukan", "Halaman tidak ditemukan."), 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---- halaman
        def _product(self) -> None:
            s = mock.scenario
            now_ms = int(mock.now() * 1000)
            open_ms = int((s.open_at if s.open_at is not None else mock.now() - 3600) * 1000)
            sold_out_now = s.sold_out and mock.sale_state() == "open"
            self._html(pages.product(
                item="1001.2002", name=s.product_name, server_now_ms=now_ms, open_at_ms=open_ms,
                button_delay_ms=s.button_delay_ms, variants=s.variants, variant_prices=s.variant_prices,
                sold_out=sold_out_now, price=s.price, original_price=s.original_price,
                shipping=_rupiah(s.shipping), active_before_open=s.buy_active_before_open,
                live_update=s.live_update, initial_flash=mock.ui_flash_now(),
                flash_available=s.flash_available, description=s.description))

        def _buy(self, body: dict) -> None:
            s = mock.scenario
            if s.buy_latency_ms:
                time.sleep(s.buy_latency_ms / 1000.0)
            state = mock.sale_state()
            if state == "not_started" and not s.buy_active_before_open:
                return self._json({"error": "not_started", "message": "Flash sale belum dimulai"})
            if s.variants and body.get("variant") not in s.variants:
                return self._json({"error": "variant",
                                   "message": "Silakan pilih variasi produk terlebih dahulu"})
            if s.sold_out:
                return self._json({"error": "sold_out", "message": "Stok habis"})
            if s.captcha_after_buy:
                return self._json({"captcha": True})
            if s.verification_after_buy:
                return self._json({"redirect": "/verify/traffic"})
            if s.captcha_redirect_after_buy:
                return self._json({"redirect": "/verify/captcha"})
            if s.captcha_iframe_after_buy:
                return self._json({"captcha_iframe": True})
            if s.unknown_page_after_buy:
                return self._json({"redirect": "/promo/kejutan"})
            if s.pin_after_buy:
                return self._json({"redirect": "/pin"})
            flash = state == "open" and s.flash_available
            unit = mock.flash_price(body.get("variant")) if flash else s.original_price
            sid = secrets.token_hex(6)
            with mock.lock:
                mock.sessions[sid] = {"variant": body.get("variant"), "qty": int(body.get("qty", 1)),
                                      "unit": unit, "flash": flash}
            return self._json({"redirect": f"/cart?sid={sid}"})

        def _cart_rows(self, sess: dict) -> list[dict]:
            s = mock.scenario
            rows = [{"id": "t", "name": s.product_name, "variant": sess["variant"], "price": sess["unit"],
                     "qty": sess["qty"], "checked": True}]
            for i, o in enumerate(s.cart_other_items):
                rows.append({"id": f"o{i}", "name": o["name"], "variant": o.get("variant"),
                             "price": o["price"], "qty": o.get("qty", 1), "checked": o.get("checked", False)})
            return rows

        def _cart(self, sid: str) -> None:
            # tanpa sid (buka /cart langsung) = keranjang tersimpan: sesi Beli terakhir
            sess = mock.sessions.get(sid) if sid else (list(mock.sessions.values())[-1] if mock.sessions else None)
            sid = sid or (list(mock.sessions)[-1] if mock.sessions else "")
            if not sess:
                return self._html(pages.simple("Keranjang", "Keranjang belanja kosong."))
            self._html(pages.cart(sid, self._cart_rows(sess), mock.scenario.cart_uncheck_fails))

        def _checkout(self, sid: str, items: str) -> None:
            sess = mock.sessions.get(sid)
            if not sess:
                return self._html(pages.simple("Checkout", "Sesi checkout tidak valid."), 400)
            s = mock.scenario
            wanted = [i for i in items.split(",") if i]
            rows = []
            for r in self._cart_rows(sess):
                if r["id"] not in wanted:
                    continue
                target = r["id"] == "t"
                qty = (s.checkout_qty or r["qty"]) if target else r["qty"]
                fmt = (lambda n: "Rp" + f"{n:,}") if (target and s.price_format_broken) else _rupiah
                rows.append({"name": (s.checkout_name or r["name"]) if target else r["name"],
                             "variant": r["variant"], "unit": r["price"], "qty": qty,
                             "unit_text": fmt(r["price"]), "subtotal_text": fmt(r["price"] * qty),
                             "strike": _rupiah(s.original_price) if target and sess.get("flash") else None})
            self._html(pages.checkout(
                sid=sid, address=s.address or "-", rows=rows, shipping=s.shipping, service_fee=s.service_fee,
                shipping_delay_ms=s.shipping_delay_ms, payment_default=s.payment_default,
                balance=_rupiah(s.balance) if s.balance is not None else None))

        def _order(self, body: dict) -> None:
            sess = mock.sessions.get(body.get("sid", ""))
            if not sess:
                return self._json({"error": "sid", "message": "Sesi checkout tidak valid"}, 400)
            if body.get("payment") == "shopeepay":
                return self._json({"redirect": "/pin"})
            return self._json({"redirect": "/order/done"})

        # ---- admin
        def _admin(self, method: str, path: str) -> None:
            try:
                if path == "/__admin/reset" and method == "POST":
                    self._body()
                    mock.reset()
                    return self._json({"ok": True})
                if path == "/__admin/scenario" and method == "POST":
                    return self._json(asdict(mock.apply(self._body())))
                if path == "/__admin/log":
                    with mock.lock:
                        return self._json(list(mock.requests))
                if path == "/__admin/state":
                    return self._json({"scenario": asdict(mock.scenario),
                                       "now_ms": int(mock.now() * 1000),
                                       "sale_state": mock.sale_state()})
                return self._json({"error": "unknown admin path"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

    return Handler


def _kind(method: str, path: str) -> str:
    if _PRODUCT_RE.match(path):
        return "product"
    return {
        ("POST", "/api/buy"): "buy",
        ("POST", "/api/order"): "order",
        ("GET", "/cart"): "cart",
        ("GET", "/checkout"): "checkout",
        ("GET", "/pin"): "pin",
        ("GET", "/buyer/login"): "login",
        ("POST", "/buyer/login"): "login_submit",
        ("GET", "/user/account/address"): "address",
        ("GET", "/user/shopeepay"): "wallet",
        ("GET", "/verify/traffic"): "verify",
        ("GET", "/verify/captcha"): "verify",
        ("GET", "/captcha/frame"): "captcha_frame",
        ("GET", "/promo/kejutan"): "unknown",
    }.get((method, path), "other")
