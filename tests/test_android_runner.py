"""E2E jalur Android: AndroidRunner di atas FakeDriver + FakeShopeeApp (waktu virtual, FakeClock).

Semua skenario lewat tests/android_harness.run_android; tidak ada sleep nyata di hot path, jadi
tiap tes selesai dalam hitungan milidetik. Waktu di sini = waktu server (ms) relatif T (slot buka).

Pesan aplikasi ("Silakan pilih variasi", "Stok habis", ...) diuji lewat dua kanal: overlay in-app yang ada
di pohon node (toast_mode="node") dan Toast Android yang hanya terbaca lewat getLastToast (driver.last_toast).
"""

from __future__ import annotations

import asyncio
import csv
import re
import threading

import pytest

from flashbuy.android_driver import Node
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus
from tests import android_harness
from tests.android_harness import assert_polling_rules, polling_actions, run_android
from tests.conftest import LIVE_NAME
from tests.fake_android import FakeShopeeApp

BOTH = pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
RELOAD = pytest.mark.parametrize("reload", ["swipe", "intent"])
# kanal pesan aplikasi: overlay in-app (node) / Toast Android (getLastToast, tampil asinkron setelah tap)
TOAST = pytest.mark.parametrize("toast", ["node", "toast"], ids=["overlay_node", "android_toast"])
VARIANTS = ["128GB Hitam", "256GB Biru"]
TARGET_VARIANT = "256GB Biru"
VARIANT_PRICES = {"128GB Hitam": 99_000, "256GB Biru": 95_000}  # variasi target lebih murah -> terlihat di checkout
VARIANT_TOAST = "Silakan pilih variasi terlebih dahulu"
BUY_SEL = "text='Beli Sekarang'"  # str(Sel) kandidat pertama buy_button (default)

# Operasi agent saja (getLastToast / clearLastToast): tidak menyentuh aplikasi maupun server Shopee.
AGENT_OPS = {"last_toast", "clear_toast"}
# Operasi driver yang sah dipakai runner (tidak ada input teks: PIN tidak mungkin diketik alat).
ALLOWED_OPS = {"exists", "info", "find_all", "click", "current_app", "start_url", "swipe_refresh", "press_back",
               "webview", "screenshot", "dump", "shell", "agent_alive", "restart_agent"} | AGENT_OPS
# Kueri satu objek (jalur A) - satu-satunya kueri yang boleh dipakai di hot path polling.
SINGLE_OBJECT_OPS = {"info", "exists"}
# Klik yang langsung diikuti clear_toast: Beli & konfirmasi sheet (keduanya "Beli Sekarang"), Buat Pesanan.
TOAST_CLEARED_CLICKS = {"Beli Sekarang", "Buat Pesanan"}
# Perintah shell yang menutup/mematikan aplikasi atau mengetik/menekan tombol.
FORBIDDEN_SHELL = re.compile(r"force-stop|\bam\s+(kill|stop)|\bpm\s+clear|\binput\b|\bkill\b", re.I)


# ------------------------------------------------------------------ util


def _t(out) -> int:
    return int(out.open_at * 1000)


def _rel(out, t_ms: int) -> int:
    return t_ms - _t(out)


def _no_order(out) -> None:
    assert out.kind("order") == [], "tidak boleh ada klik 'Buat Pesanan'"
    assert [e for e in out.kind("tap") if e["detail"] == "place_order"] == []


def _alarmed(out, status) -> None:
    assert out.events() == [str(status)]


def _subsequence(names: list[str], expected: list[str]) -> None:
    """`expected` muncul berurutan di `names` (boleh diselingi langkah lain)."""
    it = iter(names)
    missing = [e for e in expected if e not in it]
    assert not missing, f"langkah {missing} tidak muncul berurutan di {names}"


def _reload_times(out, method: str) -> list[int]:
    """Waktu reload yang SAMPAI ke aplikasi (ms server), hanya di sekitar jendela polling."""
    if method == "swipe":
        return [e["t_server_ms"] for e in out.kind("refresh")]
    return [e["t_server_ms"] for e in out.kind("intent") if e["t_server_ms"] >= _t(out) - 1000]


def _ops(out) -> list[str]:
    return [op for op, _ in out.driver.calls]


def _pause_at(app, t_rel_s: float) -> tuple[threading.Event, threading.Event]:
    """Tahan thread runner di query pertama setelah T+t_rel_s sampai `resume` di-set.

    Dengan jam virtual, coroutine `during` tidak bisa "menunggu waktu"; jadi runner yang ditahan di
    titik waktu deterministik, lalu coroutine bertindak (abort / stop_event), lalu runner dilepas.
    """
    reached, resume = threading.Event(), threading.Event()
    orig = app.nodes
    target = app.sc.open_at + t_rel_s

    def nodes():
        if not reached.is_set() and app.now() >= target:
            reached.set()
            resume.wait(5)
        return orig()

    app.nodes = nodes
    return reached, resume


class _AsyncToastApp(FakeShopeeApp):
    """Toast Android yang tampil ASINKRON: tercatat di getLastToast `toast_delay_s` setelah tap (seperti di HP:
    tap -> handler aplikasi -> NotificationManager -> event aksesibilitas). clearLastToast hanya menghapus
    toast yang sudah tercatat; toast yang belum tampil tetap datang sesudahnya."""

    toast_delay_s = 0.06

    def __init__(self, sc, clock):
        self._pending: tuple[str, float] | None = None
        self._recorded: str | None = None
        super().__init__(sc, clock)

    def _flush(self) -> None:
        if self._pending is not None and self.now() >= self._pending[1]:
            self._recorded, self._pending = self._pending[0], None

    def _show_toast(self, text: str) -> None:
        self.toast = (text, self.now() + self.toast_delay_s + 1.5)
        if self.sc.toast_mode == "toast":
            self._pending = (text, self.now() + self.toast_delay_s)

    @property
    def last_toast(self) -> str | None:
        self._flush()
        return self._recorded

    @last_toast.setter
    def last_toast(self, value: str | None) -> None:
        self._flush()
        self._recorded = value


def _run_toast(tmp_path, monkeypatch, toast: str, **kw):
    """run_android dengan pesan aplikasi sebagai overlay node ("node") atau Toast Android asinkron ("toast")."""
    if toast == "node":
        return run_android(tmp_path, toast_mode="node", **kw)
    monkeypatch.setattr(android_harness, "FakeShopeeApp", _AsyncToastApp)
    return run_android(tmp_path, **kw)


def _assert_toast_channel(out, toast: str, text: str) -> None:
    """Pesan benar-benar lewat kanal yang diuji (bukan kebetulan terbaca dari kanal lain)."""
    assert out.app.sc.toast_mode == ("node" if toast == "node" else "toast")
    if toast == "toast":
        assert out.app.last_toast == text, "toast harus tercatat di getLastToast (bukan node)"
        assert "last_toast" in _ops(out), "toast Android hanya terbaca lewat driver.last_toast()"


def _gated_actions(out) -> list[int]:
    """Aksi polling ke aplikasi (ms server): klik Beli, reload, DAN konfirmasi sheet ulang.

    Konfirmasi pertama setelah klik Beli = langkah maju (tanpa gate); konfirmasi berikutnya tanpa klik Beli
    di antaranya = konfirmasi ulang lewat PollingGate (aksi polling)."""
    acts: list[int] = []
    after_buy = False
    for e in out.app.events:
        k = e["kind"]
        if k in ("buy", "buy_disabled", "refresh") or (k == "intent" and e["t_server_ms"] >= _t(out) - 1000):
            acts.append(e["t_server_ms"])
            after_buy = k == "buy"
        elif k == "confirm":
            if not after_buy:
                acts.append(e["t_server_ms"])
            after_buy = False
    return sorted(acts)


