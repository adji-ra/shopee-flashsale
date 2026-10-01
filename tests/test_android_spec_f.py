"""Spesifikasi F (guards Android) end-to-end: status layar dari package/activity + elemen.

- Enum Screen = 12 status spesifikasi + 3 sub-status terdokumentasi (VARIANT_REQUIRED, ERROR_TOAST, LOADING).
- Paket/activity asing di depan -> VERIFICATION segera; activity Shopee bernama verifikasi -> CAPTCHA/VERIFICATION.
- Aplikasi/dialog sistem yang dikenal (telepon, Phone Master, izin, launcher HP dari precheck), crash, dialog ANR
  -> UNKNOWN; UNKNOWN > 1,5 s -> UNKNOWN_STATE (dump + screenshot + activity, alarm, tanpa retry). UNKNOWN singkat
  tidak menghentikan run. Berlaku saat polling (sebelum Beli), setelah Beli, dan setelah "Buat Pesanan" (pesan
  wajib "Pesanan MUNGKIN sudah terbuat").
- Dialog ANR di atas Shopee (elemen Shopee masih terbaca): tidak ada tap selama dialog tampil (Beli, konfirmasi
  sheet, "Buat Pesanan").
- LOADING selamanya setelah Beli -> UNKNOWN_STATE di loading_limit_s, tanpa klik ulang.
- Hot path polling tidak membaca package/activity (current_app) maupun WebView.
Aturan keras di setiap tes: tidak ada "Buat Pesanan" selain live sukses, tidak ada input teks (PIN), aksi polling
>= 400 ms & di T-1..T+8 s, aplikasi tidak pernah ditutup. Waktu = waktu server (ms) relatif T (slot buka).
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import threading

import pytest

from flashbuy.android_driver import Node
from flashbuy.android_runner import LOADING_LIMIT_S, UNKNOWN_LIMIT_S, Screen
from flashbuy.runner_base import MAYBE_ORDERED_MSG, STOP_ALL_STATUSES, RunStatus
from tests import android_harness
from tests.android_harness import assert_polling_rules, make_android, polling_actions, run_android
from tests.fake_android import FakeShopeeApp

BOTH = pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
RECAPTCHA = "com.google.android.gms/.recaptcha.RecaptchaActivity"
INCALL = "com.android.incallui/.InCallActivity"
ANR_TEXT = "dialog sistem 'Shopee tidak merespons' (crash/ANR)"

# Operasi driver yang mengubah layar aplikasi; selain ini hanya membaca/diagnosa.
ACTION_OPS = {"click", "start_url", "swipe_refresh", "press_back", "restart_agent"}
READ_OPS = {"exists", "info", "info_any", "find_all", "current_app", "webview", "screenshot", "dump", "agent_alive",
            "last_toast", "clear_toast"}
# Semua operasi yang sah (tidak ada set_text/send_keys: PIN tidak mungkin diketik alat).
ALLOWED_OPS = ACTION_OPS | READ_OPS | {"shell"}
# Perintah shell yang menutup/mematikan aplikasi atau mengetik/menekan tombol.
FORBIDDEN_SHELL = re.compile(r"force-stop|\bam\s+(kill|stop)|\bpm\s+clear|\binput\b|\bkill\b|monkey", re.I)


# ------------------------------------------------------------------ util


def _t(out) -> int:
    return int(out.open_at * 1000)


def _ops(out) -> list[str]:
    return [op for op, _ in out.driver.calls]


def _step_t(out, name: str) -> int:
    step = out.result.step(name)
    assert step is not None, f"langkah {name} tidak ada di {out.step_names()}"
    return step.t_server_ms


def _stop() -> dict:
    """runner_attrs dengan stop_event bersama (seolah ada runner lain yang ikut dihentikan)."""
    return {"stop_event": threading.Event()}


def _run(tmp_path, monkeypatch, *, app_cls=None, setup=None, **kw):
    """run_android dengan subkelas FakeShopeeApp dan/atau hook `setup(runner, app, driver)` sebelum run."""
    if app_cls is not None:
        monkeypatch.setattr(android_harness, "FakeShopeeApp", app_cls)
    if setup is not None:
        orig = android_harness.make_android

        def make(*a, **k):
            built = orig(*a, **k)
            setup(*built[:3])
            return built

        monkeypatch.setattr(android_harness, "make_android", make)
    return run_android(tmp_path, **kw)


def _hard_rules(out, *, ordered: bool = False, maybe: bool = False) -> None:
    """Aturan keras yang berlaku di SEMUA skenario. ordered: live sukses (layar PIN); maybe: 'Buat Pesanan'
    diklik tetapi layar PIN tidak muncul (hasil tidak pasti)."""
    orders = out.kind("order")
    if ordered or maybe:
        assert len(orders) == 1 and out.step_names().count("click_place_order") == 1, "tepat satu 'Buat Pesanan'"
        assert len([t for op, t in out.driver.calls if op == "click" and t == "Buat Pesanan"]) == 1
        i = out.app.events.index(orders[0])
        assert out.app.events[i + 1:] == [], "tidak ada tap/aksi setelah 'Buat Pesanan' (PIN tidak disentuh)"
        if ordered:
            assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
            assert out.app.screen == "pin", "layar PIN dibiarkan (PIN diketik manual)"
        else:
            assert out.result.message == MAYBE_ORDERED_MSG and out.runner.order_clicked
            assert out.app.screen != "pin"
    else:
        assert orders == [], "tidak boleh ada klik 'Buat Pesanan'"
        assert "click_place_order" not in out.step_names()
        assert not [op for op, t in out.driver.calls if op == "click" and t == "Buat Pesanan"]
    assert set(_ops(out)) <= ALLOWED_OPS, set(_ops(out)) - ALLOWED_OPS  # tanpa input teks
    assert [e for e in out.kind("tap") if e["detail"] == "-"] == [], "tap di area tanpa elemen (mis. kolom PIN)"
    assert [e for e in out.kind("tap") if e["detail"].startswith("system:")] == [], "tap mengenai dialog sistem"
    assert not [t for op, t in out.driver.calls if op == "shell" and FORBIDDEN_SHELL.search(t)]
    if polling_actions(out):
        assert_polling_rules(out)


def _left_alone(out, since: dict | None) -> None:
    """Setelah event `since` (None: setelah intent arm) tidak ada aksi ke aplikasi; hanya diagnosa baca, diakhiri
    current_app + screenshot + dump."""
    if since is None:
        since = out.kind("intent")[-1]
    i = out.app.events.index(since)
    assert out.app.events[i + 1:] == [], f"aksi ke aplikasi setelah deteksi: {out.app.events[i + 1:]}"
    ops = _ops(out)
    last_action = max(k for k, op in enumerate(ops) if op in ACTION_OPS)
    assert set(ops[last_action + 1:]) <= READ_OPS, set(ops[last_action + 1:]) - READ_OPS
    assert ops[-3:] == ["current_app", "screenshot", "dump"], ops[-6:]


def _one_buy(out) -> dict:
    buys = out.kind("buy")
    assert len(buys) == 1, f"klik Beli harus tepat sekali, dapat {len(buys)}"
    assert out.kind("buy_disabled") == [] and out.step_names().count("click_buy") == 1
    assert polling_actions(out) == [buys[0]["t_server_ms"]], "tanpa klik ulang / reload"
    return buys[0]


def _stopped(out, attrs: dict, status: RunStatus) -> None:
    """Status stop: semua runner dihentikan, alarm sekali dengan status itu."""
    assert out.result.status == status, out.result.message
    assert status in STOP_ALL_STATUSES
    assert attrs["stop_event"].is_set(), f"{status} wajib menghentikan SEMUA runner"
    assert out.events() == [str(status)], out.notifier.events
    assert out.notifier.events[0]["platform"] == "android"


def _log_text(out) -> str:
    return (out.log_dir / "android.log").read_text(encoding="utf-8")


def _run_queries(out) -> list[tuple[int, str, str]]:
    """(t_server_ms, op, target) setiap kueri run (arm + attempt) dari android-queries-run.csv."""
    with (out.log_dir / "android-queries-run.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["t_server_ms", "op", "target", "ms"]
    return [(int(r[0]), r[1], r[2]) for r in rows[1:]]


# ------------------------------------------------------------------ subkelas aplikasi palsu


def _front_app(package: str, activity: str, texts: tuple[str, ...], dur_s: float | None,
               home: str | None = None) -> type[FakeShopeeApp]:
    """Setelah tap Beli, aplikasi/dialog `package/activity` tampil di depan Shopee (mis. telepon masuk, Phone
    Master) selama `dur_s` (None: selamanya). Jendelanya hanya terlihat lewat info_any (package lain); di
    belakangnya Shopee tetap di layar hasil tap (bottom sheet). `home`: jawaban `cmd package resolve-activity`
    HOME saat precheck (launcher HP ini); None = jawaban bawaan (com.android.launcher3)."""

    class App(FakeShopeeApp):
        def __init__(self, sc, clock):
            super().__init__(sc, clock)
            self.front: tuple[str, float | None] | None = None  # (layar Shopee di belakang, selesai)

        def _sync(self) -> None:
            if self.front is not None and self.front[1] is not None and self.now() >= self.front[1]:
                self.screen, self.front = self.front[0], None

        def _tap_buy(self, node: Node) -> None:
            super()._tap_buy(node)
            if node.enabled and self.front is None and not self.kind("front"):
                self._event("front", f"{package}/{activity}")
                self.front = (self.screen, None if dur_s is None else self.now() + dur_s)
                self.screen = "front"

        @property
        def package(self) -> str:
            self._sync()
            return package if self.screen == "front" else super().package

        def activity(self) -> str:
            self._sync()
            return activity if self.screen == "front" else super().activity()

        def system_nodes(self) -> list[Node]:
            self._sync()
            if self.screen != "front":
                return super().system_nodes()
            return _front_nodes(texts)

        def _render(self):
            self._sync()
            return super()._render()

        def _r_front(self):
            return []  # Shopee tidak di depan: kueri yang dibatasi ke com.shopee.id tidak melihat apa pun

        def shell(self, cmd: list[str]) -> str:
            if home is not None and " ".join(cmd).startswith("cmd package resolve-activity"):
                return f"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n{home}\n"
            return super().shell(cmd)

    return App


class _AnrOnStepApp(FakeShopeeApp):
    """Dialog ANR sistem ("Shopee tidak merespons") muncul di atas Shopee saat runner PERTAMA kali sudah mencatat
    langkah `trigger`, selama `anr_s`. Elemen Shopee tetap terbaca di bawahnya. Waktu setiap pembacaan dialog
    (info_any) dicatat di `dialog_reads`."""

    trigger = "price_guard_ok"
    anr_s = 0.6

    def __init__(self, sc, clock):
        super().__init__(sc, clock)
        self.runner = None
        self.window: tuple[float, float] | None = None
        self.dialog_reads: list[float] = []

    def anr_active(self) -> bool:
        if self.window is None and self.runner is not None and \
                any(s.name == self.trigger for s in self.runner.log.steps):
            self.window = (self.now(), self.now() + self.anr_s)
            self._event("anr", self.trigger)
        return self.window is not None and self.window[0] <= self.now() < self.window[1]

    def system_nodes(self) -> list[Node]:
        nodes = super().system_nodes()
        if nodes:
            self.dialog_reads.append(self.now())
        return nodes


class _AnrReadsApp(FakeShopeeApp):
    """FakeShopeeApp biasa (anr_dialog dari AppScenario) yang mencatat waktu setiap pembacaan dialog."""

    def __init__(self, sc, clock):
        super().__init__(sc, clock)
        self.dialog_reads: list[float] = []

    def system_nodes(self) -> list[Node]:
        nodes = super().system_nodes()
        if nodes:
            self.dialog_reads.append(self.now())
        return nodes


def _anr_on_step(trigger: str, anr_s: float) -> tuple[type[FakeShopeeApp], object]:
    app_cls = type("App", (_AnrOnStepApp,), {"trigger": trigger, "anr_s": anr_s})

    def setup(runner, app, driver):
        app.runner = runner

    return app_cls, setup


def _front_nodes(texts: tuple[str, ...]) -> list[Node]:
    return [Node(text=t, bounds=(60, 1300 + 100 * i, 660, 1360 + 100 * i), clickable=i > 0)
            for i, t in enumerate(texts)]


def _covered_app(front: str, texts: tuple[str, ...], start_s: float, dur_s: float) -> type[FakeShopeeApp]:
    """Jendela `front` (package/activity) menutupi Shopee pada T+start_s .. T+start_s+dur_s (waktu server, tidak
    bergantung pada aksi runner). Selama tertutup kueri com.shopee.id tidak melihat apa pun, jendela depan hanya
    lewat info_any; tap mengenai jendela depan (event tap system:/'-'), swipe/intent tetap tercatat."""
    package, activity = front.split("/", 1)

    class App(FakeShopeeApp):
        def covered(self) -> bool:
            return start_s <= self.now() - self.sc.open_at < start_s + dur_s

        @property
        def package(self) -> str:
            return package if self.covered() else super().package

        def activity(self) -> str:
            return activity if self.covered() else super().activity()

        def system_nodes(self) -> list[Node]:
            return _front_nodes(texts) if self.covered() else super().system_nodes()

        def webview(self) -> bool:
            return False if self.covered() else super().webview()

        def _render(self):
            return [] if self.covered() else super()._render()

    return App


def _leaves_after_order(screen: str, front: str | None, texts: tuple[str, ...]) -> type[FakeShopeeApp]:
    """Tap 'Buat Pesanan' mencatat pesanan, lalu Shopee keluar dari foreground: `screen` = crashed (launcher +
    dialog crash), other_app (launcher), atau foreign (`front` package/activity di depan, teks `texts`)."""

    class App(FakeShopeeApp):
        def _handle(self, key: str, node: Node) -> None:
            if key != "place_order":
                return super()._handle(key, node)
            self._event("order", self.payment_method)
            if front is not None:
                self.sc.foreign_after_buy = front  # Beli sudah lewat: hanya menentukan package/activity di depan
            self.screen = screen

        def system_nodes(self) -> list[Node]:
            if self.screen == "foreign" and texts:
                return _front_nodes(texts)
            return super().system_nodes()

    return App


# ------------------------------------------------------------------ unit: enum & klasifikasi


SPEC_STATES = {"PRODUCT_WAITING", "PRODUCT_ACTIVE", "VARIANT_SHEET", "CART", "CHECKOUT", "PIN_SCREEN", "NOT_STARTED",
               "SOLD_OUT", "CAPTCHA", "VERIFICATION", "LOGIN_REQUIRED", "UNKNOWN"}
SUB_STATES = {"VARIANT_REQUIRED", "ERROR_TOAST", "LOADING"}


def test_screen_enum_is_exactly_spec_states_plus_documented_substates():
    """Spesifikasi F: 12 status + 3 sub-status yang didokumentasikan di docstring Screen, tidak lebih."""
    names = {s.name for s in Screen}
    assert len(Screen) == 15
    assert names == SPEC_STATES | SUB_STATES, names ^ (SPEC_STATES | SUB_STATES)
    assert all(s.value == s.name for s in Screen)
    for sub in SUB_STATES:
        assert sub in Screen.__doc__, f"sub-status {sub} harus terdokumentasi"
    assert UNKNOWN_LIMIT_S == 1.5 and LOADING_LIMIT_S == 10.0


def _classifier(tmp_path, *, after_t: bool = True, app_cls=None, precheck: bool = False, **scenario):
    """Runner + driver terpasang (prepare, opsional precheck); jam sebelum T (tombol Beli belum aktif) atau sudah
    lewat T."""
    runner, app, driver, sclock, open_at, log, _notifier = make_android(tmp_path, **scenario)
    if app_cls is not None:
        app = app_cls(app.sc, app.clock)
        driver.app = app
    asyncio.run(runner.prepare())
    if precheck:
        res = asyncio.run(runner.precheck())
        assert res.status is None, res.items
    if after_t:
        sclock.clock.advance(open_at - sclock.now() + 1.0)
    return runner, app, driver, log


# (layar FakeShopeeApp, setelah T?, skenario, status, bukti, butuh package/activity)
CLASSIFY_CASES = [
    # konteks after_buy: banner "dimulai dalam" = reaksi klik (NOT_STARTED), diuji terpisah di bawah
    ("product", False, {}, Screen.PRODUCT_WAITING, "tombol Beli", False),
    ("product", True, {}, Screen.PRODUCT_ACTIVE, "tombol Beli", False),
    ("product", False, {"button_before_open": "Ingatkan Saya"}, Screen.NOT_STARTED, "teks ", False),
    ("product", True, {"sold_out": True}, Screen.SOLD_OUT, "tombol 'Habis'", False),
    ("sheet", True, {}, Screen.VARIANT_SHEET, "teks '", False),  # "Jumlah" / "Stok: 25" (per kueri)
    ("cart", True, {}, Screen.CART, "teks 'Keranjang Saya (0)'", False),
    ("checkout", True, {}, Screen.CHECKOUT, "tombol Buat Pesanan", False),
    ("pin", True, {}, Screen.PIN_SCREEN, "teks 'Masukkan PIN ShopeePay'", False),
    ("captcha", True, {}, Screen.CAPTCHA, "teks 'Geser untuk verifikasi'", False),
    ("verification", True, {}, Screen.VERIFICATION, "teks 'Kami mendeteksi aktivitas", False),
    ("login", True, {}, Screen.LOGIN_REQUIRED, "teks 'Log in'", False),
    ("loading", True, {}, Screen.LOADING, "indikator loading", False),
    ("unknown", True, {}, Screen.UNKNOWN, "tidak ada elemen yang dikenali", True),
    ("webview", True, {}, Screen.VERIFICATION, "WebView tanpa elemen Shopee yang dikenal (com.shopee.id/", True),
    ("foreign", True, {"foreign_after_buy": RECAPTCHA}, Screen.VERIFICATION,
     f"aplikasi/activity asing di depan: {RECAPTCHA}", True),
    ("verify_activity", True, {"verify_activity_after_buy": "com.shopee.app.ui.antifraud.CaptchaActivity"},
     Screen.CAPTCHA, "activity verifikasi com.shopee.id/com.shopee.app.ui.antifraud.CaptchaActivity", True),
    ("verify_activity", True, {"verify_activity_after_buy": "com.shopee.app.ui.security.RiskCheckActivity"},
     Screen.VERIFICATION, "activity verifikasi com.shopee.id/com.shopee.app.ui.security.RiskCheckActivity", True),
    ("crashed", True, {}, Screen.UNKNOWN, "Shopee keluar dari foreground: com.android.launcher3/.Launcher", True),
    ("other_app", True, {}, Screen.UNKNOWN, "Shopee keluar dari foreground: com.android.launcher3/.Launcher", True),
]


@pytest.mark.parametrize("screen, after_t, scenario, expected, evidence, needs_fg", CLASSIFY_CASES,
                         ids=[f"{c[0]}-{c[3]}" for c in CLASSIFY_CASES])
def test_classify_maps_every_state_from_elements_or_package_activity(tmp_path, screen, after_t, scenario, expected,
                                                                     evidence, needs_fg):
    """Setiap status spesifikasi F (+LOADING) dari elemen layar, atau dari package/activity bila tidak ada elemen
    Shopee yang dikenali. Klasifikasi hanya membaca (tanpa tap/intent); package/activity (current_app, mahal)
    hanya dibaca bila tidak ada elemen yang dikenali."""
    runner, app, driver, log = _classifier(tmp_path, after_t=after_t, **scenario)
    app.screen = screen
    n = len(driver.calls)
    contexts = ("product", "any") if expected == Screen.PRODUCT_WAITING else ("product", "after_buy", "any")
    for context in contexts:
        seen = runner._classify(context)
        assert seen.screen == expected, (context, seen)
        assert seen.evidence.startswith(evidence), (context, seen.evidence)
    ops = [op for op, _ in driver.calls[n:]]
    assert ("current_app" in ops) is needs_fg, ops
    assert not set(ops) & ACTION_OPS, "klasifikasi tidak boleh mengubah layar"
    assert app.events == [] and app.screen == screen
    log.close()


@pytest.mark.parametrize("package, home, expected", [
    ("android", False, Screen.UNKNOWN),  # dialog sistem (crash/ANR) sebagai jendela terdepan
    ("com.android.systemui", False, Screen.UNKNOWN),
    ("com.android.incallui", False, Screen.UNKNOWN),
    ("com.transsion.phonemaster", False, Screen.UNKNOWN),
    ("com.google.android.permissioncontroller", False, Screen.UNKNOWN),
    ("com.google.android.inputmethod.latin", False, Screen.UNKNOWN),  # keyboard
    ("com.android.launcher3", False, Screen.UNKNOWN),  # launcher (regex)
    ("com.transsion.smartpanel", False, Screen.UNKNOWN),  # aplikasi sistem HiOS (com.transsion.*)
    # launcher HP ini (nama tanpa "launcher"): hanya dikenal lewat resolve-activity HOME saat precheck
    ("org.example.desk", "org.example.desk/.Desk", Screen.UNKNOWN),
    ("org.example.desk", False, Screen.VERIFICATION),  # tanpa precheck: paket asing
    ("org.example.desk", "com.android.launcher3/.Launcher", Screen.VERIFICATION),  # launcher HP lain
    ("com.android.chrome", False, Screen.VERIFICATION),  # verifikasi dibuka di browser
    ("com.google.android.gms", False, Screen.VERIFICATION),  # verifikasi Play Services
])
def test_front_package_known_system_is_unknown_foreign_is_verification(tmp_path, package, home, expected):
    """Tanpa elemen Shopee: paket sistem yang dikenal / launcher / keyboard = Shopee keluar dari foreground
    (UNKNOWN, jaring 1,5 s); paket lain = VERIFICATION (stop segera). `home`: precheck dijalankan dan
    resolve-activity HOME menjawab nilai itu (diurai jadi home_packages)."""
    app_cls = _front_app(package, ".Act", ("Teks lain",), None, home=home or None)
    runner, app, driver, log = _classifier(tmp_path, app_cls=app_cls, precheck=bool(home))
    if home:
        resolve = [t for op, t in driver.calls if op == "shell" and t.startswith("cmd package resolve-activity")]
        assert len(resolve) == 1 and "android.intent.category.HOME" in resolve[0], resolve
    assert runner.home_packages == ({home.split("/")[0]} if home else set())
    app.screen, app.front = "front", ("product", None)
    seen = runner._classify("after_buy")
    assert seen.screen == expected, seen
    prefix = "Shopee keluar dari foreground: " if expected == Screen.UNKNOWN else "aplikasi/activity asing di depan: "
    assert seen.evidence == f"{prefix}{package}/.Act"
    assert "webview" not in [op for op, _ in driver.calls], "WebView hanya diperiksa bila Shopee di depan"
    log.close()


# ------------------------------------------------------------------ paket / activity asing setelah Beli


@BOTH
def test_foreign_package_after_buy_is_verification_immediately(tmp_path, live):
    """reCAPTCHA Play Services (paket asing) di depan setelah Beli -> VERIFICATION < 1 s (tanpa menunggu jaring
    UNKNOWN), stop semua + alarm, tanpa retry; tombol 'Verifikasi' di jendela asing tidak disentuh.
    terkait: test_android_spec_k STOP_CASES foreign_activity (hanya status + nol 'Buat Pesanan')."""
    attrs = _stop()
    out = run_android(tmp_path, live=live, foreign_after_buy=RECAPTCHA, runner_attrs=attrs)
    _stopped(out, attrs, RunStatus.VERIFICATION)
    assert out.result.message == f"aplikasi/activity asing di depan: {RECAPTCHA}"
    assert out.result.detail == "" and not out.runner.order_clicked
    buy = _one_buy(out)
    dt = _step_t(out, "result") - buy["t_server_ms"]
    assert dt < 1000, f"VERIFICATION {dt} ms setelah klik Beli"
    assert "no_response" not in out.step_names()
    assert out.kind("confirm") == [] and out.kind("checkout") == []
    assert [e["detail"] for e in out.kind("tap")] == ["buy"]
    assert out.app.screen == "foreign", "jendela verifikasi dibiarkan untuk diselesaikan manual"
    _left_alone(out, buy)
    _hard_rules(out)


@BOTH
@pytest.mark.parametrize("activity, status", [
    ("com.shopee.app.ui.antifraud.CaptchaActivity", RunStatus.CAPTCHA),
    ("com.shopee.app.ui.security.RiskCheckActivity", RunStatus.VERIFICATION),
], ids=["captcha_activity", "risk_activity"])
def test_shopee_verification_activity_after_buy_stops_immediately(tmp_path, live, activity, status):
    """Activity Shopee bernama verifikasi (berisi WebView tanpa elemen dikenal): nama activity menentukan
    CAPTCHA/VERIFICATION (didahulukan dari aturan WebView -> VERIFICATION). Kasus WebView biasa sudah diuji di
    test_android_safety.test_webview_without_known_elements_after_buy_is_verification."""
    attrs = _stop()
    out = run_android(tmp_path, live=live, verify_activity_after_buy=activity, runner_attrs=attrs)
    _stopped(out, attrs, status)
    assert out.result.message == f"activity verifikasi com.shopee.id/{activity}"
    buy = _one_buy(out)
    assert _step_t(out, "result") - buy["t_server_ms"] < 1000
    assert out.kind("confirm") == [] and out.kind("checkout") == []
    assert out.app.screen == "verify_activity"
    _left_alone(out, buy)
    _hard_rules(out)


# ------------------------------------------------------------------ Shopee keluar dari foreground


CALL_TEXTS = ("Panggilan masuk", "Tolak", "Jawab")
FRONT_APPS = [
    ("com.android.incallui", ".InCallActivity", CALL_TEXTS),
    ("com.transsion.phonemaster", "com.transsion.phonemaster.clean.CleanActivity",
     ("Membersihkan memori", "Selesai")),
    ("com.google.android.permissioncontroller", ".permission.ui.GrantPermissionsActivity",
     ("Izinkan Shopee mengirim notifikasi?", "Izinkan", "Jangan izinkan")),
]
DESK = ("org.example.desk", ".Desk", ("Beranda", "Aplikasi"))  # launcher HP ini, dikenal dari precheck


@BOTH
@pytest.mark.parametrize("package, activity, texts, home", [*((*a, None) for a in FRONT_APPS),
                                                            (*DESK, "org.example.desk/.Desk")],
                         ids=["incallui", "phonemaster", "permission", "device_launcher"])
def test_known_system_app_in_front_after_buy_is_unknown_state_not_verification(tmp_path, monkeypatch, live, package,
                                                                               activity, texts, home):
    """Telepon masuk / Phone Master / dialog izin / launcher HP (resolve-activity HOME saat precheck run) menutupi
    Shopee setelah Beli: UNKNOWN (bukan VERIFICATION), lalu UNKNOWN_STATE setelah ~1,5 s; sheet di belakang tidak
    dikonfirmasi, tombol dialog tidak disentuh.
    terkait: test_android_runner.test_unknown_screen_after_buy_diagnosed_once_without_reclick dan
    test_android_safety.test_unknown_screen_after_buy_within_unknown_limit (varian launcher3 / layar asing)."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_front_app(package, activity, texts, None, home=home), live=live,
               runner_attrs=attrs)
    assert out.runner.home_packages == {(home or "com.android.launcher3/").split("/")[0]}
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    where = f"{package}/{activity}"
    assert out.result.message.startswith(f"layar tidak dikenali > 1.5 s (Shopee keluar dari foreground: {where}, "), \
        out.result.message
    assert f"aplikasi aktif {where}, (aplikasi lain di depan)" in out.result.message
    assert out.result.message != MAYBE_ORDERED_MSG and out.result.detail == ""
    buy = _one_buy(out)
    dt = _step_t(out, "result") - buy["t_server_ms"]
    assert 1500 <= dt <= 2200, f"UNKNOWN_STATE {dt} ms setelah klik (jaring 1,5 s)"
    assert "no_response" not in out.step_names() and out.kind("confirm") == []
    assert out.app.screen == "front", "aplikasi di depan dibiarkan (Shopee tidak dibuka ulang)"
    _left_alone(out, out.kind("front")[0])
    _hard_rules(out)


