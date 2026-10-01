"""E2E jalur Android: AndroidRunner di atas FakeDriver + FakeShopeeApp (waktu virtual, FakeClock).

Semua skenario lewat tests/android_harness.run_android; tidak ada sleep nyata di hot path, jadi
tiap tes selesai dalam hitungan milidetik. Waktu di sini = waktu server (ms) relatif T (slot buka).
"""

from __future__ import annotations

import asyncio
import re
import threading

import pytest

from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus
from tests.android_harness import assert_polling_rules, polling_actions, run_android
from tests.conftest import LIVE_NAME

BOTH = pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
RELOAD = pytest.mark.parametrize("reload", ["swipe", "intent"])
VARIANTS = ["128GB Hitam", "256GB Biru"]
TARGET_VARIANT = "256GB Biru"
VARIANT_PRICES = {"128GB Hitam": 99_000, "256GB Biru": 95_000}  # variasi target lebih murah -> terlihat di checkout

# Operasi driver yang sah dipakai runner (tidak ada input teks: PIN tidak mungkin diketik alat).
ALLOWED_OPS = {"exists", "info", "find_all", "click", "current_app", "start_url", "swipe_refresh", "press_back",
               "webview", "screenshot", "dump", "shell", "agent_alive", "restart_agent"}
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


def test_variant_required_without_config_variant_direct_checkout_is_error(tmp_path):
    # tanpa bottom sheet: toast "Silakan pilih variasi" langsung di halaman produk
    out = run_android(tmp_path, variants=VARIANTS, variant_required=True, sheet=False)
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert "variasi" in out.result.message and "variant" in out.result.message
    assert len(out.kind("buy")) == 1, "tidak boleh retry"
    assert out.kind("checkout") == []
    _no_order(out)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_runner._after_buy/_classify_nodes - toast 'Silakan pilih variasi' di bottom sheet tidak "
    "pernah dikenali (SHEET diklasifikasi lebih dulu daripada VARIANT_REQUIRED); setelah 1,5 s runner "
    "menganggap 'sheet tidak bereaksi' dan mengklik ulang 'Beli' yang tertutup sheet sehingga tap jatuh ke "
    "tombol konfirmasi sheet, lalu _handle_sheet mengonfirmasi lagi ~60 ms kemudian (2 konfirmasi < 400 ms), "
    "berulang sampai T+8 s -> NOT_STARTED_TIMEOUT, bukan ERROR 'wajib pilih variasi'"))
def test_variant_required_without_config_variant_in_sheet_is_error(tmp_path):
    out = run_android(tmp_path, variants=VARIANTS, variant_required=True)
    assert out.result.status == RunStatus.ERROR, out.result.message
    assert "variasi" in out.result.message
    assert len(out.kind("confirm")) == 1, "konfirmasi sheet tidak boleh diulang"
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


@pytest.mark.xfail(strict=True, reason=(
    "BUG: android_runner._after_buy - `elapsed` dihitung sejak klik Beli dan tidak direset setelah LOADING; "
    "saat spinner hilang di antara snapshot teks dan cek ProgressBar, satu frame UNKNOWN sesaat langsung "
    "dianggap 'tidak ada reaksi' (elapsed > 1,5 s) -> klik ulang 'Beli' (no_response + click_buy #2), dan tap "
    "itu jatuh ke tombol konfirmasi bottom sheet sehingga _handle_sheet dilewati"))
def test_loading_spinner_after_buy_does_not_reclick(tmp_path):
    out = run_android(tmp_path, loading_after_buy_ms=2500)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    assert "no_response" not in names
    assert names.count("click_buy") == 1
    assert "click_sheet_confirm" in names


@pytest.mark.xfail(strict=True, reason=(
    "BUG (pesanan variasi salah): sama dengan klik ulang pasca-loading di _after_buy; tap ulang 'Beli' jatuh "
    "ke tombol konfirmasi sheet sebelum variasi dipilih, lapis 3 tidak memeriksa variasi -> live membuat "
    "pesanan TANPA variasi yang dikonfigurasi"))
def test_loading_spinner_after_buy_live_keeps_configured_variant(tmp_path):
    out = run_android(tmp_path, live=True, loading_after_buy_ms=2500, variants=VARIANTS,
                      variant_prices=VARIANT_PRICES, cfg={"variant": TARGET_VARIANT})
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    assert len(out.kind("order")) == 1
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


@pytest.mark.xfail(strict=True, reason=(
    "BUG: PollingGate/RateLimiter memesan slot di AWAL reload, padahal swipe (~150 ms) / intent `am start -W` "
    "(~300 ms) baru sampai ke aplikasi di akhir aksi; klik Beli berikutnya dijadwalkan slot+425 ms sehingga "
    "aplikasi/server melihat jarak reload->Beli ~296 ms (swipe) / ~146 ms (intent) < 400 ms"))
@RELOAD
def test_static_ui_reload_then_buy_respects_polling_rules(tmp_path, reload):
    out = run_android(tmp_path, live_update=False, cfg={"android": {"reload": reload}})
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert_polling_rules(out)


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


@pytest.mark.xfail(strict=True, reason=(
    "BUG: AndroidRunner.precheck()/arm() memanggil _wait_screen -> _checkpoint, sehingga stop_event yang sudah "
    "di-set / abort() sebelum T melempar exception privat `_Aborted` keluar dari API Runner (run_single crash, "
    "tanpa RunResult ABORTED, tanpa result.json/diagnosa); web_runner tidak begitu (ABORTED di attempt)"))
@pytest.mark.parametrize("trigger", ["stop_event_preset", "abort_during_slow_arm"])
def test_stop_or_abort_before_t_returns_aborted_result(tmp_path, trigger):
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
    assert polling_actions(out) == []
    assert out.kind("tap") == []
    _no_order(out)
    assert (out.log_dir / "android-result.json").exists()


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
