"""E2E tahap 2.1: pengaman harga 3 lapis, deteksi captcha non-dialog, UNKNOWN_STATE, reload awal."""

from __future__ import annotations

import threading

import pytest

from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus
from tests.conftest import assert_polling_rules, run_web

BOTH = pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])


def _no_order(out):
    assert out.kind("order") == [], "tidak boleh ada request buat pesanan"


def _alarmed(out, status):
    assert [e["event"] for e in out.notifier.events] == [str(status)]


# ------------------------------------------------------------------ lapis 1: halaman produk


def test_normal_price_before_open_waits_for_flash_price(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="normal_price_before_open"))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    assert names.index("price_not_yet") < names.index("click_buy")
    buys = out.kind("buy")
    assert len(buys) == 1 and buys[0]["t_server_ms"] >= out.open_at * 1000
    assert "Rp1.499.000" in out.result.step("price_not_yet").detail
    assert "Rp99.000" in out.result.step("buy_ready").detail
    assert "total=Rp111.000" in out.result.step("price_guard_ok").detail
    assert_polling_rules(out)
    _no_order(out)


@BOTH
def test_flash_sold_out_normal_price_is_price_guard(mock, admin, tmp_path, run, live):
    out = run(run_web(mock, admin, tmp_path, scenario="flash_sold_out_normal_price", live=live))
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    assert "Rp1.499.000" in out.result.message
    assert out.kind("buy") == [], "tidak boleh klik Beli dengan harga normal"
    _no_order(out)
    _alarmed(out, RunStatus.PRICE_GUARD)
    assert out.step_names().count("reload") >= 2  # T+0,5 s lalu tiap 2 s
    assert_polling_rules(out)


def test_static_ui_success_via_early_reload(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="static_ui"))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    t = out.open_at * 1000
    reload = out.result.step("reload")
    assert t + 500 <= reload.t_server_ms <= t + 900, reload.t_server_ms - t
    assert out.result.step("click_buy").t_server_ms > reload.t_server_ms
    assert len(out.kind("buy")) == 1
    assert_polling_rules(out)
    _no_order(out)


def test_variant_reselected_after_reload(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="variant_required", variant="128GB Hitam",
                      live_update=False))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    names = out.step_names()
    i_reload = names.index("reload")
    assert names.index("variant_selected") < i_reload  # dipilih saat arm
    assert "variant_selected" in names[i_reload:names.index("click_buy")]  # dipilih ULANG setelah reload
    # pilih ulang variasi = aksi polling (lewat RateLimiter): >= 425 ms setelah reload, klik Beli >= 425 ms lagi
    steps = [s for s in out.result.steps if s.name in ("reload", "variant_selected", "click_buy")]
    after = steps[[s.name for s in steps].index("reload"):]
    assert [s.name for s in after[:3]] == ["reload", "variant_selected", "click_buy"], after
    assert after[1].t_server_ms - after[0].t_server_ms >= 420, after
    slots = out.runner.limiter.history  # reload, pilih variasi, klik Beli: masing-masing satu slot
    assert len(slots) >= 3 and all(b - a >= 0.425 - 1e-6 for a, b in zip(slots, slots[1:], strict=False)), slots
    assert [b["body"]["variant"] for b in out.kind("buy")] == ["128GB Hitam"]
    assert_polling_rules(out)
    _no_order(out)


def test_variant_price_over_limit_blocks(mock, admin, tmp_path, run):
    # 256GB Biru harga flash Rp129.000 > maks Rp100.000
    out = run(run_web(mock, admin, tmp_path, scenario="variant_required", variant="256GB Biru"))
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    assert out.kind("buy") == []
    _no_order(out)


# ------------------------------------------------------------------ lapis 2: keranjang


def test_cart_other_item_unchecked_then_success(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="cart_other_checked", live=True))
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    assert "Kabel Data" in out.result.step("cart_uncheck").detail
    checkout = out.kind("checkout")
    assert len(checkout) == 1 and checkout[0]["query"]["items"] == "t"
    assert len(out.kind("order")) == 1


@BOTH
def test_cart_uncheck_fails_is_price_guard(mock, admin, tmp_path, run, live):
    out = run(run_web(mock, admin, tmp_path, scenario="cart_other_checked", cart_uncheck_fails=True,
                      live=live))
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    assert "keranjang" in out.result.message and "2 item tercentang" in out.result.message
    assert out.kind("checkout") == []
    _no_order(out)
    _alarmed(out, RunStatus.PRICE_GUARD)


# ------------------------------------------------------------------ lapis 3: checkout


@BOTH
@pytest.mark.parametrize("overrides, cfg, reason", [
    ({"checkout_qty": 2}, {}, "kuantitas 2"),
    ({"checkout_name": "Ponsel Palsu KW"}, {"expected_name": "Uji Coba"}, "nama produk"),
    ({"price_format_broken": True}, {}, "harga satuan tidak terbaca"),
    ({"service_fee": 15_000}, {}, "total Rp126.000 > maks"),
], ids=["qty2", "nama", "format", "total"])
def test_checkout_guard_blocks(mock, admin, tmp_path, run, live, overrides, cfg, reason):
    out = run(run_web(mock, admin, tmp_path, live=live, cfg=cfg, **overrides))
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    assert reason in out.result.message
    assert "place_order_gate" not in out.step_names()
    assert out.result.screenshots and out.result.screenshots[0].exists()
    _no_order(out)
    _alarmed(out, RunStatus.PRICE_GUARD)


