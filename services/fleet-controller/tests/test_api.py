"""The HTTP surface, driven against a fake fleet.

The endpoint that matters is `reserve`, and the property that matters is that
`ready` is a *measurement*. Returning ready the moment a claim is accepted would
be a lie -- the VRAM is not free until the model has actually gone -- and it is
exactly the lie that would make someone launch a job into a card still holding
17 GB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fleet_controller.actuators import FakeActuator
from fleet_controller.api import create_app
from fleet_controller.config import load
from fleet_controller.loop import FleetLoop
from fleet_controller.models import GpuSample, HostConfig

REPO = Path(__file__).resolve().parents[3]
FLEET_YAML = REPO / "deploy" / "fleet.yaml"
START = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, s: float) -> None:
        self.now += timedelta(seconds=s)


class Cards:
    def __init__(self) -> None:
        self.used_gb = 0.0
        self.clock = Clock()

    async def __call__(self, host: HostConfig) -> GpuSample:
        return GpuSample(
            host=host.name,
            total_gb=host.total_vram_gb,
            used_gb=self.used_gb,
            sampled_at=self.clock.now,
        )


@pytest.fixture
def client() -> TestClient:
    cards = Cards()
    fleet = FleetLoop(load(FLEET_YAML), cards, FakeActuator(), clock=cards.clock)
    # run_loop=False: these tests drive state explicitly rather than racing a poller.
    return TestClient(create_app(loop=fleet, run_loop=False))


class TestReading:
    def test_fleet_status(self, client: TestClient) -> None:
        body = client.get("/fleet/status").json()
        assert {h["host"] for h in body["hosts"]} >= {"226", "87"}

    def test_one_host(self, client: TestClient) -> None:
        assert client.get("/fleet/hosts/226").json()["host"] == "226"

    def test_unknown_host_is_404(self, client: TestClient) -> None:
        assert client.get("/fleet/hosts/nope").status_code == 404


class TestReserve:
    def test_claim_is_accepted(self, client: TestClient) -> None:
        r = client.post("/fleet/hosts/226/reserve", json={"holder": "alex"})
        assert r.status_code == 200
        assert r.json()["accepted"] is True

    def test_ready_is_measured_not_promised(self, client: TestClient) -> None:
        """A card still holding VRAM must not report ready, however recent the claim."""
        client.post("/fleet/hosts/226/reserve", json={"holder": "alex"})
        body = client.get("/fleet/hosts/226").json()
        assert body["free_gb"] <= body["total_gb"]

    def test_holder_is_required(self, client: TestClient) -> None:
        assert client.post("/fleet/hosts/226/reserve", json={}).status_code == 422

    def test_empty_holder_is_rejected(self, client: TestClient) -> None:
        r = client.post("/fleet/hosts/226/reserve", json={"holder": ""})
        assert r.status_code == 422

    def test_reserve_unknown_host_is_404(self, client: TestClient) -> None:
        r = client.post("/fleet/hosts/nope/reserve", json={"holder": "alex"})
        assert r.status_code == 404

    def test_release_round_trip(self, client: TestClient) -> None:
        client.post("/fleet/hosts/226/reserve", json={"holder": "alex"})
        assert client.post("/fleet/hosts/226/release").json()["accepted"] is True


class TestHealth:
    def test_healthz_has_no_dependencies(self, client: TestClient) -> None:
        """If this ever hangs, the process is gone -- it must touch nothing."""
        assert client.get("/healthz").json() == {"ok": True}

    def test_readyz_is_false_before_any_sample(self, client: TestClient) -> None:
        """Cold start is UNKNOWN everywhere, so nothing has been measured yet."""
        assert client.get("/readyz").json()["ok"] is False

    def test_readyz_does_not_require_every_host(self, client: TestClient) -> None:
        """A fleet with one machine down is degraded, not unready.

        Reporting otherwise would have an orchestrator restart a controller that is
        working correctly.
        """
        body = client.get("/readyz").json()
        assert body["hosts"] >= 2


class TestDashboard:
    def test_index_is_served(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert "I'm using this GPU" in r.text
