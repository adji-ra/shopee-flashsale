"""Entry point: python -m flashbuy <perintah>."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from flashbuy import selector_store, timesync
from flashbuy.android_driver import DriverError
from flashbuy.config import (
    POLL_WINDOW_AFTER_S,
    ConfigError,
    TargetConfig,
    load_config,
    require_live_ready,
)

console = Console()

OK_STATUSES = ("DRYRUN_OK", "ORDER_PLACED_AWAIT_PIN")
LATE_START_S = 70  # run harus mulai paling lambat T-70 s (precheck + arm T-60 s di luar jendela polling)


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
    """'web' | 'android' untuk perintah satu jalur (login, calibrate)."""
    value = getattr(args, attr)
    if value not in ("web", "android"):
        console.print(f"[yellow]Pilih satu jalur dengan --{attr} web|android.[/]")
        return None
    return value


def _android_driver(cfg: TargetConfig):
    """Driver device nyata (diganti FakeDriver di tes)."""
    from flashbuy.android_driver import U2Driver

    return U2Driver(cfg.android.serial, package=cfg.android.package)


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
    from flashbuy.orchestrator import EXIT_BUSY

    platform = _platform(args, "platform")
    if platform is None:
        return 2
    cfg = _load(args)
    lock = _lock("login")
    if lock is None:
        return EXIT_BUSY
    try:
        if platform == "android":
            login_android(cfg, _android_driver(cfg))
            return 0
        asyncio.run(login_web(cfg, selector_store.load(args.selectors), headless=args.headless))
        return 0
    finally:
        lock.release()


# --------------------------------------------------------------------------- precheck


async def precheck_web(cfg: TargetConfig, sel: selector_store.SelectorSet, *, headless: bool,
                       log_dir: Path | None = None, on_runner: Callable[[object], None] | None = None):
    """Precheck web. Tidak menambah ke keranjang / checkout: hanya membuka halaman alamat, saldo, dan produk."""
    from flashbuy.notifier import Notifier
    from flashbuy.runner_base import RunLog
    from flashbuy.web_runner import WebRunner

    log = RunLog(log_dir or Path("logs") / f"{time.strftime('%Y%m%d-%H%M%S')}-precheck", timesync.ServerClock(),
                 "web", Console(stderr=True, quiet=True))
    runner = WebRunner(cfg, sel, log=log, notifier=Notifier(cfg.notify_webhook), headless=headless)
    if on_runner is not None:
        on_runner(runner)
    try:
        await runner.prepare()
        return await runner.precheck()
    finally:
        await runner.close()
        log.close()


async def precheck_android(cfg: TargetConfig, sel, driver, log_dir: Path,
                           on_runner: Callable[[object], None] | None = None):
    """Precheck Android. Tidak menambah ke keranjang / checkout: hanya intent ke produk, alamat, dan saldo."""
    from flashbuy.android_runner import AndroidRunner
    from flashbuy.notifier import Notifier
    from flashbuy.runner_base import RunLog

    log = RunLog(log_dir, timesync.ServerClock(), "android", Console(stderr=True, quiet=True))
    runner = AndroidRunner(cfg, sel, log=log, notifier=Notifier(cfg.notify_webhook), driver=driver)
    if on_runner is not None:
        on_runner(runner)
    try:
        await runner.prepare()
        return await runner.precheck()
    finally:
        await runner.close()
        log.close()


async def precheck_all(cfg: TargetConfig, platforms: list[str], selectors_path: str, driver, *, headless: bool,
                       log_dir: Path, on_runner: Callable[[object], None] | None = None,
                       return_exceptions: bool = False) -> list:
    """Precheck semua jalur di `platforms` secara paralel (urutan hasil = urutan `platforms`)."""
    coros = [precheck_web(cfg, selector_store.load(selectors_path), headless=headless, log_dir=log_dir,
                          on_runner=on_runner) if p == "web" else
             precheck_android(cfg, _android_selectors(selectors_path), driver, log_dir, on_runner=on_runner)
             for p in platforms]
    return list(await asyncio.gather(*coros, return_exceptions=return_exceptions))


def cmd_precheck(args: argparse.Namespace) -> int:
    from flashbuy.orchestrator import EXIT_BUSY

    cfg = _load(args)
    platforms = _platforms(args, cfg)
    if platforms is None:
        return 2
    lock = _lock("precheck")
    if lock is None:
        return EXIT_BUSY
    try:
        driver = _android_driver(cfg) if "android" in platforms else None
        log_dir = Path("logs") / f"{time.strftime('%Y%m%d-%H%M%S')}-precheck"
        pres = asyncio.run(precheck_all(cfg, platforms, args.selectors, driver, headless=args.headless,
                                        log_dir=log_dir))
        if "android" in platforms:
            console.print(f"Log (info device & latensi query): {log_dir}")
        for platform, pre in zip(platforms, pres, strict=True):
            _print_precheck(pre, platform)
        return 0 if all(pre.ok for pre in pres) else 1
    finally:
        lock.release()


# --------------------------------------------------------------------------- doctor


def _print_doctor(checks) -> None:
    table = Table(title="flashbuy doctor (cek akhir H-1)")
    for col in ("Cek", "Hasil", "Detail"):
        table.add_column(col)
    style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    for c in checks:
        table.add_row(c.name, Text(c.level, style=f"bold {style[c.level]}"), Text(c.detail))
    console.print(table)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Cek akhir H-1: config, timesync, precheck semua jalur (tanpa keranjang/checkout), versi Shopee vs
    kalibrasi, umur selector, latensi Android + saran android.lead_ms, ruang disk, uji alarm (--beep)."""
    from flashbuy import doctor
    from flashbuy.notifier import DeferredNotifier, Notifier
    from flashbuy.orchestrator import EXIT_BUSY

    cfg, checks = doctor.check_config(args.config, allow_local=args.allow_local)
    lock = _lock("doctor")
    if lock is None:
        return EXIT_BUSY
    try:
        if cfg is not None:
            console.print("Timesync...")
            checks.append(doctor.check_timesync(timesync.sync(samples=args.samples, http_url=cfg.origin + "/")))
            platforms = [p for p in ("web", "android") if getattr(cfg, p).enabled]
            runners: dict[str, object] = {}

            def on_runner(runner) -> None:
                runner.notifier = DeferredNotifier()  # doctor tidak membunyikan alarm precheck (pakai --beep)
                runners[runner.name] = runner

            driver, results = None, {}
            if "android" in platforms:
                try:
                    driver = _android_driver(cfg)
                except DriverError as e:
                    results["android"] = e
            todo = [p for p in platforms if p not in results]
            console.print(f"Precheck paralel: {', '.join(todo) or '-'} (tanpa keranjang/checkout)...")
            log_dir = Path("logs") / f"{time.strftime('%Y%m%d-%H%M%S')}-doctor"
            pres = asyncio.run(precheck_all(cfg, todo, args.selectors, driver, headless=args.headless,
                                            log_dir=log_dir, on_runner=on_runner, return_exceptions=True))
            results.update(zip(todo, pres, strict=True))
            for p in platforms:
                checks.append(doctor.check_precheck(p, results[p]))
            if "android" in platforms:
                sel = _android_selectors(args.selectors)
                runner = runners.get("android")
                info = getattr(runner, "device_info", {}) if runner is not None else {}
                checks.append(doctor.check_app_version(info.get("versionName", ""), sel.calibrated or {}))
            for p in platforms:
                calibrated = (selector_store.load(args.selectors) if p == "web" else
                              _android_selectors(args.selectors)).calibrated or {}
                checks.append(doctor.check_selector_age(p, calibrated))
            if "android" in platforms:
                runner = runners.get("android")
                checks.append(doctor.check_latency(getattr(runner, "latency", {}) if runner else {},
                                                   cfg.android.lead_ms))
        checks.append(doctor.check_disk(Path("logs")))
        if args.beep:
            checks.append(doctor.check_beep(Notifier(cfg.notify_webhook if cfg is not None else "")))
        _print_doctor(checks)
        verdict = doctor.worst(checks)
        console.print(f"Hasil doctor: [bold]{verdict}[/]" + ("" if args.beep else
                                                          " (alarm belum diuji: tambahkan --beep)"))
        return 1 if verdict == doctor.FAIL else 0
    finally:
        lock.release()


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

    cal = AndroidCalibrator(driver, android_selectors.load(selectors_path), variant=cfg.variant,
                            prompt=prompt, say=lambda s: console.print(Text(s), highlight=False))
    result = cal.run()
    # versi Shopee & resolusi disimpan: precheck memberi PERINGATAN KERAS bila versi aplikasi berubah
    meta = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": driver.serial, **cal.device_meta(),
            "skipped": result.skipped}
    backup = android_selectors.save(selectors_path, result.steps, meta)
    console.print(f"Tersimpan: [bold]{selectors_path}[/] (bagian android; Shopee {meta['app_version'] or '?'}, "
                  f"{meta['wm_size'] or 'resolusi ?'})" + (f" (backup: {backup})" if backup else ""))
    for w in result.warnings:
        console.print(f"[yellow]! {w}[/]")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from flashbuy.orchestrator import EXIT_BUSY

    platform = _platform(args, "platform")
    if platform is None:
        return 2
    cfg = _load(args)
    lock = _lock("calibrate")
    if lock is None:
        return EXIT_BUSY
    try:
        if platform == "android":
            console.print("[bold]Kalibrasi Android.[/] Gunakan produk biasa yang murah; Anda yang men-tap HP, "
                          "alat hanya membaca layar (tidak pernah menekan 'Buat Pesanan').")
            return calibrate_android(cfg, _android_driver(cfg), Path(args.selectors))
        url = args.url or cfg.product_url
        if not args.url:
            console.print("[yellow]--url tidak diisi; memakai product_url dari config. Sebaiknya kalibrasi "
                          "di produk biasa yang murah (bukan produk flash sale).[/]")
        return asyncio.run(calibrate_web(cfg, url, Path(args.selectors), headless=args.headless))
    finally:
        lock.release()


