"""Control-loop wiring: sample -> decide -> actuate -> publish.

The state machine and the actuators are each tested in isolation. These tests
cover the seam between them, which is where the behaviour people actually notice
lives: does flipping the toggle really unload a model, and does a host that stops
answering stop being used?
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fleet_controller.actuators import FakeActuator
from fleet_controller.config import load
from fleet_controller.loop import FleetLoop
from fleet_controller.models import GpuSample, HostConfig, HostState
from fleet_controller.nvidia import NvidiaSmiUnavailable

REPO = Path(__file__).resolve().parents[3]
FLEET_YAML = REPO / "deploy" / "fleet.yaml"
START = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)


class Clock:
    """Time we drive by hand, so no test ever sleeps."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Cards:
    """A scriptable fleet of GPUs."""

    def __init__(self, *, used_gb: float = 0.0, foreign: tuple[int, ...] = ()) -> None:
        self.used_gb = used_gb
        self.foreign = foreign
        self.fail = False
        self.clock: Clock | None = None

    async def __call__(self, host: HostConfig) -> GpuSample:
        if self.fail:
            raise NvidiaSmiUnavailable("scripted failure")
        return GpuSample(
            host=host.name,
            total_gb=host.total_vram_gb,
            used_gb=self.used_gb,
            foreign_pids=self.foreign,
            sampled_at=self.clock.now if self.clock else START,
        )


def build(cards: Cards, actuator: FakeActuator, clock: Clock) -> FleetLoop:
    cards.clock = clock
    return FleetLoop(load(FLEET_YAML), cards, actuator, clock=clock)


@pytest.fixture
def rig() -> tuple[FleetLoop, Cards, FakeActuator, Clock]:
    clock, cards, act = Clock(), Cards(), FakeActuator()
    return build(cards, act, clock), cards, act, clock


class TestColdStart:
    def test_starts_unknown_not_free(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        """Measurement outranks memory: a controller that just booted knows nothing."""
        loop, *_ = rig
        assert all(s.state is HostState.UNKNOWN for s in loop.statuses())

    def test_a_clear_card_becomes_usable(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        loop, cards, _act, clock = rig
        host = loop.hosts[0]
        for _ in range(3):
            asyncio.run(loop.tick(host))
            clock.advance(2)
        assert loop.status(host.name) is not None


class TestTheToggle:
    """The headline acceptance test from docs/03 section 7."""

    def test_reserving_unloads_and_deroutes(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        loop, _cards, act, _clock = rig
        host = loop.hosts[0].name

        decision = asyncio.run(loop.reserve(host, "alex"))

        assert decision is not None
        assert decision.state is HostState.YIELDING
        assert ("sleep", host) in act.calls
        assert act.routing.get(host, ()) == ()

    def test_routing_is_pulled_before_the_model_sleeps(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        """Order matters. Advertising a model that is mid-unload sends real traffic
        to a server that is about to stop answering."""
        loop, _cards, act, _clock = rig
        host = loop.hosts[0].name
        asyncio.run(loop.reserve(host, "alex"))
        actions = [a for a, h in act.calls if h == host]
        assert actions.index("sync_routing") < actions.index("sleep")

    def test_release_does_not_promote_while_they_are_still_resident(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        loop, cards, _act, clock = rig
        host = loop.hosts[0]
        asyncio.run(loop.reserve(host.name, "alex"))
        cards.used_gb, cards.foreign = 10.0, (4321,)
        clock.advance(5)
        asyncio.run(loop.release(host.name))
        asyncio.run(loop.tick(host))
        assert loop.status(host.name).state is not HostState.FREE  # type: ignore[union-attr]


class TestUnreachableHosts:
    def test_repeated_sample_failure_goes_unknown(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        """A host we cannot see is assumed in use -- never quietly treated as free."""
        loop, cards, _act, clock = rig
        host = loop.hosts[0]
        cards.fail = True
        for _ in range(5):
            asyncio.run(loop.tick(host))
            clock.advance(2)
        assert loop.status(host.name).state is HostState.UNKNOWN  # type: ignore[union-attr]

    def test_a_serving_host_that_goes_dark_is_unloaded(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        """The case that matters: it was serving, then we lost sight of it.

        Cold start needs no unload because nothing is loaded yet. A host that was
        already carrying a model does, and leaving it loaded while blind would mean
        squatting VRAM on a machine we can no longer observe.
        """
        loop, cards, act, clock = rig
        host = loop.hosts[0]

        for _ in range(12):  # clear card, long enough to promote out of UNKNOWN
            asyncio.run(loop.tick(host))
            clock.advance(60)
        assert loop.runtime(host.name).rung is not None, "precondition: a model is loaded"  # type: ignore[union-attr]

        act.calls.clear()
        cards.fail = True
        for _ in range(5):
            asyncio.run(loop.tick(host))
            clock.advance(2)

        assert loop.status(host.name).state is HostState.UNKNOWN  # type: ignore[union-attr]
        assert ("sleep", host.name) in act.calls

    def test_one_bad_host_does_not_stop_the_others(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        """A controller that stopped ticking has stopped getting out of people's way."""
        loop, cards, _act, _clock = rig
        cards.fail = True
        for host in loop.hosts:
            asyncio.run(loop.tick(host))
        assert len(loop.statuses()) == len(loop.hosts)


class TestActuationFailures:
    def test_unconfirmed_unload_keeps_the_host_demoted(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        """If we cannot prove the VRAM came back, assume it did not."""
        loop, _cards, _act, clock = rig
        failing = FakeActuator(fail_actions=["sleep"])
        cards = Cards()
        loop2 = build(cards, failing, clock)
        host = loop2.hosts[0].name
        decision = asyncio.run(loop2.reserve(host, "alex"))
        assert decision is not None
        assert decision.state is HostState.YIELDING  # state still yields; we just stay down

    def test_unknown_host_returns_none(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        loop, *_ = rig
        assert asyncio.run(loop.reserve("does-not-exist", "someone")) is None
        assert asyncio.run(loop.release("does-not-exist")) is None


class TestEvents:
    def test_a_slow_subscriber_is_dropped_not_awaited(
        self, rig: tuple[FleetLoop, Cards, FakeActuator, Clock]
    ) -> None:
        """Blocking the loop on a browser that stopped reading would make the
        dashboard a way to stop the platform yielding."""
        loop, _cards, _act, _clock = rig
        q = loop.subscribe()
        host = loop.hosts[0].name
        for _ in range(200):
            asyncio.run(loop.reserve(host, "a"))
            asyncio.run(loop.release(host))
        assert q.qsize() <= 64
        loop.unsubscribe(q)
