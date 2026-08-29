"""`generate_image` -- ComfyUI on `.226`, behind admission control.

This is the only tool that takes tens of seconds and holds a GPU, and the GPU it
holds is the one someone may be coding on. Two rules from docs/03 shape
everything here:

**Ask the fleet controller first.** `.226` is shared, and answering "unavailable"
in milliseconds beats submitting a job and waiting three minutes for it to fail
(docs/14 section 4.4 rule 3). The precheck is a single cheap local call.

**An image request never preempts a coding session -- it queues.** The 30B coder
(~17 GB) and FLUX (~12 GB) cannot co-reside, so there is no version of "make room
for the picture" that does not interrupt someone mid-task. Image generation is the
least important capability in scope. Structurally: nothing in this module calls
`/fleet/hosts/{id}/reserve`, and there is no code path that can. It reads state
and either proceeds or declines.

`quality` is a step count, not a model. Which model is loaded is decided by free
VRAM, so a `model` argument would make the tool lie every time the ladder moved
(docs/14 section 2). The result reports which model actually ran, and says so when
that is the reduced rung -- an unexplained quality drop erodes trust faster than
an explained one.
"""

from __future__ import annotations

import contextlib
import copy
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx

from mcp_tools.tools import (
    Timer,
    ToolResult,
    log_tool_call,
    ok,
    refused,
    store_artifact,
    unavailable,
)

ASPECTS: dict[str, tuple[int, int]] = {
    "16:9": (1344, 768),
    "1:1": (1024, 1024),
    "4:3": (1152, 896),
}
"""A closed set. Dimensions are never numbers the model supplied."""

QUALITY_STEPS: dict[str, int] = {"fast": 4, "high": 8}
"""Sampling steps. FLUX.1-schnell is a few-step distilled model; more is not always better."""

MAX_PROMPT_CHARS = 1_000
POLL_INTERVAL_S = 2.0


@dataclass(frozen=True)
class Rung:
    """One admission rung for image work (docs/03, image generation section)."""

    name: str
    model_label: str
    min_free_gb: float
    reduced: bool


RUNGS: tuple[Rung, ...] = (
    # FLUX.1-schnell is Apache-2.0. FLUX.1-dev is NOT: it is non-commercial, and
    # deploying it would fail N2 and N1 at once (docs/15 section 4.2). The weights
    # are one download away, so the constraint is recorded next to the code that
    # would use them.
    Rung("flux-schnell", "FLUX.1-schnell", min_free_gb=15.0, reduced=False),
    Rung("sd35-medium", "SD3.5-medium", min_free_gb=9.0, reduced=True),
)


@dataclass(frozen=True)
class HostAdmission:
    """What the fleet controller says about the host, reduced to what we need."""

    reachable: bool
    state: str
    free_gb: float
    current_rung: str | None = None


class FleetUnavailable(RuntimeError):
    """The fleet controller did not answer. We decline rather than guess.

    Absence of evidence is not evidence of a free GPU -- the same reasoning that
    makes `HostState.UNKNOWN` load nothing in the controller's own ladder.
    """


class ComfyBusy(RuntimeError):
    """ComfyUI or our own queue is full. Retrying in a minute is reasonable."""


class ComfyUnavailable(RuntimeError):
    """ComfyUI is offline, misconfigured, or took longer than we will wait."""


# ------------------------------------------------------------------- admission


def admit(host: HostAdmission) -> tuple[Rung | None, str]:
    """Pick the rung `.226` can hold right now, or explain why none.

    Pure and free of I/O so the bands can be tested exhaustively -- the same
    reason `fleet_controller.ladder` is written that way, and the same class of
    bug it prevents.
    """
    if not host.reachable or host.state == "unknown":
        return None, "unreachable"
    if host.state == "yielding":
        return None, "claimed"
    for rung in RUNGS:
        if host.free_gb >= rung.min_free_gb:
            return rung, "ok"
    return None, "queued"