@BOTH
def test_short_incoming_call_after_buy_resumes_without_reclick(tmp_path, monkeypatch, live):
    """Telepon masuk 1 s (< 1,5 s) setelah Beli: UNKNOWN sementara tidak menghentikan run; tidak ada aksi apa pun
    selama Shopee tertutup; setelah kembali sheet dikonfirmasi sekali (tanpa klik Beli ulang)."""
    pkg, act, texts = FRONT_APPS[0]
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_front_app(pkg, act, texts, 1.0), live=live, runner_attrs=attrs)
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message
    buy = _one_buy(out)
    back_at = buy["t_server_ms"] + 1000
    confirms = out.kind("confirm")
    assert len(confirms) == 1 and confirms[0]["t_server_ms"] >= back_at, "konfirmasi hanya setelah Shopee kembali"
    # nol aksi apa pun (tap, back, swipe, intent) selama Shopee tertutup, bukan hanya tap
    assert [e for e in out.app.events if buy["t_server_ms"] < e["t_server_ms"] < back_at and e["kind"] != "front"] \
        == [], "tidak ada aksi selama telepon menutupi Shopee"
    assert not attrs["stop_event"].is_set()
    assert out.events() == ([str(RunStatus.ORDER_PLACED_AWAIT_PIN)] if live else [])
    _hard_rules(out, ordered=live)