def _assert_gated_rules(out) -> list[int]:
    acts = _gated_actions(out)
    t = _t(out)
    assert acts, "tidak ada aksi polling"
    assert all(t - 1000 <= a <= t + 8000 for a in acts), [a - t for a in acts]
    gaps = [b - a for a, b in zip(acts, acts[1:], strict=False)]
    assert all(g >= 400 for g in gaps), f"jarak antaraksi < 400 ms: {gaps}"
    return acts


def _run_queries(out) -> list[tuple[int, str, str]]:
    """(t_server_ms, op, target) setiap kueri run (arm + attempt) dari android-queries-run.csv."""
    with (out.log_dir / "android-queries-run.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["t_server_ms", "op", "target", "ms"]
    return [(int(r[0]), r[1], r[2]) for r in rows[1:]]


def _during_patch(fn):
    """`during` yang memasang patch ke app/runner sebelum precheck berjalan (tanpa menunggu waktu virtual)."""

    async def during(runner, app):
        fn(runner, app)

    return during


def _pin_after_buy(runner, app) -> None:
    """Tap Beli berakhir di layar PIN ShopeePay (mis. tap tak sengaja / sesi checkout lama)."""
    orig = app._tap_buy

    def tap(node):
        orig(node)
        app.screen = "pin"

    app._tap_buy = tap


def _sheet_open_at_arm(runner, app) -> None:
    """Bottom sheet sudah terbuka di halaman produk saat arm (sebelum klik Beli pertama)."""
    orig = app.on_intent

    def on_intent(url: str, package: str) -> None:
        orig(url, package)
        if app.screen == "product" and app.now() > app.sc.open_at - 10:
            app.screen = "sheet"

    app.on_intent = on_intent


def _ignore_confirms(n: int):
    """N tap konfirmasi pertama di bottom sheet tidak bereaksi (server lambat menelan tap)."""

    def patch(runner, app) -> None:
        left = [n]
        orig = app._confirm

        def confirm() -> None:
            if left[0] > 0:
                left[0] -= 1
                return
            orig()

        app._confirm = confirm

    return patch


def _screen_at_arm(screen: str):
    """Pre-check normal, tetapi intent produk saat arm (T-4 s) berakhir di `screen`."""

    def patch(runner, app) -> None:
        orig = app.on_intent

        def on_intent(url: str, package: str) -> None:
            orig(url, package)
            if app.screen == "product" and app.now() > app.sc.open_at - 10:
                app.screen = screen

        app.on_intent = on_intent

    return patch


class _StampedEvent(threading.Event):
    """stop_event bersama yang mencatat waktu server saat pertama kali di-set."""

    def __init__(self):
        super().__init__()
        self.app = None
        self.set_at_ms: int | None = None

    def set(self) -> None:
        if self.set_at_ms is None and self.app is not None:
            self.set_at_ms = int(self.app.now() * 1000)
        super().set()


# ------------------------------------------------------------------ dry-run & live dasar


DRY_TIMELINE = ["precheck", "arm", "page_open", "armed", "t_minus_lead", "poll_start", "buy_ready", "click_buy",
                "click_sheet_confirm", "buy_ok", "checkout_loaded", "payment_ok", "price_guard_ok",
                "place_order_gate", "result"]


def test_dry_run_reaches_checkout_without_ordering(tmp_path):
    out = run_android(tmp_path)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert out.runner.cfg.expected_name is None  # dry-run boleh tanpa expected_name
    assert out.step_names() == DRY_TIMELINE
    steps_t = [s.t_server_ms for s in out.result.steps]
    assert steps_t == sorted(steps_t), "timeline harus urut waktu"
    assert out.result.step("place_order_gate").detail.startswith("dry-run")
    _no_order(out)
    # klik Beli tepat sekali, pada/sesudah T; kuantitas 1
    buys = out.kind("buy")
    assert len(buys) == 1
    assert buys[0]["t_server_ms"] >= _t(out)
    assert out.result.step("click_buy").t_server_ms >= _t(out)
    assert [e["detail"] for e in out.kind("checkout")] == ["[('Ponsel Uji Coba 128GB', 99000, 1)]"]
    assert out.app.screen == "checkout"  # berhenti di checkout, tidak kembali/menutup
    assert out.events() == []  # dry-run sukses tidak membunyikan alarm
    assert_polling_rules(out)


def test_dry_run_writes_screenshot_dump_and_latency_csv(tmp_path):
    out = run_android(tmp_path)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    logs = out.log_dir
    shot = out.result.screenshots[0]
    assert shot.exists() and shot.parent == logs and shot.name.endswith("-DRYRUN_OK.png")
    xml = shot.with_suffix(".xml")
    assert xml.exists(), sorted(p.name for p in logs.iterdir())
    dump = xml.read_text(encoding="utf-8")
    assert dump.startswith("<hierarchy>") and "Buat Pesanan" in dump  # status akhir = layar checkout
    for label in ("precheck", "run"):
        lines = (logs / f"android-queries-{label}.csv").read_text(encoding="utf-8").splitlines()
        assert lines[0] == "t_server_ms,op,target,ms"
        assert len(lines) > 5
        for row in lines[1:]:
            assert float(row.rsplit(",", 1)[1]) >= 0
    run_ops = {row.split(",")[1] for row in (logs / "android-queries-run.csv").read_text().splitlines()[1:]}
    assert {"info", "find_all", "click"} <= run_ops
    log_text = (logs / "android.log").read_text(encoding="utf-8")
    assert "latensi run" in log_text and "latensi query precheck" in log_text
    # hot path tanpa dump_hierarchy: dump hanya sekali, untuk status akhir
    assert _ops(out).count("dump") == 1 and _ops(out)[-1] == "dump"
    assert (logs / "android-result.json").exists()


def test_live_places_one_order_and_stops_at_pin(tmp_path):
    out = run_android(tmp_path, live=True)
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    assert out.result.detail == ""
    orders = out.kind("order")
    assert len(orders) == 1 and orders[0]["detail"] == "ShopeePay"
    assert len(out.kind("buy")) == 1
    _subsequence(out.step_names(), ["price_guard_ok", "place_order_gate", "click_place_order", "pin_screen",
                                    "result"])
    assert out.result.step("place_order_gate").detail == "live"
    assert "nama cocok=True" in out.result.step("price_guard_ok").detail
    # layar PIN tercapai dan dibiarkan apa adanya
    assert out.app.screen == "pin"
    # PIN tidak diketik: tidak ada tap sama sekali setelah "Buat Pesanan", tidak ada tap ke kolom tanpa kunci
    # (EditText PIN), tidak ada operasi input teks / `input` shell
    i_order = out.app.events.index(orders[0])
    assert [e for e in out.app.events[i_order + 1:] if e["kind"] == "tap"] == []
    assert [e for e in out.kind("tap") if e["detail"] == "-"] == []
    assert set(_ops(out)) <= ALLOWED_OPS, set(_ops(out)) - ALLOWED_OPS
    assert not [t for op, t in out.driver.calls if op == "shell" and FORBIDDEN_SHELL.search(t)]
    _alarmed(out, RunStatus.ORDER_PLACED_AWAIT_PIN)
    assert out.notifier.events[0]["platform"] == "android"
    assert_polling_rules(out)


def test_clear_toast_right_after_buy_confirm_and_order_clicks(tmp_path):
    """clear_toast (agent saja) tepat setelah setiap klik Beli / konfirmasi sheet / Buat Pesanan, tidak di tempat
    lain; last_toast hanya dibaca setelah ada klik (bukan bagian iterasi polling)."""
    out = run_android(tmp_path, live=True, variants=VARIANTS, variant_prices=VARIANT_PRICES,
                      payment_default="COD - Cek Dulu", cfg={"variant": TARGET_VARIANT})
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    calls = out.driver.calls
    cleared = [i for i, (op, t) in enumerate(calls) if op == "click" and t in TOAST_CLEARED_CLICKS]
    labels = [calls[i][1] for i in cleared]
    assert labels == ["Beli Sekarang", "Beli Sekarang", "Buat Pesanan"], labels  # Beli, konfirmasi sheet, pesan
    for i in cleared:
        assert calls[i + 1] == ("clear_toast", ""), calls[i:i + 3]
    clears = [i for i, (op, _) in enumerate(calls) if op == "clear_toast"]
    assert clears == [i + 1 for i in cleared], "clear_toast hanya tepat setelah klik Beli/konfirmasi/Buat Pesanan"
    # klik lain (variasi, metode bayar, konfirmasi metode) tidak diikuti clear_toast
    others = [i for i, (op, t) in enumerate(calls) if op == "click" and t not in TOAST_CLEARED_CLICKS]
    assert {calls[i][1] for i in others} >= {TARGET_VARIANT, "ShopeePay"}
    assert all(calls[i + 1][0] != "clear_toast" for i in others)
    first_click = cleared[0]
    assert "last_toast" not in [op for op, _ in calls[:first_click]], "last_toast bukan bagian hot path polling"
    assert set(_ops(out)) <= ALLOWED_OPS
    assert len(out.kind("order")) == 1 and out.app.selected_variant == TARGET_VARIANT


@pytest.mark.parametrize("scenario", [{}, {"sale_skew_ms": 9500}], ids=["click", "never_opens"])
def test_polling_hot_path_uses_single_object_queries_only(tmp_path, scenario):
    """Iterasi polling = info tombol Beli + satu info bahaya (captcha|verifikasi|PIN) + satu info harga;
    tanpa find_all / dump / toast / diagnosa di jendela polling."""
    out = run_android(tmp_path, **scenario)
    t0 = out.result.step("poll_start").t_server_ms
    click = out.result.step("click_buy")
    t1 = click.t_server_ms if click is not None else out.result.step("result").t_server_ms
    window = [(t, op, target) for t, op, target in _run_queries(out) if t0 <= t <= t1]
    assert window, "tidak ada kueri di jendela polling"
    queries = [(op, target) for _, op, target in window if op not in ("swipe_refresh", "start_url")]
    assert {op for op, _ in queries} <= SINGLE_OBJECT_OPS, {op for op, _ in queries} - SINGLE_OBJECT_OPS
    # setiap iterasi diawali info tombol Beli, diikuti paling banyak 3 kueri satu objek (bahaya, harga, sheet)
    starts = [i for i, (_, target) in enumerate(queries) if target == BUY_SEL]
    assert len(starts) >= 3, queries[:6]
    for a, b in zip(starts, starts[1:], strict=False):
        assert b - a - 1 <= 3, queries[a:b]
    if click is None:
        assert out.result.status == RunStatus.NOT_STARTED_TIMEOUT, out.result.message
    else:
        assert out.result.status == RunStatus.DRYRUN_OK, out.result.message


# ------------------------------------------------------------------ variasi


def test_variant_selected_in_sheet(tmp_path):
    out = run_android(tmp_path, variants=VARIANTS, variant_prices=VARIANT_PRICES, cfg={"variant": TARGET_VARIANT})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _subsequence(out.step_names(), ["click_buy", "variant_selected", "click_sheet_confirm", "buy_ok"])
    assert out.result.step("variant_selected").detail == TARGET_VARIANT
    assert [e["detail"] for e in out.kind("variant")] == [TARGET_VARIANT]
    assert out.app.selected_variant == TARGET_VARIANT
    assert "harga=Rp95.000" in out.result.step("price_guard_ok").detail  # harga variasi target di checkout
    assert len(out.kind("buy")) == 1
    _no_order(out)


def test_variant_on_page_preselected_before_polling(tmp_path):
    out = run_android(tmp_path, variants=VARIANTS, variants_on_page=True, variant_prices=VARIANT_PRICES,
                      cfg={"variant": TARGET_VARIANT})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    assert names.index("variant_selected") < names.index("armed") < names.index("poll_start")
    assert names.count("variant_selected") == 1  # di sheet sudah terpilih -> tidak diklik ulang
    variant_taps = out.kind("variant")
    assert [e["detail"] for e in variant_taps] == [TARGET_VARIANT]
    assert variant_taps[0]["t_server_ms"] < _t(out) - 1000  # saat arm, sebelum jendela polling
    # chip di halaman produk membuka sheet -> ditutup dengan back saat arm (bukan setelah hasil)
    backs = out.kind("back")
    assert [e["detail"] for e in backs] == ["sheet"] and backs[0]["t_server_ms"] < _t(out)
    assert out.app.selected_variant == TARGET_VARIANT
    assert "harga=Rp95.000" in out.result.step("price_guard_ok").detail
    _no_order(out)
    assert_polling_rules(out)


def test_variant_required_with_config_variant(tmp_path):
    out = run_android(tmp_path, variants=VARIANTS, variant_required=True, cfg={"variant": "128GB Hitam"})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert out.app.selected_variant == "128GB Hitam"
    assert len(out.kind("buy")) == 1 and len(out.kind("confirm")) == 1
    _no_order(out)


@TOAST
def test_variant_required_without_config_variant_direct_checkout_is_error(tmp_path, monkeypatch, toast):
    # tanpa bottom sheet: pesan "Silakan pilih variasi" langsung di halaman produk
    out = _run_toast(tmp_path, monkeypatch, toast, variants=VARIANTS, variant_required=True, sheet=False)
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert "variasi" in out.result.message and "variant" in out.result.message
    assert len(out.kind("buy")) == 1, "tidak boleh retry"
    assert out.kind("checkout") == []
    _assert_toast_channel(out, toast, VARIANT_TOAST)
    _no_order(out)


@TOAST
def test_variant_required_without_config_variant_in_sheet_is_error(tmp_path, monkeypatch, toast):
    # pesan di atas bottom sheet yang tetap terbuka: pesan diprioritaskan di atas SHEET
    out = _run_toast(tmp_path, monkeypatch, toast, variants=VARIANTS, variant_required=True)
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert "variasi" in out.result.message
    assert len(out.kind("buy")) == 1, "klik Beli tidak boleh diulang"
    assert len(out.kind("confirm")) == 1, "konfirmasi sheet tidak boleh diulang"
    assert "no_response" not in out.step_names()
    assert out.kind("checkout") == []
    _assert_toast_channel(out, toast, VARIANT_TOAST)
    _no_order(out)
    _assert_gated_rules(out)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_runner._attempt/_handle_sheet memanggil d.clear_toast() SETELAH d.click(); toast yang "
    "dipicu klik itu sendiri dan sudah tercatat sebelum RPC clearLastToast sampai ke agent ikut terhapus "
    "(race nyata di HP cepat; deterministik di FakeShopeeApp yang menampilkan toast saat tap). Reaksi "
    "'Silakan pilih variasi' hilang -> dianggap 'tidak ada reaksi'/'sheet tidak bereaksi' -> Beli/konfirmasi "
    "diulang lewat gate sampai T+8 s (5 klik) dan hasil NOT_STARTED_TIMEOUT, bukan ERROR 'wajib pilih "
    "variasi'. Perbaikan: clear_toast SEBELUM klik"))
