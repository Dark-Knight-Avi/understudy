"""Actuation: the half of the controller that makes a decision real.

`ladder.py` decides what *should* be loaded on a host. This module is what
actually reaches out and does it, over HTTP, to four surfaces:

    1. vLLM sleep / wake      -- yield the card and take it back, same model
    2. Rung change            -- load a DIFFERENT model: a server restart
    3. LiteLLM routing        -- tell the gateway what is currently answerable
    4. ComfyUI admission      -- "is there room for this image job right now?"

Four rules govern everything here, and each exists because of a specific failure
this platform must not have.

**Nothing raises into the control loop.** Every public method returns an
`ActuationResult`. The loop in docs/08 section 2 runs every ~2 s per host; an
exception escaping step 5 would kill the task that is supposed to be protecting
somebody's GPU. A failed actuation is data, not an event.

**Failure degrades toward yielding VRAM.** If we cannot *confirm* that a model
was unloaded, we assume it is still loaded and keep the host demoted -- see
`ActuationResult.vram_released` and `keep_demoted`. Erring toward giving the
person at the machine more VRAM is always the safe direction; erring the other
way is how the platform becomes the reason an eight-hour run died.

**Every call has an explicit timeout.** A hung actuator must not stall the loop.
For the same reason timeouts are *not* retried by default: burning three
deadlines inside one loop iteration is the stall we are trying to avoid, and the
loop comes back in ~2 s anyway.

**Every path is configurable.** vLLM's sleep endpoints, LiteLLM's
model-management routes and ComfyUI's stats route have all moved between
releases. Hardcoding them means the next upgrade looks like a controller bug.
The defaults below are documented with their source, and a 404 is reported as a
*version difference on the target*, in those words, so nobody goes hunting
through this package for it.

Two orderings this module deliberately does NOT own, because they belong to the
control loop:

  * Stop routing before sleeping (docs/07 section 8) -- in-flight requests are
    lost at sleep time, so `sync_routing` runs before `sleep`.
  * Confirm with `nvidia-smi`, not with an HTTP 200 (docs/08 section 6.1). The
    invariant is "VRAM is free", not "the API said OK". `vram_released=True`
    here means only that the server accepted the instruction; the poller's next
    sample is what may promote the dashboard to `ready`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field, SecretStr

from fleet_controller.ladder import select_rung
from fleet_controller.models import HostConfig, HostState, Rung

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- results


class ActuationOutcome(StrEnum):
    """How an actuation ended. Only OK and ALREADY count as success."""

    OK = "ok"
    """The target accepted the instruction."""

    ALREADY = "already"
    """Nothing to do -- it was already in the requested state.

    Sleeping a sleeping server and waking an awake one succeed quietly, because
    the control loop reconciles on every pass and will replay both constantly.
    """

    UNSUPPORTED = "unsupported"
    """404: the endpoint is not where we asked. A version difference on the
    target, not a fault in this package. See `_version_note`."""

    TIMEOUT = "timeout"
    """The deadline expired. Not retried by default -- see the module docstring."""

    UNREACHABLE = "unreachable"
    """Transport failure: host down, WSL stopped, subnet partitioned."""

    FAILED = "failed"
    """The target answered, and answered no."""


class ActuationResult(BaseModel):
    """The outcome of one actuation. Returned, never raised."""

    model_config = {"frozen": True}

    action: str
    host: str
    outcome: ActuationOutcome
    detail: str = ""
    attempts: int = 0
    elapsed_s: float = 0.0
    vram_released: bool = Field(
        default=False,
        description=(
            "True only when we have positive evidence the host gave VRAM back. "
            "Unknown counts as False: if we cannot confirm an unload we assume "
            "the model is still resident and keep the host demoted."
        ),
    )

    @property
    def ok(self) -> bool:
        return self.outcome in (ActuationOutcome.OK, ActuationOutcome.ALREADY)

    @property
    def version_mismatch(self) -> bool:
        """True when the failure is 'the target is a different version'."""
        return self.outcome is ActuationOutcome.UNSUPPORTED


def keep_demoted(result: ActuationResult) -> bool:
    """Should the host stay demoted -- out of routing, never promoted -- after this?

    Yes, unless we positively confirmed the VRAM came back. Absence of evidence
    is not evidence of a free GPU; this is the same rule that makes
    `HostState.UNKNOWN` behave pessimistically (docs/03 section 2).
    """
    return not result.vram_released


class AdmissionAnswer(BaseModel):
    """Answer to "is there room for this image job right now?" (docs/03 section 3).

    Carries the underlying `ActuationResult` so a failed query is still a value
    the caller can inspect rather than an exception it has to catch.
    """

    model_config = {"frozen": True}

    admitted: bool
    rung: Rung | None = None
    free_gb: float | None = None
    reason: str = ""
    result: ActuationResult


# --------------------------------------------------------------------------- config


class VllmEndpoints(BaseModel):
    """Where vLLM's sleep/wake endpoints live, and how they take their level.

    **These defaults are a starting point, not a fact about your server.** vLLM's
    sleep endpoints are *development* endpoints: they exist only when the server
    is started with `--enable-sleep-mode` AND `VLLM_SERVER_DEV_MODE=1` in its
    environment (docs/07 section 4). Across releases the paths have moved, the
    level has been passed both as a query parameter and as a JSON body -- docs/07
    section 8 shows `POST /sleep?level=1` while docs/08 section 6.1 shows
    `-d '{"level": 1}'`, which is exactly the drift this config exists to absorb
    -- and the meaning of the levels themselves has changed.

    So: verify against the version you pinned (M0 spike 6 exists to establish
    this), set these fields, and never hardcode a path at a call site. A 404 from
    any of them is reported as a version difference on the target.

    Level semantics as documented at the time of writing (docs/07 section 8):
      * level 1 -- weights offloaded to system RAM, KV cache discarded. Wake in
        seconds. The default, and the whole point of .226's 256 GB.
      * level 2 -- weights discarded entirely. Cold reload from NVMe on wake.
    """

    model_config = {"frozen": True}

    sleep_path: str = "/sleep"
    wake_path: str = "/wake_up"
    is_sleeping_path: str | None = "/is_sleeping"
    """Optional. Set to None on versions that do not expose it; idempotency then
    falls back to re-probing after a rejected call, which costs one round trip."""

    sleep_level: int = 1
    level_in_query: bool = True
    """True: `POST /sleep?level=1`. False: `POST /sleep` with `{"level": 1}`."""

    api_key: SecretStr | None = None
    """vLLM starts with `--api-key` so a stray LAN process cannot drive the GPUs
    (docs/06 section 8). The dev endpoints sit behind the same server."""


class AgentEndpoints(BaseModel):
    """The per-host agent's fixed verb list (docs/08 sections 7.1 and 6.4).

    A rung change is a *server restart*, which is the agent's job rather than
    vLLM's: no running process can be told to hold a different model.

    The verb list is fixed on purpose. The moment this can run arbitrary commands
    on somebody's workstation, the platform stops being a thing people accept on
    their machine (docs/08 section 6.4). Paths are configurable; the vocabulary
    is not.
    """

    model_config = {"frozen": True}

    restart_path: str = "/actuate/restart"
    stop_path: str = "/actuate/stop"
    token: SecretStr | None = None
    """`Authorization: Bearer $FLEET_AGENT_TOKEN`, per host (docs/08 section 7.4)."""


class LiteLlmEndpoints(BaseModel):
    """LiteLLM's model-management surface (docs/06 section 7).

    Config-file rewriting is explicitly rejected there: a restart drops in-flight
    streams and a config bug takes the whole gateway down. The admin API is the
    supported path, and it needs `store_model_in_db: true` in the gateway config.

    **Route names have changed between LiteLLM releases and differ depending on
    whether the proxy is database-backed** -- `/model/new`, `/model/delete` and
    the update route have all moved. Verify against your pinned tag; a 404 here
    is reported as a version difference on the gateway.

    Reconciliation reads `/model/info` first and applies only the difference, so
    replaying it on every loop is a no-op rather than a duplicate registration.
    That matters because the controller *will* replay it on every reconcile
    (docs/08 section 13).
    """

    model_config = {"frozen": True}

    base_url: str
    master_key: SecretStr
    info_path: str = "/model/info"
    new_path: str = "/model/new"
    delete_path: str = "/model/delete"


class ComfyEndpoints(BaseModel):
    """ComfyUI's stats route, used for admission control only.

    ComfyUI has no sleep mode and its VRAM use is transient -- it allocates per
    job and releases afterwards -- so residency is the wrong mental model. The
    question is asked when a job arrives instead (docs/03 section 3).

    The shape of `/system_stats` has changed across ComfyUI versions; parsing is
    deliberately defensive, and a shape we do not recognise refuses the job
    rather than guessing.
    """

    model_config = {"frozen": True}

    stats_path: str = "/system_stats"
    device_index: int = 0


class HostEndpoints(BaseModel):
    """Base URLs for one host's actuation surfaces.

    `HostConfig.vllm_url` already carries the vLLM address; this fills in the two
    surfaces it does not model, without modifying `models.py`.
    """

    model_config = {"frozen": True}

    vllm_base_url: str | None = None
    agent_base_url: str | None = None
    comfy_base_url: str | None = None


class ActuatorTimeouts(BaseModel):
    """Deadlines, in seconds. **These are budgets, not measurements.**

    `yield_s` is docs/08 section 8.1's `yield_deadline_s`, to be re-set from M0
    spike 6. `rung_change_s` is generous on purpose: a rung change is a process
    restart and possibly a cold load from NVMe, which is a different order of
    magnitude from a wake. No number here is a claim about how long anything
    actually takes on this hardware -- spike 6 measures that.
    """

    model_config = {"frozen": True}

    yield_s: float = 10.0
    wake_s: float = 20.0
    probe_s: float = 3.0
    rung_change_s: float = 180.0
    routing_s: float = 5.0
    admission_s: float = 3.0


class RetryPolicy(BaseModel):
    """A bounded retry budget. Bounded is the operative word.

    The control loop retries everything on its own cadence regardless, so
    retrying here buys only the case where one more immediate attempt would have
    worked -- a 5xx during a restart, a dropped connection. Anything more and a
    single loop iteration stops being bounded in time.
    """

    model_config = {"frozen": True}

    attempts: int = Field(default=3, ge=1)
    backoff_s: float = Field(default=0.5, ge=0.0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    retry_on_timeout: bool = False


class RoutingTarget(BaseModel):
    """One LiteLLM deployment that should exist while a rung is live.

    `deployment_id` is the stable identity used to add and remove it -- the
    `model_info.id` convention from docs/06 section 5 (`chat-226`, `coder-226`).
    """

    model_config = {"frozen": True, "protected_namespaces": ()}

    deployment_id: str
    public_name: str
    """The catalog name users pick: `chat`, `coder`, `chat-small`."""
    served_model: str
    """vLLM's `--served-model-name`, sent to LiteLLM as `openai/<served_model>`."""
    api_base: str
    api_key: str = "os.environ/VLLM_KEY"
    """An `os.environ/NAME` reference, so no secret crosses this wire."""
    mode: str = "chat"
    """`chat` or `embedding`. Wrong here means health checks probe the wrong
    surface, and a perfectly healthy model reads as permanently unhealthy."""
    order: int = 1
    """Preference within the model group, lower first -- so the standby host only
    answers when the preferred one has been pulled out of the catalog."""


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class Actuator(Protocol):
    """What the control loop is allowed to ask of the world.

    `FakeActuator` implements this too, which is the only way any of this gets
    exercised: no host is reachable from a development machine.
    """

    async def sleep(self, host: HostConfig) -> ActuationResult: ...

    async def wake(self, host: HostConfig) -> ActuationResult: ...

    async def change_rung(
        self,
        host: HostConfig,
        target: Rung | None,
        *,
        gpu_memory_utilization: float | None = None,
    ) -> ActuationResult: ...

    async def sync_routing(
        self,
        host: HostConfig,
        targets: Sequence[RoutingTarget],
        owned_ids: Sequence[str],
    ) -> ActuationResult: ...

    async def image_admission(
        self,
        host: HostConfig,
        rungs: tuple[Rung, ...],
        state: HostState,
    ) -> AdmissionAnswer: ...