@BOTH
def test_crash_after_buy_is_unknown_state_with_dump_screenshot_and_activity(tmp_path, live):
    """Shopee crash setelah Beli (launcher + dialog 'Shopee telah berhenti'): UNKNOWN_STATE setelah ~1,5 s, dialog
    crash disebut di pesan; dump hierarki + screenshot + activity akhir tersimpan; alarm; tanpa retry; tombol
    'Tutup aplikasi' tidak disentuh. Pesan dasar (dry) sudah diuji di test_android_runner
    test_unknown_screen_after_buy_diagnosed_once_without_reclick; di sini diagnosa & live."""
    attrs = _stop()
    out = run_android(tmp_path, live=live, crash_after_buy=True, runner_attrs=attrs)
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert out.result.message.startswith("layar tidak dikenali > 1.5 s (dialog sistem 'Shopee telah berhenti' "
                                         "(crash/ANR), aplikasi aktif com.android.launcher3/.Launcher"), \
        out.result.message
    assert out.result.detail == "" and not out.runner.order_clicked
    buy = _one_buy(out)
    assert 1500 <= _step_t(out, "result") - buy["t_server_ms"] <= 2200
    assert [e["detail"] for e in out.kind("tap")] == ["buy"], "tanpa tap di dialog crash"
    # diagnosa akhir: screenshot + dump (nama sama) + activity di log
    assert len(out.result.screenshots) == 1
    shot = out.result.screenshots[0]
    assert shot.exists() and shot.name.endswith("-UNKNOWN_STATE.png")
    dump = shot.with_suffix(".xml")
    assert dump.exists() and dump.read_text(encoding="utf-8").startswith("<hierarchy>")
    log = _log_text(out)
    assert "layar akhir: com.android.launcher3/.Launcher" in log
    assert f"dump hierarki: {dump.name}" in log
    data = json.loads((out.log_dir / "android-result.json").read_text(encoding="utf-8"))
    assert data["status"] == "UNKNOWN_STATE" and data["screenshots"] == [str(shot)]
    assert out.app.screen == "crashed"
    _left_alone(out, buy)
    _hard_rules(out)