@pytest.mark.parametrize("sheet", [True, False], ids=["sheet", "direct"])
def test_reaction_toast_shown_before_clear_toast_is_not_lost(tmp_path, sheet):
    # FakeShopeeApp bawaan: Toast Android tercatat seketika saat tap (sebelum clear_toast runner)
    out = run_android(tmp_path, variants=VARIANTS, variant_required=True, sheet=sheet)
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert "variasi" in out.result.message
    assert len(out.kind("buy")) == 1
    assert len(out.kind("confirm")) == (1 if sheet else 0)


@pytest.mark.parametrize("sheet", [True, False], ids=["sheet", "direct"])
def test_lost_reaction_toast_never_reaches_checkout_and_keeps_polling_rules(tmp_path, sheet):
    # pengaman yang tetap berlaku walau toast reaksi hilang (lihat tes xfail di atas)
    out = run_android(tmp_path, variants=VARIANTS, variant_required=True, sheet=sheet)
    assert out.result.status != RunStatus.DRYRUN_OK, out.result.message
    assert out.kind("checkout") == [] and out.app.selected_variant is None
    assert out.kind("variant") == [], "variasi tidak boleh dipilih tanpa config `variant`"
    _assert_gated_rules(out)
    _no_order(out)


@pytest.mark.parametrize("on_page", [False, True], ids=["sheet", "on_page"])
def test_missing_variant_is_error(tmp_path, on_page):
    out = run_android(tmp_path, variants=VARIANTS, variants_on_page=on_page, cfg={"variant": "512GB Emas"})
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert "512GB Emas" in out.result.message
    assert out.kind("variant") == [] and out.kind("confirm") == [] and out.kind("checkout") == []
    assert len(out.kind("buy")) == 1, "tidak boleh retry"
    _no_order(out)