# --------------------------------------------------------------------------- messages

_VERSION_NOTES: Mapping[str, str] = {
    "vllm": (
        "vLLM's sleep/wake routes are development endpoints: they exist only with "
        "--enable-sleep-mode and VLLM_SERVER_DEV_MODE=1, and their paths, payload shape "
        "(?level=N versus a JSON body) and level semantics have all changed between "
        "releases. Check the running server's version against docs/07 section 8 and set "
        "VllmEndpoints to match."
    ),
    "agent": (
        "The per-host agent exposes a fixed verb list (docs/08 section 7.1). Either the "
        "agent predates this verb or its route was renamed; set AgentEndpoints to match "
        "the agent actually deployed on this host."
    ),
    "litellm": (
        "LiteLLM's model-management routes (/model/info, /model/new, /model/delete) have "
        "moved between releases and differ when the proxy is not database-backed "
        "(store_model_in_db: true is required). Check the pinned gateway tag against "
        "docs/06 section 7 and set LiteLlmEndpoints to match."
    ),
    "comfy": (
        "ComfyUI's stats route has moved between versions. Set ComfyEndpoints.stats_path "
        "to match the running build."
    ),
}


def _version_note(action: str, url: str, surface: str) -> str:
    """The message a 404 produces.

    It has one job: send the reader to the target's version, not into this
    package. Someone hitting this at 3 a.m. should not spend twenty minutes
    grepping fleet_controller for a bug that is not there.
    """
    return (
        f"{action}: {url} returned 404. This is a VERSION DIFFERENCE on the target, not a "
        f"fault in fleet_controller -- there is nothing to fix in our code. "
        f"{_VERSION_NOTES.get(surface, '')}"
    )


