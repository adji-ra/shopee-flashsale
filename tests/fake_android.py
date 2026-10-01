"""Aplikasi Shopee palsu untuk FakeDriver: state machine skenario (setara tests/mock_shopee untuk web).

Semua waktu dari jam yang sama dengan runner (FakeClock -> waktu server). Tata letak node memakai
koordinat layar 720x1612 supaya tap koordinat & pembacaan geometris teruji.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flashbuy.android_driver import DriverError, Node
from flashbuy.pricing import rupiah

W, H = 720, 1612
PACKAGE = "com.shopee.id"
PRODUCT_URL = "https://shopee.co.id/Ponsel-Uji-Coba-128GB-i.1001.2002"


@dataclass
class AppScenario:
    open_at: float  # waktu server slot flash sale dibuka
    product_name: str = "Ponsel Uji Coba 128GB"
    normal_price: int = 150_000
    flash_price: int = 99_000
    variants: list[str] = field(default_factory=list)
    variant_prices: dict[str, int] = field(default_factory=dict)  # harga flash per variasi
    variants_on_page: bool = False  # chip variasi tampil langsung di halaman produk
    variant_chip_sold_out: bool = False  # chip variasi lain berlabel "Habis" (bukan produk habis)
    sheet: bool = True  # Beli membuka bottom sheet variasi/qty
    variant_required: bool = False  # konfirmasi tanpa variasi -> toast "Silakan pilih variasi"
    button_before_open: str = "Beli Sekarang"
    button_enabled_before_open: bool = False  # True: bisa beli harga normal sebelum slot buka
    live_update: bool = True  # False: harga/tombol flash baru tampil setelah reload
    sale_skew_ms: int = 0  # slot di server buka sekian ms setelah open_at
    sold_out: bool = False  # saat slot buka tombol jadi "Habis"
    sold_out_on_confirm: bool = False  # konfirmasi di sheet -> toast "Stok habis"
    captcha_after_buy: bool = False
    verification_after_buy: bool = False
    webview_after_buy: bool = False  # WebView tanpa teks (captcha tak dikenali)
    captcha_on_refresh: bool = False
    login_required: bool = False
    go_cart: bool = False  # konfirmasi -> keranjang (bukan langsung checkout)
    cart_other_items: list[tuple[str, int, bool]] = field(default_factory=list)  # (nama, harga, tercentang)
    cart_uncheck_fails: bool = False
    payment_default: str = "ShopeePay"
    payment_confirm_button: bool = True
    checkout_qty: int = 1
    checkout_name: str | None = None
    shipping: int = 10_000
    shipping_delay_ms: int = 0  # ongkir "Menghitung..." selama ini
    service_fee: int = 0
    price_format_broken: bool = False
    strike_in_checkout: bool = True  # harga coret (lebih kecil) sebaris dengan harga jual & "x1"
    loading_after_buy_ms: int = 0  # spinner tanpa teks setelah klik Beli (server lambat)
    unknown_after_buy: bool = False
    other_app_after_buy: bool = False
    unknown_after_order: bool = False
    no_response_clicks: int = 0  # N klik Beli pertama tidak bereaksi
    flash_banner: bool = True  # "Flash Sale dimulai dalam .." / "Flash Sale berakhir dalam .."
    wallet_balance: int | None = 500_000
    address_utama: bool = True
    wallet_unsupported: bool = False  # intent halaman ShopeePay tidak bisa di-resolve
    installed: bool = True
    screen_on: bool = True
    locked: bool = False
    stay_on: str = "3"
    pin_title: str | None = "Masukkan PIN ShopeePay"  # None: layar PIN tanpa teks (hanya resource-id)


def _n(text: str = "", bounds=(0, 0, 0, 0), **kw) -> Node:
    return Node(text=text, bounds=tuple(bounds), **kw)


class FakeShopeeApp:
    def __init__(self, sc: AppScenario, clock):
        self.sc = sc
        self.clock = clock
        self.screen = "home"
        self.refreshed_at: float | None = None
        self.selected_variant: str | None = None
        self.payment_method = sc.payment_default
        self.list_choice: str | None = None
        self.toast: tuple[str, float] | None = None
        self.loading_until: float | None = None
        self.after_loading: str | None = None
        self.checkout_at = 0.0
        self.checkout_items: list[tuple[str, int, int]] = []  # (nama, harga satuan, qty)
        self.cart: list[dict] = []
        self.sold_out = sc.sold_out
        self.no_response_left = sc.no_response_clicks
        self.events: list[dict] = []
        self._sheet_start = 0

    # ------------------------------------------------------------------ waktu & log

    def now(self) -> float:
        return self.clock.time()

    def _event(self, kind: str, detail: str = "") -> None:
        self.events.append({"kind": kind, "t_server_ms": int(self.now() * 1000), "detail": detail})

    def kind(self, k: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == k]

    @property
    def open_eff(self) -> float:
        return self.sc.open_at + self.sc.sale_skew_ms / 1000

    def sale_active(self) -> bool:
        return self.now() >= self.open_eff

    def page_live(self) -> bool:
        return self.sale_active() and (self.sc.live_update or
                                       (self.refreshed_at is not None and self.refreshed_at >= self.open_eff))

    def unit_price(self) -> int:
        if not self.sale_active():
            return self.sc.normal_price
        return self.sc.variant_prices.get(self.selected_variant or "", self.sc.flash_price)

    # ------------------------------------------------------------------ FakeApp

    @property
    def package(self) -> str:
        return "com.android.launcher3" if self.screen == "other_app" else PACKAGE

    def activity(self) -> str:
        return {"other_app": ".Launcher"}.get(self.screen, f"com.shopee.app.ui.{self.screen}.Activity")

    def webview(self) -> bool:
        return self.screen == "webview"

    def nodes(self) -> list[Node]:
        """Semua jendela (jalur A: exists/info)."""
        return [n for _, n in self._render()]

    def active_nodes(self) -> list[Node]:
        """Jendela aktif saja (jalur B: find_all). Bottom sheet = jendela tersendiri di atas halaman produk."""
        out = self._render()
        if self.screen == "sheet":
            out = out[self._sheet_start:]
        return [n for _, n in out]

    def on_intent(self, url: str, package: str) -> None:
        if package != PACKAGE:
            return
        self._event("intent", url)
        self.toast = None
        if "/user/account/address" in url:
            self.screen = "address"
        elif "/user/shopeepay" in url:
            if self.sc.wallet_unsupported:
                raise DriverError("am start gagal: Error: Activity not started, unable to resolve Intent")
            self.screen = "wallet"
        elif self.sc.login_required:
            self.screen = "login"
        else:
            self.refreshed_at = self.now()
            self.selected_variant = None
            self.screen = "captcha" if self.sc.captcha_on_refresh and self.sale_active() else "product"

    def on_refresh(self) -> None:
        if self.screen != "product":
            return
        self._event("refresh")
        self.refreshed_at = self.now()
        self.toast = None
        if self.sc.captcha_on_refresh and self.sale_active():
            self.screen = "captcha"

    def on_back(self) -> None:
        self._event("back", self.screen)
        self.screen = {"sheet": "product", "payment_list": "checkout", "checkout": "product",
                       "cart": "product"}.get(self.screen, self.screen)

    def on_tap(self, x: int, y: int) -> None:
        for key, n in reversed(self._render()):
            left, top, right, bottom = n.bounds
            if key and left <= x <= right and top <= y <= bottom:
                self._event("tap", key)
                self._handle(key, n)
                return
        self._event("tap", "-")

    def shell(self, cmd: list[str]) -> str:
        line = " ".join(cmd)
        if line.startswith("pm path"):
            return f"package:/data/app/{PACKAGE}/base.apk\n" if self.sc.installed else ""
        if line.startswith("dumpsys package"):
            return "    versionName=3.40.21\n" if self.sc.installed else ""
        if line.startswith("dumpsys power"):
            return f"  mWakefulness={'Awake' if self.sc.screen_on else 'Asleep'}\n"
        if line.startswith("dumpsys window"):
            return f"    mDreamingLockscreen={str(self.sc.locked).lower()}\n"
        if line == "settings get global stay_on_while_plugged_in":
            return self.sc.stay_on + "\n"
        if line == "settings get system screen_off_timeout":
            return "60000\n"
        return ""

    # ------------------------------------------------------------------ aksi

    def _handle(self, key: str, node: Node) -> None:
        if key == "buy":
            self._tap_buy(node)
        elif key == "confirm":
            self._event("confirm")
            self._confirm()
        elif key.startswith("variant:"):
            v = key.split(":", 1)[1]
            if node.enabled:
                self.selected_variant = v
                self._event("variant", v)
                if self.screen == "product" and self.sc.sheet:
                    self.screen = "sheet"  # chip di halaman produk membuka sheet
        elif key == "payment_row":
            self.list_choice = self.payment_method
            self.screen = "payment_list"
        elif key.startswith("pay:"):
            self.list_choice = key.split(":", 1)[1]
            self._event("payment", self.list_choice)
            if not self.sc.payment_confirm_button:
                self.payment_method = self.list_choice
                self.screen = "checkout"
        elif key == "pay_confirm":
            self.payment_method = self.list_choice or self.payment_method
            self.screen = "checkout"
        elif key.startswith("cartbox:"):
            i = int(key.split(":")[1])
            self._event("cart_toggle", self.cart[i]["name"])
            if not self.sc.cart_uncheck_fails:
                self.cart[i]["checked"] = not self.cart[i]["checked"]
        elif key == "cart_checkout":
            self._enter_checkout([(c["name"], c["price"], 1) for c in self.cart if c["checked"]])
        elif key == "place_order":
            self._event("order", self.payment_method)
            self.screen = "order_unknown" if self.sc.unknown_after_order else "pin"

    def _tap_buy(self, node: Node) -> None:
        if not node.enabled:
            self._event("buy_disabled")
            return
        self._event("buy")
        if self.no_response_left > 0:
            self.no_response_left -= 1
            return
        nxt = None
        if self.sc.captcha_after_buy:
            nxt = "captcha"
        elif self.sc.verification_after_buy:
            nxt = "verification"
        elif self.sc.webview_after_buy:
            nxt = "webview"
        elif self.sc.unknown_after_buy:
            nxt = "unknown"
        elif self.sc.other_app_after_buy:
            nxt = "other_app"
        if nxt is None and not self.sc.sheet:
            self._confirm()
            return
        nxt = nxt or "sheet"
        if self.sc.loading_after_buy_ms:
            self.loading_until = self.now() + self.sc.loading_after_buy_ms / 1000
            self.after_loading = nxt
            self.screen = "loading"
        else:
            self.screen = nxt

    def _confirm(self) -> None:
        sc = self.sc
        if sc.variant_required and sc.variants and not self.selected_variant:
            self.toast = ("Silakan pilih variasi terlebih dahulu", self.now() + 1.5)
            return
        if not self.sale_active() and not sc.button_enabled_before_open:
            self.toast = ("Flash sale belum dimulai", self.now() + 1.5)
            return
        if sc.sold_out_on_confirm:
            self.toast = ("Stok habis", self.now() + 1.5)
            self.sold_out = True
            self.screen = "product"
            return
        item = (sc.checkout_name or sc.product_name, self.unit_price(), sc.checkout_qty)
        if sc.go_cart:
            self.cart = [{"name": sc.product_name, "price": item[1], "checked": True}]
            self.cart += [{"name": n, "price": p, "checked": c} for n, p, c in sc.cart_other_items]
            self.screen = "cart"
            return
        self._enter_checkout([item])

    def _enter_checkout(self, items: list[tuple[str, int, int]]) -> None:
        self._event("checkout", str(items))
        self.checkout_items = [(n, p, self.sc.checkout_qty if i == 0 else q) for i, (n, p, q) in enumerate(items)]
        self.checkout_at = self.now()
        self.payment_method = self.sc.payment_default
        self.screen = "checkout"

    # ------------------------------------------------------------------ render

    def _render(self) -> list[tuple[str, Node]]:
        if self.screen == "loading" and self.loading_until is not None and self.now() >= self.loading_until:
            self.screen, self.loading_until = self.after_loading or "product", None
        out = getattr(self, f"_r_{self.screen}")()
        if self.toast and self.now() < self.toast[1]:
            out.append(("", _n(self.toast[0], (160, 1300, 560, 1350))))
        elif self.toast:
            self.toast = None
        return out

    def _r_home(self):
        return [("", _n("Beranda", (0, 1540, 140, 1612))), ("", _n("Untukmu", (20, 200, 300, 240)))]

    def _r_product(self):
        sc = self.sc
        live = self.page_live()
        out = [("", _n("Detail Produk", (100, 60, 500, 110)))]
        if sc.flash_banner:
            banner = "Flash Sale berakhir dalam 01:59:59" if live else "Flash Sale dimulai dalam 00:00:05"
            out.append(("", _n(banner, (20, 560, 700, 600))))
        if live:
            price = sc.variant_prices.get(self.selected_variant or "", sc.flash_price)
            out.append(("", _n(rupiah(price), (20, 610, 400, 690))))
            out.append(("", _n(rupiah(sc.normal_price), (420, 640, 600, 670))))  # coret, lebih kecil
        else:
            out.append(("", _n(rupiah(sc.normal_price), (20, 610, 400, 690))))
        out.append(("", _n(sc.product_name, (20, 700, 700, 740))))
        out.append(("", _n("Terjual 1RB", (20, 750, 200, 780))))
        if sc.variants_on_page:
            for i, v in enumerate(sc.variants):
                out.append((f"variant:{v}", _n(v, (20 + i * 230, 800, 230 + i * 230, 850), clickable=True,
                                               selected=self.selected_variant == v)))
        if sc.variant_chip_sold_out:
            out.append(("", _n("Habis", (500, 860, 700, 900))))
        out.append(("", _n("Masukkan Keranjang", (0, 1500, 360, 1612), clickable=True)))
        if self.sold_out and self.sale_active():
            out.append(("buy", _n("Habis", (360, 1500, 720, 1612), clickable=True, enabled=False)))
        elif live:
            out.append(("buy", _n("Beli Sekarang", (360, 1500, 720, 1612), clickable=True)))
        else:
            out.append(("buy", _n(sc.button_before_open, (360, 1500, 720, 1612), clickable=True,
                                  enabled=sc.button_enabled_before_open)))
        return out

    def _r_sheet(self):
        sc = self.sc
        out = self._r_product()
        self._sheet_start = len(out)
        out.append(("", _n("", (0, 900, 720, 1612), cls="android.view.ViewGroup")))
        if self.selected_variant or not sc.variants:
            price = rupiah(self.unit_price())
        else:
            prices = [sc.variant_prices.get(v, sc.flash_price) for v in sc.variants]
            price = f"{rupiah(min(prices))} - {rupiah(max(prices))}"
        out.append(("", _n(price, (180, 920, 560, 980))))
        out.append(("", _n("Stok: 25", (180, 990, 400, 1020))))
        if sc.variants:
            out.append(("", _n("Variasi", (20, 1050, 200, 1080))))
            for i, v in enumerate(sc.variants):
                out.append((f"variant:{v}", _n(v, (20 + i * 230, 1090, 230 + i * 230, 1140), clickable=True,
                                               selected=self.selected_variant == v)))
        out += [("", _n("Jumlah", (20, 1300, 200, 1340))), ("", _n("1", (510, 1300, 560, 1340)))]
        out.append(("confirm", _n("Beli Sekarang", (0, 1500, 720, 1612), clickable=True)))
        return out

    def _r_checkout(self):
        sc = self.sc
        out = [("", _n("Checkout", (20, 60, 300, 110))),
               ("", _n("Alamat Pengiriman", (20, 130, 400, 160))),
               ("", _n("Budi Santoso | (+62) 812-0000-0000", (20, 165, 700, 195))),
               ("", _n("Toko Uji Resmi", (20, 240, 400, 270)))]
        y = 290
        subtotal = 0
        for name, unit, qty in self.checkout_items:
            out.append(("", _n(name, (140, y, 700, y + 40))))
            if self.selected_variant:
                out.append(("", _n(f"Variasi: {self.selected_variant}", (140, y + 45, 600, y + 75))))
            if sc.strike_in_checkout:
                out.append(("", _n(rupiah(sc.normal_price), (140, y + 102, 300, y + 127))))
            text = rupiah(unit).replace("000", "OOO") if sc.price_format_broken else rupiah(unit)
            out.append(("", _n(text, (320, y + 95, 500, y + 130))))
            out.append(("", _n(f"x{qty}", (640, y + 98, 700, y + 128))))
            subtotal += unit * qty
            y += 170
        n_items = sum(q for _, _, q in self.checkout_items)
        out.append(("", _n(f"Total Pesanan ({n_items} Produk):", (20, y + 10, 400, y + 40))))
        out.append(("", _n(rupiah(subtotal), (500, y + 10, 700, y + 40))))
        out.append(("payment_row", _n("Metode Pembayaran", (20, y + 80, 300, y + 120), clickable=True)))
        out.append(("payment_row", _n(self.payment_method, (420, y + 80, 700, y + 120), clickable=True)))
        ready = self.now() >= self.checkout_at + sc.shipping_delay_ms / 1000
        ship = rupiah(sc.shipping) if ready else "Menghitung..."
        total = subtotal + sc.service_fee + (sc.shipping if ready else 0)
        y += 160
        out.append(("", _n("Rincian Pembayaran", (20, y, 400, y + 30))))
        out += [("", _n("Subtotal untuk Produk", (20, y + 40, 400, y + 70))),
                ("", _n(rupiah(subtotal), (500, y + 40, 700, y + 70))),
                ("", _n("Subtotal Pengiriman", (20, y + 80, 400, y + 110))),
                ("", _n(ship, (500, y + 80, 700, y + 110), rid="labelShippingFinalPrice"))]
        if sc.service_fee:
            out += [("", _n("Biaya Layanan", (20, y + 120, 400, y + 150))),
                    ("", _n(rupiah(sc.service_fee), (500, y + 120, 700, y + 150)))]
        out += [("", _n("Total Pembayaran", (20, y + 160, 400, y + 195))),
                ("", _n(rupiah(total), (500, y + 160, 700, y + 195)))]
        # bar bawah: label di atas nilai (pasangan "di bawah")
        out += [("", _n("Total Pembayaran", (300, 1515, 510, 1545))),
                ("", _n(rupiah(total), (300, 1550, 510, 1595), rid="labelTotalPayment")),
                ("place_order", _n("Buat Pesanan", (520, 1500, 720, 1612), clickable=True))]
        return out

    def _r_payment_list(self):
        out = [("", _n("Metode Pembayaran", (20, 60, 400, 110)))]
        for i, m in enumerate(["ShopeePay", "SPayLater", "COD - Cek Dulu", "Transfer Bank"]):
            top = 150 + i * 100
            out.append((f"pay:{m}", _n(m, (100, top, 500, top + 40), clickable=True)))
            if m == "ShopeePay" and self.sc.wallet_balance is not None:
                out.append(("", _n(f"Saldo {rupiah(self.sc.wallet_balance)}", (100, top + 45, 500, top + 75))))
            out.append((f"pay:{m}", _n("", (640, top, 690, top + 40), cls="android.widget.RadioButton",
                                       checked=self.list_choice == m)))
        if self.sc.payment_confirm_button:
            out.append(("pay_confirm", _n("Konfirmasi", (0, 1500, 720, 1612), clickable=True)))
        return out

    def _r_cart(self):
        out = [("", _n(f"Keranjang Saya ({len(self.cart)})", (20, 60, 500, 110))),
               ("", _n("", (20, 150, 70, 200), cls="android.widget.CheckBox", checked=False)),
               ("", _n("Toko Uji Resmi", (90, 150, 500, 200)))]
        for i, c in enumerate(self.cart):
            y0 = 230 + i * 200
            out += [(f"cartbox:{i}", _n("", (20, y0 + 40, 70, y0 + 90), cls="android.widget.CheckBox",
                                        checked=c["checked"], clickable=True)),
                    ("", _n(c["name"], (200, y0, 700, y0 + 40))),
                    ("", _n(rupiah(c["price"]), (200, y0 + 100, 400, y0 + 140))),
                    ("", _n("1", (510, y0 + 100, 560, y0 + 140)))]
        n = sum(1 for c in self.cart if c["checked"])
        out += [("", _n("", (20, 1530, 70, 1580), cls="android.widget.CheckBox", checked=False)),
                ("", _n("Semua", (80, 1530, 200, 1580))),
                ("cart_checkout", _n(f"Checkout ({n})", (480, 1500, 720, 1612), clickable=True,
                                     rid="labelButtonCheckout"))]
        return out

    def _r_pin(self):
        title = [] if self.sc.pin_title is None else [("", _n(self.sc.pin_title, (100, 300, 620, 350)))]
        return title + [("", _n("", (100, 400, 640, 470), cls="android.widget.EditText",
                                rid="com.shopee.id:id/payment_password_field"))]

    def _r_captcha(self):
        return [("", _n("Geser untuk verifikasi", (100, 700, 620, 750))),
                ("", _n("", (100, 800, 200, 860), cls="android.widget.SeekBar"))]

    def _r_verification(self):
        return [("", _n("Kami mendeteksi aktivitas tidak biasa pada akun Anda", (40, 600, 680, 680)))]

    def _r_webview(self):
        return [("", _n("", (0, 0, W, H), cls="android.webkit.WebView"))]

    def _r_login(self):
        return [("", _n("Log in", (20, 60, 300, 110))),
                ("", _n("No. Handphone/Email/Username", (20, 200, 700, 260))),
                ("", _n("Lupa Password?", (500, 300, 700, 340)))]

    def _r_unknown(self):
        return [("", _n("Promo Spesial Hari Ini", (100, 500, 620, 560)))]

    def _r_order_unknown(self):
        return [("", _n("Terima kasih!", (100, 500, 620, 560)))]

    def _r_other_app(self):
        return [("", _n("Telepon", (40, 1500, 160, 1560))), ("", _n("Pesan", (200, 1500, 320, 1560)))]

    def _r_loading(self):
        return [("", _n("", (310, 760, 410, 860), cls="android.widget.ProgressBar"))]

    def _r_address(self):
        out = [("", _n("Alamat Saya", (20, 60, 400, 110)))]
        if self.sc.address_utama:
            out += [("", _n("Budi Santoso | (+62) 812-0000-0000", (20, 150, 700, 190))),
                    ("", _n("Utama", (20, 240, 120, 270)))]
        else:
            out.append(("", _n("Belum ada alamat", (100, 600, 620, 650))))
        return out

    def _r_wallet(self):
        out = [("", _n("ShopeePay", (20, 60, 400, 110)))]
        if self.sc.wallet_balance is not None:
            out += [("", _n("Saldo", (20, 200, 200, 240))),
                    ("", _n(rupiah(self.sc.wallet_balance), (20, 245, 400, 300)))]
        return out
