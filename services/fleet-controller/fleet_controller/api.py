"""HTTP surface for the fleet controller.

Endpoints are per docs/08 section 9. The design constraint that shapes all of them:
**this service must never become the reason a host stays occupied.** So every
handler is cheap, nothing blocks on the control loop, and `/healthz` touches no
dependency at all.

Auth is deliberately absent. M2 is cooperative, not adversarial (docs/03 section 6)
-- a colleague who wants to defeat this can, trivially, and that is the right
amount of engineering for a handful of people sharing a handful of machines.
Anything stronger belongs behind Caddy, not in here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from fleet_controller.actuators import HttpActuator
from fleet_controller.config import FleetConfig, load
from fleet_controller.loop import FleetLoop
from fleet_controller.models import HostState, HostStatus
from fleet_controller.remote import RemoteSampler

_log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
DEFAULT_CONFIG = Path(os.environ.get("FLEET_CONFIG", "/etc/understudy/fleet.yaml"))


class ReserveRequest(BaseModel):
    holder: str = Field(min_length=1, max_length=64, description="Who is claiming it")


class ReserveResponse(BaseModel):
    """202-and-poll. `ready` is a measured fact, never an acknowledgement.

    Returning `ready: true` the instant a claim is accepted would be a lie: the
    VRAM is not free until the model has actually gone. The whole value of the
    toggle is that its status line reports what is, not what was requested.
    """

    host: str
    accepted: bool
    ready: bool
    state: HostState
    free_gb: float
    detail: str
    poll_after_s: float = 1.0


class FleetStatus(BaseModel):
    hosts: list[HostStatus]
    at: datetime


def create_app(
    config: FleetConfig | None = None,
    *,
    loop: FleetLoop | None = None,
    run_loop: bool = True,
) -> FastAPI:
    """Build the app.

    `loop` is injectable so tests drive a fake fleet without HTTP or a GPU, and
    `run_loop=False` keeps the background task out of those tests.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = config or load(DEFAULT_CONFIG)
        client = httpx.AsyncClient()
        fleet = loop or FleetLoop(cfg, RemoteSampler(client), HttpActuator(client))
        app.state.fleet = fleet
        task: asyncio.Task[None] | None = None
        if run_loop:
            task = asyncio.create_task(fleet.run(), name="fleet-loop")
        try:
            yield
        finally:
            if task is not None:
                await fleet.stop()
            await client.aclose()

    app = FastAPI(title="Understudy fleet controller", version="0.1.0", lifespan=lifespan)
    if loop is not None:
        app.state.fleet = loop

    def fleet_of(request: Request) -> FleetLoop:
        f: FleetLoop | None = getattr(request.app.state, "fleet", None)
        if f is None:  # pragma: no cover -- only before startup
            raise HTTPException(503, "controller is still starting")
        return f

    def status_or_404(fleet: FleetLoop, host: str) -> HostStatus:
        s = fleet.status(host)
        if s is None:
            raise HTTPException(404, f"unknown host {host!r}")
        return s

    # ----------------------------------------------------------------- reading

    @app.get("/fleet/status", response_model=FleetStatus)
    def fleet_status(request: Request) -> FleetStatus:
        """Everything the dashboard needs, in one document."""
        return FleetStatus(hosts=fleet_of(request).statuses(), at=datetime.now(UTC))

    @app.get("/fleet/hosts/{host}", response_model=HostStatus)
    def host_status(host: str, request: Request) -> HostStatus:
        """One host. This is what `gpu-run` polls until it sees the VRAM released."""
        return status_or_404(fleet_of(request), host)

    # ---------------------------------------------------------------- commands

    @app.post("/fleet/hosts/{host}/reserve", response_model=ReserveResponse)
    async def reserve(host: str, body: ReserveRequest, request: Request) -> ReserveResponse:
        fleet = fleet_of(request)
        decision = await fleet.reserve(host, body.holder)
        if decision is None:
            raise HTTPException(404, f"unknown host {host!r}")
        s = status_or_404(fleet, host)
        return ReserveResponse(
            host=host,
            accepted=True,
            # Only a measurement can say ready. Anything else is a promise.
            ready=s.free_gb >= s.total_gb - 1.0,
            state=s.state,
            free_gb=s.free_gb,
            detail=decision.detail,
        )

    @app.post("/fleet/hosts/{host}/release", response_model=ReserveResponse)
    async def release(host: str, request: Request) -> ReserveResponse:
        fleet = fleet_of(request)
        decision = await fleet.release(host)
        if decision is None:
            raise HTTPException(404, f"unknown host {host!r}")
        s = status_or_404(fleet, host)
        return ReserveResponse(
            host=host,
            accepted=True,
            ready=False,
            state=s.state,
            free_gb=s.free_gb,
            detail=decision.detail,
        )

    # ------------------------------------------------------------------ events

    @app.get("/fleet/events")
    async def events(request: Request) -> StreamingResponse:
        """SSE of state changes, so the dashboard does not poll."""
        fleet = fleet_of(request)
        queue = fleet.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        decision = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"  # keeps proxies from closing the stream
                        continue
                    payload = {
                        "host": decision.host,
                        "state": decision.state,
                        "rung": decision.rung.name if decision.rung else None,
                        "reason": decision.reason,
                        "detail": decision.detail,
                        "emergency": decision.emergency,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
            finally:
                fleet.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ------------------------------------------------------------------ health

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Liveness. No dependencies, never blocks -- if this hangs, the process is gone."""
        return {"ok": True}

    @app.get("/readyz")
    def readyz(request: Request) -> dict[str, Any]:
        """Ready once any host has been sampled.

        Not *all* hosts: a fleet with one machine down is degraded, not unready, and
        reporting otherwise would have an orchestrator restart a controller that is
        working correctly.
        """
        statuses = fleet_of(request).statuses()
        seen = [s for s in statuses if s.state is not HostState.UNKNOWN]
        return {"ok": bool(seen), "hosts": len(statuses), "sampled": len(seen)}

    # --------------------------------------------------------------- dashboard

    @app.get("/")
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
