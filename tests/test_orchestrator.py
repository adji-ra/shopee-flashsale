"""Orchestrator: jalur web (mock Shopee + Playwright) dan Android (FakeDriver, jam nyata) berjalan BERSAMAAN.

Aturan yang diuji:
- lock pemenang lewat before_place_order(): total tepat 1 klik/request "Buat Pesanan" di mode live; jalur lain
  ABORTED sebelum klik; lock tidak pernah dilepas walaupun pemenang jatuh ke UNKNOWN_STATE;
- stop global: CAPTCHA di satu jalur -> jalur lain berhenti, 0 tap setelah event stop; jalur yang sudah mengklik
  "Buat Pesanan" tetap menunggu layar PIN;
- PRICE_GUARD di satu jalur tidak menghentikan jalur lain;
- precheck: satu gagal -> jalan sendiri + alarm; keduanya gagal -> batal (exit 3);
- SATU alarm hasil akhir (pola mendesak bila pesanan terbuat), status gabungan berprioritas, exit code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from flashbuy import selector_store
from flashbuy.android_driver import FakeDriver, Node
from flashbuy.android_runner import AndroidRunner
from flashbuy.control import RunControl
from flashbuy.notifier import Notifier
from flashbuy.orchestrator import (
    EXIT_MANUAL,
    EXIT_OK,
    EXIT_PRECHECK,
    EXIT_UNKNOWN,
    UNKNOWN_AFTER_ORDER,
    Lane,
    orchestrate,
    wire,
)
from flashbuy.runner_base import MAYBE_ORDERED_MSG, RunLog, RunStatus
from flashbuy.session import Schedule
from flashbuy.timesync import ServerClock
from flashbuy.web_runner import WebRunner
from tests.android_harness import RealClock, calibrated_selectors
from tests.conftest import HEADLESS, LIVE_NAME, make_cfg, quiet_console
from tests.fake_android import PRODUCT_URL, AppScenario, FakeShopeeApp

SCHEDULE = Schedule(precheck_before_s=60, resync_before_s=0, arm_before_s=2.0)
# aksi Android yang mengubah layar (bukan baca): tidak boleh ada setelah event stop
ANDROID_ACTIONS = ("tap", "buy", "buy_disabled", "confirm", "refresh", "intent", "back", "order", "variant",
                   "payment", "cart_toggle")


@dataclass
class Both:
    outcome: object
    open_at: float
    requests: list  # log mock web
    app: FakeShopeeApp
    driver: FakeDriver
    control: RunControl
    alarms: list = field(default_factory=list)  # alarm orchestrator (notifier asli)
    web: WebRunner | None = None
    android: AndroidRunner | None = None

    def lane(self, name: str):
        return next(lane for lane in self.outcome.lanes if lane.name == name)

    def result(self, name: str):
        return self.lane(name).result

    def web_kind(self, k: str) -> list[dict]:
        return [e for e in self.requests if e["kind"] == k]

    def orders(self) -> int:
        """Total request/klik "Buat Pesanan" yang sampai ke server (web + Android)."""
        return len(self.web_kind("order")) + len(self.app.kind("order"))

    def step_ms(self, name: str, step: str) -> int | None:
        s = self.result(name).step(step)
        return None if s is None else s.t_server_ms


def run_both(mock, admin, tmp_path, *, live: bool = False, web_scenario: str = "normal", web: dict | None = None,
             app_cls=FakeShopeeApp, android: dict | None = None, open_in_ms: int = 5000,
             platforms=("web", "android")) -> Both:
    admin.scenario(name=web_scenario, open_in_ms=open_in_ms, **(web or {}))
    st = admin.state()["scenario"]
    open_at = st["open_at"]
    clock = ServerClock(st["clock_offset_ms"] / 1000)
    cfg = make_cfg(mock, tmp_path, open_at, android={"enabled": True, "serial": "FAKE123"},
                   **({"expected_name": LIVE_NAME} if live else {}))
    # jalur Android memakai URL produk Shopee (intent ke aplikasi palsu); jalur web memakai URL mock
    cfg_android = cfg.model_copy(update={"product_url": PRODUCT_URL})
    real = RealClock()
    app = app_cls(AppScenario(open_at=open_at, **(android or {})), real)
    driver = FakeDriver(app, real, latency_s=0.005)
    logs = tmp_path / "logs"
    notifier = Notifier(beep=lambda f, d: None, post=lambda u, p: None, repeat=1)
    control = RunControl()
    lanes: list[Lane] = []
    web_runner = android_runner = None
    if "web" in platforms:
        log = RunLog(logs, clock, "web", quiet_console())
        web_runner = WebRunner(cfg, selector_store.defaults(), log=log, notifier=notifier, headless=HEADLESS)
        lanes.append(Lane("web", web_runner, log, 150))
    if "android" in platforms:
        log = RunLog(logs, clock, "android", quiet_console())
        android_runner = AndroidRunner(cfg_android, calibrated_selectors(), log=log, notifier=notifier,
                                       driver=driver)
        android_runner.unknown_limit_s = 1.5
        lanes.append(Lane("android", android_runner, log, 300))
    for lane in lanes:
        wire(lane, control)

    async def go():
        try:
            return await orchestrate(lanes, open_at=open_at, live=live, clock=clock, notifier=notifier,
                                     control=control, schedule=SCHEDULE)
        finally:
            for lane in lanes:
                await lane.runner.close()
                lane.log.close()

    outcome = asyncio.run(go())
    notifier.join(1)
    alarms = [e for e in notifier.events if e["level"] == "alarm"]
    return Both(outcome, open_at, admin.log(), app, driver, control, alarms, web_runner, android_runner)


def _one_final_alarm(out: Both, status: str, pattern: str) -> None:
    finals = [a for a in out.alarms if not a["event"].startswith("precheck")]
    assert [(a["event"], a["pattern"]) for a in finals] == [(status, pattern)], out.alarms


def _no_android_action_after(out: Both, t_ms: int) -> None:
    late = [e for e in out.app.events if e["kind"] in ANDROID_ACTIONS and e["t_server_ms"] >= t_ms]
    assert late == [], f"aksi Android setelah {t_ms}: {late}"


def _loser_aborted(out: Both, name: str) -> None:
    res = out.result(name)
    assert res.status == RunStatus.ABORTED, (res.status, res.message)
    assert "lock" in res.message, res.message


# ------------------------------------------------------------------ lock pemenang


def test_both_reach_checkout_live_exactly_one_place_order(mock, admin, tmp_path):
    """Kedua jalur normal sampai checkout di mode live: total tepat SATU request/klik "Buat Pesanan"."""
    out = run_both(mock, admin, tmp_path, live=True)
    assert out.orders() == 1, (out.web_kind("order"), out.app.kind("order"))
    winner = out.control.winner
    loser = "web" if winner == "android" else "android"
    assert out.result(winner).status == RunStatus.ORDER_PLACED_AWAIT_PIN
    _loser_aborted(out, loser)
    assert out.outcome.status == str(RunStatus.ORDER_PLACED_AWAIT_PIN) and out.outcome.exit_code == EXIT_OK
    assert out.outcome.winner == winner
    assert [c[:2] for c in out.control.gate_calls][0] == (winner, True)
    assert all(not ok for name, ok, _ in out.control.gate_calls if name == loser)
    _one_final_alarm(out, str(RunStatus.ORDER_PLACED_AWAIT_PIN), "urgent")


@pytest.mark.parametrize("winner", ["android", "web"])
@pytest.mark.parametrize("live", [False, True], ids=["dry", "live"])
def test_faster_lane_wins_other_aborted_before_click(mock, admin, tmp_path, winner, live):
    """Jalur yang lebih cepat memegang lock; jalur lain ABORTED sebelum klik "Buat Pesanan" (dry & live)."""
    slow = {"web": {"web": {"checkout_latency_ms": 2500}},
            "android": {"android": {"shipping_delay_ms": 3000}}}["web" if winner == "android" else "android"]
    out = run_both(mock, admin, tmp_path, live=live, **slow)
    loser = "web" if winner == "android" else "android"
    assert out.control.winner == winner
    expected = RunStatus.ORDER_PLACED_AWAIT_PIN if live else RunStatus.DRYRUN_OK
    assert out.result(winner).status == expected, out.result(winner).message
    _loser_aborted(out, loser)
    assert out.orders() == (1 if live else 0)
    if loser == "web":
        assert out.web_kind("order") == [] and "click_place_order" not in [s.name for s in out.result("web").steps]
    else:
        assert out.app.kind("order") == [] and out.android.order_clicked is False
    assert out.outcome.exit_code == EXIT_OK


class _UnknownAfterOrderApp(FakeShopeeApp):
    """'Buat Pesanan' tercatat, lalu layar tak dikenal (bukan PIN)."""

    def __init__(self, sc, clock):
        sc.unknown_after_order = True
        super().__init__(sc, clock)


def test_winner_falls_to_unknown_state_lock_never_released(mock, admin, tmp_path):
    """Pemenang (Android) mengambil lock, klik "Buat Pesanan", lalu UNKNOWN_STATE: lock tidak dilepas; web tidak
    pernah mengklik "Buat Pesanan"; status gabungan UNKNOWN_STATE (setelah order), alarm mendesak, exit 5."""
    out = run_both(mock, admin, tmp_path, live=True, web={"checkout_latency_ms": 2500}, app_cls=_UnknownAfterOrderApp)
    assert out.control.winner == "android"
    assert out.result("android").status == RunStatus.UNKNOWN_STATE
    assert out.result("android").message == MAYBE_ORDERED_MSG
    assert out.web_kind("order") == [] and out.orders() == 1
    _loser_aborted(out, "web")
    assert out.outcome.status == UNKNOWN_AFTER_ORDER and out.outcome.exit_code == EXIT_UNKNOWN
    _one_final_alarm(out, UNKNOWN_AFTER_ORDER, "urgent")


# ------------------------------------------------------------------ stop global


def test_captcha_on_web_stops_android_with_zero_taps_after_stop(mock, admin, tmp_path):
    """CAPTCHA di web (setelah klik Beli) -> event stop; Android (slot belum buka di sisi app, masih polling)
    berhenti tanpa satu pun tap/reload setelah event; status gabungan CAPTCHA, exit 4, satu alarm."""
    out = run_both(mock, admin, tmp_path, live=True, web_scenario="captcha",
                   android={"sale_skew_ms": 3000, "live_update": False})
    assert out.result("web").status == RunStatus.CAPTCHA
    assert out.control.stopped() and out.control.stop_source == "web"
    res = out.result("android")
    assert res.status == RunStatus.ABORTED and "stop global" in res.message, res.message
    _no_android_action_after(out, out.control.stop_ms)
    assert out.orders() == 0 and out.control.winner is None
    assert out.outcome.status == str(RunStatus.CAPTCHA) and out.outcome.exit_code == EXIT_MANUAL
    _one_final_alarm(out, str(RunStatus.CAPTCHA), "short")


class _SlowPinApp(FakeShopeeApp):
    """'Buat Pesanan' -> spinner 3 s -> layar PIN (server lambat)."""

    def _handle(self, key: str, node: Node) -> None:
        if key != "place_order":
            return super()._handle(key, node)
        self._event("order", self.payment_method)
        self.loading_until, self.after_loading, self.screen = self.now() + 3.0, "pin", "loading"


def test_winner_after_place_order_survives_other_lane_captcha(mock, admin, tmp_path):
    """Android menang dan sudah mengklik "Buat Pesanan" (PIN muncul 3 s kemudian); jawaban klik Beli web baru
    datang 1,5 s setelahnya berupa CAPTCHA -> stop global SETELAH klik pemenang: pemenang tetap sampai
    ORDER_PLACED_AWAIT_PIN; status gabungan ORDER_PLACED_AWAIT_PIN (prioritas tertinggi)."""
    out = run_both(mock, admin, tmp_path, live=True, web_scenario="captcha", web={"buy_latency_ms": 1500},
                   app_cls=_SlowPinApp)
    assert out.control.winner == "android"
    click = out.step_ms("android", "click_place_order")
    assert out.control.stopped() and out.control.stop_source == "web" and out.control.stop_ms > click
    assert out.result("android").status == RunStatus.ORDER_PLACED_AWAIT_PIN, out.result("android").message
    assert out.app.screen == "pin" and out.app.events[-1]["kind"] == "order", "PIN tidak disentuh alat"
    assert out.result("web").status == RunStatus.CAPTCHA and out.web_kind("order") == []
    assert out.outcome.status == str(RunStatus.ORDER_PLACED_AWAIT_PIN) and out.outcome.exit_code == EXIT_OK
    _one_final_alarm(out, str(RunStatus.ORDER_PLACED_AWAIT_PIN), "urgent")


def test_price_guard_on_one_lane_other_lane_continues_and_succeeds(mock, admin, tmp_path):
    """PRICE_GUARD (total checkout web > max_total) bukan status stop: Android tetap jalan dan sukses."""
    out = run_both(mock, admin, tmp_path, live=True, web={"shipping": 50_000})
    assert out.result("web").status == RunStatus.PRICE_GUARD, out.result("web").message
    assert not out.control.stopped()
    assert out.result("android").status == RunStatus.ORDER_PLACED_AWAIT_PIN
    assert out.control.winner == "android" and out.orders() == 1 and out.web_kind("order") == []
    assert out.outcome.status == str(RunStatus.ORDER_PLACED_AWAIT_PIN) and out.outcome.exit_code == EXIT_OK


# ------------------------------------------------------------------ precheck


def test_precheck_one_lane_fails_other_runs_alone_with_alarm(mock, admin, tmp_path):
    out = run_both(mock, admin, tmp_path, web_scenario="login_expired")
    web = out.lane("web")
    assert web.skipped and out.result("web").status == RunStatus.LOGIN_REQUIRED
    assert out.web_kind("buy") == [] and out.web_kind("product") == []
    assert out.result("android").status == RunStatus.DRYRUN_OK
    assert [a["event"] for a in out.alarms] == ["precheck_satu_platform", str(RunStatus.DRYRUN_OK)]
    assert "jalan dengan satu platform (android)" in out.alarms[0]["message"]
    assert out.outcome.exit_code == EXIT_OK


def test_precheck_both_fail_run_cancelled(mock, admin, tmp_path):
    out = run_both(mock, admin, tmp_path, web_scenario="login_expired", android={"installed": False})
    assert all(lane.skipped for lane in out.outcome.lanes)
    assert out.web_kind("buy") == [] and out.app.kind("buy") == [] and out.app.kind("intent") == []
    assert [a["event"] for a in out.alarms] == ["precheck"], "satu alarm, tanpa alarm hasil akhir tambahan"
    assert out.outcome.exit_code == EXIT_PRECHECK
    assert {lane.result.status for lane in out.outcome.lanes} == {RunStatus.LOGIN_REQUIRED, RunStatus.ERROR}