@dataclass(frozen=True)
class _Attempt:
    """Internal: one HTTP call's outcome, before it becomes an ActuationResult."""

    result: ActuationResult
    response: httpx.Response | None


# --------------------------------------------------------------------------- http impl


class HttpActuator:
    """The real thing: async httpx calls to vLLM, the host agent, LiteLLM, ComfyUI.

    Takes a shared `httpx.AsyncClient` rather than making its own, because the
    control loop runs one asyncio task per host against one client pool
    (docs/08 section 2). Tests inject a client built on `httpx.MockTransport`.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        endpoints: Mapping[str, HostEndpoints] | None = None,
        vllm: VllmEndpoints | None = None,
        agent: AgentEndpoints | None = None,
        litellm: LiteLlmEndpoints | None = None,
        comfy: ComfyEndpoints | None = None,
        timeouts: ActuatorTimeouts | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._client = client
        self._endpoints = dict(endpoints or {})
        self._vllm = vllm or VllmEndpoints()
        self._agent = agent or AgentEndpoints()
        self._litellm = litellm
        self._comfy = comfy or ComfyEndpoints()
        self._timeouts = timeouts or ActuatorTimeouts()
        self._retry = retry or RetryPolicy()

    # ------------------------------------------------------------------ vLLM

    async def sleep(self, host: HostConfig) -> ActuationResult:
        """Park this host's model in system RAM and give the card back.

        Sleep level 1 offloads weights to system RAM and discards the KV cache,
        so waking is seconds rather than a cold load from NVMe. That is what
        makes yielding feel instant instead of annoying (docs/03 section 4.5).

        Idempotent: sleeping an already-sleeping server returns ALREADY. We probe
        first where the version exposes `is_sleeping`, and re-probe after a
        rejection, because some versions answer a redundant sleep with a 4xx and
        the control loop replays this constantly.

        This does NOT change which model is loaded -- see `change_rung`.
        """
        action = "sleep"
        base = self._vllm_base(host)
        if base is None:
            return self._misconfigured(action, host.name, "no vLLM base URL for this host")

        if await self._is_sleeping(host, base) is True:
            return self._quiet(action, host.name, "already asleep", vram_released=True)

        params = {"level": self._vllm.sleep_level} if self._vllm.level_in_query else None
        body = None if self._vllm.level_in_query else {"level": self._vllm.sleep_level}
        attempt = await self._call(
            action=action,
            host=host.name,
            method="POST",
            url=f"{base}{self._vllm.sleep_path}",
            surface="vllm",
            timeout_s=self._timeouts.yield_s,
            params=params,
            json_body=body,
            headers=self._bearer(self._vllm.api_key),
        )
        if attempt.result.ok:
            return attempt.result.model_copy(update={"vram_released": True})

        # A rejection may only mean "already asleep" on versions that 4xx it.
        if attempt.result.outcome is ActuationOutcome.FAILED and (
            await self._is_sleeping(host, base) is True
        ):
            return self._quiet(
                action,
                host.name,
                "already asleep (the server rejected a redundant sleep)",
                vram_released=True,
            )
        return attempt.result

    async def wake(self, host: HostConfig) -> ActuationResult:
        """Bring the parked weights back onto the card.

        Idempotent in the same way as `sleep`. Never reports `vram_released`:
        waking takes VRAM, it does not give any back, so a wake can never be the
        thing that lets us stop keeping a host demoted.
        """
        action = "wake"
        base = self._vllm_base(host)
        if base is None:
            return self._misconfigured(action, host.name, "no vLLM base URL for this host")

        if await self._is_sleeping(host, base) is False:
            return self._quiet(action, host.name, "already awake")

        attempt = await self._call(
            action=action,
            host=host.name,
            method="POST",
            url=f"{base}{self._vllm.wake_path}",
            surface="vllm",
            timeout_s=self._timeouts.wake_s,
            headers=self._bearer(self._vllm.api_key),
        )
        if attempt.result.ok:
            return attempt.result
        if attempt.result.outcome is ActuationOutcome.FAILED and (
            await self._is_sleeping(host, base) is False
        ):
            return self._quiet(
                action, host.name, "already awake (the server rejected a redundant wake)"
            )
        return attempt.result

    async def _is_sleeping(self, host: HostConfig, base: str) -> bool | None:
        """Best-effort sleep probe. None means "we could not tell".

        Deliberately never fails the caller: on versions without this route the
        probe 404s and we proceed to the real call. Idempotency is nicer with it
        and still correct without it.
        """
        if self._vllm.is_sleeping_path is None:
            return None
        attempt = await self._call(
            action="is_sleeping",
            host=host.name,
            method="GET",
            url=f"{base}{self._vllm.is_sleeping_path}",
            surface="vllm",
            timeout_s=self._timeouts.probe_s,
            headers=self._bearer(self._vllm.api_key),
            attempts=1,
        )
        if attempt.response is None or not attempt.result.ok:
            return None
        return _coerce_bool(_safe_json(attempt.response))

    # ------------------------------------------------------------------ rung change

    async def change_rung(
        self,
        host: HostConfig,
        target: Rung | None,
        *,
        gpu_memory_utilization: float | None = None,
    ) -> ActuationResult:
        """Load a DIFFERENT model on this host. This is not sleep/wake.

        Read this before assuming otherwise, because it is the single most
        confusing failure mode in the system (docs/08 section 5):

        Sleep parks *one* model's weights in system RAM and wakes them back onto
        the same card. It cannot change which model a vLLM process holds, nor how
        much memory that process reserved when it started. Moving between rungs
        means stopping the server and starting it with a different `--model` and
        a different `--gpu-memory-utilization` -- tens of seconds, or a cold load
        from NVMe. An entirely different order of cost from a wake, and the
        reason hysteresis (docs/08 section 4.4) exists to keep this rare.

        **Continuity during a rung change comes from gateway failover to another
        host, not from sleep mode.** For the length of the restart this host
        answers nothing at all. Acceptance test 1 passes because LiteLLM falls
        over to .87's small chat model; if .87 has no chat rung live at that
        moment the test fails, and it will look like a controller bug when it is
        a routing gap (docs/08 section 5.3). Configure the fallback first.

        `target=None` is the "off" rung: stop the server rather than restart it.
        That is the only outcome here that releases VRAM.

        `gpu_memory_utilization` is passed through untouched. Whether vLLM reads
        it as a fraction of *total* or of *free* VRAM has varied between versions
        and is not resolved here -- pin it per docs/07 section 7 before trusting
        the arithmetic, because that one flag decides whether the sharing policy
        is safe.
        """
        action = "change_rung"
        base = self._agent_base(host)
        if base is None:
            return self._misconfigured(action, host.name, "no agent base URL for this host")

        if target is None:
            stopped = await self._call(
                action=f"{action}:stop",
                host=host.name,
                method="POST",
                url=f"{base}{self._agent.stop_path}",
                surface="agent",
                timeout_s=self._timeouts.rung_change_s,
                headers=self._bearer(self._agent.token),
            )
            if stopped.result.ok:
                return stopped.result.model_copy(update={"vram_released": True})
            return stopped.result

        body: dict[str, Any] = {
            "rung": target.name,
            "served_model": target.served_model,
            "footprint_gb": target.footprint_gb,
        }
        if gpu_memory_utilization is not None:
            body["gpu_memory_utilization"] = gpu_memory_utilization
        attempt = await self._call(
            action=f"{action}:{target.name}",
            host=host.name,
            method="POST",
            url=f"{base}{self._agent.restart_path}",
            surface="agent",
            timeout_s=self._timeouts.rung_change_s,
            json_body=body,
            headers=self._bearer(self._agent.token),
        )
        return attempt.result

    # ------------------------------------------------------------------ LiteLLM

    async def sync_routing(
        self,
        host: HostConfig,
        targets: Sequence[RoutingTarget],
        owned_ids: Sequence[str],
    ) -> ActuationResult:
        """Make the gateway's catalog match what this host can actually answer.

        `targets` is what should exist now; `owned_ids` is every deployment id
        this host could ever own. Anything owned but not targeted is removed. The
        caller supplies both rather than us inferring ownership from a naming
        convention, because guessing wrong there deletes somebody else's
        deployment.

        This is the *push* half of docs/06 section 6.2, and it is an optimisation
        over a correct pull: fallbacks, `allowed_fails` and `cooldown_time` in the
        gateway config are what keep chat working when this controller is dead.
        Push only saves the one user request that would otherwise have to fail in
        order to discover a sleeping backend. So when the admin API is
        unavailable we report it and stop -- we never fall back to rewriting the
        config file, which docs/06 section 7 rejects outright.

        Idempotent by construction: read `/model/info`, diff, apply only the
        difference. Replaying it every loop is a no-op, which is what
        reconciliation needs.
        """
        action = "sync_routing"
        if self._litellm is None:
            return self._misconfigured(action, host.name, "no LiteLLM endpoint configured")
        cfg = self._litellm
        headers = self._bearer(cfg.master_key)
        started = time.monotonic()

        info = await self._call(
            action=f"{action}:info",
            host=host.name,
            method="GET",
            url=f"{cfg.base_url}{cfg.info_path}",
            surface="litellm",
            timeout_s=self._timeouts.routing_s,
            headers=headers,
        )
        if info.response is None or not info.result.ok:
            return info.result

        present = _deployment_ids(_safe_json(info.response))
        wanted = {t.deployment_id: t for t in targets}
        to_add = [t for dep_id, t in wanted.items() if dep_id not in present]
        to_remove = [dep_id for dep_id in owned_ids if dep_id in present and dep_id not in wanted]

        if not to_add and not to_remove:
            return self._quiet(action, host.name, "routing already matches the desired state")

        failures: list[str] = []
        for dep_id in to_remove:
            removed = await self._call(
                action=f"{action}:delete:{dep_id}",
                host=host.name,
                method="POST",
                url=f"{cfg.base_url}{cfg.delete_path}",
                surface="litellm",
                timeout_s=self._timeouts.routing_s,
                json_body={"id": dep_id},
                headers=headers,
            )
            if not removed.result.ok:
                failures.append(removed.result.detail)

        for target in to_add:
            added = await self._call(
                action=f"{action}:new:{target.deployment_id}",
                host=host.name,
                method="POST",
                url=f"{cfg.base_url}{cfg.new_path}",
                surface="litellm",
                timeout_s=self._timeouts.routing_s,
                json_body=_model_new_payload(target),
                headers=headers,
            )
            if not added.result.ok:
                failures.append(added.result.detail)

        changes = len(to_add) + len(to_remove)
        elapsed = time.monotonic() - started
        if failures:
            return ActuationResult(
                action=action,
                host=host.name,
                outcome=ActuationOutcome.FAILED,
                detail=(
                    f"{len(failures)} of {changes} routing changes failed; the gateway's own "
                    f"fallbacks and cooldowns still cover this. " + " | ".join(failures)
                ),
                attempts=changes,
                elapsed_s=elapsed,
            )
        return ActuationResult(
            action=action,
            host=host.name,
            outcome=ActuationOutcome.OK,
            detail=f"added {len(to_add)}, removed {len(to_remove)}",
            attempts=changes,
            elapsed_s=elapsed,
        )

    # ------------------------------------------------------------------ ComfyUI

    async def image_admission(
        self,
        host: HostConfig,
        rungs: tuple[Rung, ...],
        state: HostState,
    ) -> AdmissionAnswer:
        """Ask whether an image job fits right now, and at which model.

        Admission control, not residency control. ComfyUI has no sleep mode and
        allocates transiently -- it peaks during a generation and falls between
        jobs -- so there is nothing to keep asleep and nothing to keep awake.

        Read-only by construction, which is how "never preempt a coding session
        for an image" is enforced: the 30B coder (~17 GB) and FLUX (~12 GB) cannot
        co-reside, so while the coder is loaded the measured free VRAM simply does
        not reach FLUX's rung and the job queues. Image generation is the least
        important capability in scope; a queued image beats a coding session that
        mysteriously got worse.

        Rung selection reuses `ladder.select_rung`, so image admission obeys the
        same inequality as everything else. With SHARING headroom that reproduces
        docs/03 section 3's published table exactly: FLUX at >= 15 GB free, SD3.5
        at >= 9 GB, queue below that.

        If we cannot measure, we refuse. An unmeasurable card is assumed busy.
        """
        action = "image_admission"
        base = self._comfy_base(host)
        if base is None:
            misconfigured = self._misconfigured(
                action, host.name, "no ComfyUI base URL for this host"
            )
            return AdmissionAnswer(
                admitted=False, reason=misconfigured.detail, result=misconfigured
            )

        attempt = await self._call(
            action=action,
            host=host.name,
            method="GET",
            url=f"{base}{self._comfy.stats_path}",
            surface="comfy",
            timeout_s=self._timeouts.admission_s,
            attempts=1,
        )
        if attempt.response is None or not attempt.result.ok:
            return AdmissionAnswer(
                admitted=False,
                reason="waiting for GPU: cannot measure the card, assuming it is in use",
                result=attempt.result,
            )

        free_gb = _comfy_free_gb(_safe_json(attempt.response), self._comfy.device_index)
        if free_gb is None:
            unparsed = attempt.result.model_copy(
                update={
                    "outcome": ActuationOutcome.FAILED,
                    "detail": (
                        f"{action}: could not read free VRAM from ComfyUI's stats response. "
                        f"{_VERSION_NOTES['comfy']} Refusing the job, which is the safe "
                        f"direction."
                    ),
                }
            )
            return AdmissionAnswer(
                admitted=False,
                reason="waiting for GPU: unrecognised stats response",
                result=unparsed,
            )

        rung = select_rung(rungs, free_gb, state)
        if rung is None:
            return AdmissionAnswer(
                admitted=False,
                free_gb=free_gb,
                reason=f"waiting for GPU: {free_gb:.1f} GB free is below the smallest image rung",
                result=attempt.result,
            )
        return AdmissionAnswer(
            admitted=True,
            rung=rung,
            free_gb=free_gb,
            reason=f"{rung.name} fits in {free_gb:.1f} GB free",
            result=attempt.result,
        )

    # ------------------------------------------------------------------ plumbing

    def _vllm_base(self, host: HostConfig) -> str | None:
        override = self._endpoints.get(host.name)
        if override is not None and override.vllm_base_url:
            return override.vllm_base_url.rstrip("/")
        return host.vllm_url.rstrip("/") if host.vllm_url else None

    def _agent_base(self, host: HostConfig) -> str | None:
        override = self._endpoints.get(host.name)
        if override is not None and override.agent_base_url:
            return override.agent_base_url.rstrip("/")
        return None

    def _comfy_base(self, host: HostConfig) -> str | None:
        override = self._endpoints.get(host.name)
        if override is not None and override.comfy_base_url:
            return override.comfy_base_url.rstrip("/")
        return None

    @staticmethod
    def _bearer(secret: SecretStr | None) -> dict[str, str] | None:
        if secret is None:
            return None
        return {"Authorization": f"Bearer {secret.get_secret_value()}"}

    @staticmethod
    def _quiet(
        action: str, host: str, detail: str, *, vram_released: bool = False
    ) -> ActuationResult:
        """An idempotent no-op. Logged at debug: it happens on every reconcile."""
        _log.debug("actuate %s host=%s: %s", action, host, detail)
        return ActuationResult(
            action=action,
            host=host,
            outcome=ActuationOutcome.ALREADY,
            detail=detail,
            vram_released=vram_released,
        )

    @staticmethod
    def _misconfigured(action: str, host: str, detail: str) -> ActuationResult:
        """Missing configuration is a failed actuation, not an exception.

        Same rule as everything else here: the control loop gets a value it can
        surface on the dashboard, and the host stays demoted because nothing
        confirmed a release.
        """
        _log.error("actuate %s host=%s: %s", action, host, detail)
        return ActuationResult(
            action=action, host=host, outcome=ActuationOutcome.FAILED, detail=detail
        )

    async def _call(
        self,
        *,
        action: str,
        host: str,
        method: str,
        url: str,
        surface: str,
        timeout_s: float,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        attempts: int | None = None,
    ) -> _Attempt:
        """One actuation call, with a deadline and a bounded retry budget.

        Every exit is an `ActuationResult`; nothing propagates. Every attempt is
        logged, because the number of actuations is a pass criterion for the
        no-flapping acceptance test (docs/08 section 14, test 7), and an
        impression is not a number.
        """
        budget = self._retry.attempts if attempts is None else attempts
        started = time.monotonic()
        backoff = self._retry.backoff_s
        tries = 0
        outcome = ActuationOutcome.FAILED
        detail = ""

        while tries < budget:
            tries += 1
            retryable = False
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=dict(json_body) if json_body is not None else None,
                    headers=dict(headers) if headers else None,
                    timeout=timeout_s,
                )
            except httpx.TimeoutException as exc:
                outcome = ActuationOutcome.TIMEOUT
                detail = (
                    f"{action}: {method} {url} exceeded its {timeout_s:.1f}s deadline "
                    f"({type(exc).__name__}). The control loop retries on its own cadence; "
                    f"a hung actuator must not stall it."
                )
                retryable = self._retry.retry_on_timeout
                _log.warning("actuate %s host=%s attempt=%d: %s", action, host, tries, detail)
            except httpx.HTTPError as exc:
                outcome = ActuationOutcome.UNREACHABLE
                detail = f"{action}: {method} {url} transport error ({type(exc).__name__}: {exc})"
                retryable = True
                _log.warning("actuate %s host=%s attempt=%d: %s", action, host, tries, detail)
            else:
                if response.status_code == httpx.codes.NOT_FOUND:
                    detail = _version_note(action, url, surface)
                    _log.error("actuate %s host=%s attempt=%d: %s", action, host, tries, detail)
                    return _Attempt(
                        result=ActuationResult(
                            action=action,
                            host=host,
                            outcome=ActuationOutcome.UNSUPPORTED,
                            detail=detail,
                            attempts=tries,
                            elapsed_s=time.monotonic() - started,
                        ),
                        response=response,
                    )
                if response.is_success:
                    _log.info(
                        "actuate %s host=%s attempt=%d: %s %s -> %d",
                        action,
                        host,
                        tries,
                        method,
                        url,
                        response.status_code,
                    )
                    return _Attempt(
                        result=ActuationResult(
                            action=action,
                            host=host,
                            outcome=ActuationOutcome.OK,
                            detail=f"{method} {url} -> {response.status_code}",
                            attempts=tries,
                            elapsed_s=time.monotonic() - started,
                        ),
                        response=response,
                    )
                outcome = ActuationOutcome.FAILED
                body_excerpt = _short_body(response)
                detail = f"{action}: {method} {url} -> {response.status_code} {body_excerpt}"
                retryable = response.status_code >= 500 or response.status_code == 429
                _log.warning("actuate %s host=%s attempt=%d: %s", action, host, tries, detail)

            if not retryable or tries >= budget:
                break
            if backoff > 0:
                await asyncio.sleep(backoff)
            backoff *= self._retry.backoff_multiplier

        return _Attempt(
            result=ActuationResult(
                action=action,
                host=host,
                outcome=outcome,
                detail=detail,
                attempts=tries,
                elapsed_s=time.monotonic() - started,
            ),
            response=None,
        )


# --------------------------------------------------------------------------- parsing


def _safe_json(response: httpx.Response) -> Any:
    """Body as JSON, or None. A malformed body is a fact, not an exception."""
    try:
        return response.json()
    except ValueError:
        return None


def _short_body(response: httpx.Response) -> str:
    """A trimmed body for the log line. Never the whole thing."""
    try:
        return response.text[:200]
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        return "<unreadable body>"


def _coerce_bool(payload: Any) -> bool | None:
    """Read a sleep probe.

    Versions have answered this as a bare `true`, as `{"is_sleeping": true}` and
    as `{"sleeping": true}`. Accept all three and give up quietly on anything
    else -- the probe is an optimisation, so an unknown shape must not become a
    failed actuation.
    """
    if isinstance(payload, bool):
        return payload
    if isinstance(payload, dict):
        for key in ("is_sleeping", "sleeping", "asleep"):
            value = payload.get(key)
            if isinstance(value, bool):
                return value
    return None


def _deployment_ids(payload: Any) -> set[str]:
    """Deployment ids currently registered in LiteLLM's catalog.

    Tolerates both the `{"data": [...]}` envelope and a bare list, and both a
    nested `model_info.id` and a top-level `id`, because which of those you get
    has depended on the version and on whether the proxy is database-backed.
    """
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return set()
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        info = row.get("model_info")
        candidate = info.get("id") if isinstance(info, dict) else row.get("id")
        if isinstance(candidate, str):
            ids.add(candidate)
    return ids


def _model_new_payload(target: RoutingTarget) -> dict[str, Any]:
    """The `/model/new` body, in the shape docs/06 section 5 configures by hand."""
    return {
        "model_name": target.public_name,
        "litellm_params": {
            "model": f"openai/{target.served_model}",
            "api_base": target.api_base,
            "api_key": target.api_key,
            "order": target.order,
        },
        "model_info": {"id": target.deployment_id, "mode": target.mode},
    }


def _comfy_free_gb(payload: Any, device_index: int) -> float | None:
    """Free VRAM in GB from ComfyUI's stats, or None if the shape is unfamiliar.

    Byte-valued keys have varied (`vram_free`, `torch_vram_free`); we take the
    smallest plausible reading rather than the largest, because underestimating
    free VRAM only ever queues an image, while overestimating it is how we would
    walk into somebody's coding session.
    """
    if not isinstance(payload, dict):
        return None
    devices = payload.get("devices")
    if not isinstance(devices, list) or len(devices) <= device_index:
        return None
    device = devices[device_index]
    if not isinstance(device, dict):
        return None
    readings: list[float] = []
    for key in ("vram_free", "torch_vram_free"):
        value = device.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        readings.append(float(value))
    if not readings:
        return None
    return min(readings) / (1024**3)


# --------------------------------------------------------------------------- fake


class FakeActuator:
    """An in-memory `Actuator` for testing the control loop without hosts.

    No host is reachable from a development machine, so this is the only way the
    loop, the state machine and the dashboard get exercised before M2. It
    reproduces the behaviours the loop actually depends on -- idempotent sleep and
    wake, results instead of exceptions, and the safe-direction rule that a failed
    unload never reports VRAM released -- and records every call, so tests can
    assert on actuation *counts*, which is the pass criterion for the no-flapping
    acceptance test.

    Inject failures with `fail_actions`: any action name in it returns a FAILED
    result instead of acting.
    """

    def __init__(
        self,
        *,
        asleep: Sequence[str] = (),
        free_gb: Mapping[str, float] | None = None,
        fail_actions: Sequence[str] = (),
    ) -> None:
        self.asleep: set[str] = set(asleep)
        self.free_gb: dict[str, float] = dict(free_gb or {})
        self.fail_actions: set[str] = set(fail_actions)
        self.calls: list[tuple[str, str]] = []
        self.rungs: dict[str, str | None] = {}
        self.routing: dict[str, tuple[str, ...]] = {}

    def _record(self, action: str, host: str) -> None:
        self.calls.append((action, host))

    @staticmethod
    def _failed(action: str, host: str) -> ActuationResult:
        return ActuationResult(
            action=action,
            host=host,
            outcome=ActuationOutcome.FAILED,
            detail=f"FakeActuator: {action} is configured to fail",
            attempts=1,
        )

    async def sleep(self, host: HostConfig) -> ActuationResult:
        self._record("sleep", host.name)
        if "sleep" in self.fail_actions:
            return self._failed("sleep", host.name)
        already = host.name in self.asleep
        self.asleep.add(host.name)
        return ActuationResult(
            action="sleep",
            host=host.name,
            outcome=ActuationOutcome.ALREADY if already else ActuationOutcome.OK,
            detail="already asleep" if already else "asleep",
            attempts=1,
            vram_released=True,
        )

    async def wake(self, host: HostConfig) -> ActuationResult:
        self._record("wake", host.name)
        if "wake" in self.fail_actions:
            return self._failed("wake", host.name)
        already = host.name not in self.asleep
        self.asleep.discard(host.name)
        return ActuationResult(
            action="wake",
            host=host.name,
            outcome=ActuationOutcome.ALREADY if already else ActuationOutcome.OK,
            detail="already awake" if already else "awake",
            attempts=1,
        )

    async def change_rung(
        self,
        host: HostConfig,
        target: Rung | None,
        *,
        gpu_memory_utilization: float | None = None,
    ) -> ActuationResult:
        self._record("change_rung", host.name)
        if "change_rung" in self.fail_actions:
            return self._failed("change_rung", host.name)
        self.rungs[host.name] = target.name if target else None
        # A restart brings up whatever it starts; only the off rung releases VRAM.
        if target is None:
            self.asleep.add(host.name)
        else:
            self.asleep.discard(host.name)
        return ActuationResult(
            action="change_rung",
            host=host.name,
            outcome=ActuationOutcome.OK,
            detail=f"restarted on {target.name if target else 'off'}",
            attempts=1,
            vram_released=target is None,
        )

    async def sync_routing(
        self,
        host: HostConfig,
        targets: Sequence[RoutingTarget],
        owned_ids: Sequence[str],
    ) -> ActuationResult:
        self._record("sync_routing", host.name)
        if "sync_routing" in self.fail_actions:
            return self._failed("sync_routing", host.name)
        desired = tuple(t.deployment_id for t in targets)
        already = self.routing.get(host.name) == desired
        self.routing[host.name] = desired
        return ActuationResult(
            action="sync_routing",
            host=host.name,
            outcome=ActuationOutcome.ALREADY if already else ActuationOutcome.OK,
            detail=f"routing = {desired}",
            attempts=1,
        )

    async def image_admission(
        self,
        host: HostConfig,
        rungs: tuple[Rung, ...],
        state: HostState,
    ) -> AdmissionAnswer:
        self._record("image_admission", host.name)
        if "image_admission" in self.fail_actions:
            return AdmissionAnswer(
                admitted=False,
                reason="waiting for GPU: cannot measure the card, assuming it is in use",
                result=self._failed("image_admission", host.name),
            )
        free = self.free_gb.get(host.name, 0.0)
        rung = select_rung(rungs, free, state)
        result = ActuationResult(
            action="image_admission",
            host=host.name,
            outcome=ActuationOutcome.OK,
            detail=f"{free:.1f} GB free",
            attempts=1,
        )
        if rung is None:
            return AdmissionAnswer(
                admitted=False,
                free_gb=free,
                reason=f"waiting for GPU: {free:.1f} GB free is below the smallest image rung",
                result=result,
            )
        return AdmissionAnswer(
            admitted=True,
            rung=rung,
            free_gb=free,
            reason=f"{rung.name} fits in {free:.1f} GB free",
            result=result,
        )
