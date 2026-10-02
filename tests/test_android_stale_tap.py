"""Mode ketuk Android (android.tap_mode) & ketukan basi.

Di aplikasi Shopee "Beli Sekarang" (halaman produk) dan "Buat Pesanan" (checkout) sama-sama di pojok kanan
bawah. Klik Beli yang diterima server terlambat bisa memunculkan checkout TEPAT di antara bacaan tombol dan
ketukannya:
- mode selector (default): elemen dicari & diketuk di device dalam satu RPC lewat selector Beli -> di checkout
  selector itu tidak cocok apa pun -> 0 ketukan (dry-run maupun live);
- mode coord: tap koordinat hasil bacaan -> mendarat di "Buat Pesanan" (celah sisa, terdokumentasi di README
  "Risiko"; dipersempit menjadi satu RPC baca oleh cek ulang sebelum tap ulang).
Elemen yang hilang saat diketuk (mode selector) -> tidak ada ketukan sama sekali. Selector Beli (default &
kalibrasi) dibuktikan tidak mungkin cocok dengan "Buat Pesanan"."""

from __future__ import annotations

import asyncio

import pytest

import tests.android_harness as harness
from flashbuy import android_selectors
from flashbuy.android_driver import FakeDriver, Node, Sel, node_matches
from flashbuy.android_selectors import PLACE_ORDER_PROBE_TEXTS, place_order_overlap
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunStatus
from tests.fake_android import FakeShopeeApp

LATENCY_S = 0.06  # RPC lambat: jendela antar-RPC lebar
LATE_ACCEPT_MS = [2270, 2300, 2340, 2380, 2410]  # sebelum perbaikan tahap 4: semua memicu klik "Buat Pesanan"
TAPS = ("click", "click_sel")


def _late_accept_app(delay_s: float):
    class LateAcceptApp(FakeShopeeApp):
        """Klik Beli aktif diterima; checkout baru tampil `delay_s` kemudian (server lambat, tanpa spinner)."""

        pending_at: float | None = None

        def _tap_buy(self, node):
            if not node.enabled:
                return super()._tap_buy(node)
            self._event("buy")
            if self.pending_at is None and self.screen == "product":
                self.pending_at = self.now() + delay_s

        def _render(self):
            if self.pending_at is not None and self.pending_at >= 0 and self.screen == "product" \
                    and self.now() >= self.pending_at:
                self.pending_at = -1.0
                self._confirm()  # tanpa sheet -> langsung checkout
            return super()._render()

    return LateAcceptApp


def _between_read_and_tap(change: str):
    class BetweenReadAndTapDriver(FakeDriver):
        """Setelah klik Beli pertama, layar berubah TEPAT sebelum ketukan berikutnya, sesudah bacaan terakhir
        tombol Beli: "checkout" (klik sebelumnya diterima server terlambat) atau "vanish" (tombol hilang)."""

        armed = False
        changed_at: int | None = None

        def info(self, sel: Sel):
            node = super().info(sel)
            if node is not None and node.label == "Beli Sekarang" and self.app.kind("buy") and self.changed_at is None:
                self.armed = True
            return node

        def _rpc(self, op: str, target: object = "", latency: float | None = None) -> None:
            if self.armed and op in TAPS:
                self.armed = False
                self.changed_at = len(self.app.events)
                if change == "checkout":
                    self.app._confirm()
                else:
                    self.app.loading_until = self.app.now() + 0.5
                    self.app.after_loading, self.app.screen = "product", "loading"
            return super()._rpc(op, target, latency)

    return BetweenReadAndTapDriver


def _run(tmp_path, monkeypatch, mode: str, *, live: bool = False, change: str = "checkout", before=None):
    monkeypatch.setattr(harness, "FakeShopeeApp", _late_accept_app(30.0))  # klik pertama tidak pernah "jadi"
    monkeypatch.setattr(harness, "FakeDriver", _between_read_and_tap(change))
    return harness.run_android(tmp_path, live=live, invariants=False, sheet=False, latency_s=0.02, before=before,
                               cfg={"android": {"tap_mode": mode}})