# ------------------------------------------------------------------ jalur checkout


@BOTH
def test_direct_checkout_without_sheet(tmp_path, live):
    out = run_android(tmp_path, sheet=False, live=live)
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message
    names = out.step_names()
    assert "click_sheet_confirm" not in names
    assert out.result.step("buy_ok").detail == "CHECKOUT"
    assert out.kind("confirm") == []
    assert [e["kind"] for e in out.app.events if e["kind"] in ("buy", "checkout")] == ["buy", "checkout"]
    assert len(out.kind("order")) == (1 if live else 0)
    assert_polling_rules(out)


def test_cart_flow_dry_run(tmp_path):
    out = run_android(tmp_path, go_cart=True)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert out.result.step("buy_ok").detail == "CART"
    _subsequence(out.step_names(), ["click_buy", "click_sheet_confirm", "buy_ok", "cart_ok", "click_checkout",
                                    "checkout_loaded", "payment_ok", "price_guard_ok", "place_order_gate"])
    assert "cart_uncheck" not in out.step_names()
    assert [e["detail"] for e in out.kind("checkout")] == ["[('Ponsel Uji Coba 128GB', 99000, 1)]"]
    _no_order(out)
    assert_polling_rules(out)


def test_cart_flow_live_unchecks_other_item_and_orders_once(tmp_path):
    out = run_android(tmp_path, go_cart=True, live=True, cart_other_items=[("Kabel Data USB-C", 25_000, True)])
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    assert out.result.step("cart_uncheck").detail == "Kabel Data USB-C"
    assert [e["detail"] for e in out.kind("cart_toggle")] == ["Kabel Data USB-C"]
    _subsequence(out.step_names(), ["cart_uncheck", "cart_ok", "click_checkout", "price_guard_ok",
                                    "click_place_order", "pin_screen"])
    # hanya item target (qty 1) yang masuk checkout; tepat satu pesanan
    assert [e["detail"] for e in out.kind("checkout")] == ["[('Ponsel Uji Coba 128GB', 99000, 1)]"]
    assert len(out.kind("order")) == 1


@pytest.mark.parametrize("confirm_button", [True, False], ids=["confirm_btn", "no_confirm_btn"])
@BOTH
def test_payment_default_cod_switched_to_shopeepay(tmp_path, live, confirm_button):
    out = run_android(tmp_path, live=live, payment_default="COD - Cek Dulu", payment_confirm_button=confirm_button)
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message
    names = out.step_names()
    _subsequence(names, ["checkout_loaded", "payment_change", "select_shopeepay", "payment_ok", "price_guard_ok",
                         "place_order_gate"])
    assert out.result.step("payment_change").detail == "sebelumnya 'COD - Cek Dulu'"
    assert ("payment_confirm" in names) == confirm_button
    assert out.result.step("payment_ok").detail == "ShopeePay dipilih (sebelumnya metode lain)"
    assert [e["detail"] for e in out.kind("payment")] == ["ShopeePay"]
    assert out.app.payment_method == "ShopeePay"
    if live:
        assert [o["detail"] for o in out.kind("order")] == ["ShopeePay"]
        assert out.app.screen == "pin"
    else:
        _no_order(out)


# ------------------------------------------------------------------ server lambat / tidak bereaksi


def test_loading_spinner_after_buy_is_not_unknown_state(tmp_path):
    out = run_android(tmp_path, loading_after_buy_ms=2500)
    assert out.result.status != RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert len(out.kind("buy")) == 1
    assert len(out.kind("checkout")) == 1
    assert out.result.step("checkout_loaded").t_server_ms >= out.kind("buy")[0]["t_server_ms"] + 2500
    assert out.events() == []
    _no_order(out)
    assert_polling_rules(out)


@pytest.mark.parametrize("loading_ms", [2500, 5000])
def test_loading_spinner_after_buy_does_not_reclick(tmp_path, loading_ms):
    # LOADING mereset timer 'tidak ada reaksi': spinner lama (> NO_RESPONSE_S, > batas UNKNOWN) tetap ditunggu
    out = run_android(tmp_path, loading_after_buy_ms=loading_ms)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    assert "no_response" not in names
    assert names.count("click_buy") == 1 and len(out.kind("buy")) == 1
    assert "click_sheet_confirm" in names and len(out.kind("confirm")) == 1
    assert out.kind("confirm")[0]["t_server_ms"] >= out.kind("buy")[0]["t_server_ms"] + loading_ms
    assert polling_actions(out) == [e["t_server_ms"] for e in out.kind("buy")], "tanpa reload selama loading"
    assert out.events() == []
    _no_order(out)


