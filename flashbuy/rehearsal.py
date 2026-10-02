"""Mode rehearsal (`flashbuy rehearse`): uji jalur dari produk target ASLI sampai halaman checkout, beberapa hari
sebelum event, saat harga masih normal.

Alur sekali jalan per platform: buka produk -> pilih variasi -> klik Beli (lapis 1 harga TIDAK ditegakkan, harga
tetap dibaca) -> (keranjang di web) -> checkout: pilih ShopeePay bila belum, baca harga/qty/nama/ongkir/total ->
STOP. Lapis 2 & 3 tetap DIEVALUASI dan dilaporkan ("akan lolos" / "akan gagal"), tetapi tidak menghentikan
rehearsal dan tidak memengaruhi exit code (saat harga normal lapis harga memang akan gagal).

Pengaman berlapis: fungsi klik/ketuk "Buat Pesanan" kedua runner melempar `PlaceOrderForbidden` di mode
rehearsal (juga di dry-run), dan `before_place_order()` diganti fungsi yang juga melempar (tidak pernah dipanggil).
CAPTCHA / VERIFICATION / UNKNOWN_STATE / layar PIN tak terduga -> stop + alarm, sama seperti run biasa.

Laporan per platform: tabel di terminal + `logs/<run_id>/<platform>-rehearsal.json`. Exit code: 0 bila semua
langkah OK; 1 langkah gagal; 4 captcha/verifikasi/login; 5 UNKNOWN_STATE (mis. PIN muncul setelah Beli).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

STOP_STATUSES = ("CAPTCHA", "VERIFICATION", "LOGIN_REQUIRED", "UNKNOWN_STATE")


class PlaceOrderForbidden(RuntimeError):
    """Klik/ketuk "Buat Pesanan" di mode rehearsal atau dry-run: dilarang di level fungsi klik."""


def forbidden_gate() -> bool:
    """`before_place_order` pengganti di mode rehearsal: tidak boleh pernah dipanggil."""
    raise PlaceOrderForbidden("before_place_order() dipanggil di mode rehearsal")


@dataclass
class RehearsalStep:
    name: str
    ok: bool
    selector: str = ""  # kandidat selector yang cocok
    latency_ms: float | None = None
    values: dict = field(default_factory=dict)  # nilai yang terbaca
    screenshot: str = ""
    detail: str = ""


@dataclass
class RehearsalReport:
    platform: str
    steps: list[RehearsalStep] = field(default_factory=list)
    # evaluasi pengaman harga (tidak ditegakkan): nama lapis -> "akan lolos: ..." / "akan gagal: ..."
    guards: dict[str, str] = field(default_factory=dict)
    cart_items: list[str] | None = None  # isi keranjang di akhir (None = tidak dibaca/terbaca)
    status: str = "OK"  # OK | GAGAL | CAPTCHA | VERIFICATION | LOGIN_REQUIRED | UNKNOWN_STATE | ABORTED | ERROR
    message: str = ""
    json_path: str = ""

    def add(self, step: RehearsalStep) -> RehearsalStep:
        self.steps.append(step)
        if not step.ok and self.status == "OK":
            self.status = "GAGAL"
            self.message = self.message or f"langkah gagal: {step.name}"
        return step

    @property
    def ok(self) -> bool:
        return self.status == "OK" and all(s.ok for s in self.steps)

    @property
    def clicked_buy(self) -> bool:
        return any(s.name == "klik Beli" for s in self.steps)

    @property
    def failed(self) -> list[str]:
        return [s.name for s in self.steps if not s.ok]

    def stop(self, status: str, message: str) -> None:
        self.status, self.message = status, message

    def exit_code(self) -> int:
        if self.ok:
            return 0
        if self.status in ("CAPTCHA", "VERIFICATION", "LOGIN_REQUIRED"):
            return 4
        if self.status == "UNKNOWN_STATE":
            return 5
        return 1

    def write(self, run_dir: Path) -> Path:
        path = Path(run_dir) / f"{self.platform}-rehearsal.json"
        data = asdict(self)
        data["ok"], data["exit_code"], data["failed_steps"] = self.ok, self.exit_code(), self.failed
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        self.json_path = str(path)
        return path


def guard_text(ok: bool, detail: str) -> str:
    return f"{'akan lolos' if ok else 'akan gagal'}: {detail}"


def cart_line(items: list[str] | None) -> str:
    if items is None:
        return "keranjang berisi: (tidak dibaca) - cek manual bila Beli sempat diklik"
    if not items:
        return "keranjang berisi: (kosong)"
    return "keranjang berisi: " + "; ".join(items) + " - hapus manual (alat tidak menghapusnya)"


def combined_exit(reports: list[RehearsalReport]) -> int:
    """Exit gabungan: 0 bila semua platform OK; selain itu yang paling serius (5 > 4 > 1)."""
    codes = [r.exit_code() for r in reports]
    for code in (5, 4, 1):
        if code in codes:
            return code
    return 0