def _place_order_taps(out) -> list[dict]:
    return [e for e in out.app.events if e["kind"] == "tap" and e["detail"] == "place_order"]


# ------------------------------------------------------------------ layar berganti di antara baca & ketuk


@pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
def test_selector_mode_checkout_between_read_and_tap_never_taps_place_order(tmp_path, monkeypatch, live):
    """Mode selector: tombol Beli dibaca, checkout muncul, ketukan selector Beli tidak menemukan apa pun -> 0
    ketukan "Buat Pesanan". Live: lock dipegang jalur lain (hook False) -> satu-satunya jalan ke "Buat Pesanan"
    (gate) tertutup, jadi ketukan basi apa pun akan terlihat sebagai pesanan."""
    out = _run(tmp_path, monkeypatch, "selector", live=live, before=(lambda: False) if live else None)
    assert out.driver.changed_at is not None, "layar tidak pernah berganti di antara baca & ketuk"
    assert _place_order_taps(out) == [] and out.kind("order") == []
    assert ("Beli Sekarang" not in [label for _, label in out.driver.sel_clicks[1:2]]), out.driver.sel_clicks
    assert out.driver.sel_clicks[1][1] == "", "ketukan selector kedua tidak menemukan tombol Beli (tidak diketuk)"
    assert "buy_missed" in [s.name for s in out.result.steps]
    expected = RunStatus.ABORTED if live else RunStatus.DRYRUN_OK
    assert out.result.status == expected, out.result.message


@pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
def test_coord_mode_checkout_between_read_and_tap_is_the_documented_residual_gap(tmp_path, monkeypatch, live):
    """Mode coord: tap koordinat setelah bacaan terakhir mendarat di "Buat Pesanan" yang kini menempati posisi
    tombol Beli (tanpa gate, tanpa lapis 3). Celah ini = satu RPC baca; terdokumentasi (README "Risiko"). Bila
    terjadi, layar PIN terdeteksi -> UNKNOWN_STATE "Pesanan MUNGKIN sudah terbuat"; PIN tidak diketik alat."""
    out = _run(tmp_path, monkeypatch, "coord", live=live, before=(lambda: False) if live else None)
    assert out.driver.changed_at is not None
    assert len(_place_order_taps(out)) == 1 and len(out.kind("order")) == 1
    assert out.result.status == RunStatus.UNKNOWN_STATE and out.result.message == MAYBE_ORDERED_MSG


@pytest.mark.parametrize("mode", ["selector", "coord"])
def test_button_vanishes_between_read_and_tap(tmp_path, monkeypatch, mode):
    """Tombol Beli hilang (spinner) tepat sebelum ketukan. Selector: tidak ada ketukan sama sekali selama tombol
    hilang. Coord: tap mendarat di area kosong (celah yang sama)."""
    out = _run(tmp_path, monkeypatch, mode, change="vanish")
    at = out.driver.changed_at
    assert at is not None
    nothing = [e for e in out.app.events[at:] if e["kind"] == "tap" and e["detail"] == "-"]
    if mode == "selector":
        assert nothing == [], "ketukan selector tanpa elemen tidak boleh mengetuk apa pun"
        assert ("", ) == tuple(label for _, label in out.driver.sel_clicks[1:2])
    else:
        assert len(nothing) == 1
    assert _place_order_taps(out) == [] and out.kind("order") == []