# --------------------------------------------------------------------------- run


def print_result(result, open_at: float) -> None:
    from flashbuy.session import step_offsets

    table = Table(title=f"Timeline {result.platform} (relatif T = start_time, waktu server)")
    for col in ("Langkah", "Δ T", "Detail"):
        table.add_column(col)
    for name, rel_ms, detail in step_offsets(result, open_at):
        # detail = teks layar (nama item, pesan): Text, bukan markup Rich ("[/promo]" tidak boleh crash)
        table.add_row(Text(name), f"{rel_ms:+,d} ms", Text(detail))
    console.print(table)
    color = "green" if str(result.status) in OK_STATUSES else "red"
    console.print(f"Status: [bold {color}]{result.status}[/] - {escape(result.message)}", highlight=False)
    if result.detail:
        console.print(f"Detail: {escape(result.detail)}", highlight=False)
    for shot in result.screenshots:
        console.print(Text(f"Screenshot: {shot}"))


def print_summary(outcome, open_at: float) -> None:
    """Satu tabel ringkasan: jalur, status, latensi langkah kunci (relatif T), pemenang lock."""
    from flashbuy.orchestrator import KEY_STEPS, step_ms

    for lane in outcome.lanes:
        if lane.result is not None and lane.result.steps:
            print_result(lane.result, open_at)
    table = Table(title="Ringkasan run (Δ T, waktu server)")
    for col in ("Jalur", "Status", *[label for _, label in KEY_STEPS], "Pemenang"):
        table.add_column(col)
    for lane in outcome.lanes:
        cells = []
        for name, _ in KEY_STEPS:
            ms = step_ms(lane, name, open_at)
            cells.append("-" if ms is None else f"{ms:+,d} ms")
        table.add_row(lane.name, Text(lane.label()), *cells, "YA" if outcome.winner == lane.name else "")
    console.print(table)
    color = "green" if outcome.exit_code == 0 else "red"
    console.print(f"Status gabungan: [bold {color}]{escape(outcome.status)}[/] (exit {outcome.exit_code})",
                  highlight=False)
    for lane in outcome.lanes:
        res = lane.result
        console.print(Text(f"  {lane.name}: {res.status} - {res.message}" + (f" ({res.detail})" if res.detail else "")))


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
    if wait_user is None and headless:
        console.print("[yellow]--headless (khusus mock/tes): tidak ada jendela untuk ditutup pengguna.[/]")
        return
    # Live: Ctrl+C PERTAMA tidak menutup browser (PIN / status pesanan masih di layar); yang kedua = paksa keluar.
    # Ctrl+C yang sudah terjadi di tengah run (task sedang dibatalkan) dihitung sebagai yang pertama.
    task = asyncio.current_task()
    ignored = task is None or task.cancelling() > 0
    while True:
        try:
            await (wait_user(runner) if wait_user is not None else runner.wait_closed())
            return
        except asyncio.CancelledError:
            if not live or ignored:
                raise
            ignored = True
            task.uncancel()
            console.print("[bold red]Ctrl+C: mode LIVE, browser TIDAK ditutup alat. Selesaikan PIN / cek status "
                          "pesanan, lalu tutup jendela browser sendiri. Ctrl+C sekali lagi = paksa keluar "
                          "(browser ikut tertutup).[/]")