# ------------------------------------------------------------------ Shopee tertutup saat polling (sebelum Beli)


COVER_STOP = [
    # reCAPTCHA sudah di depan saat polling dimulai
    pytest.param(RECAPTCHA, ("Saya bukan robot", "Verifikasi"), -0.3, 10.0, {}, RunStatus.VERIFICATION,
                 id="recaptcha_before_T"),
    # reCAPTCHA muncul T+0,3 s, halaman belum live: reload intent (T+0,5 s) akan membuka Shopee di atasnya
    pytest.param(RECAPTCHA, ("Saya bukan robot", "Verifikasi"), 0.3, 10.0,
                 {"live_update": False, "cfg": {"android": {"reload": "intent"}}}, RunStatus.VERIFICATION,
                 id="recaptcha_after_T_intent"),
    # telepon masuk sejak sebelum T, 5 s
    pytest.param(INCALL, CALL_TEXTS, -0.3, 5.0, {}, RunStatus.UNKNOWN_STATE, id="call_5s"),
]


@BOTH
@pytest.mark.parametrize("front, texts, start_s, dur_s, scenario, status", COVER_STOP)
def test_front_window_during_polling_stops_without_tap_or_reload(tmp_path, monkeypatch, live, front, texts, start_s,
                                                                 dur_s, scenario, status):
    """Package/activity di depan saat polling (sebelum klik Beli): paket asing -> VERIFICATION segera; telepon
    (paket sistem) -> UNKNOWN -> UNKNOWN_STATE ~1,5 s. Keduanya: NOL tap, NOL reload (swipe/intent), stop semua +
    alarm, jendela depan dibiarkan. terkait: kasus setelah Beli di test_foreign_package_after_buy_* dan
    test_known_system_app_in_front_after_buy_*."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_covered_app(front, texts, start_s, dur_s), live=live,
               runner_attrs=attrs, **scenario)
    _stopped(out, attrs, status)
    if status == RunStatus.VERIFICATION:
        assert out.result.message == f"aplikasi/activity asing di depan: {front}"
    else:
        assert out.result.message.startswith(f"layar tidak dikenali > 1.5 s (Shopee keluar dari foreground: {front}, "
                                             f"aplikasi aktif {front}, (aplikasi lain di depan)"), out.result.message
    assert out.result.detail == "" and not out.runner.order_clicked
    assert out.kind("tap") == [] and "click" not in _ops(out), "nol tap"
    assert polling_actions(out) == [] and "reload" not in out.step_names(), "nol reload"
    assert not {"swipe_refresh", "press_back"} & set(_ops(out))
    covered_at = max(_t(out) + int(start_s * 1000), _step_t(out, "poll_start"))
    dt = _step_t(out, "result") - covered_at
    if status == RunStatus.VERIFICATION:
        assert 0 <= dt < 400, f"VERIFICATION {dt} ms setelah Shopee tertutup (tanpa menunggu jaring UNKNOWN)"
    else:
        assert 1500 <= dt <= 2200, f"UNKNOWN_STATE {dt} ms setelah Shopee tertutup"
    assert out.app.covered(), "jendela depan dibiarkan (Shopee tidak dibuka ulang)"
    _left_alone(out, None)
    _hard_rules(out)


@BOTH
@pytest.mark.parametrize("reload", ["intent", "swipe"])
def test_short_call_during_polling_defers_reload_until_shopee_back(tmp_path, monkeypatch, live, reload):
    """Telepon T+0,2..T+1,2 s (< 1,5 s) saat halaman belum live: reload T+0,5 s TIDAK dilakukan selama Shopee
    tertutup (intent akan menarik Shopee ke depan, swipe mengenai layar telepon); reload pertama setelah telepon
    selesai, lalu Beli dan alur normal (tanpa stop). terkait: test_android_spec_k
    test_refresh_first_at_t_plus_0_5_then_every_2_s_through_rate_limiter (jadwal tanpa gangguan)."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_covered_app(INCALL, CALL_TEXTS, 0.2, 1.0), live=live,
               runner_attrs=attrs, live_update=False, cfg={"android": {"reload": reload}})
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message
    t = _t(out)
    start, end = t + 200, t + 1200
    assert [e for e in out.app.events if start <= e["t_server_ms"] < end] == [], "nol aksi selama Shopee tertutup"
    assert any(op == "current_app" and start <= ts < end for ts, op, _ in _run_queries(out)), \
        "telepon harus terbaca dari package/activity (UNKNOWN), bukan terlewat"
    acts = polling_actions(out)
    buy = out.kind("buy")
    assert len(acts) == 2 and len(buy) == 1 and acts[1] == buy[0]["t_server_ms"], "satu reload lalu satu Beli"
    assert end <= acts[0] <= end + 400, f"reload pertama {acts[0] - t} ms (setelah telepon selesai)"
    assert out.step_names().count("reload") == 1 and _step_t(out, "reload") >= end
    assert not attrs["stop_event"].is_set() and out.result.detail == ""
    assert out.events() == ([str(RunStatus.ORDER_PLACED_AWAIT_PIN)] if live else [])
    _hard_rules(out, ordered=live)


