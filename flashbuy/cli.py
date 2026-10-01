"""Entry point: python -m flashbuy <perintah>."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.console import Console
from rich.table import Table

from flashbuy import selector_store, timesync
from flashbuy.android_driver import DriverError
from flashbuy.config import (
    DEFAULT_LEAD_MS,
    POLL_WINDOW_AFTER_S,
    ConfigError,
    TargetConfig,
    load_config,
    require_live_ready,
)

console = Console()

OK_STATUSES = ("DRYRUN_OK", "ORDER_PLACED_AWAIT_PIN")


def _ms(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.1f} ms" if signed else f"{v:.1f} ms"


def print_sync_report(report: timesync.SyncReport) -> None:
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


def cmd_timesync(args: argparse.Namespace) -> int:
    console.print(f"Mengukur offset ({args.samples} sampel/sumber)...")
    report = timesync.sync(samples=args.samples, ntp_host=args.ntp_host, http_url=args.url)
    print_sync_report(report)
    if report.primary is None:
        console.print("[red bold]Tidak ada sumber waktu yang berhasil.[/]")
        return 1
    clock = timesync.server_clock_from(report)
    console.print(f"Waktu server terkoreksi sekarang: [bold]{clock.now_iso()}[/]")
    return 0


# --------------------------------------------------------------------------- util


def _load(args: argparse.Namespace) -> TargetConfig:
    return load_config(args.config, allow_local=args.allow_local)


def _platform(args: argparse.Namespace, attr: str) -> str | None:
    """'web' | 'android'; None (tanpa --only/--platform) = orchestrator, belum tersedia (tahap 4)."""
    value = getattr(args, attr)
    if value not in ("web", "android"):
        console.print(f"[yellow]Pilih satu jalur dengan --{attr} web|android. Menjalankan keduanya sekaligus "
                      "(orchestrator) menyusul di tahap 4.[/]")
        return None
    return value


def _android_driver(cfg: TargetConfig):
    """Driver device nyata (diganti FakeDriver di tes)."""
    from flashbuy.android_driver import U2Driver

    return U2Driver(cfg.android.serial)


def _android_selectors(path: str):
    from flashbuy import android_selectors

    return android_selectors.load(path)


def _print_precheck(pre, platform: str) -> None:
    table = Table(title=f"Pre-check {platform}")
    for col in ("Cek", "Status", "Detail"):
        table.add_column(col)
    for item in pre.items:
        status = {True: "[green]OK[/]", False: "[red]GAGAL[/]", None: "[yellow]PERINGATAN[/]"}[item.ok]
        table.add_row(item.name, status, item.detail)
    console.print(table)


# --------------------------------------------------------------------------- login


async def login_web(cfg: TargetConfig, sel: selector_store.SelectorSet, *, headless: bool = False,
                    on_open: Callable[[object], Awaitable[None]] | None = None) -> None:
    from playwright.async_api import async_playwright

    from flashbuy.web_runner import launch_context

    async with async_playwright() as pw:
        ctx = await launch_context(pw, cfg.web, headless=headless)
        closed = asyncio.Event()
        ctx.on("close", lambda _ctx: closed.set())
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(cfg.origin + sel.urls["login_page"], wait_until="domcontentloaded")
        console.print(f"Profil: {cfg.web.profile_dir}\n[bold]Login manual di jendela browser "
                      "(termasuk OTP bila diminta), lalu TUTUP browser untuk menyimpan sesi.[/]")
        if on_open is not None:
            await on_open(ctx)
        await closed.wait()
    console.print("Browser ditutup; sesi tersimpan di profil.")


def login_android(cfg: TargetConfig, driver) -> None:
    """Buka aplikasi Shopee di HP; login dilakukan manual oleh pengguna (alat tidak mengetik apa pun)."""
    driver.start_url(cfg.origin + "/", cfg.android.package)
    console.print(f"Device {driver.serial or '(pertama)'}: aplikasi {cfg.android.package} dibuka.\n"
                  "[bold]Login manual di HP (termasuk OTP bila diminta) dan pastikan ShopeePay aktif. "
                  "Sesi aplikasi tersimpan di HP; tidak ada yang disimpan alat.[/]")


def cmd_login(args: argparse.Namespace) -> int:
    platform = _platform(args, "platform")
    if platform is None:
        return 2
    cfg = _load(args)
    if platform == "android":
        login_android(cfg, _android_driver(cfg))
        return 0
    asyncio.run(login_web(cfg, selector_store.load(args.selectors), headless=args.headless))
    return 0


# --------------------------------------------------------------------------- precheck


async def precheck_web(cfg: TargetConfig, sel: selector_store.SelectorSet, *, headless: bool):
    from flashbuy.notifier import Notifier
    from flashbuy.runner_base import RunLog
    from flashbuy.web_runner import WebRunner

    log = RunLog(Path("logs") / f"{time.strftime('%Y%m%d-%H%M%S')}-precheck", timesync.ServerClock(),
                 "web", Console(stderr=True, quiet=True))
    runner = WebRunner(cfg, sel, log=log, notifier=Notifier(cfg.notify_webhook), headless=headless)
    try:
        await runner.prepare()
        return await runner.precheck()
    finally:
        await runner.close()
        log.close()


async def precheck_android(cfg: TargetConfig, sel, driver, log_dir: Path):
    from flashbuy.android_runner import AndroidRunner
    from flashbuy.notifier import Notifier
    from flashbuy.runner_base import RunLog

    log = RunLog(log_dir, timesync.ServerClock(), "android", Console(stderr=True, quiet=True))
    runner = AndroidRunner(cfg, sel, log=log, notifier=Notifier(cfg.notify_webhook), driver=driver)
    try:
        await runner.prepare()
        return await runner.precheck()
    finally:
        await runner.close()
        log.close()


def cmd_precheck(args: argparse.Namespace) -> int:
    platform = _platform(args, "only")
    if platform is None:
        return 2
    cfg = _load(args)
    if platform == "android":
        log_dir = Path("logs") / f"{time.strftime('%Y%m%d-%H%M%S')}-precheck"
        pre = asyncio.run(precheck_android(cfg, _android_selectors(args.selectors), _android_driver(cfg), log_dir))
        console.print(f"Log (info device & latensi query): {log_dir}")
    else:
        pre = asyncio.run(precheck_web(cfg, selector_store.load(args.selectors), headless=args.headless))
    _print_precheck(pre, platform)
    return 0 if pre.ok else 1


# --------------------------------------------------------------------------- calibrate


async def calibrate_web(cfg: TargetConfig, url: str, selectors_path: Path, *, headless: bool) -> int:
    from playwright.async_api import async_playwright

    from flashbuy.calibrate import WebCalibrator, save_result
    from flashbuy.web_runner import launch_context

    async with async_playwright() as pw:
        ctx = await launch_context(pw, cfg.web, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        console.print("[bold]Kalibrasi web.[/] Alt+klik = rekam elemen (tidak dijalankan), "
                      "klik biasa = lanjut. Klik 'Buat Pesanan' diblokir.")
        result = await WebCalibrator(ctx, page, url, console=console).run()
        backup = save_result(selectors_path, result, url)
        await ctx.close()
    console.print(f"Tersimpan: [bold]{selectors_path}[/]" + (f" (backup: {backup})" if backup else ""))
    for w in result.warnings:
        console.print(f"[yellow]! {w}[/]")
    return 0


def calibrate_android(cfg: TargetConfig, driver, selectors_path: Path, *, prompt=input) -> int:
    from flashbuy import android_selectors
    from flashbuy.android_calibrate import AndroidCalibrator

    result = AndroidCalibrator(driver, android_selectors.load(selectors_path), variant=cfg.variant,
                               prompt=prompt, say=lambda s: console.print(s, highlight=False)).run()
    meta = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": driver.serial,
            "skipped": result.skipped}
    backup = android_selectors.save(selectors_path, result.steps, meta)
    console.print(f"Tersimpan: [bold]{selectors_path}[/] (bagian android)" + (f" (backup: {backup})" if backup else ""))
    for w in result.warnings:
        console.print(f"[yellow]! {w}[/]")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    platform = _platform(args, "platform")
    if platform is None:
        return 2
    cfg = _load(args)
    if platform == "android":
        console.print("[bold]Kalibrasi Android.[/] Gunakan produk biasa yang murah; Anda yang men-tap HP, "
                      "alat hanya membaca layar (tidak pernah menekan 'Buat Pesanan').")
        return calibrate_android(cfg, _android_driver(cfg), Path(args.selectors))
    url = args.url or cfg.product_url
    if not args.url:
        console.print("[yellow]--url tidak diisi; memakai product_url dari config. Sebaiknya kalibrasi "
                      "di produk biasa yang murah (bukan produk flash sale).[/]")
    return asyncio.run(calibrate_web(cfg, url, Path(args.selectors), headless=args.headless))


# --------------------------------------------------------------------------- run


def print_result(result, open_at: float) -> None:
    from flashbuy.session import step_offsets

    table = Table(title=f"Timeline {result.platform} (relatif T = start_time, waktu server)")
    for col in ("Langkah", "Δ T", "Detail"):
        table.add_column(col)
    for name, rel_ms, detail in step_offsets(result, open_at):
        table.add_row(name, f"{rel_ms:+,d} ms", detail)
    console.print(table)
    color = "green" if str(result.status) in OK_STATUSES else "red"
    console.print(f"Status: [bold {color}]{result.status}[/] - {result.message}", highlight=False)
    if result.detail:
        console.print(f"Detail: {result.detail}", highlight=False)
    for shot in result.screenshots:
        console.print(f"Screenshot: {shot}")


async def _hand_over_browser(runner, live: bool, status, headless: bool,
                             wait_user: Callable[[object], Awaitable[None]] | None) -> None:
    """Serahkan browser ke pengguna dan tunggu sampai jendelanya ditutup sendiri."""
    from flashbuy.runner_base import RunStatus

    if status in (RunStatus.CAPTCHA, RunStatus.VERIFICATION):
        console.print("[bold red]Halaman captcha/verifikasi dibiarkan terbuka. Selesaikan manual; "
                      "alat TIDAK akan retry.[/]")
    if live:
        console.print("[bold]Mode LIVE: browser TIDAK ditutup otomatis. Periksa/selesaikan manual "
                      "(PIN, verifikasi, status pesanan), lalu tutup jendela browser sendiri.[/]")
    else:
        console.print("[bold]Browser dibiarkan terbuka. Selesaikan secara manual, lalu tutup jendela browser.[/]")
    if wait_user is not None:
        await wait_user(runner)
    elif headless:
        console.print("[yellow]--headless (khusus mock/tes): tidak ada jendela untuk ditutup pengguna.[/]")
    else:
        await runner.wait_closed()


async def run_web(cfg: TargetConfig, sel: selector_store.SelectorSet, *, live: bool, lead_ms: int,
                  report: timesync.SyncReport, run_dir: Path, headless: bool, samples: int,
                  wait_user: Callable[[object], Awaitable[None]] | None = None):
    """Satu run web. Live: browser tidak pernah ditutup alat (apa pun hasilnya, termasuk error);
    pengguna yang menutupnya. Dry-run: ditutup, kecuali captcha/verifikasi/login (butuh tindakan manual).
    `wait_user` menggantikan "tunggu pengguna menutup jendela" (dipakai tes)."""
    from flashbuy.notifier import Notifier
    from flashbuy.runner_base import RunLog, keep_open
    from flashbuy.session import run_single
    from flashbuy.web_runner import WebRunner

    clock = timesync.ServerClock(report.offset_s)
    log = RunLog(run_dir, clock, "web", console)
    notifier = Notifier(cfg.notify_webhook)
    runner = WebRunner(cfg, sel, log=log, notifier=notifier, headless=headless)

    def sync_fn() -> timesync.SyncReport:
        return timesync.sync(samples=samples, http_url=cfg.origin + "/")

    result = None
    try:
        result = await run_single(runner, open_at=cfg.start_epoch, live=live, lead_ms=lead_ms,
                                  clock=clock, log=log, notifier=notifier, sync_fn=sync_fn)
        print_result(result, cfg.start_epoch)
        return result
    finally:
        status = result.status if result is not None else None
        try:
            if keep_open(live, status) and runner.context is not None:
                await _hand_over_browser(runner, live, status, headless, wait_user)
        finally:
            await runner.close()
            log.close()
            notifier.join(1)


async def run_android(cfg: TargetConfig, sel, driver, *, live: bool, lead_ms: int, report: timesync.SyncReport,
                      run_dir: Path, samples: int):
    """Satu run Android. Aplikasi tidak pernah ditutup alat; layar dibiarkan apa adanya untuk pengguna."""
    from flashbuy.android_runner import AndroidRunner
    from flashbuy.notifier import Notifier
    from flashbuy.runner_base import RunLog, RunStatus, keep_open
    from flashbuy.session import run_single

    clock = timesync.ServerClock(report.offset_s)
    log = RunLog(run_dir, clock, "android", console)
    notifier = Notifier(cfg.notify_webhook)
    runner = AndroidRunner(cfg, sel, log=log, notifier=notifier, driver=driver)

    def sync_fn() -> timesync.SyncReport:
        return timesync.sync(samples=samples, http_url=cfg.origin + "/")

    result = None
    try:
        result = await run_single(runner, open_at=cfg.start_epoch, live=live, lead_ms=lead_ms,
                                  clock=clock, log=log, notifier=notifier, sync_fn=sync_fn)
        print_result(result, cfg.start_epoch)
        return result
    finally:
        status = result.status if result is not None else None
        if status in (RunStatus.CAPTCHA, RunStatus.VERIFICATION):
            console.print("[bold red]Captcha/verifikasi di HP dibiarkan apa adanya. Selesaikan manual; "
                          "alat TIDAK akan retry.[/]")
        if keep_open(live, status):
            console.print("[bold]Aplikasi di HP dibiarkan terbuka apa adanya (alat tidak menutup aplikasi). "
                          "Periksa/selesaikan manual (PIN, verifikasi, status pesanan).[/]")
        await runner.close()
        log.close()
        notifier.join(1)


def cmd_run(args: argparse.Namespace) -> int:
    platform = _platform(args, "only")
    if platform is None:
        return 2
    cfg = _load(args)
    if args.live:
        require_live_ready(cfg)
    if not getattr(cfg, platform).enabled:
        console.print(f"[red]{platform}.enabled = false di config.[/]")
        return 2
    lead_ms = cfg.lead_ms if args.lead_ms is None else args.lead_ms
    if not 0 <= lead_ms <= 1000:
        console.print("[red]--lead-ms harus 0..1000[/]")
        return 2
    if time.time() > cfg.start_epoch + POLL_WINDOW_AFTER_S:
        console.print(f"[red]start_time {cfg.start_time.isoformat()} sudah lewat.[/]")
        return 2
    sel = selector_store.load(args.selectors) if platform == "web" else _android_selectors(args.selectors)
    mode = "[bold red]LIVE - pesanan sungguhan akan dibuat[/]" if args.live else "[bold green]DRY-RUN[/]"
    console.print(f"Jalur {platform} | Mode: {mode} | T = {cfg.start_time.isoformat()} | lead {lead_ms} ms | "
                  f"selectors: {sel.source or 'default teks'}")
    driver = _android_driver(cfg) if platform == "android" else None  # gagal konek -> berhenti sebelum timesync

    console.print("Timesync awal...")
    report = timesync.sync(samples=args.samples, http_url=cfg.origin + "/")
    print_sync_report(report)
    if report.primary is None:
        console.print("[red bold]Timesync gagal total; run dibatalkan.[/]")
        return 1
    run_dir = Path("logs") / f"{time.strftime('%Y%m%d-%H%M%S')}-{platform}"
    if platform == "android":
        result = asyncio.run(run_android(cfg, sel, driver, live=args.live, lead_ms=lead_ms, report=report,
                                         run_dir=run_dir, samples=args.samples))
    else:
        result = asyncio.run(run_web(cfg, sel, live=args.live, lead_ms=lead_ms, report=report,
                                     run_dir=run_dir, headless=args.headless, samples=args.samples))
    console.print(f"Log: {run_dir}")
    return 0 if str(result.status) in OK_STATUSES else 1


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flashbuy", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, config: bool = True) -> None:
        if config:
            sp.add_argument("--config", required=True)
            sp.add_argument("--selectors", default=str(selector_store.DEFAULT_PATH))
        # opsi tersembunyi untuk tes/demo terhadap mock lokal
        sp.add_argument("--allow-local", action="store_true", help=argparse.SUPPRESS)
        sp.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)

    ts = sub.add_parser("timesync", help="ukur offset jam lokal vs NTP & header Date Shopee")
    ts.add_argument("--samples", type=int, default=timesync.DEFAULT_SAMPLES)
    ts.add_argument("--ntp-host", default=timesync.DEFAULT_NTP_HOST)
    ts.add_argument("--url", default=timesync.DEFAULT_HTTP_URL)
    ts.set_defaults(func=cmd_timesync)

    lg = sub.add_parser("login", help="buka profil browser untuk login manual")
    lg.add_argument("--platform", choices=("web", "android"), required=True)
    common(lg)
    lg.set_defaults(func=cmd_login)

    pc = sub.add_parser("precheck", help="cek sesi login, alamat default, saldo ShopeePay")
    pc.add_argument("--only", choices=("web", "android"))
    common(pc)
    pc.set_defaults(func=cmd_precheck)

    cal = sub.add_parser("calibrate", help="rekam selector di produk biasa")
    cal.add_argument("--platform", choices=("web", "android"), required=True)
    cal.add_argument("--url", help="URL produk biasa yang murah untuk kalibrasi")
    common(cal)
    cal.set_defaults(func=cmd_calibrate)

    run = sub.add_parser("run", help="jalankan checkout (default DRY-RUN)")
    run.add_argument("--live", action="store_true", help="checkout sungguhan (klik Buat Pesanan)")
    run.add_argument("--lead-ms", type=int, default=None,
                     help=f"mulai cek tombol N ms sebelum T (default config / {DEFAULT_LEAD_MS})")
    run.add_argument("--only", choices=("web", "android"))
    run.add_argument("--samples", type=int, default=timesync.DEFAULT_SAMPLES, help="sampel timesync")
    common(run)
    run.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        console.print(f"[red]{e}[/]")
        return 2
    except DriverError as e:
        console.print(f"[red]Android: {e}[/]\nCek kabel USB, izin USB debugging, dan `adb devices`.")
        return 2
    except KeyboardInterrupt:
        console.print("[red]Dibatalkan.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