async def run_orchestrated(cfg: TargetConfig, platforms: list[str], sels: dict, driver, *, live: bool,
                           leads: dict[str, int], report: timesync.SyncReport, run_dir: Path, headless: bool,
                           samples: int, wait_user: Callable[[object], Awaitable[None]] | None = None,
                           schedule=None, control=None):
    """Satu run lewat orchestrator (semua jalur di `platforms`). Live: browser & aplikasi TIDAK pernah ditutup
    alat (apa pun hasilnya); pengguna yang menutup browser. Dry-run: browser ditutup kecuali butuh tindakan
    manual. `wait_user` menggantikan "tunggu pengguna menutup jendela" (dipakai tes)."""
    from flashbuy.android_runner import AndroidRunner
    from flashbuy.control import RunControl
    from flashbuy.notifier import Notifier
    from flashbuy.orchestrator import Lane, orchestrate, wire
    from flashbuy.runner_base import RunLog, RunStatus, keep_open
    from flashbuy.web_runner import WebRunner

    clock = timesync.ServerClock(report.offset_s)
    notifier = Notifier(cfg.notify_webhook)
    control = control or RunControl()
    lanes: list[Lane] = []
    for p in platforms:
        log = RunLog(run_dir, clock, p, console)
        if p == "web":
            runner = WebRunner(cfg, sels["web"], log=log, notifier=notifier, headless=headless)
        else:
            runner = AndroidRunner(cfg, sels["android"], log=log, notifier=notifier, driver=driver)
            runner.hold_screen_on = True  # svc power stayon usb selama run; dikembalikan di runner.close()
        lane = Lane(p, runner, log, leads[p])
        wire(lane, control)
        lanes.append(lane)

    def sync_fn() -> timesync.SyncReport:
        return timesync.sync(samples=samples, http_url=cfg.origin + "/")

    outcome = None
    try:
        outcome = await orchestrate(lanes, open_at=cfg.start_epoch, live=live, clock=clock, notifier=notifier,
                                    control=control, sync_fn=sync_fn, schedule=schedule)
        print_summary(outcome, cfg.start_epoch)
        return outcome
    finally:
        try:
            for lane in lanes:
                status = lane.result.status if lane.result is not None else None
                if lane.name == "android":
                    if status in (RunStatus.CAPTCHA, RunStatus.VERIFICATION):
                        console.print("[bold red]Captcha/verifikasi di HP dibiarkan apa adanya. Selesaikan manual; "
                                      "alat TIDAK akan retry.[/]")
                    if keep_open(live, status):
                        console.print("[bold]Aplikasi di HP dibiarkan terbuka apa adanya (alat tidak menutup "
                                      "aplikasi). Periksa/selesaikan manual (PIN, verifikasi, status pesanan).[/]")
                elif keep_open(live, status) and getattr(lane.runner, "context", None) is not None:
                    await _hand_over_browser(lane.runner, live, status, headless, wait_user)
        finally:
            for lane in lanes:
                # alarm saat close (mis. `svc power stayon` gagal dikembalikan) = alarm keselamatan terpisah:
                # dibunyikan sungguhan, bukan ditahan DeferredNotifier milik fase run
                lane.runner.notifier = notifier
                try:
                    await lane.runner.close()
                finally:
                    lane.log.close()
            notifier.join(1)