# ------------------------------------------------------------------ Shopee keluar setelah "Buat Pesanan"


LAUNCHER = "com.android.launcher3/.Launcher"


@pytest.mark.parametrize("screen, front, texts, status, evidence", [
    ("crashed", None, (), RunStatus.UNKNOWN_STATE, f"Shopee keluar dari foreground: {LAUNCHER}"),
    ("other_app", None, (), RunStatus.UNKNOWN_STATE, f"Shopee keluar dari foreground: {LAUNCHER}"),
    ("foreign", INCALL, CALL_TEXTS, RunStatus.UNKNOWN_STATE, f"Shopee keluar dari foreground: {INCALL}"),
    ("foreign", RECAPTCHA, (), RunStatus.VERIFICATION, f"aplikasi/activity asing di depan: {RECAPTCHA}"),
], ids=["crash", "launcher", "incallui", "recaptcha"])
def test_shopee_leaves_foreground_after_place_order_is_maybe_ordered(tmp_path, monkeypatch, screen, front, texts,
                                                                     status, evidence):
    """Live: setelah klik 'Buat Pesanan' Shopee crash / ke launcher / tertutup telepon -> UNKNOWN -> UNKNOWN_STATE
    ~1,5 s; reCAPTCHA (paket asing) -> VERIFICATION segera. Semua: pesan wajib 'Pesanan MUNGKIN sudah terbuat',
    penyebab asli di detail, alarm sekali dengan pesan itu, stop semua, tepat 1 pesanan, nol aksi setelahnya (PIN
    tidak disentuh), screenshot + dump + activity akhir. terkait: test_android_safety
    test_spinner_forever_after_order_* / test_challenge_after_order_* (layar di dalam Shopee)."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_leaves_after_order(screen, front, texts), live=True,
               runner_attrs=attrs)
    _stopped(out, attrs, status)
    assert out.result.message == MAYBE_ORDERED_MSG
    assert out.notifier.events[0]["message"] == MAYBE_ORDERED_MSG
    if status == RunStatus.UNKNOWN_STATE:
        assert out.result.detail.startswith(f"UNKNOWN_STATE: layar tidak dikenali > 1.5 s ({evidence}, "), \
            out.result.detail
    else:
        assert out.result.detail == f"VERIFICATION: {evidence}"
    order = out.kind("order")[0]
    dt = _step_t(out, "result") - order["t_server_ms"]
    if status == RunStatus.UNKNOWN_STATE:
        assert 1500 <= dt <= 2200, f"UNKNOWN_STATE {dt} ms setelah 'Buat Pesanan' (jaring 1,5 s)"
    else:
        assert dt < 400, f"VERIFICATION {dt} ms setelah 'Buat Pesanan'"
    assert "pin_screen" not in out.step_names()
    shot = out.result.screenshots[0]
    assert len(out.result.screenshots) == 1 and shot.exists() and shot.name.endswith(f"-{status}.png")
    assert shot.with_suffix(".xml").read_text(encoding="utf-8").startswith("<hierarchy>")
    where = f"{front}" if front else LAUNCHER
    assert f"layar akhir: {where}" in _log_text(out)
    data = json.loads((out.log_dir / "android-result.json").read_text(encoding="utf-8"))
    assert (data["status"], data["message"], data["detail"]) == (str(status), MAYBE_ORDERED_MSG, out.result.detail)
    assert out.app.screen == screen, "aplikasi di depan dibiarkan (Shopee tidak dibuka ulang)"
    _left_alone(out, order)
    _hard_rules(out, maybe=True)


# ------------------------------------------------------------------ dialog ANR di atas Shopee


@BOTH
def test_short_anr_dialog_at_open_blocks_buy_until_gone(tmp_path, monkeypatch, live):
    """Dialog ANR T-0,3..T+0,6 s (elemen Shopee tetap terbaca di bawahnya): tidak ada klik Beli/reload selama
    dialog tampil (tap akan mengenai 'Tutup aplikasi'); setelah hilang, run berlanjut normal."""
    out = _run(tmp_path, monkeypatch, app_cls=_AnrReadsApp, live=live, anr_dialog=(-0.3, 0.6))
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message
    gone = _t(out) + 600
    assert out.app.dialog_reads, "dialog harus terbaca (info_any) sebelum klik"
    assert min(out.app.dialog_reads) * 1000 < _t(out), "dialog sudah terlihat sebelum T"
    buy = _one_buy(out)
    assert buy["t_server_ms"] >= gone, f"klik Beli {buy['t_server_ms'] - _t(out)} ms saat dialog masih tampil"
    assert all(e["t_server_ms"] >= gone for e in out.app.events if e["t_server_ms"] >= _t(out) - 1000), \
        "tidak ada aksi selama dialog tampil"
    assert any("tidak ada klik selama dialog tampil" in w for w in out.runner.log.warnings)
    assert out.events() == ([str(RunStatus.ORDER_PLACED_AWAIT_PIN)] if live else [])
    _hard_rules(out, ordered=live)


@BOTH
def test_long_anr_dialog_at_open_is_unknown_state_without_any_tap(tmp_path, monkeypatch, live):
    """Dialog ANR T-0,5..T+5 s: UNKNOWN sejak pertama terlihat -> UNKNOWN_STATE ~1,5 s kemudian, NOL tap/reload,
    alarm, stop semua; dialog dibiarkan (tanpa 'Tunggu'/'Tutup aplikasi')."""
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=_AnrReadsApp, live=live, anr_dialog=(-0.5, 5.0), runner_attrs=attrs)
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert out.result.message.startswith(f"layar tidak dikenali > 1.5 s ({ANR_TEXT}"), out.result.message
    assert out.result.message != MAYBE_ORDERED_MSG and not out.runner.order_clicked
    first = int(min(out.app.dialog_reads) * 1000)
    assert -200 <= first - _t(out) <= 0, "dialog terlihat di iterasi polling pertama (T-lead)"
    dt = _step_t(out, "result") - first
    assert 1500 <= dt <= 2000, f"UNKNOWN_STATE {dt} ms setelah dialog pertama terlihat"
    assert out.kind("tap") == [] and out.kind("buy") == [] and "click" not in _ops(out), "nol tap"
    assert polling_actions(out) == [], "tanpa reload selama dialog tampil"
    _left_alone(out, None)
    _hard_rules(out)


@BOTH
@pytest.mark.parametrize("trigger, blocked", [
    ("click_buy", "confirm"),  # dialog di atas bottom sheet sebelum konfirmasi
    ("price_guard_ok", "order"),  # dialog di checkout tepat sebelum "Buat Pesanan"
], ids=["over_sheet", "before_place_order"])
def test_short_anr_dialog_after_buy_delays_next_tap_until_gone(tmp_path, monkeypatch, live, trigger, blocked):
    """Dialog ANR 0,6 s muncul setelah Beli (di atas sheet) atau tepat sebelum 'Buat Pesanan': tap berikutnya
    (konfirmasi / Buat Pesanan) baru terjadi setelah dialog hilang; tidak ada tap di dialog."""
    app_cls, setup = _anr_on_step(trigger, 0.6)
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, setup=setup, live=live)
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message
    start, end = (int(x * 1000) for x in out.app.window)
    assert len(out.kind("anr")) == 1 and out.app.dialog_reads, "dialog harus muncul dan terbaca"
    assert [e for e in out.app.events if start <= e["t_server_ms"] < end and e["kind"] != "anr"] == [], \
        "tidak ada aksi selama dialog tampil"
    _one_buy(out)
    if blocked == "confirm":
        assert [e["t_server_ms"] >= end for e in out.kind("confirm")] == [True]
    if live:
        assert out.kind("order")[0]["t_server_ms"] >= end
    else:
        assert _step_t(out, "place_order_gate") >= end, "gerbang 'Buat Pesanan' baru dilewati setelah dialog hilang"
    _hard_rules(out, ordered=live)


@BOTH
@pytest.mark.parametrize("trigger", ["click_buy", "price_guard_ok"], ids=["over_sheet", "before_place_order"])
def test_persistent_anr_dialog_after_buy_is_unknown_state_without_order(tmp_path, monkeypatch, live, trigger):
    """Dialog ANR menetap setelah Beli / tepat sebelum 'Buat Pesanan': UNKNOWN_STATE ~1,5 s setelah terlihat,
    'Buat Pesanan' TIDAK diklik (order_clicked False -> bukan pesan 'Pesanan MUNGKIN sudah terbuat')."""
    app_cls, setup = _anr_on_step(trigger, 5.0)
    attrs = _stop()
    out = _run(tmp_path, monkeypatch, app_cls=app_cls, setup=setup, live=live, runner_attrs=attrs)
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert out.result.message.startswith(f"layar tidak dikenali > 1.5 s ({ANR_TEXT}"), out.result.message
    assert out.result.message != MAYBE_ORDERED_MSG and out.result.detail == ""
    assert not out.runner.order_clicked and out.kind("order") == []
    assert out.app.events[-1]["kind"] == "anr", "tidak ada aksi setelah dialog muncul"
    dt = _step_t(out, "result") - int(out.app.window[0] * 1000)
    assert 1500 <= dt <= 2000, f"UNKNOWN_STATE {dt} ms setelah dialog muncul"
    _one_buy(out)
    if trigger == "click_buy":
        assert out.kind("confirm") == [] and out.kind("checkout") == []
    else:
        assert "price_guard_ok" in out.step_names() and "place_order_gate" not in out.step_names()
    _left_alone(out, out.app.events[-1])
    _hard_rules(out)


# ------------------------------------------------------------------ loading selamanya


@pytest.mark.parametrize("limit", [None, 3.0], ids=["default_10s", "configured_3s"])
def test_loading_forever_after_buy_is_unknown_state_at_loading_limit(tmp_path, limit):
    """Spinner tanpa teks yang tidak pernah selesai setelah Beli: ditunggu (bukan 'tidak ada reaksi', tanpa klik
    ulang/reload) sampai loading_limit_s, lalu UNKNOWN_STATE + alarm + stop semua. Kasus spinner setelah
    'Buat Pesanan' sudah diuji di test_android_safety."""
    attrs = _stop() | ({"loading_limit_s": limit} if limit else {})
    out = run_android(tmp_path, live=True, loading_after_buy_ms=60_000, runner_attrs=attrs)
    expected_limit = limit or LOADING_LIMIT_S
    if limit is None:
        assert out.runner.loading_limit_s == LOADING_LIMIT_S == 10.0, "batas bawaan 10 s"
    _stopped(out, attrs, RunStatus.UNKNOWN_STATE)
    assert out.result.message.startswith(f"indikator loading > {expected_limit:.0f} s (indikator loading, aplikasi "
                                         "aktif com.shopee.id/"), out.result.message
    assert out.result.message != MAYBE_ORDERED_MSG and not out.runner.order_clicked
    buy = _one_buy(out)
    dt = _step_t(out, "result") - buy["t_server_ms"]
    assert expected_limit * 1000 <= dt <= expected_limit * 1000 + 800, dt
    assert "no_response" not in out.step_names() and out.kind("confirm") == []
    assert out.app.screen == "loading"
    _left_alone(out, buy)
    _hard_rules(out)


# ------------------------------------------------------------------ hot path


@pytest.mark.parametrize("scenario", [
    {"live_update": False, "cfg": {"android": {"reload": "intent"}}},
    {"variants": ["128GB Hitam", "256GB Biru"], "variants_on_page": True, "sheet": False, "live_update": False,
     "cfg": {"variant": "256GB Biru", "android": {"reload": "intent"}}},
    {"anr_dialog": (-0.3, 0.6)},
], ids=["reload_intent", "variant_reselect", "short_anr"])
def test_polling_hot_path_never_reads_package_activity_or_webview(tmp_path, scenario):
    """poll_start..click_buy: tanpa current_app (adb dumpsys, mahal) dan tanpa cek WebView, juga saat reload
    intent, pilih ulang variasi, dan dialog ANR singkat (dialog dibaca lewat info_any satu objek).
    terkait: test_android_runner.test_polling_hot_path_uses_single_object_queries_only (run normal dan reload
    swipe T+0,5/+2,5 s [never_opens]: hanya info/exists/info_any)."""
    out = run_android(tmp_path, **scenario)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    t0, t1 = _step_t(out, "poll_start"), _step_t(out, "click_buy")
    window = [op for t, op, _ in _run_queries(out) if t0 <= t <= t1]
    assert window.count("info") > 3
    assert window.count("current_app") == 0 and window.count("webview") == 0, sorted(set(window))
    assert not {"find_all", "dump", "screenshot", "last_toast"} & set(window), sorted(set(window))
    if scenario.get("live_update") is False:
        assert "reload" in out.step_names()
    if "variant" in scenario.get("cfg", {}):
        # variasi dipilih ulang SETELAH reload dan SEBELUM klik Beli (di dalam jendela polling)
        steps, names = out.result.steps, out.step_names()
        i0, i1 = names.index("poll_start"), names.index("click_buy")
        again = [k for k in range(i0, i1)
                 if steps[k].name == "variant_selected" and "ulang setelah reload" in steps[k].detail]
        assert len(again) == 1 and names.index("reload", i0) < again[0], names[i0:i1 + 1]
        assert [e["detail"] for e in out.kind("variant")][-1] == "256GB Biru"
        assert out.app.selected_variant == "256GB Biru"
    _hard_rules(out)
