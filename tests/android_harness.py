"""Harness tes jalur Android: AndroidRunner + FakeDriver + FakeShopeeApp, waktu virtual (FakeClock)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from flashbuy import android_selectors
from flashbuy.android_driver import FakeDriver
from flashbuy.android_runner import AndroidRunner
from flashbuy.config import TargetConfig
from flashbuy.notifier import Notifier
from flashbuy.runner_base import RunLog, always_allow
from flashbuy.session import Schedule, run_single
from flashbuy.timesync import WIB, ServerClock
from tests.conftest import LIVE_NAME, PRICE_DEFAULTS, FakeClock, quiet_console
from tests.fake_android import PRODUCT_URL, AppScenario, FakeShopeeApp


def android_cfg(open_at: float, **top) -> TargetConfig:
    android = top.pop("android", {})
    return TargetConfig.model_validate({
        "product_url": PRODUCT_URL,
        "start_time": datetime.fromtimestamp(open_at, WIB).isoformat(),
        "web": {"enabled": False},
        "android": {"serial": "FAKE123", **android},
        **PRICE_DEFAULTS, **top,
    })


@dataclass
class AndroidOutcome:
    result: object
    open_at: float
    app: FakeShopeeApp
    driver: FakeDriver
    runner: AndroidRunner
    notifier: Notifier
    log_dir: object

    def kind(self, k: str) -> list[dict]:
        return self.app.kind(k)

    def step_names(self) -> list[str]:
        return [s.name for s in self.result.steps]

    def events(self) -> list[str]:
        return [e["event"] for e in self.notifier.events]


def make_android(tmp_path, *, open_in_s: float = 12.0, cfg: dict | None = None, live: bool = False,
                 selectors=None, latency_s: float = 0.02, driver_kw: dict | None = None,
                 runner_attrs: dict | None = None, before=None, **scenario):
    clock = FakeClock(tick=0.00005)
    sclock = ServerClock(0.0, clock)
    open_at = clock.time() + open_in_s
    app = FakeShopeeApp(AppScenario(open_at=open_at, **scenario), clock)
    driver = FakeDriver(app, clock, latency_s=latency_s,
                        props={"ro.product.brand": "TECNO", "ro.product.manufacturer": "TECNO",
                               "ro.product.model": "TECNO BG6", "ro.build.version.release": "13",
                               "ro.build.version.sdk": "33", "ro.tranos.version": "hios13.6.0"},
                        **(driver_kw or {}))
    cfg = dict(cfg or {})
    if live:
        cfg.setdefault("expected_name", LIVE_NAME)
    target = android_cfg(open_at, **cfg)
    log = RunLog(tmp_path / "logs", sclock, "android", quiet_console())
    notifier = Notifier(beep=lambda f, d: None, post=lambda u, p: None, repeat=1)
    runner = AndroidRunner(target, selectors or android_selectors.defaults(), log=log, notifier=notifier,
                           driver=driver, before_place_order=before or always_allow)
    for k, v in (runner_attrs or {}).items():
        setattr(runner, k, v)
    return runner, app, driver, sclock, open_at, log, notifier


def run_android(tmp_path, *, live: bool = False, lead_ms: float = 150, during=None, **kw) -> AndroidOutcome:
    runner, app, driver, sclock, open_at, log, notifier = make_android(tmp_path, live=live, **kw)

    async def go():
        task = asyncio.create_task(during(runner, app)) if during is not None else None
        try:
            return await run_single(runner, open_at=open_at, live=live, lead_ms=lead_ms, clock=sclock, log=log,
                                    notifier=notifier, schedule=Schedule(precheck_before_s=60, resync_before_s=0,
                                                                         arm_before_s=4))
        finally:
            if task is not None:
                task.cancel()
            await runner.close()

    result = asyncio.run(go())
    log.close()
    notifier.join(1)
    return AndroidOutcome(result, open_at, app, driver, runner, notifier, tmp_path / "logs")


def polling_actions(out: AndroidOutcome) -> list[int]:
    """Aksi polling ke server (klik Beli + reload) dalam ms server."""
    return sorted(e["t_server_ms"] for e in out.app.events
                  if e["kind"] in ("buy", "buy_disabled", "refresh") or
                  (e["kind"] == "intent" and e["t_server_ms"] >= out.open_at * 1000 - 1000))


def assert_polling_rules(out: AndroidOutcome) -> list[int]:
    t = int(out.open_at * 1000)
    acts = polling_actions(out)
    assert acts, "tidak ada aksi polling"
    for a in acts:
        assert t - 1000 <= a <= t + 8000, f"aksi di luar jendela: {a - t} ms relatif T"
    gaps = [b - a for a, b in zip(acts, acts[1:], strict=False)]
    assert all(g >= 400 for g in gaps), f"jarak antaraksi < 400 ms: {gaps}"
    return gaps
