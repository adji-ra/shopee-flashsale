from __future__ import annotations

import asyncio

import pytest


class FakeClock:
    """Jam palsu deterministik.

    Setiap pembacaan jam memajukan waktu `tick` detik (mensimulasikan biaya busy-wait),
    `sleep` memajukan waktu sebesar durasi + `oversleep` (jitter OS).
    """

    def __init__(self, wall0: float = 1_790_000_000.0, tick: float = 0.0001,
                 oversleep: float = 0.0):
        self.wall0 = wall0
        self.tick = tick
        self.oversleep = oversleep
        self.t = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        self.t += self.tick
        return self.wall0 + self.t

    def monotonic(self) -> float:
        self.t += self.tick
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds > 0:
            self.t += seconds + self.oversleep

    async def asleep(self, seconds: float) -> None:
        self.sleep(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def _flashbuy_home(tmp_path_factory, monkeypatch):
    """Lock antar-proses (~/.flashbuy/run.lock) per tes, tidak pernah di home pengguna."""
    home = tmp_path_factory.mktemp("flashbuy-home")
    monkeypatch.setenv("FLASHBUY_HOME", str(home))
    return home


# ------------------------------------------------------------------ mock Shopee + web runner

import io  # noqa: E402
import os  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime  # noqa: E402

from rich.console import Console  # noqa: E402

from tests.mock_shopee import MockAdmin, MockShopee  # noqa: E402

HEADLESS = os.environ.get("FLASHBUY_HEADED") != "1"


@pytest.fixture(scope="session")
def mock():
    m = MockShopee().start()
    yield m
    m.stop()


@pytest.fixture
def admin(mock) -> MockAdmin:
    a = MockAdmin(mock.base_url)
    a.reset()
    return a


def quiet_console() -> Console:
    return Console(file=io.StringIO(), width=200)


PRICE_DEFAULTS = {"max_item_price": 100_000, "max_total": 120_000}
LIVE_NAME = "Uji Coba"  # potongan nama produk mock ("Ponsel Uji Coba 128GB"), default expected_name tes live


def make_cfg(mock, tmp_path, open_at: float, variant: str | None = None, web: dict | None = None,
             **top):
    from flashbuy.config import TargetConfig
    from flashbuy.timesync import WIB

    return TargetConfig.model_validate({
        "product_url": mock.product_url,
        "variant": variant,
        "start_time": datetime.fromtimestamp(open_at, WIB).isoformat(),
        "web": {"profile_dir": str(tmp_path / "profile"), "channel": None, **(web or {})},
        "android": {"enabled": False},
        **PRICE_DEFAULTS, **top,
    }, context={"allow_local": True})


@dataclass
class WebOutcome:
    result: object
    open_at: float
    notifier: object
    requests: list
    runner: object

    def kind(self, k: str) -> list[dict]:
        return [e for e in self.requests if e["kind"] == k]

    def step_names(self) -> list[str]:
        return [s.name for s in self.result.steps]


async def run_web(mock, admin, tmp_path, *, scenario: str = "normal", live: bool = False,
                  open_in_ms: int = 3500, variant: str | None = None, before=None,
                  lead_ms: float = 150, selectors=None, web: dict | None = None,
                  cfg: dict | None = None, runner_attrs: dict | None = None,
                  inspect=None, during=None, **overrides) -> WebOutcome:
    from flashbuy import selector_store
    from flashbuy.notifier import Notifier
    from flashbuy.runner_base import RunLog, always_allow
    from flashbuy.session import Schedule, run_single
    from flashbuy.timesync import ServerClock
    from flashbuy.web_runner import WebRunner

    admin.scenario(name=scenario, open_in_ms=open_in_ms, **overrides)
    st = admin.state()["scenario"]
    open_at = st["open_at"]
    clock = ServerClock(st["clock_offset_ms"] / 1000)
    cfg = dict(cfg or {})
    if live:
        cfg.setdefault("expected_name", LIVE_NAME)  # --live wajib expected_name
    target = make_cfg(mock, tmp_path, open_at, variant, web=web, **cfg)
    log = RunLog(tmp_path / "logs", clock, "web", quiet_console())
    notifier = Notifier(beep=lambda f, d: None, post=lambda u, p: None, repeat=1)
    runner = WebRunner(target, selectors or selector_store.defaults(), log=log, notifier=notifier,
                       headless=HEADLESS, before_place_order=before or always_allow)
    for k, v in (runner_attrs or {}).items():
        setattr(runner, k, v)
    task = None
    try:
        if during is not None:
            task = asyncio.create_task(during(runner, open_at))
        result = await run_single(runner, open_at=open_at, live=live, lead_ms=lead_ms, clock=clock,
                                  log=log, notifier=notifier,
                                  schedule=Schedule(precheck_before_s=60, resync_before_s=0,
                                                    arm_before_s=1.5))
        if inspect is not None:
            await inspect(runner)
    finally:
        if task is not None:
            task.cancel()
        await runner.close()
        log.close()
    notifier.join(1)
    return WebOutcome(result, open_at, notifier, admin.log(), runner)


def assert_polling_rules(out: WebOutcome) -> list[int]:
    """Aksi polling (klik Beli + reload produk di jendela) patuh jendela & jarak >= 400 ms."""
    t = int(out.open_at * 1000)
    acts = sorted(e["t_server_ms"] for e in out.requests
                  if e["kind"] == "buy" or (e["kind"] == "product" and e["t_server_ms"] >= t - 1000))
    assert acts, "tidak ada aksi polling"
    for a in acts:
        assert t - 1000 <= a <= t + 8000, f"aksi di luar jendela: {a - t} ms relatif T"
    gaps = [b - a for a, b in zip(acts, acts[1:], strict=False)]
    assert all(g >= 400 for g in gaps), f"jarak antaraksi < 400 ms: {gaps}"
    return gaps


@pytest.fixture
def run():
    return asyncio.run
