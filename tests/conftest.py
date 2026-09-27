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