_DECLINE_DETAIL = {
    # The wording is the point (docs/14 section 4.4 rule 4). "Host in use" tells
    # the model to stop retrying; "queued" tells it to come back. Which sentence
    # we send changes what the agent does next, so it is chosen, not typed.
    "unreachable": (
        "Image generation is unavailable: the fleet controller cannot see host .226, "
        "so we assume it is in use. Try again later."
    ),
    "claimed": (
        "Image generation is unavailable: host .226 is in use by its owner. Try again later."
    ),
    "queued": (
        "Image generation is waiting for the GPU, which is busy with a coding session. "
        "Nothing was interrupted to make room. Try again in a few minutes."
    ),
}


class FleetBackend:
    """`GET /fleet/status`, reduced to one host's admission state."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = client

    async def host_state(self, host_id: str) -> HostAdmission:
        try:
            body = await _get_json(
                self._client, f"{self._base_url}/fleet/status", timeout_s=self._timeout_s
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise FleetUnavailable(str(exc)) from exc

        for host in body.get("hosts") or []:
            if str(host.get("id")) != host_id:
                continue
            gpu = host.get("gpu") or {}
            rung = host.get("rung") or {}
            return HostAdmission(
                reachable=bool(host.get("reachable", False)),
                state=str(host.get("state", "unknown")),
                free_gb=float(gpu.get("vram_free_mb", 0)) / 1024.0,
                current_rung=rung.get("id"),
            )
        raise FleetUnavailable(f"host '{host_id}' is not in the fleet status")


# --------------------------------------------------------------------- workflow

# Node ids belong to *your* exported graph, not to ours. Re-exporting a workflow
# after editing it in the ComfyUI UI can renumber nodes, and a silently
# mis-patched graph produces a plausible image with the wrong settings -- the
# worst kind of bug, because it does not look like one (docs/15 section 4.4).
# Keep this mapping beside the workflow files and check it after every re-export.
NODE_POSITIVE_PROMPT = "6"  # CLIPTextEncode  -> inputs.text
NODE_EMPTY_LATENT = "5"  # EmptyLatentImage -> inputs.width / inputs.height
NODE_SAMPLER = "3"  # KSampler         -> inputs.seed / inputs.steps


def build_workflow(
    rung: Rung,
    *,
    prompt: str,
    aspect: str,
    seed: int,
    steps: int,
    workflow_dir: Path | None,
) -> dict[str, Any]:
    """Load the rung's API-format workflow and patch it by node id.

    We never generate the graph. A model asked to emit a node graph with integer
    ids produces something that parses about as often as it does not, and the
    failure lands at render time after the user has already waited.
    """
    if workflow_dir is None:
        raise ComfyUnavailable(
            "no ComfyUI workflow directory is configured; export the API-format "
            "workflows from ComfyUI and set COMFY_WORKFLOW_DIR"
        )
    path = workflow_dir / f"{rung.name}.json"
    if not path.is_file():
        raise ComfyUnavailable(f"no workflow for rung '{rung.name}' in {workflow_dir}")
    try:
        workflow: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ComfyUnavailable(f"workflow '{path.name}' is unreadable: {exc}") from exc

    patched = copy.deepcopy(workflow)
    width, height = ASPECTS[aspect]
    for node_id in (NODE_POSITIVE_PROMPT, NODE_EMPTY_LATENT, NODE_SAMPLER):
        if node_id not in patched or "inputs" not in patched[node_id]:
            raise ComfyUnavailable(
                f"workflow '{path.name}' has no node '{node_id}'; re-export it and "
                "check the node-id mapping in mcp_tools/tools/image.py"
            )
    patched[NODE_POSITIVE_PROMPT]["inputs"]["text"] = prompt
    patched[NODE_EMPTY_LATENT]["inputs"]["width"] = width
    patched[NODE_EMPTY_LATENT]["inputs"]["height"] = height
    patched[NODE_SAMPLER]["inputs"]["seed"] = seed
    if "steps" in patched[NODE_SAMPLER]["inputs"]:
        patched[NODE_SAMPLER]["inputs"]["steps"] = steps
    return patched


# ---------------------------------------------------------------------- ComfyUI


class ComfyBackend:
    """ComfyUI's HTTP API: submit, poll, fetch, and cancel if we give up.

    The endpoint shapes have been stable for a while but remain unversioned --
    verify them against the build you install.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = client

    async def render(self, workflow: dict[str, Any]) -> bytes:
        prompt_id = await self._submit(workflow)
        outputs = await self._await_outputs(prompt_id)
        return await self._fetch(outputs)

    async def _submit(self, workflow: dict[str, Any]) -> str:
        try:
            body = await _post_json(
                self._client,
                f"{self._base_url}/prompt",
                payload={"prompt": workflow, "client_id": "mcp-tools"},
                timeout_s=min(self._timeout_s, 30.0),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise ComfyBusy("ComfyUI rejected the job: its queue is full") from exc
            raise ComfyUnavailable(str(exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ComfyUnavailable(str(exc)) from exc
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise ComfyUnavailable("ComfyUI accepted the job but returned no prompt_id")
        return str(prompt_id)

    async def _await_outputs(self, prompt_id: str) -> dict[str, Any]:
        """Poll `/history` until the job lands or the deadline passes.

        Polling rather than the websocket: simpler, and sufficient unless we want
        a progress figure in the UI. On timeout we interrupt rather than orphan
        the job, so the host is not still working for us after we gave up.
        """
        deadline = time.monotonic() + self._timeout_s
        while time.monotonic() < deadline:
            try:
                body = await _get_json(
                    self._client,
                    f"{self._base_url}/history/{prompt_id}",
                    timeout_s=min(self._timeout_s, 30.0),
                )
            except (httpx.HTTPError, ValueError) as exc:
                raise ComfyUnavailable(str(exc)) from exc
            entry = body.get(prompt_id)
            if entry and entry.get("outputs"):
                outputs: dict[str, Any] = entry["outputs"]
                return outputs
            await anyio.sleep(POLL_INTERVAL_S)
        await self._interrupt()
        raise ComfyUnavailable(f"no image after {self._timeout_s:.0f}s; the job was cancelled")

    async def _interrupt(self) -> None:
        # Best effort. We are already failing the request; a failed cancel must
        # not turn into a second, more confusing error.
        with contextlib.suppress(httpx.HTTPError, ValueError):
            await _post_json(self._client, f"{self._base_url}/interrupt", payload={}, timeout_s=5.0)

    async def _fetch(self, outputs: dict[str, Any]) -> bytes:
        for node_output in outputs.values():
            for image in node_output.get("images") or []:
                params = {
                    "filename": image.get("filename", ""),
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                }
                try:
                    return await _get_bytes(
                        self._client,
                        f"{self._base_url}/view",
                        params=params,
                        timeout_s=min(self._timeout_s, 60.0),
                    )
                except httpx.HTTPError as exc:
                    raise ComfyUnavailable(f"could not fetch the rendered image: {exc}") from exc
        raise ComfyUnavailable("the job finished but produced no image")


# -------------------------------------------------------------------- our queue


class ImageQueue:
    """One job at a time from the platform, with a shallow waiting line.

    ComfyUI has its own queue; this is a semaphore on our side so a chatty agent
    cannot enqueue twenty jobs and wedge the host for its owner (docs/15
    section 4.5). Beyond `max_pending` waiting callers we answer "busy"
    immediately rather than holding a request open.
    """

    def __init__(self, *, max_pending: int = 2) -> None:
        self._lock = anyio.Semaphore(1)
        self._max_pending = max_pending
        self._pending = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._pending >= self._max_pending:
            raise ComfyBusy("too many image jobs are already waiting")
        self._pending += 1
        try:
            async with self._lock:
                yield
        finally:
            self._pending -= 1


# ------------------------------------------------------------------ HTTP helpers


async def _get_json(
    client: httpx.AsyncClient | None, url: str, *, timeout_s: float
) -> dict[str, Any]:
    if client is not None:
        response = await client.get(url, timeout=timeout_s)
    else:
        async with httpx.AsyncClient(timeout=timeout_s) as owned:
            response = await owned.get(url)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


async def _post_json(
    client: httpx.AsyncClient | None, url: str, *, payload: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    if client is not None:
        response = await client.post(url, json=payload, timeout=timeout_s)
    else:
        async with httpx.AsyncClient(timeout=timeout_s) as owned:
            response = await owned.post(url, json=payload)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


async def _get_bytes(
    client: httpx.AsyncClient | None, url: str, *, params: dict[str, str], timeout_s: float
) -> bytes:
    if client is not None:
        response = await client.get(url, params=params, timeout=timeout_s)
    else:
        async with httpx.AsyncClient(timeout=timeout_s) as owned:
            response = await owned.get(url, params=params)
    response.raise_for_status()
    return response.content


# ------------------------------------------------------------------------- tool


async def generate_image(
    prompt: str,
    aspect: str,
    quality: str,
    *,
    fleet: FleetBackend,
    comfy: ComfyBackend,
    queue: ImageQueue,
    host_id: str,
    workflow_dir: Path | None,
    artifact_dir: Path,
    artifact_base_url: str,
    seed: int | None = None,
) -> ToolResult:
    """Precheck, admit, render, store. Never raises, never preempts."""
    timer = Timer()
    prompt = prompt.strip()
    if not prompt:
        return refused("generate_image", "Describe the image you want.")
    if len(prompt) > MAX_PROMPT_CHARS:
        return refused("generate_image", f"Prompt is longer than {MAX_PROMPT_CHARS} characters.")
    # Validated server-side with the valid values in the message, rather than as
    # an enum in the schema that every prompt would pay for.
    if aspect not in ASPECTS:
        return refused("generate_image", f"Unknown aspect '{aspect}'. Valid: {', '.join(ASPECTS)}.")
    if quality not in QUALITY_STEPS:
        return refused(
            "generate_image", f"Unknown quality '{quality}'. Valid: {', '.join(QUALITY_STEPS)}."
        )

    try:
        host = await fleet.host_state(host_id)
    except FleetUnavailable as exc:
        log_tool_call("generate_image", outcome="fleet_unavailable", duration_ms=timer.ms)
        return unavailable("fleet-controller", f"{_DECLINE_DETAIL['unreachable']} ({exc})")

    rung, reason = admit(host)
    if rung is None:
        log_tool_call(
            "generate_image",
            outcome=f"declined_{reason}",
            duration_ms=timer.ms,
            free_gb=round(host.free_gb, 1),
        )
        return unavailable("comfyui", _DECLINE_DETAIL[reason])

    try:
        workflow = build_workflow(
            rung,
            prompt=prompt,
            aspect=aspect,
            seed=seed if seed is not None else int(time.time_ns() % 2**31),
            steps=QUALITY_STEPS[quality],
            workflow_dir=workflow_dir,
        )
    except ComfyUnavailable as exc:
        log_tool_call("generate_image", outcome="workflow_error", duration_ms=timer.ms)
        return unavailable("comfyui", f"Image generation is misconfigured: {exc}")

    try:
        async with queue.slot():
            data = await comfy.render(workflow)
    except ComfyBusy as exc:
        log_tool_call("generate_image", outcome="busy", duration_ms=timer.ms)
        return unavailable(
            "comfyui", f"Image generation is temporarily busy ({exc}); try again in a minute."
        )
    except ComfyUnavailable as exc:
        log_tool_call("generate_image", outcome="unavailable", duration_ms=timer.ms)
        return unavailable("comfyui", f"Image generation is offline: {exc}")

    artifact = store_artifact(
        data,
        suffix=".png",
        filename=prompt[:60],
        directory=artifact_dir,
        base_url=artifact_base_url,
    )
    log_tool_call(
        "generate_image",
        outcome="ok",
        duration_ms=timer.ms,
        model=rung.model_label,
        free_gb=round(host.free_gb, 1),
    )
    result = ok(**artifact, model=rung.model_label)
    if rung.reduced:
        # Say why the picture is worse than last time. The alternative is a user
        # who concludes the platform got worse and never asks.
        result["note"] = (
            f"Reduced quality: host .226 is partly in use, so {rung.model_label} was used "
            "instead of FLUX.1-schnell."
        )
    return result