def test_loading_spinner_after_buy_live_keeps_configured_variant(tmp_path):
    out = run_android(tmp_path, live=True, loading_after_buy_ms=2500, variants=VARIANTS,
                      variant_prices=VARIANT_PRICES, cfg={"variant": TARGET_VARIANT})
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    assert len(out.kind("order")) == 1
    assert len(out.kind("buy")) == 1 and len(out.kind("confirm")) == 1
    assert [e["detail"] for e in out.kind("variant")] == [TARGET_VARIANT]
    assert out.app.selected_variant == TARGET_VARIANT, "pesanan dibuat tanpa variasi yang diminta"
    assert "harga=Rp95.000" in out.result.step("price_guard_ok").detail


def test_no_response_clicks_are_reclicked_with_spacing(tmp_path):
    out = run_android(tmp_path, no_response_clicks=2)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    assert names.count("no_response") == 2
    assert names.count("click_buy") == 3
    buys = [e["t_server_ms"] for e in out.kind("buy")]
    assert len(buys) == 3
    gaps = [b - a for a, b in zip(buys, buys[1:], strict=False)]
    assert all(g >= 400 for g in gaps), gaps
    assert len(out.kind("checkout")) == 1
    assert_polling_rules(out)
    _no_order(out)


def test_never_responding_buy_stops_at_window_end(tmp_path):
    out = run_android(tmp_path, no_response_clicks=50)
    assert out.result.status == RunStatus.NOT_STARTED_TIMEOUT, out.result.message
    buys = [_rel(out, e["t_server_ms"]) for e in out.kind("buy")]
    assert 2 <= len(buys) <= 20
    assert all(0 <= b <= 8000 for b in buys), buys
    assert_polling_rules(out)
    assert out.kind("checkout") == []
    _no_order(out)


@pytest.mark.parametrize("flag, evidence", [
    ("unknown_after_buy", "aplikasi aktif com.shopee.id/com.shopee.app.ui.unknown.Activity"),
    ("other_app_after_buy", "aplikasi aktif com.android.launcher3/.Launcher, (aplikasi lain di depan)"),
    ("webview_after_buy", "aplikasi aktif com.shopee.id/com.shopee.app.ui.webview.Activity, WebView"),
], ids=["unknown", "other_app", "webview"])
def test_unknown_screen_after_buy_diagnosed_once_without_reclick(tmp_path, flag, evidence):
    """UNKNOWN / aplikasi lain tidak pernah memicu 'tidak ada reaksi', klik ulang, atau reload: hanya jaring
    UNKNOWN_STATE yang memutuskan; diagnosa mahal hanya sekali, saat eskalasi."""
    stop = threading.Event()
    out = run_android(tmp_path, runner_attrs={"stop_event": stop}, **{flag: True})
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.message.startswith("layar tidak dikenali > 1.5 s (tidak ada elemen yang dikenali, ")
    assert evidence in out.result.message
    names = out.step_names()
    assert "no_response" not in names and "reload" not in names and names.count("click_buy") == 1
    buys = out.kind("buy")
    assert len(buys) == 1 and polling_actions(out) == [buys[0]["t_server_ms"]], "tanpa klik ulang / reload"
    assert out.kind("back") == []
    dt = out.result.step("result").t_server_ms - buys[0]["t_server_ms"]
    assert 1500 <= dt <= 2200, f"UNKNOWN_STATE {dt} ms setelah klik (batas 1,5 s sejak pengamatan)"
    ops = _ops(out)
    assert ops.count("webview") == 1
    assert sum(1 for op, t in out.driver.calls if op == "find_all" and t.startswith("descriptionMatches")) == 1
    assert stop.is_set()
    _alarmed(out, RunStatus.UNKNOWN_STATE)
    _no_order(out)


def _pin_common(out, stop) -> dict:
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    buys = out.kind("buy")
    assert len(buys) == 1, "tidak boleh klik ulang"
    i = out.app.events.index(buys[0])
    assert out.app.events[i + 1:] == [], "layar PIN dibiarkan apa adanya (tanpa tap/back/intent)"
    assert out.app.screen == "pin"
    assert "click_place_order" not in out.step_names() and out.kind("order") == []
    assert stop.is_set(), "UNKNOWN_STATE harus menghentikan semua runner"
    _alarmed(out, RunStatus.UNKNOWN_STATE)
    assert set(_ops(out)) <= ALLOWED_OPS
    return buys[0]


@BOTH
@pytest.mark.parametrize("pin_title", ["Masukkan PIN ShopeePay", None], ids=["pin_text", "pin_rid_only"])
def test_pin_screen_after_buy_click_is_unknown_state_and_left_alone(tmp_path, live, pin_title):
    stop = threading.Event()
    out = run_android(tmp_path, live=live, pin_title=pin_title, during=_during_patch(_pin_after_buy),
                      runner_attrs={"stop_event": stop})
    buy = _pin_common(out, stop)
    if pin_title is not None:
        # PIN bukan dari klik "Buat Pesanan" alat: pesanan MUNGKIN sudah terbuat -> pesan wajib
        assert out.result.message == MAYBE_ORDERED_MSG
        assert out.result.detail.startswith("UNKNOWN_STATE: layar PIN muncul setelah klik Beli")
        assert out.runner.order_clicked
        assert out.notifier.events[0]["message"] == MAYBE_ORDERED_MSG
        assert out.result.step("result").t_server_ms - buy["t_server_ms"] < 500, "segera, tanpa menunggu 1,5 s"


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_runner._classify memeriksa resource-id pin_screen hanya di konteks 'after_order'; layar PIN "
    "tanpa teks (hanya payment_password_field) yang muncul setelah klik Beli jadi UNKNOWN -> UNKNOWN_STATE "
    "'layar tidak dikenali' setelah 1,5 s, tanpa pesan wajib 'Pesanan MUNGKIN sudah terbuat' dan "
    "order_clicked=False (rancangan: PIN setelah Beli = mungkin sudah memesan)"))
def test_pin_screen_without_text_after_buy_click_uses_mandatory_message(tmp_path):
    stop = threading.Event()
    out = run_android(tmp_path, pin_title=None, during=_during_patch(_pin_after_buy),
                      runner_attrs={"stop_event": stop})
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.message == MAYBE_ORDERED_MSG
    assert out.runner.order_clicked


# ------------------------------------------------------------------ bottom sheet


@pytest.mark.parametrize("variant", [None, TARGET_VARIANT], ids=["no_variant", "variant"])
def test_sheet_not_reacting_is_reconfirmed_once_through_gate(tmp_path, variant):
    """Sheet masih terbuka setelah konfirmasi: konfirmasi berikutnya = aksi polling lewat gate (bukan konfirmasi
    ganda), variasi tidak diklik ulang."""
    extra = {"variants": VARIANTS, "variant_prices": VARIANT_PRICES, "cfg": {"variant": variant}} if variant else {}
    out = run_android(tmp_path, during=_during_patch(_ignore_confirms(1)), **extra)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert len(out.kind("buy")) == 1, "Beli tidak diklik ulang di atas sheet"
    confirms = [e["t_server_ms"] for e in out.kind("confirm")]
    assert len(confirms) == 2
    assert confirms[1] - confirms[0] >= 1500, "konfirmasi ulang hanya setelah sheet tidak bereaksi"
    retries = [s for s in out.result.steps if s.name == "click_sheet_confirm"]
    assert [s.detail for s in retries] == ["", "ulang (lewat gate)"]
    _subsequence(out.step_names(), ["click_buy", "click_sheet_confirm", "no_response", "click_sheet_confirm",
                                    "buy_ok"])
    if variant:
        assert [e["detail"] for e in out.kind("variant")] == [TARGET_VARIANT], "variasi terpilih tidak diklik ulang"
        assert "harga=Rp95.000" in out.result.step("price_guard_ok").detail
    assert len(out.kind("checkout")) == 1
    _assert_gated_rules(out)
    _no_order(out)


