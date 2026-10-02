"""Regresi review tahap 4: tap ulang memakai koordinat basi tidak boleh mendarat di "Buat Pesanan".

Skenario: klik Beli pertama diterima server TERLAMBAT (tanpa spinner), jadi setelah 1,5 s "tidak ada reaksi"
runner menyiapkan klik ulang; checkout muncul di antara bacaan tombol dan tap. Tombol "Buat Pesanan" di
checkout berada di koordinat yang sama dengan "Beli Sekarang" (seperti tata letak Shopee). Sebelum perbaikan,
beberapa RPC (cek sheet, penanda bahaya, harga, clear_toast, cek dialog) berada di antara bacaan dan tap,
sehingga tap ulang di dry-run mengenai "Buat Pesanan" (tanpa gate, tanpa lapis 3). Sekarang tombol dibaca
ulang tepat sebelum tap, bersama cek bahwa "Buat Pesanan" tidak tampil. Sisa jendela = satu RPC baca
(batas tap koordinat; lihat README "Risiko")."""

from __future__ import annotations

import pytest

import tests.android_harness as harness
from flashbuy.runner_base import RunStatus
from tests.fake_android import FakeShopeeApp

LATENCY_S = 0.06  # RPC lambat: jendela antar-RPC lebar
# server menerima klik pertama setelah X ms; sebelum perbaikan semua nilai ini memicu klik "Buat Pesanan"
LATE_ACCEPT_MS = [2270, 2300, 2340, 2380, 2410]


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


@pytest.mark.parametrize("delay_ms", LATE_ACCEPT_MS)
def test_dry_run_retap_never_lands_on_place_order(tmp_path, monkeypatch, delay_ms):
    monkeypatch.setattr(harness, "FakeShopeeApp", _late_accept_app(delay_ms / 1000))
    out = harness.run_android(tmp_path, live=False, invariants=False, sheet=False, latency_s=LATENCY_S)
    assert out.kind("order") == [], [e["detail"] for e in out.kind("tap")]
    assert out.result.status == RunStatus.DRYRUN_OK, out.result.message
    assert "buy_recheck" in [s.name for s in out.result.steps], "tap ulang ditahan karena layar berubah"


def test_live_retap_with_lock_held_elsewhere_never_orders(tmp_path, monkeypatch):
    """Jalur lain memegang lock (hook False): tidak ada pesanan dari tap basi; hook dipanggil & menolak."""
    calls: list[int] = []

    def lost() -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(harness, "FakeShopeeApp", _late_accept_app(2.3))
    out = harness.run_android(tmp_path, live=True, invariants=False, sheet=False, latency_s=LATENCY_S,
                              before=lost)
    assert out.kind("order") == []
    assert out.result.status == RunStatus.ABORTED and calls == [1], out.result.message


def test_retap_reads_button_as_last_rpc_before_tap(tmp_path, monkeypatch):
    """Urutan RPC sebelum tap ulang: ... cek 'Buat Pesanan' tidak tampil -> baca tombol Beli -> tap."""
    monkeypatch.setattr(harness, "FakeShopeeApp", _late_accept_app(30.0))  # klik pertama tidak pernah "jadi"
    out = harness.run_android(tmp_path, live=False, invariants=False, sheet=False, latency_s=0.02)
    calls = out.driver.calls
    clicks = [i for i, (op, t) in enumerate(calls) if op == "click" and t == "Beli Sekarang"]
    assert len(clicks) >= 2, calls
    for i in clicks[1:]:
        before = calls[i - 3:i]
        assert any("Buat" in str(t) and "Pesanan" in str(t) for _, t in before[:-1]), before
        assert before[-1][0] in ("info", "info_any", "find_all", "exists") and "Beli" in str(before[-1][1]), before
    assert out.kind("order") == []