@pytest.mark.parametrize("delay_ms", LATE_ACCEPT_MS)
@pytest.mark.parametrize("mode", ["selector", "coord"])
def test_late_accept_sweep_never_taps_place_order(tmp_path, monkeypatch, mode, delay_ms):
    """Checkout tampil terlambat di titik-titik yang dulu (sebelum tahap 4) memicu tap "Buat Pesanan" pada tap
    ulang: kedua mode aman (coord berkat cek ulang tepat sebelum tap ulang)."""
    monkeypatch.setattr(harness, "FakeShopeeApp", _late_accept_app(delay_ms / 1000))
    out = harness.run_android(tmp_path, live=False, invariants=False, sheet=False, latency_s=LATENCY_S,
                              cfg={"android": {"tap_mode": mode}})
    assert out.kind("order") == [], [e["detail"] for e in out.kind("tap")]
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message


def test_coord_retap_reads_button_as_last_rpc_before_tap(tmp_path, monkeypatch):
    """Mode coord, urutan RPC sebelum tap ulang: ... cek 'Buat Pesanan' tidak tampil -> baca tombol Beli -> tap."""
    monkeypatch.setattr(harness, "FakeShopeeApp", _late_accept_app(30.0))  # klik pertama tidak pernah "jadi"
    out = harness.run_android(tmp_path, live=False, invariants=False, sheet=False, latency_s=0.02,
                              cfg={"android": {"tap_mode": "coord"}})
    calls = out.driver.calls
    clicks = [i for i, (op, t) in enumerate(calls) if op == "click" and t == "Beli Sekarang"]
    assert len(clicks) >= 2, calls
    for i in clicks[1:]:
        before = calls[i - 3:i]
        assert any("Buat" in str(t) and "Pesanan" in str(t) for _, t in before[:-1]), before
        assert before[-1][0] in ("info", "info_any", "find_all", "exists") and "Beli" in str(before[-1][1]), before
    assert out.kind("order") == []


def test_selector_mode_taps_are_single_selector_rpcs(tmp_path):
    """Mode selector (default): SEMUA ketukan maju lewat klik selector di device (Beli, konfirmasi sheet, chip
    variasi, metode bayar, "Buat Pesanan"), tidak ada tap koordinat; tunggu implisit dimatikan saat precheck."""
    out = harness.run_android(tmp_path, live=True, variants=["128GB Hitam", "256GB Biru"],
                              cfg={"variant": "128GB Hitam"}, payment_default="Transfer Bank")
    assert out.result.status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result.message
    ops = [op for op, _ in out.driver.calls]
    assert "configure" in ops and ops.index("configure") < ops.index("click")
    tapped = [label for _, label in out.driver.sel_clicks if label]
    taps = [label for op, label in out.driver.calls if op == "click"]
    assert taps == tapped, "setiap ketukan = klik selector (tap koordinat tidak dipakai di mode selector)"
    for label in ("Beli Sekarang", "128GB Hitam", "Metode Pembayaran", "ShopeePay", "Buat Pesanan"):
        assert label in tapped, (label, tapped)


# ------------------------------------------------------------------ selector Beli spesifik


def test_default_buy_selectors_cannot_match_place_order():
    sels = android_selectors.defaults()
    place = sels.candidates("place_order", None)
    buy = sels.candidates("buy_button", None)
    assert buy, "kandidat Beli kosong"
    for c in buy:
        assert place_order_overlap(c, place) == "", c
        for t in PLACE_ORDER_PROBE_TEXTS:
            assert not node_matches(Node(text=t), c) and not node_matches(Node(desc=t), c), (c, t)
    # regex gabungan (textMatches) yang dipakai bacaan & klik selector juga bersih
    rx = android_selectors.union([r for c in buy if (r := android_selectors.text_regex(c)) is not None])
    assert place_order_overlap(Sel("textMatches", rx), place) == ""
    assert node_matches(Node(text="Beli Sekarang"), Sel("textMatches", rx))