def test_sheet_never_reacting_reconfirms_only_within_window(tmp_path):
    out = run_android(tmp_path, during=_during_patch(_ignore_confirms(50)))
    assert out.result.status == RunStatus.NOT_STARTED_TIMEOUT, out.result.message
    assert len(out.kind("buy")) == 1
    confirms = [e["t_server_ms"] for e in out.kind("confirm")]
    assert 2 <= len(confirms) <= 8
    gaps = [b - a for a, b in zip(confirms, confirms[1:], strict=False)]
    assert all(g >= 1500 for g in gaps), gaps  # tidak ada konfirmasi ganda
    acts = _assert_gated_rules(out)
    assert acts[-1] <= _t(out) + 8000
    assert out.kind("checkout") == []
    _no_order(out)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_runner._wait_ready hanya menutup sheet (press_back) bila tombol Beli TIDAK ditemukan; "
    "info (jalur A, semua jendela) tetap menemukan 'Beli Sekarang' halaman produk di belakang sheet, jadi "
    "klik Beli pertama mendarat di tombol konfirmasi sheet -> sheet dikonfirmasi tanpa _handle_sheet "
    "(variasi tidak dipilih, tanpa press_back). Live dengan variant diselamatkan lapis 3 (PRICE_GUARD); "
    "rancangan: sheet terbuka sebelum klik pertama -> press_back, tanpa konfirmasi"))
def test_sheet_open_before_first_click_is_closed_not_confirmed(tmp_path):
    out = run_android(tmp_path, variants=VARIANTS, variant_prices=VARIANT_PRICES, cfg={"variant": TARGET_VARIANT},
                      during=_during_patch(_sheet_open_at_arm))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    first_tap = next(e for e in out.kind("tap") if e["t_server_ms"] >= _t(out) - 1000)
    assert first_tap["detail"] == "buy", f"klik pertama mendarat di {first_tap['detail']!r}, bukan tombol Beli"
    backs = [e for e in out.kind("back") if e["detail"] == "sheet"]
    assert backs and backs[0]["t_server_ms"] < first_tap["t_server_ms"]
    assert out.app.selected_variant == TARGET_VARIANT


def test_sheet_open_before_first_click_live_never_orders_wrong_variant(tmp_path):
    out = run_android(tmp_path, live=True, variants=VARIANTS, variant_prices=VARIANT_PRICES,
                      cfg={"variant": TARGET_VARIANT, "expected_name": LIVE_NAME},
                      during=_during_patch(_sheet_open_at_arm))
    orders = out.kind("order")
    assert len(orders) <= 1
    if orders:
        assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
        assert out.app.selected_variant == TARGET_VARIANT, "pesanan dibuat tanpa variasi yang diminta"
    else:
        # lapis 3 (fail-closed): variasi yang diminta tidak terlihat di baris produk checkout
        assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
        assert TARGET_VARIANT in out.result.message
        _alarmed(out, RunStatus.PRICE_GUARD)
        _no_order(out)
    assert set(_ops(out)) <= ALLOWED_OPS
    _assert_gated_rules(out)  # klik "Beli" yang mendarat di konfirmasi sheet tetap lewat gate


# ------------------------------------------------------------------ UI statis & slot terlambat


@RELOAD
def test_static_ui_needs_reload(tmp_path, reload):
    # live_update=False: harga/tombol flash baru tampil setelah reload; slot server baru buka T+4,8 s
    out = run_android(tmp_path, live_update=False, sale_skew_ms=4800, cfg={"android": {"reload": reload}})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    reloads = _reload_times(out, reload)
    assert len(reloads) >= 2 and len(reloads) == out.step_names().count("reload")
    assert reloads[0] >= _t(out) + 500, f"reload pertama terlalu awal: {_rel(out, reloads[0])} ms"
    gaps = [b - a for a, b in zip(reloads, reloads[1:], strict=False)]
    assert all(g >= 2000 for g in gaps), gaps
    assert all(_t(out) - 1000 <= r <= _t(out) + 8000 for r in reloads)
    # metode reload sesuai config, tidak tercampur
    other = _reload_times(out, "intent" if reload == "swipe" else "swipe")
    assert other == []
    buys = out.kind("buy")
    assert len(buys) == 1 and buys[0]["t_server_ms"] > reloads[-1]
    assert buys[0]["t_server_ms"] >= _t(out) + 4800
    _no_order(out)


@RELOAD
def test_static_ui_reload_then_buy_respects_polling_rules(tmp_path, reload):
    # reload sampai ke aplikasi di AKHIR gestur/intent; RateLimiter.touch -> Beli >= akhir reload + 425 ms
    out = run_android(tmp_path, live_update=False, cfg={"android": {"reload": reload}})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert_polling_rules(out)
    reloads = _reload_times(out, reload)
    buys = [e["t_server_ms"] for e in out.kind("buy")]
    assert reloads and len(buys) == 1
    last = max(r for r in reloads if r < buys[0])
    assert buys[0] - last >= 425, f"reload->Beli {buys[0] - last} ms (dihitung dari akhir reload)"


def _buy_button_glitch_after_reload(move: bool):
    """Tombol Beli hilang (atau bergeser) 0,3..0,9 s setelah reload saat sale - tepat saat runner menunggu slot."""

    def patch(runner, app) -> None:
        orig = app._r_product

        def render():
            out = orig()
            ra = app.refreshed_at
            if ra is None or ra < app.sc.open_at or not ra + 0.3 <= app.now() < ra + 0.9:
                return out
            if not move:
                return [(k, n) for k, n in out if k != "buy"]
            return [(k, n) if k != "buy" else (k, Node(n.text, bounds=(360, 1380, 720, 1490), clickable=True,
                                                       enabled=n.enabled)) for k, n in out]

        app._r_product = render

    return patch


@pytest.mark.parametrize("move", [False, True], ids=["vanished", "moved"])
def test_buy_button_rechecked_after_waiting_for_slot(tmp_path, move):
    """Runner menunggu slot > 50 ms (reload barusan) -> tombol dibaca ulang; hilang/bergeser -> tidak diklik di
    koordinat lama (langkah buy_recheck)."""
    out = run_android(tmp_path, live_update=False, during=_during_patch(_buy_button_glitch_after_reload(move)))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    assert "buy_recheck" in names
    assert names.index("reload") < names.index("buy_recheck") < names.index("click_buy")
    assert names.count("click_buy") == 1
    assert [e["detail"] for e in out.kind("tap") if e["t_server_ms"] >= _t(out) - 1000][:1] == ["buy"]
    assert [e for e in out.kind("tap") if e["detail"] == "-"] == [], "tap di koordinat tombol yang sudah pindah"
    buys = out.kind("buy")
    assert len(buys) == 1
    refresh = out.kind("refresh")[0]["t_server_ms"]
    if not move:
        assert buys[0]["t_server_ms"] >= refresh + 900, "klik hanya setelah tombol muncul lagi"
    assert out.result.step("buy_recheck").t_server_ms >= refresh + 425
    assert_polling_rules(out)
    _no_order(out)


