import asyncio

from telemetry_nerd.sources.gate import Gate


async def test_concurrency_is_bounded():
    gate = Gate(max_concurrency=2)
    active = peak = 0

    async def work():
        nonlocal active, peak
        async with gate.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(work() for _ in range(6)))
    assert peak == 2


async def test_min_interval_spaces_request_starts():
    now = [100.0]
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    gate = Gate(max_concurrency=4, min_interval_ms=500, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(3):
        async with gate.slot():
            pass
    assert slept == [0.5, 0.5]


async def test_zero_interval_never_sleeps():
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    gate = Gate(min_interval_ms=0, sleep=fake_sleep)
    for _ in range(3):
        async with gate.slot():
            pass
    assert slept == []