async def run_web(cfg: TargetConfig, sel: selector_store.SelectorSet, *, live: bool, lead_ms: int,
                  report: timesync.SyncReport, run_dir: Path, headless: bool, samples: int,
                  wait_user: Callable[[object], Awaitable[None]] | None = None):
    """Satu run web (orchestrator dengan satu jalur); mengembalikan RunResult jalur web."""
    outcome = await run_orchestrated(cfg, ["web"], {"web": sel}, None, live=live, leads={"web": lead_ms},
                                     report=report, run_dir=run_dir, headless=headless, samples=samples,
                                     wait_user=wait_user)
    return outcome.lanes[0].result


async def run_android(cfg: TargetConfig, sel, driver, *, live: bool, lead_ms: int, report: timesync.SyncReport,
                      run_dir: Path, samples: int):
    """Satu run Android (orchestrator dengan satu jalur); mengembalikan RunResult jalur android."""
    outcome = await run_orchestrated(cfg, ["android"], {"android": sel}, driver, live=live,
                                     leads={"android": lead_ms}, report=report, run_dir=run_dir, headless=False,
                                     samples=samples)
    return outcome.lanes[0].result


def _platforms(args: argparse.Namespace, cfg: TargetConfig) -> list[str] | None:
    """--only X -> [X] (harus enabled); tanpa --only -> semua jalur yang enabled di config."""
    if args.only:
        if not getattr(cfg, args.only).enabled:
            console.print(f"[red]{args.only}.enabled = false di config.[/]")
            return None
        return [args.only]
    return [p for p in ("web", "android") if getattr(cfg, p).enabled]