def test_sale_skew_1500_buys_once_after_server_opens(tmp_path):
    out = run_android(tmp_path, sale_skew_ms=1500)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    buys = out.kind("buy")
    assert len(buys) == 1
    assert buys[0]["t_server_ms"] >= _t(out) + 1500
    assert out.kind("buy_disabled") == [], "tombol nonaktif tidak boleh diklik"
    assert out.step_names().count("click_buy") == 1
    assert_polling_rules(out)
    _no_order(out)


@RELOAD
def test_never_opens_is_not_started_timeout(tmp_path, reload):
    out = run_android(tmp_path, sale_skew_ms=9500, cfg={"android": {"reload": reload}})
    assert out.result.status == RunStatus.NOT_STARTED_TIMEOUT, out.result.message
    assert "T+8 s" in out.result.message
    assert out.kind("buy") == [] and out.kind("buy_disabled") == []
    reloads = _reload_times(out, reload)
    assert 2 <= len(reloads) <= 5
    assert all(_t(out) + 500 <= r <= _t(out) + 8000 for r in reloads), [_rel(out, r) for r in reloads]
    gaps = [b - a for a, b in zip(reloads, reloads[1:], strict=False)]
    assert all(g >= 2000 for g in gaps), gaps
    assert_polling_rules(out)
    assert out.result.step("result").t_server_ms <= _t(out) + 8000 + 500, "harus berhenti segera setelah T+8 s"
    _no_order(out)


@pytest.mark.parametrize("scenario", [
    {},
    {"live": True},
    {"sheet": False},
    {"go_cart": True},
    {"sale_skew_ms": 1500},
    {"no_response_clicks": 2},
    {"sale_skew_ms": 9500},
    {"sale_skew_ms": 9500, "cfg": {"android": {"reload": "intent"}}},
    {"variants": VARIANTS, "variants_on_page": True, "cfg": {"variant": TARGET_VARIANT}},
    {"payment_default": "COD - Cek Dulu", "live": True},
], ids=["dry", "live", "no_sheet", "cart", "skew1500", "no_response", "skew9500", "skew9500_intent",
        "variant_on_page", "cod_live"])
def test_polling_rules_hold(tmp_path, scenario):
    out = run_android(tmp_path, **scenario)
    acts = polling_actions(out)
    assert acts and acts[0] >= _t(out) - 1000
    assert_polling_rules(out)
    assert len(out.kind("order")) <= 1


# ------------------------------------------------------------------ abort / stop / lock


def test_abort_during_polling(tmp_path):
    seen: dict = {}

    async def during(runner, app):
        reached, resume = _pause_at(app, 1.2)
        try:
            if await asyncio.to_thread(reached.wait, 5):
                seen["abort_ms"] = int(app.now() * 1000)
                await runner.abort("tes abort")
        finally:
            resume.set()

    out = run_android(tmp_path, sale_skew_ms=9500, during=during)
    assert "abort_ms" in seen, "runner tidak pernah mencapai T+1,2 s"
    assert out.result.status == RunStatus.ABORTED, out.result.message
    assert "tes abort" in out.result.message
    acts = polling_actions(out)
    assert acts, "abort harus terjadi di tengah polling (setelah reload pertama)"
    assert all(a < seen["abort_ms"] for a in acts), "tidak boleh ada aksi polling setelah abort"
    assert out.result.step("result").t_server_ms >= seen["abort_ms"]
    assert out.result.step("result").t_server_ms - seen["abort_ms"] < 400, "abort harus segera berlaku"
    assert out.events() == []
    _no_order(out)


@pytest.mark.parametrize("event_type", [threading.Event, asyncio.Event], ids=["threading", "asyncio"])
def test_stop_event_from_other_runner_aborts(tmp_path, event_type):
    stop = event_type()
    seen: dict = {}

    async def during(runner, app):
        reached, resume = _pause_at(app, 1.2)
        try:
            if await asyncio.to_thread(reached.wait, 5):
                seen["stop_ms"] = int(app.now() * 1000)
                stop.set()  # runner lain (mis. web) kena captcha
        finally:
            resume.set()

    out = run_android(tmp_path, sale_skew_ms=9500, during=during, runner_attrs={"stop_event": stop})
    assert "stop_ms" in seen
    assert out.result.status == RunStatus.ABORTED, out.result.message
    assert "runner lain" in out.result.message
    assert all(a < seen["stop_ms"] for a in polling_actions(out))
    assert out.result.step("result").t_server_ms - seen["stop_ms"] < 400
    _no_order(out)


def _slow_product_open(app, seconds: float) -> None:
    """Intent produk menjelang T menampilkan spinner dulu (cold start aplikasi di HiOS), lalu halaman produk."""
    orig = app.on_intent

    def on_intent(url: str, package: str) -> None:
        orig(url, package)
        if app.screen == "product" and app.now() > app.sc.open_at - 10:
            app.loading_until, app.after_loading, app.screen = app.now() + seconds, "product", "loading"

    app.on_intent = on_intent


@pytest.mark.parametrize("trigger", ["stop_event_preset", "abort_during_slow_arm"])
def test_stop_or_abort_before_t_returns_aborted_result(tmp_path, trigger):
    # precheck()/arm() tidak pernah melempar: ABORTED dikembalikan sebagai hasil (precheck / attempt)
    stop = threading.Event()

    async def during(runner, app):
        if trigger == "stop_event_preset":
            return
        _slow_product_open(app, 1.5)
        reached, resume = _pause_at(app, -3.5)  # arm sedang menunggu halaman produk (spinner)
        try:
            if await asyncio.to_thread(reached.wait, 5):
                await runner.abort("tes abort")
        finally:
            resume.set()

    if trigger == "stop_event_preset":
        stop.set()  # runner lain sudah berhenti (captcha) sebelum jalur Android mulai
    out = run_android(tmp_path, during=during, runner_attrs={"stop_event": stop})
    assert out.result.status == RunStatus.ABORTED, out.result.message
    assert ("runner lain" if trigger == "stop_event_preset" else "tes abort") in out.result.message
    assert polling_actions(out) == []
    assert out.kind("tap") == []
    names = out.step_names()
    assert "poll_start" not in names and "click_buy" not in names
    if trigger == "stop_event_preset":
        assert "arm" not in names, "precheck ABORTED -> run berhenti sebelum arm"
        # paling banyak intent produk precheck (baca saja); tanpa tap/back/reload
        assert [e["kind"] for e in out.app.events] in ([], ["intent"]), out.app.events
    _no_order(out)
    assert (out.log_dir / "android-result.json").exists()


@pytest.mark.parametrize("screen, status", [("captcha", RunStatus.CAPTCHA),
                                            ("verification", RunStatus.VERIFICATION)])
def test_challenge_at_arm_alarms_and_stops_all_immediately(tmp_path, screen, status):
    """Captcha/verifikasi saat arm (T-4 s): alarm + stop_event SAAT ITU (bukan menunggu T-lead), alarm tidak
    diulang oleh hasil akhir, tanpa aksi polling."""
    stop = _StampedEvent()

    def patch(runner, app) -> None:
        stop.app = app
        _screen_at_arm(screen)(runner, app)

    out = run_android(tmp_path, live=True, during=_during_patch(patch), runner_attrs={"stop_event": stop})
    assert stop.is_set() and stop.set_at_ms is not None
    t_arm = out.result.step("arm").t_server_ms
    t_lead = out.result.step("t_minus_lead").t_server_ms
    assert t_arm <= stop.set_at_ms < t_lead, "stop_event harus di-set saat arm, sebelum T-lead"
    assert stop.set_at_ms < _t(out) - 3000
    _alarmed(out, status)  # tepat sekali, walau hasil akhir juga status alarm
    assert polling_actions(out) == [] and out.kind("tap") == [] and out.kind("buy") == []
    assert "poll_start" not in out.step_names()
    assert out.app.screen == screen, "layar tantangan dibiarkan untuk diselesaikan manual"
    assert out.result.status != RunStatus.DRYRUN_OK
    _no_order(out)


