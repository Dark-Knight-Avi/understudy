"""The control loop: sample, decide, actuate, publish.

One asyncio task per host. Three hosts at a 2 s poll is 1.5 requests per second in
total -- this loop must never be the reason anything is slow.

The ordering rule that matters: **decide from a fresh sample, actuate, then store.**
The state machine is pure and hands back the next runtime, so a tick that throws
leaves the previous runtime untouched rather than half-applied. Actuation failures
do not roll the state back -- they keep the host demoted, which is the safe
direction (`actuators.keep_demoted`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fleet_controller import state as st
from fleet_controller.actuators import Actuator, RoutingTarget, keep_demoted
from fleet_controller.config import FleetConfig
from fleet_controller.ladder import always_on_rungs
from fleet_controller.models import GpuSample, HostConfig, HostState, HostStatus, Rung
from fleet_controller.nvidia import NvidiaSmiError

_log = logging.getLogger(__name__)

SampleFn = Callable[[HostConfig], Awaitable[GpuSample]]
"""Fetch one sample for a host.

Async because the controller runs on `.87` and every other host is across the
network -- the per-host agent on :8099 is what it actually calls. `LocalSampler`
is for the agent's own process, not for here.
"""

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FleetLoop:
    """Owns the runtime state of every host, and advances it.

    Deliberately not a singleton and not module-level state: the API layer holds one
    instance, and tests construct their own with a fake sampler and actuator.
    """

    def __init__(
        self,
        config: FleetConfig,
        sample: SampleFn,
        actuator: Actuator,
        *,
        clock: Clock = _utcnow,
    ) -> None:
        self._config = config
        self._sample = sample
        self._actuator = actuator
        self._clock = clock
        now = clock()
        self._runtimes: dict[str, st.HostRuntime] = {
            h.name: st.initial(h, now) for h in config.hosts
        }
        self._timings = config.tunables.as_timings()
        self._subscribers: list[asyncio.Queue[st.Decision]] = []
        self._tasks: list[asyncio.Task[None]] = []

    # ----------------------------------------------------------------- accessors

    @property
    def hosts(self) -> tuple[HostConfig, ...]:
        return self._config.hosts

    def runtime(self, host: str) -> st.HostRuntime | None:
        return self._runtimes.get(host)

    def status(self, host: str) -> HostStatus | None:
        cfg = self._config.host(host)
        rt = self._runtimes.get(host)
        if cfg is None or rt is None:
            return None
        return st.status(rt, cfg, self._clock(), timings=self._timings)

    def statuses(self) -> list[HostStatus]:
        return [s for h in self._config.hosts if (s := self.status(h.name)) is not None]

    # ------------------------------------------------------------------ commands

    async def reserve(self, host: str, holder: str) -> st.Decision | None:
        """Claim a host. Returns as soon as the decision is made; yielding proceeds.

        The caller is expected to poll until the host reports it has actually
        released -- 'ready' is a measured fact, not an acknowledgement, and that
        distinction is the entire value of the toggle.
        """
        cfg = self._config.host(host)
        rt = self._runtimes.get(host)
        if cfg is None or rt is None:
            return None
        decision = st.reserve(rt, holder, self._clock(), timings=self._timings)
        await self._apply(cfg, decision)
        return decision

    async def release(self, host: str) -> st.Decision | None:
        cfg = self._config.host(host)
        rt = self._runtimes.get(host)
        if cfg is None or rt is None:
            return None
        decision = st.release(rt, self._clock())
        await self._apply(cfg, decision)
        return decision

    # --------------------------------------------------------------------- loop

    async def tick(self, cfg: HostConfig) -> st.Decision:
        """One iteration for one host."""
        sample: GpuSample | None
        try:
            sample = await self._sample(cfg)
        except (NvidiaSmiError, OSError) as exc:
            # A sample we cannot trust is not a sample. The state machine turns
            # repeated Nones into UNKNOWN, which never promotes.
            _log.warning("sample failed host=%s: %s", cfg.name, exc)
            sample = None

        rt = self._runtimes[cfg.name]
        decision = st.observe(rt, cfg, sample, self._clock(), timings=self._timings)
        await self._apply(cfg, decision)
        return decision

    async def run(self) -> None:
        """Start one task per host. Cancels cleanly."""
        await self._adopt_known_state()
        interval = self._config.tunables.poll_interval_s
        self._tasks = [
            asyncio.create_task(self._host_loop(h, interval), name=f"fleet-{h.name}")
            for h in self._config.hosts
        ]
        await asyncio.gather(*self._tasks)

    async def _adopt_known_state(self) -> None:
        """Park every host's model before deciding anything.

        A fresh controller believes nothing is loaded, because its runtime starts
        with `rung=None`. That belief is unrecoverable on its own: nvidia-smi
        reports free VRAM, which already excludes our own model, and
        `_available_gb` only adds back a rung we think we hold. So a controller
        restarted while .226 served a 16 GB model saw 7.8 GB free, concluded that
        nothing in the ladder fits, selected no rung, advertised nothing, and
        sat there -- with a perfectly healthy model running that it could not
        account for, and chat returning 400.

        Nothing moves it out of that state either, since no decision changes.

        Sleeping first makes the card's contents a fact rather than an
        assumption: free VRAM then genuinely reflects what is not ours, and the
        ladder chooses from a clean measurement. The cost is that a controller
        restart briefly interrupts chat -- seconds to park, then the settle
        window before it promotes again. That is the honest price of not
        guessing, and restarts are rare.
        """
        for cfg in self._config.hosts:
            if cfg.vllm_url is None:
                continue
            try:
                result = await self._actuator.sleep(cfg)
                _log.info("host=%s startup: parked model to establish a known card (%s)",
                          cfg.name, result.outcome)
            except Exception:  # noqa: BLE001 -- a host we cannot park is a host we do not trust
                _log.exception("host=%s startup: could not park model; it will be "
                               "treated as unknown until a sample proves otherwise", cfg.name)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _host_loop(self, cfg: HostConfig, interval: float) -> None:
        while True:
            try:
                await self.tick(cfg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a loop that dies stops yielding
                # Never let one host's bug stop the others. A controller that has
                # stopped ticking is a controller that has stopped getting out of
                # people's way, which is worse than any single bad tick.
                _log.exception("tick failed host=%s", cfg.name)
            await asyncio.sleep(interval)

    # ----------------------------------------------------------------- internals

    async def _apply(self, cfg: HostConfig, decision: st.Decision) -> None:
        """Actuate if the decision demands it, then store, then publish."""
        if decision.changed:
            await self._actuate(cfg, decision)
        else:
            # Reconcile routing even when nothing moved. sync_routing reads
            # /model/info and applies only the difference, so this is a no-op in
            # the ordinary case -- and the ordinary case is not the one that
            # matters. Drift is one-directional and permanent otherwise: the
            # gateway's database can be restored, its container recreated, or a
            # deployment deleted by hand, and a fleet that is sitting still
            # produces no decisions, so nothing ever puts the catalog back.
            await self._route(cfg, self._resident(cfg, decision))
        self._runtimes[cfg.name] = decision.runtime
        self._publish(decision)
        if decision.changed:
            _log.info(
                "host=%s %s -> %s rung=%s reason=%s%s",
                decision.host,
                decision.previous_state,
                decision.state,
                decision.rung.name if decision.rung else "none",
                decision.reason,
                " EMERGENCY" if decision.emergency else "",
            )

    async def _actuate(self, cfg: HostConfig, decision: st.Decision) -> None:
        if decision.state in (HostState.YIELDING, HostState.UNKNOWN):
            # Routing first, deliberately: pull the host out of the catalog before
            # its server goes away, so in-flight requests are not sent to a model
            # that is mid-unload.
            await self._route(cfg, ())
            result = await self._actuator.sleep(cfg)
            if keep_demoted(result):
                _log.warning(
                    "host=%s unload unconfirmed, staying demoted: %s", cfg.name, result.detail
                )
            return

        # always_on rungs are not choices; they ride along with any loaded state.
        desired = (*always_on_rungs(cfg.rungs), *((decision.rung,) if decision.rung else ()))

        if decision.previous_state in (HostState.YIELDING, HostState.UNKNOWN):
            await self._actuator.wake(cfg)
        if decision.rung_changed:
            await self._actuator.change_rung(cfg, decision.rung)
        # Routing last on the way up, for the mirror-image reason: only advertise a
        # model once it is actually loadable.
        await self._route(cfg, desired)

    def _resident(self, cfg: HostConfig, decision: st.Decision) -> tuple[Rung, ...]:
        """The rungs that should be advertised for a host that is not changing."""
        if decision.state in (HostState.YIELDING, HostState.UNKNOWN):
            return ()
        return (*always_on_rungs(cfg.rungs), *((decision.rung,) if decision.rung else ()))

    def _targets(self, cfg: HostConfig, rungs: tuple[Rung, ...]) -> tuple[RoutingTarget, ...]:
        # `vllm_url` is the server ROOT, because that is where the sleep and wake
        # endpoints live (`/sleep`, `/wake_up`). LiteLLM addressing an OpenAI
        # backend wants the API prefix, so the two uses of this one field differ
        # by exactly `/v1`. Registering the root instead yields a deployment that
        # 404s on every request while the gateway reports it configured.
        base = (cfg.vllm_url or "").rstrip("/")
        api_base = base if base.endswith("/v1") else f"{base}/v1"

        # The backend's key. An `os.environ/NAME` reference would be preferable --
        # nothing secret on this wire -- and LiteLLM does resolve those, but only
        # for deployments declared in its config FILE. A deployment registered
        # through /model/new is stored in the database with its api_key
        # encrypted, and what gets encrypted is the literal string: the gateway
        # then sends "os.environ/VLLM_226_KEY" as the bearer token and vLLM
        # answers 401. The symptom points at credentials, which is where the
        # hours go, rather than at where the credential was resolved.
        #
        # So send the value when we hold it. It travels controller -> gateway on
        # the internal Docker network and is stored encrypted under
        # LITELLM_SALT_KEY. The reference stays as the fallback: it is correct
        # for a config-file catalog, and a wrong-but-inert key beats crashing.
        env_name = f"VLLM_{cfg.name}_KEY"
        key_ref = os.environ.get(env_name) or f"os.environ/{env_name}"

        return tuple(
            RoutingTarget(
                deployment_id=f"{r.name}-{cfg.name}",
                public_name=r.public_name or r.name,
                served_model=r.served_model,
                api_base=api_base,
                api_key=key_ref,
                order=r.order,
                mode="embedding" if r.always_on else "chat",
            )
            for r in rungs
        )

    async def _route(self, cfg: HostConfig, desired: tuple[Rung, ...]) -> None:
        if cfg.vllm_url is None:
            return
        owned = tuple(f"{r.name}-{cfg.name}" for r in cfg.rungs)
        result = await self._actuator.sync_routing(cfg, self._targets(cfg, desired), owned)
        if not result.ok:
            # Routing failure means the gateway may send traffic to a sleeping
            # server. Loud, but not fatal: the next tick retries.
            _log.warning("host=%s routing sync failed: %s", cfg.name, result.detail)

    # ------------------------------------------------------------------- events

    def subscribe(self) -> asyncio.Queue[st.Decision]:
        q: asyncio.Queue[st.Decision] = asyncio.Queue(maxsize=64)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[st.Decision]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _publish(self, decision: st.Decision) -> None:
        """Fan out to dashboards. A slow subscriber is dropped, never awaited.

        Blocking the control loop on a browser that stopped reading would make the
        dashboard a way to stop the platform yielding.
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait(decision)
            except asyncio.QueueFull:
                _log.debug("dropping event for a slow subscriber")
