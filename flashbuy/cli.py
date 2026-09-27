"""Entry point: python -m flashbuy <perintah>."""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from flashbuy import timesync
from flashbuy.config import DEFAULT_LEAD_MS

console = Console()


def _ms(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.1f} ms" if signed else f"{v:.1f} ms"


def cmd_timesync(args: argparse.Namespace) -> int:
    console.print(f"Mengukur offset ({args.samples} sampel/sumber)...")
    report = timesync.sync(samples=args.samples, ntp_host=args.ntp_host, http_url=args.url)

    table = Table(title="Offset jam (server - lokal)")
    for col in ("Sumber", "Offset", "±", "RTT", "Sampel", "Metode", "Status"):
        table.add_column(col)
    primary = report.primary
    for r in (report.shopee, report.ntp):
        status = f"[red]GAGAL: {r.error}[/]" if not r.ok else "[green]OK[/]"
        if r is primary:
            status += " [bold](acuan)[/]"
        table.add_row(r.source, _ms(r.offset_ms, signed=True), _ms(r.uncertainty_ms),
                      _ms(r.rtt_ms), str(r.samples), r.method, status)
    console.print(table)
    for r in (report.shopee, report.ntp):
        for w in r.warnings:
            console.print(f"[yellow]! {r.source}: {w}[/]")
    if (d := report.disagreement_ms) is not None:
        console.print(f"Selisih Shopee vs NTP: {d:+.1f} ms")
    if primary is None:
        console.print("[red bold]Tidak ada sumber waktu yang berhasil.[/]")
        return 1
    clock = timesync.server_clock_from(report)
    console.print(f"Waktu server terkoreksi sekarang: [bold]{clock.now_iso()}[/]")
    return 0


def _not_yet(args: argparse.Namespace) -> int:
    console.print(f"[yellow]Perintah '{args.command}' belum diimplementasi (tahap berikutnya).[/]")
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flashbuy", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    ts = sub.add_parser("timesync", help="ukur offset jam lokal vs NTP & header Date Shopee")
    ts.add_argument("--samples", type=int, default=timesync.DEFAULT_SAMPLES)
    ts.add_argument("--ntp-host", default=timesync.DEFAULT_NTP_HOST)
    ts.add_argument("--url", default=timesync.DEFAULT_HTTP_URL)
    ts.set_defaults(func=cmd_timesync)

    pc = sub.add_parser("precheck", help="cek sesi login, alamat, ShopeePay")
    pc.add_argument("--config", required=True)
    pc.set_defaults(func=_not_yet)

    cal = sub.add_parser("calibrate", help="rekam selector di produk biasa")
    cal.add_argument("--config", required=True)
    cal.add_argument("--platform", choices=("web", "android"), required=True)
    cal.set_defaults(func=_not_yet)

    run = sub.add_parser("run", help="jalankan checkout (default DRY-RUN)")
    run.add_argument("--config", required=True)
    run.add_argument("--live", action="store_true", help="checkout sungguhan (klik Buat Pesanan)")
    run.add_argument("--lead-ms", type=int, default=None,
                     help=f"mulai cek tombol N ms sebelum T (default config / {DEFAULT_LEAD_MS})")
    run.add_argument("--only", choices=("web", "android"))
    run.set_defaults(func=_not_yet)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("[red]Dibatalkan.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