def _lock(command: str):
    """Lock antar-proses (~/.flashbuy/run.lock). None + pesan bila proses lain memegangnya."""
    from flashbuy.runlock import RunLock, RunLockBusy

    try:
        return RunLock(command).acquire()
    except RunLockBusy as e:
        console.print(f"[red bold]{escape(str(e))}[/]", highlight=False)
        return None


def cmd_run(args: argparse.Namespace) -> int:
    from flashbuy.orchestrator import EXIT_BUSY

    cfg = _load(args)
    platforms = _platforms(args, cfg)
    if platforms is None:
        return 2
    if args.live:
        require_live_ready(cfg)
    if args.lead_ms is not None and not 0 <= args.lead_ms <= 1000:
        console.print("[red]--lead-ms harus 0..1000[/]")
        return 2
    leads = {p: getattr(cfg, p).lead_ms if args.lead_ms is None else args.lead_ms for p in platforms}
    if time.time() > cfg.start_epoch + POLL_WINDOW_AFTER_S:
        console.print(f"[red]start_time {cfg.start_time.isoformat()} sudah lewat.[/]")
        return 2
    if time.time() > cfg.start_epoch - LATE_START_S and not args.allow_local:
        # precheck + buka halaman (T-60 s) di dalam jendela polling = aksi tanpa rate limit
        console.print(f"[red]Terlambat: run harus dimulai paling lambat T-{LATE_START_S} s "
                      "(precheck & buka halaman produk sebelum T-60 s).[/]")
        return 2
    if args.live and args.headless and not args.allow_local:
        console.print("[red]--live tidak boleh --headless: browser live tidak pernah ditutup otomatis.[/]")
        return 2
    lock = _lock("run")
    if lock is None:
        return EXIT_BUSY
    try:
        sels = {p: selector_store.load(args.selectors) if p == "web" else _android_selectors(args.selectors)
                for p in platforms}
        mode = "[bold red]LIVE - pesanan sungguhan akan dibuat[/]" if args.live else "[bold green]DRY-RUN[/]"
        lead_text = ", ".join(f"{p} {leads[p]} ms" for p in platforms)
        sources = ", ".join(f"{p}: {sels[p].source or 'default teks'}" for p in platforms)
        console.print(f"Jalur {'+'.join(platforms)} | Mode: {mode} | T = {cfg.start_time.isoformat()} | "
                      f"lead {lead_text} | selectors {sources}")
        driver = _android_driver(cfg) if "android" in platforms else None  # gagal konek -> berhenti sebelum timesync

        console.print("Timesync awal...")
        report = timesync.sync(samples=args.samples, http_url=cfg.origin + "/")
        print_sync_report(report)
        if report.primary is None:
            console.print("[red bold]Timesync gagal total; run dibatalkan.[/]")
            return 1
        run_dir = Path("logs") / f"{time.strftime('%Y%m%d-%H%M%S')}-{'+'.join(platforms)}"
        outcome = asyncio.run(run_orchestrated(cfg, platforms, sels, driver, live=args.live, leads=leads,
                                               report=report, run_dir=run_dir, headless=args.headless,
                                               samples=args.samples))
        console.print(f"Log: {run_dir}")
        return outcome.exit_code
    finally:
        lock.release()


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

    pc = sub.add_parser("precheck", help="cek sesi login, alamat default, saldo ShopeePay (semua jalur paralel)")
    pc.add_argument("--only", choices=("web", "android"), help="hanya satu jalur (default: semua yang enabled)")
    common(pc)
    pc.set_defaults(func=cmd_precheck)

    cal = sub.add_parser("calibrate", help="rekam selector di produk biasa")
    cal.add_argument("--platform", choices=("web", "android"), required=True)
    cal.add_argument("--url", help="URL produk biasa yang murah untuk kalibrasi")
    common(cal)
    cal.set_defaults(func=cmd_calibrate)

    doc = sub.add_parser("doctor", help="cek akhir H-1 (PASS/WARN/FAIL); tanpa keranjang/checkout")
    doc.add_argument("--beep", action="store_true", help="uji alarm: pola pendek & mendesak (+ webhook)")
    doc.add_argument("--samples", type=int, default=timesync.DEFAULT_SAMPLES, help="sampel timesync")
    common(doc)
    doc.set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="jalankan checkout semua jalur enabled (default DRY-RUN)")
    run.add_argument("--live", action="store_true", help="checkout sungguhan (klik Buat Pesanan)")
    run.add_argument("--lead-ms", type=int, default=None,
                     help="mulai cek tombol N ms sebelum T untuk SEMUA jalur (default: web.lead_ms / android.lead_ms)")
    run.add_argument("--only", choices=("web", "android"), help="hanya satu jalur (default: semua yang enabled)")
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
        return 130  # orchestrator.EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