def test_checkout_name_match_passes(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, cfg={"expected_name": "uji COBA"}))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert "nama cocok=True" in out.result.step("price_guard_ok").detail


def test_delayed_shipping_uses_final_total(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, shipping_delay_ms=500))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    guard = out.result.step("price_guard_ok")
    assert "ongkir=Rp12.000" in guard.detail and "total=Rp111.000" in guard.detail
    assert guard.t_server_ms - out.result.step("checkout_loaded").t_server_ms >= 450


@BOTH
def test_delayed_shipping_final_total_over_limit(mock, admin, tmp_path, run, live):
    # total awal Rp99.000 (tanpa ongkir) lolos batas; total final Rp111.000 tidak.
    out = run(run_web(mock, admin, tmp_path, shipping_delay_ms=500, live=live, cfg={"max_total": 105_000}))
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    assert "total Rp111.000 > maks Rp105.000" in out.result.message
    _no_order(out)


def test_shipping_never_loads_times_out(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, shipping_delay_ms=5000))
    assert out.result.status == RunStatus.PRICE_GUARD, out.result.message
    assert "tidak stabil/terbaca" in out.result.message
    _no_order(out)


# ------------------------------------------------------------------ captcha / verifikasi / unknown


@BOTH
def test_full_page_captcha_redirect(mock, admin, tmp_path, run, live):
    out = run(run_web(mock, admin, tmp_path, scenario="captcha_redirect", live=live))
    assert out.result.status == RunStatus.CAPTCHA, out.result.message
    assert "/verify/captcha" in out.result.message
    assert len(out.kind("buy")) == 1
    _no_order(out)
    _alarmed(out, RunStatus.CAPTCHA)


@BOTH
def test_captcha_iframe_overlay(mock, admin, tmp_path, run, live):
    out = run(run_web(mock, admin, tmp_path, scenario="captcha_iframe", live=live))
    assert out.result.status == RunStatus.CAPTCHA, out.result.message
    assert "iframe" in out.result.message and "captcha" in out.result.message
    assert len(out.kind("buy")) == 1
    _no_order(out)


def test_verification_redirect_on_reload(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="static_ui", verification_on_reload=True, live=True))
    assert out.result.status == RunStatus.VERIFICATION, out.result.message
    assert "reload" in out.step_names()
    assert out.kind("verify") and out.kind("buy") == []
    _no_order(out)
    _alarmed(out, RunStatus.VERIFICATION)
    assert_polling_rules(out)


@BOTH
def test_unknown_page_becomes_unknown_state(mock, admin, tmp_path, run, live):
    out = run(run_web(mock, admin, tmp_path, scenario="unknown_page", live=live))
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    elapsed = out.result.step("result").t_server_ms - out.result.step("click_buy").t_server_ms
    assert 1500 <= elapsed <= 2600, elapsed
    _no_order(out)
    _alarmed(out, RunStatus.UNKNOWN_STATE)
    log = (tmp_path / "logs" / "web.log").read_text()
    assert "/promo/kejutan" in log and "judul='Promo | Mock Shop'" in log


@BOTH
def test_pin_after_buy_is_maybe_ordered_and_stops_other_lanes_now(mock, admin, tmp_path, run, live):
    """Layar PIN langsung setelah klik Beli (tanpa klik "Buat Pesanan" dari alat): pesanan MUNGKIN terbuat ->
    UNKNOWN_STATE + pesan wajib, stop global saat itu juga (jalur lain tidak lewat gate), alarm."""
    stop = threading.Event()
    out = run(run_web(mock, admin, tmp_path, scenario="pin_after_buy", live=live,
                      runner_attrs={"stop_event": stop}))
    assert out.result.status == RunStatus.UNKNOWN_STATE, out.result.message
    assert out.result.message == MAYBE_ORDERED_MSG and "layar PIN muncul setelah klik Beli" in out.result.detail
    assert stop.is_set() and out.runner.order_clicked
    _no_order(out)
    _alarmed(out, RunStatus.UNKNOWN_STATE)


def test_unknown_limit_is_scalable(mock, admin, tmp_path, run):
    out = run(run_web(mock, admin, tmp_path, scenario="unknown_page", runner_attrs={"unknown_limit_s": 0.3}))
    assert out.result.status == RunStatus.UNKNOWN_STATE
    elapsed = out.result.step("result").t_server_ms - out.result.step("click_buy").t_server_ms
    # batas 0,3 s berlaku: lebih cepat dari batas default (yang tidak mungkin < 1,5 s); longgar terhadap beban mesin
    assert elapsed < 1500, elapsed


def test_description_with_ambiguous_words_does_not_trigger(mock, admin, tmp_path, run):
    desc = ("Produk original, bukan tiruan. Tanpa captcha saat aktivasi. Cek kode verifikasi IMEI di situs "
            "resmi. Jika stok habis pesanan dibatalkan. Verifikasi akun tidak diperlukan. ") * 20
    out = run(run_web(mock, admin, tmp_path, description=desc))
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    _no_order(out)