@pytest.mark.parametrize(("cand", "why"), [
    ({"textContains": "Pesan"}, "cocok dengan 'Buat Pesanan'"),
    ({"textMatches": "(?i)(beli|buat).*"}, "cocok dengan 'Buat Pesanan'"),
    ({"descriptionContains": "Buat"}, "cocok dengan 'Buat Pesanan'"),
    ({"resourceId": "com.shopee.id:id/bottom_btn"}, "cocok dengan 'Buat Pesanan'"),
    ({"className": "android.widget.Button"}, "tidak spesifik"),
])
def test_unsafe_buy_selector_is_rejected_by_precheck_and_never_tapped(tmp_path, cand, why):
    """Kalibrasi yang bisa cocok dengan "Buat Pesanan" (mis. resource-id tombol bawah yang sama) -> precheck GAGAL
    (run tidak dimulai); klik selector dengan selector itu ditolak tanpa ketukan."""
    path = tmp_path / "selectors.json"
    steps = {"buy_button": [cand], "place_order": [{"resourceId": "com.shopee.id:id/bottom_btn"}]}
    android_selectors.save(path, steps, {"app_version": "3.40.21"})
    runner, app, driver, *_ = harness.make_android(tmp_path, selectors=android_selectors.load(path))
    problems = runner.tap_selector_problems()
    assert any(p.startswith("buy_button: ") and why in p for p in problems), problems

    async def precheck():
        await runner.prepare()
        try:
            return await runner.precheck()
        finally:
            await runner.close()

    pre = asyncio.run(precheck())
    item = next(i for i in pre.items if i.name == "mode ketuk")
    assert not pre.ok and item.ok is False and why in item.detail, item
    assert app.kind("tap") == [] and app.kind("order") == []
    with pytest.raises(Exception, match="tidak diketuk"):  # klik selector dengan selector itu ditolak
        runner._tap_selector("buy_button", Node(text="Beli Sekarang", via=(android_selectors.to_sel(cand), None)))


# ------------------------------------------------------------------ elemen berubah saat diklik di server


def _stale_on(label_part: str, times: int = 1):
    """Klik selector berikutnya yang selectornya memuat `label_part` gagal di server dengan StaleObjectException
    (elemen dibangun ulang di antara pencarian & klik, SEBELUM gesture): tidak ada ketukan."""
    from flashbuy.android_driver import SelectorStale

    class StaleDriver(FakeDriver):
        left = times

        def _rpc(self, op: str, target: object = "", latency: float | None = None) -> None:
            if op == "click_sel" and label_part in str(target) and self.left > 0:
                self.left -= 1
                self.fail_next.append(SelectorStale(f"click_sel {target}: elemen berubah saat diklik"))
            return super()._rpc(op, target, latency)

    return StaleDriver


def test_stale_element_on_buy_tap_is_not_tapped_and_reclassified(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "FakeDriver", _stale_on("Beli"))
    out = harness.run_android(tmp_path, live=False, sheet=False)
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert ("click_sel", "text='Beli Sekarang'") in out.driver.calls, "klik selector pertama gagal di server"
    assert len(out.kind("buy")) == 1 and out.kind("order") == []


def test_stale_element_on_place_order_tap_is_maybe_ordered(tmp_path, monkeypatch):
    """"Buat Pesanan" + error server: tetap dianggap mungkin terketuk (pesan wajib), bukan "tidak diketuk"."""
    monkeypatch.setattr(harness, "FakeDriver", _stale_on("Buat Pesanan"))
    out = harness.run_android(tmp_path, live=True, sheet=False, invariants=False)
    assert out.result.status == RunStatus.UNKNOWN_STATE and out.result.message == MAYBE_ORDERED_MSG
    assert out.runner.order_clicked


def test_agent_restart_reapplies_selector_click_setup_at_once(tmp_path):
    out = harness.run_android(tmp_path, driver_kw={"alive": False})
    ops = [op for op, _ in out.driver.calls]
    i = ops.index("restart_agent")
    assert "configure" in ops[i + 1:ops.index("click")], "tunggu implisit dimatikan lagi segera setelah restart"