@pytest.mark.parametrize("screen, status", [("captcha", RunStatus.CAPTCHA),
                                            ("verification", RunStatus.VERIFICATION),
                                            ("login", RunStatus.LOGIN_REQUIRED)])
def test_challenge_at_arm_is_returned_by_attempt(tmp_path, screen, status):
    # tanpa runner lain (stop_event None): hasil attempt() = status tantangan yang tersimpan saat arm
    out = run_android(tmp_path, live=True, during=_during_patch(_screen_at_arm(screen)))
    assert out.result.status == status, out.result.message
    assert out.result.message.startswith("saat membuka produk")
    _alarmed(out, status)
    assert polling_actions(out) == [] and out.kind("tap") == []
    _no_order(out)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: AndroidRunner._arm_sync men-set stop_event bersama (_signal_stop) saat captcha/verifikasi di arm, "
    "lalu _attempt memanggil _checkpoint() SEBELUM mengembalikan _arm_state -> runner menghentikan dirinya "
    "sendiri: hasil ABORTED 'dihentikan oleh runner lain' alih-alih CAPTCHA/VERIFICATION (result.json, "
    "ringkasan CLI, dan exit code salah melaporkan penyebab; alarm & stop sendiri sudah benar)"))
@pytest.mark.parametrize("screen, status", [("captcha", RunStatus.CAPTCHA),
                                            ("verification", RunStatus.VERIFICATION)])
def test_challenge_at_arm_with_shared_stop_event_reports_challenge_not_aborted(tmp_path, screen, status):
    out = run_android(tmp_path, live=True, during=_during_patch(_screen_at_arm(screen)),
                      runner_attrs={"stop_event": threading.Event()})
    assert out.result.status == status, f"{out.result.status}: {out.result.message}"
    assert "runner lain" not in out.result.message


@BOTH
def test_before_place_order_false_aborts_without_order(tmp_path, live):
    calls: list[int] = []

    def deny() -> bool:
        calls.append(1)
        return False

    out = run_android(tmp_path, live=live, before=deny)
    assert out.result.status == RunStatus.ABORTED, out.result.message
    assert "before_place_order" in out.result.message
    assert calls == [1]
    names = out.step_names()
    assert "price_guard_ok" in names, "lock hanya diminta setelah semua pengaman lolos"
    assert "place_order_gate" not in names and "click_place_order" not in names
    assert not out.runner.order_clicked
    _no_order(out)
    assert out.events() == []


# ------------------------------------------------------------------ syarat live & pasca "Buat Pesanan"


def test_live_without_expected_name_is_error_without_buy(tmp_path):
    out = run_android(tmp_path, live=True, cfg={"expected_name": None})
    assert out.runner.cfg.expected_name is None
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert "expected_name" in out.result.message
    assert out.kind("buy") == [] and out.kind("tap") == []
    assert polling_actions(out) == []
    assert "click_buy" not in out.step_names() and "poll_start" not in out.step_names()
    _no_order(out)


def test_unknown_after_order_live_uses_mandatory_message(tmp_path):
    stop = threading.Event()
    out = run_android(tmp_path, live=True, unknown_after_order=True, runner_attrs={"stop_event": stop})
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.message == "Pesanan MUNGKIN sudah terbuat — cek status pesanan manual" == MAYBE_ORDERED_MSG
    assert out.result.detail, "penyebab asli wajib ada di detail"
    assert "layar tidak dikenali" in out.result.detail
    assert "aplikasi aktif com.shopee.id/" in out.result.detail  # diagnosa sekali saat eskalasi
    assert len(out.kind("order")) == 1, "tidak boleh pesan dua kali"
    i_order = out.app.events.index(out.kind("order")[0])
    assert [e for e in out.app.events[i_order + 1:] if e["kind"] in ("tap", "back", "intent", "refresh")] == []
    _alarmed(out, RunStatus.UNKNOWN_STATE)
    assert out.notifier.events[0]["message"] == MAYBE_ORDERED_MSG
    assert stop.is_set(), "UNKNOWN_STATE harus menghentikan semua runner"
    assert out.app.screen == "order_unknown"  # dibiarkan untuk dicek manual


# ------------------------------------------------------------------ aplikasi tidak pernah ditutup


@pytest.mark.parametrize("live, scenario, status", [
    (False, {}, RunStatus.DRYRUN_OK),
    (True, {}, RunStatus.ORDER_PLACED_AWAIT_PIN),
    (True, {"unknown_after_order": True}, RunStatus.UNKNOWN_STATE),
    (True, {"captcha_after_buy": True}, RunStatus.CAPTCHA),
    (False, {"captcha_after_buy": True}, RunStatus.CAPTCHA),
    (True, {"verification_after_buy": True}, RunStatus.VERIFICATION),
    (True, {"sold_out": True}, RunStatus.SOLD_OUT),
    (True, {"sale_skew_ms": 9500}, RunStatus.NOT_STARTED_TIMEOUT),
    (True, {"cfg": {"expected_name": None}}, RunStatus.ERROR),
    (True, {"variants": VARIANTS, "variants_on_page": True, "cfg": {"variant": TARGET_VARIANT,
                                                                    "expected_name": LIVE_NAME}},
     RunStatus.ORDER_PLACED_AWAIT_PIN),
], ids=["dry", "live", "unknown_after_order", "captcha_live", "captcha_dry", "verification", "sold_out",
        "not_started", "no_expected_name", "variant_on_page"])
def test_app_never_closed(tmp_path, live, scenario, status):
    out = run_android(tmp_path, live=live, **scenario)
    assert out.result.status == status, out.result.message
    calls = out.driver.calls
    ops = [op for op, _ in calls]
    assert set(ops) <= ALLOWED_OPS, set(ops) - ALLOWED_OPS
    bad = [t for op, t in calls if op == "shell" and FORBIDDEN_SHELL.search(t)]
    assert bad == [], f"perintah shell terlarang: {bad}"
    # setelah hasil: hanya diagnosa (aplikasi aktif, screenshot, dump) - tanpa back / force-stop / close
    assert ops[-3:] == ["current_app", "screenshot", "dump"], ops[-6:]
    t_result = out.result.step("result").t_server_ms
    assert all(e["t_server_ms"] < t_result for e in out.kind("back")), "press_back setelah hasil"
    if status in (RunStatus.CAPTCHA, RunStatus.VERIFICATION):
        # dibiarkan terbuka untuk diselesaikan manual; tanpa retry
        assert out.app.screen == {RunStatus.CAPTCHA: "captcha", RunStatus.VERIFICATION: "verification"}[status]
        assert len(out.kind("buy")) == 1
        _alarmed(out, status)
    if status == RunStatus.ORDER_PLACED_AWAIT_PIN:
        assert out.app.screen == "pin"
