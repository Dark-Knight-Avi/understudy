"""The per-host state machine: FREE / YIELDING / SHARING / UNKNOWN.

`ladder.py` answers "what fits?". This module answers "what are we allowed to do
right now, and why?" -- the harder half, because it is where the social contract in
docs/03 turns into timing.

Three properties are deliberate and should survive refactoring.

**It is pure.** No clock, no sockets, no subprocess. Every entry point takes the
current time as a parameter and a `GpuSample` (or `None`, for a host we could not
read) and returns a `Decision` carrying the *next* `HostRuntime`. That is what lets
the 60 s settle window and the 5 minute promotion window be tested in microseconds
rather than with `time.sleep`, and it is why the logic that decides whether someone's
job survives does not live inside an asyncio loop where it could only ever be
exercised on their workstation, in production, once.

**It explains itself.** Nothing changes quietly. Every step emits a `Decision` with a
`TransitionReason` and a sentence a human can read. "The model got smaller and nobody
knows why" is the failure mode that destroys trust in a system like this, far faster
than the model being smaller ever does (docs/03 section 5).

**It fails toward yielding.** Every ambiguity -- an unreachable host, a stale sample, a
fresh controller with no history, a claim held by someone who may be about to launch --
resolves toward giving VRAM back, never toward taking more. Demotion is always allowed;
promotion always requires fresh evidence (docs/08 section 3.4).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from fleet_controller.ladder import breaches_headroom, select_rung
from fleet_controller.models import GpuSample, HostConfig, HostState, HostStatus, Rung

# --------------------------------------------------------------------------- timings
#
# Every number below is a policy decision from docs/03, not a knob someone picked.
# They are defaults on `StateTimings`, which is per-host config, because .210 is
# somebody's daily workstation and deserves to be more timid than the hub.

SETTLE = timedelta(seconds=60)
"""docs/03 section 2. NOT padding.

PyTorch's caching allocator grows through warm-up: a job reading 4 GB at launch is
routinely 11 GB a minute later. Sizing a rung against the first reading claims memory
their job is about to ask for, and then the platform is the reason their run died. So:
yield first, wait while the card is entirely theirs, measure second -- and keep
re-measuring afterwards, because the growth does not stop politely at 60 s.
"""

SUSTAIN = timedelta(seconds=60)
"""docs/03 section 4.6. A qualifying free-VRAM change must hold this long.

A rung change is a model *swap* -- a server restart, not a sleep/wake (docs/08
section 5) -- so flapping is expensive. This is what keeps it rare.
"""

CLEAR_BEFORE_FREE = timedelta(minutes=5)
"""docs/03 section 4.6. How long a card must read clear before we take the top rung.

Deliberately long. Coming back eagerly puts us in the way of someone who was only
between runs.
"""

RESERVATION_IDLE_WARNING = timedelta(minutes=25)
"""docs/08 section 8.1. A visible warning before the release, never a surprise."""

RESERVATION_IDLE_RELEASE = timedelta(minutes=30)
"""docs/03 section 4.2. A claim held with no CUDA process releases itself.

Forgetting to flip the toggle back is the obvious human failure mode, so the system
handles it rather than relying on anyone's discipline. The clock starts at *claim*,
so on a slow day someone may need one re-toggle -- the right side to err on.
"""

RUNG_CHANGE_GB = 2.0
"""docs/03 section 4.6. Ignore free-VRAM movements smaller than this."""

UNREACHABLE_AFTER_SAMPLES = 3
"""docs/08 section 3.4. Three failed samples (~6 s at a 2 s poll) means UNKNOWN.

One dropped poll is a network. Three is an outage.
"""

MAX_SAMPLE_AGE = timedelta(seconds=10)
"""A reading older than this is not evidence; it counts as a failed sample.

This is what makes "never promote on stale data" true by construction, rather than by
everyone downstream remembering to check a timestamp.
"""


class StateTimings(BaseModel):
    """Per-host timings. The defaults are the policy's; a host may be more cautious."""

    model_config = {"frozen": True}

    settle: timedelta = SETTLE
    sustain: timedelta = SUSTAIN
    clear_before_free: timedelta = CLEAR_BEFORE_FREE
    reservation_idle_warning: timedelta = RESERVATION_IDLE_WARNING
    reservation_idle_release: timedelta = RESERVATION_IDLE_RELEASE
    rung_change_gb: float = Field(default=RUNG_CHANGE_GB, gt=0)
    unreachable_after_samples: int = Field(default=UNREACHABLE_AFTER_SAMPLES, ge=1)
    max_sample_age: timedelta = MAX_SAMPLE_AGE


DEFAULT_TIMINGS = StateTimings()


# --------------------------------------------------------------------------- types


class ReservationSource(StrEnum):
    """How the claim arrived. Both paths hit the same API (docs/03 section 4.3)."""

    TOGGLE = "toggle"
    GPU_RUN = "gpu-run"


class ReleaseKind(StrEnum):
    """Why a claim ended. Logged, because 'idle' is the one people ask about."""

    EXPLICIT = "explicit"
    IDLE = "idle"
    FORCE = "force"


class TransitionReason(StrEnum):
    """Why a `Decision` looks the way it does. The dashboard's vocabulary, too."""

    FOREIGN_PROCESS = "foreign_process"
    RESERVED = "reserved"
    RELEASED = "released"
    RESERVATION_AUTO_RELEASED = "reservation_auto_released"
    SETTLING = "settling"
    SETTLED = "settled"
    CARD_CLEAR = "card_clear"
    AWAITING_CLEAR_WINDOW = "awaiting_clear_window"
    HEADROOM_BREACH = "headroom_breach"
    SUSTAINED_CHANGE = "sustained_change"
    HYSTERESIS_SUPPRESSED = "hysteresis_suppressed"
    SAMPLE_MISSED = "sample_missed"
    HOST_UNREACHABLE = "host_unreachable"
    RECOVERED = "recovered"
    HELD = "held"


class Reservation(BaseModel):
    """Someone has claimed this host, via the toggle or via `gpu-run`."""

    model_config = {"frozen": True}

    holder: str
    source: ReservationSource = ReservationSource.TOGGLE
    acquired_at: datetime
    last_cuda_at: datetime
    """Last moment a foreign CUDA process was seen, or `acquired_at` if never.

    Seeding it from the claim is what makes the 30 minute idle release start at claim
    time rather than at first launch (docs/08 section 10.5).
    """
    idle_warned: bool = False


class Reading(BaseModel):
    """One sample, plus the number the ladder actually wants.

    `available_gb` is free VRAM *as if the platform held nothing* -- which is
    rung-independent, and therefore stays meaningful after a model swap. Storing it
    with the sample is what lets the rolling window survive a rung change without
    silently comparing readings taken under different residency.
    """

    model_config = {"frozen": True}

    sample: GpuSample
    available_gb: float

    @property
    def at(self) -> datetime:
        return self.sample.sampled_at


class HostRuntime(BaseModel):
    """Everything carried between samples. Immutable: replaced, never edited.

    Public on purpose. The dashboard, the logs and the tests all read it, and hidden
    state is precisely what this module is trying not to have.
    """

    model_config = {"frozen": True}

    host: str
    state: HostState
    state_since: datetime

    rung: Rung | None = None
    rung_since: datetime | None = None
    rung_available_gb: float = 0.0
    """Available VRAM at the moment the current rung was chosen.

    The baseline the ~2 GB change threshold is measured against, so that drift
    accumulates from the last actuation rather than from the last sample.
    """

    reservation: Reservation | None = None
    settle_until: datetime | None = None
    clear_since: datetime | None = None
    """When the card first read clear *and* unclaimed. Reset the moment either fails."""

    window: tuple[Reading, ...] = ()
    """Rolling readings spanning `sustain`. Promotion is judged against its worst one."""

    consecutive_failures: int = 0

    def evolve(self, **changes: object) -> HostRuntime:
        """Return a copy with `changes` applied. `model_copy`, spelled readably."""
        return self.model_copy(update=dict(changes))

    @property
    def last_sample(self) -> GpuSample | None:
        return self.window[-1].sample if self.window else None


class Decision(BaseModel):
    """What the machine did this tick, and why.

    Log it, stream it to the dashboard, assert on it in tests. It *carries* the next
    `HostRuntime` rather than mutating anything, so a caller that drops a decision has
    simply not advanced; it cannot end up half-applied.
    """

    model_config = {"frozen": True}

    host: str
    at: datetime
    previous_state: HostState
    state: HostState
    previous_rung: Rung | None
    rung: Rung | None
    reason: TransitionReason
    detail: str
    emergency: bool = False
    """True only on a headroom breach -- the one path that bypasses every timer."""
    notes: tuple[str, ...] = ()
    """Secondary facts worth logging that did not drive the primary reason."""
    runtime: HostRuntime

    @property
    def state_changed(self) -> bool:
        return self.state is not self.previous_state

    @property
    def rung_changed(self) -> bool:
        return self.rung != self.previous_rung

    @property
    def changed(self) -> bool:
        """True when the caller must actuate something. Usually false, by design."""
        return self.state_changed or self.rung_changed


# --------------------------------------------------------------------------- helpers


def _footprint(rung: Rung | None) -> float:
    """Size, for ordering. `None` -- holding nothing -- sorts below every real rung."""
    return -1.0 if rung is None else rung.footprint_gb


def _available_gb(free_gb: float, rung: Rung | None) -> float:
    """Free VRAM we would have if we unloaded whatever we currently hold.

    `select_rung` asks "does footprint + headroom fit in free VRAM?", which presumes
    free VRAM *before* we load anything. nvidia-smi has already subtracted a resident
    rung, so it has to be added back -- otherwise every loaded host would look one rung
    poorer than it is and demote itself in a loop.

    Note the direction of the error. docs/08 section 4.1 warns that a configured
    `footprint_gb` may understate what the rung really holds; where it does, this
    *under*-estimates what is available and we choose a smaller model. That is the safe
    way to be wrong. `breaches_headroom` is deliberately not given this number: it wants
    the raw reading, which is what is genuinely left for the person right now.
    """
    return free_gb + (rung.footprint_gb if rung is not None else 0.0)


def _trim(window: tuple[Reading, ...], now: datetime, sustain: timedelta) -> tuple[Reading, ...]:
    """Keep the readings covering the sustain window.

    One reading from *before* the cutoff is kept as well, so the window genuinely spans
    `sustain` instead of falling just short of it. Without that straddling reading a
    2 s poll leaves an oldest sample ~58 s back, the "has this held for 60 s?" test
    never passes, and the platform would never promote again -- a bug that would look
    exactly like the model mysteriously refusing to come back.
    """
    cutoff = now - sustain
    fresh = tuple(r for r in window if r.at >= cutoff)
    stale = tuple(r for r in window if r.at < cutoff)
    if stale:
        fresh = (stale[-1], *fresh)
    return fresh if fresh else window[-1:]


def _name(rung: Rung | None) -> str:
    return rung.name if rung is not None else "nothing"


def _decide(
    before: HostRuntime,
    after: HostRuntime,
    at: datetime,
    reason: TransitionReason,
    detail: str,
    *,
    emergency: bool = False,
    notes: tuple[str, ...] = (),
) -> Decision:
    return Decision(
        host=after.host,
        at=at,
        previous_state=before.state,
        state=after.state,
        previous_rung=before.rung,
        rung=after.rung,
        reason=reason,
        detail=detail,
        emergency=emergency,
        notes=notes,
        runtime=after,
    )


def _enter(rt: HostRuntime, state: HostState, now: datetime, **changes: object) -> HostRuntime:
    return rt.evolve(state=state, state_since=now, **changes)


def _commit_rung(
    rt: HostRuntime, rung: Rung | None, available_gb: float, now: datetime
) -> HostRuntime:
    """Record an actuation. The window is kept: `available_gb` is rung-independent."""
    return rt.evolve(rung=rung, rung_since=now, rung_available_gb=available_gb)


def _clear_long_enough(rt: HostRuntime, now: datetime, timings: StateTimings) -> bool:
    return rt.clear_since is not None and now - rt.clear_since >= timings.clear_before_free


def _remaining_s(since: datetime | None, window: timedelta, now: datetime) -> float:
    if since is None:
        return window.total_seconds()
    return max(0.0, (window - (now - since)).total_seconds())


# --------------------------------------------------------------------------- entry points


def initial(config: HostConfig, now: datetime) -> HostRuntime:
    """A freshly started controller starts UNKNOWN, never FREE.

    It has measured nothing, and docs/08 section 8.2 is explicit that measurement
    outranks memory. The cost is that a restart onto an idle card waits out the clear
    window before taking the top rung. The alternative is a restart that squats VRAM
    somebody is already using, which is the failure this service exists to prevent.
    """
    return HostRuntime(host=config.name, state=HostState.UNKNOWN, state_since=now)


def reserve(
    runtime: HostRuntime,
    holder: str,
    now: datetime,
    *,
    source: ReservationSource = ReservationSource.TOGGLE,
    timings: StateTimings = DEFAULT_TIMINGS,
) -> Decision:
    """Someone flipped the toggle, or ran `gpu-run`. Yield immediately -- no timers.

    Re-claiming a host you already hold is a no-op, because a double-clicked toggle and
    a re-run script must both be harmless (docs/08 section 9.2). A claim by a *different*
    holder is refused here and left to the API to answer with a 409: this machine will
    not silently reassign a lease out from under whoever holds it.
    """
    held = runtime.reservation
    if held is not None and held.holder == holder:
        return _decide(
            runtime,
            runtime,
            now,
            TransitionReason.HELD,
            f"{holder} already holds {runtime.host}; reserve is idempotent",
        )
    if held is not None:
        return _decide(
            runtime,
            runtime,
            now,
            TransitionReason.HELD,
            f"{runtime.host} is held by {held.holder}; not reassigning it to {holder}",
        )

    reservation = Reservation(holder=holder, source=source, acquired_at=now, last_cuda_at=now)
    after = _enter(
        runtime,
        HostState.YIELDING,
        now,
        reservation=reservation,
        rung=None,
        rung_since=now,
        rung_available_gb=0.0,
        settle_until=None,
        clear_since=None,
    )
    return _decide(
        runtime,
        after,
        now,
        TransitionReason.RESERVED,
        f"{holder} claimed {runtime.host} via {source.value}; "
        f"dropping {_name(runtime.rung)} to zero VRAM now",
    )


def release(
    runtime: HostRuntime,
    now: datetime,
    *,
    kind: ReleaseKind = ReleaseKind.EXPLICIT,
) -> Decision:
    """Drop the claim. The state itself is re-derived from the next measurement.

    Releasing promotes nothing. If their job is still resident we stay exactly where we
    are; if the card is genuinely clear, the clear window starts on the next reading.
    Promotion is never granted by an API call, only by evidence.
    """
    held = runtime.reservation
    if held is None:
        return _decide(
            runtime, runtime, now, TransitionReason.HELD, f"{runtime.host} was not claimed"
        )
    after = runtime.evolve(reservation=None)
    return _decide(
        runtime,
        after,
        now,
        TransitionReason.RELEASED,
        f"{held.holder} released {runtime.host} ({kind.value}); "
        "the top rung still waits for a clear card",
    )


def observe(
    runtime: HostRuntime,
    config: HostConfig,
    sample: GpuSample | None,
    now: datetime,
    *,
    timings: StateTimings = DEFAULT_TIMINGS,
) -> Decision:
    """Advance the machine by one reading. The control loop of docs/08 section 2.

    Pass `sample=None` for a host that did not answer *or* whose `nvidia-smi` output
    would not parse. Never synthesise a zero-used sample from a bad parse: a bad parse
    that looks like a free card is the one error this service must not make
    (docs/08 section 13).
    """
    if sample is not None and sample.host != config.name:
        raise ValueError(f"sample for {sample.host!r} fed to the {config.name!r} machine")

    if sample is None:
        return _missed(runtime, now, timings, "no usable reading")
    age = now - sample.sampled_at
    if age > timings.max_sample_age:
        return _missed(runtime, now, timings, f"reading is {age.total_seconds():.0f}s stale")

    # Computed against the rung resident *now*, before anything this tick changes it.
    reading = Reading(sample=sample, available_gb=_available_gb(sample.free_gb, runtime.rung))
    rt = runtime.evolve(
        consecutive_failures=0,
        window=_trim((*runtime.window, reading), now, timings.sustain),
    )

    if rt.state is HostState.UNKNOWN:
        return _recover(runtime, rt, reading, now, timings)

    rt, note, released = _run_reservation_timers(rt, sample, now, timings)
    notes: tuple[str, ...] = (note,) if note is not None else ()

    occupied = sample.has_foreign_process or rt.reservation is not None
    if occupied:
        rt = rt.evolve(clear_since=None)
    elif rt.clear_since is None:
        rt = rt.evolve(clear_since=now)

    # A clear card outside FREE is governed by the 5 minute top-rung wait, not by the
    # 60 s sustain window -- otherwise a job ending would let us climb back a minute
    # after it stopped and the longer timer would never bind (docs/03 section 4.6).
    promote_ok = occupied or rt.state is HostState.FREE

    if rt.state is HostState.FREE:
        decision = (
            _yield_now(runtime, rt, sample, now, timings, notes)
            if occupied
            else _move_rung(runtime, rt, config, sample, now, timings, notes, promote_ok)
        )
    elif rt.state is HostState.YIELDING:
        decision = _while_yielding(runtime, rt, config, sample, now, timings, notes)
    elif not occupied and _clear_long_enough(rt, now, timings):
        decision = _promote_to_free(runtime, rt, config, now, notes)
    else:
        decision = _move_rung(runtime, rt, config, sample, now, timings, notes, promote_ok)

    if released and note is not None and not decision.changed:
        # Nothing else moved this tick, so the auto-release is the story worth telling.
        return decision.model_copy(
            update={"reason": TransitionReason.RESERVATION_AUTO_RELEASED, "detail": note}
        )
    return decision


# --------------------------------------------------------------------------- internals


def _missed(runtime: HostRuntime, now: datetime, timings: StateTimings, why: str) -> Decision:
    """No usable reading. Hold, count, and go UNKNOWN once it is clearly an outage.

    Note what does *not* happen here: the idle timer does not run. Auto-release needs
    positive evidence that no CUDA process exists, and a host we cannot read supplies
    none -- so a blind controller can never release somebody's claim out from under them.
    """
    failures = runtime.consecutive_failures + 1
    if failures < timings.unreachable_after_samples or runtime.state is HostState.UNKNOWN:
        after = runtime.evolve(consecutive_failures=failures)
        return _decide(
            runtime,
            after,
            now,
            TransitionReason.SAMPLE_MISSED,
            f"{runtime.host}: {why}; {failures} in a row, holding {runtime.state.value}",
        )
    after = _enter(
        runtime,
        HostState.UNKNOWN,
        now,
        consecutive_failures=failures,
        rung=None,
        rung_since=now,
        rung_available_gb=0.0,
        clear_since=None,
    )
    return _decide(
        runtime,
        after,
        now,
        TransitionReason.HOST_UNREACHABLE,
        f"{runtime.host}: {why}; {failures} failed samples -- assuming in use, "
        "unloading and pulling out of routing",
    )


def _recover(
    before: HostRuntime,
    rt: HostRuntime,
    reading: Reading,
    now: datetime,
    timings: StateTimings,
) -> Decision:
    """Leaving UNKNOWN goes to YIELDING -- never straight back to a rung.

    While we were blind we could not have demoted, so the honest assumption is that
    something started which we did not see. Yield, wait out the settle window, measure,
    re-enter. The window is reset to this single fresh reading as well, so nothing
    recorded before the outage can count toward a promotion.
    """
    after = _enter(
        rt,
        HostState.YIELDING,
        now,
        rung=None,
        rung_since=now,
        rung_available_gb=0.0,
        settle_until=None,
        clear_since=None,
        window=(reading,),
    )
    return _decide(
        before,
        after,
        now,
        TransitionReason.RECOVERED,
        f"{rt.host} is readable again; yielding and re-measuring rather than trusting "
        "what we remembered",
    )


def _run_reservation_timers(
    rt: HostRuntime,
    sample: GpuSample,
    now: datetime,
    timings: StateTimings,
) -> tuple[HostRuntime, str | None, bool]:
    """Warn at ~25 minutes idle, auto-release at ~30 (docs/03 section 4.2).

    The timer resets the instant any CUDA process appears: someone who claims the card
    and then spends twenty minutes writing the script must not be punished for thinking
    first. Returns the runtime, a note for the log, and whether a release happened.
    """
    held = rt.reservation
    if held is None:
        return rt, None, False
    if sample.has_foreign_process:
        refreshed = held.model_copy(update={"last_cuda_at": now, "idle_warned": False})
        return rt.evolve(reservation=refreshed), None, False

    idle = now - held.last_cuda_at
    if idle >= timings.reservation_idle_release:
        note = (
            f"auto-released {held.holder}'s claim on {rt.host}: "
            f"{idle.total_seconds() / 60:.0f} min with no CUDA process"
        )
        return rt.evolve(reservation=None), note, True
    if idle >= timings.reservation_idle_warning and not held.idle_warned:
        left = (timings.reservation_idle_release - idle).total_seconds() / 60
        note = f"{held.holder}'s claim on {rt.host} auto-releases in {left:.0f} min"
        return rt.evolve(reservation=held.model_copy(update={"idle_warned": True})), note, False
    return rt, None, False


def _yield_now(
    before: HostRuntime,
    rt: HostRuntime,
    sample: GpuSample,
    now: datetime,
    timings: StateTimings,
    notes: tuple[str, ...],
) -> Decision:
    """FREE -> YIELDING. Immediate, unconditional, hysteresis-free. The human wins."""
    if sample.has_foreign_process:
        reason = TransitionReason.FOREIGN_PROCESS
        trigger = f"foreign CUDA process {list(sample.foreign_pids)} appeared"
    else:
        reason = TransitionReason.RESERVED
        trigger = "the host is claimed"
    after = _enter(
        rt,
        HostState.YIELDING,
        now,
        rung=None,
        rung_since=now,
        rung_available_gb=0.0,
        settle_until=None,
        clear_since=None,
    )
    return _decide(
        before,
        after,
        now,
        reason,
        f"{trigger} on {rt.host}; dropping {_name(before.rung)} to zero VRAM immediately",
        notes=notes,
    )


def _while_yielding(
    before: HostRuntime,
    rt: HostRuntime,
    config: HostConfig,
    sample: GpuSample,
    now: datetime,
    timings: StateTimings,
    notes: tuple[str, ...],
) -> Decision:
    """Hold at zero VRAM until the settle window closes, or the card comes back clear.

    The settle window is timed from the moment their *job* appears, not from the claim.
    Someone who flips the toggle and then spends ten minutes preparing should not have
    their warm-up measured against a window that expired while they were thinking; and a
    claim held over an empty card must never re-enter SHARING, because they said they
    are using this GPU and the only respectful answer to that is zero.
    """
    if not sample.has_foreign_process:
        rt = rt.evolve(settle_until=None)
        if rt.reservation is not None:
            return _decide(
                before,
                rt,
                now,
                TransitionReason.HELD,
                f"{rt.host} is claimed by {rt.reservation.holder} and empty; "
                "holding at zero until their job appears",
                notes=notes,
            )
        if _clear_long_enough(rt, now, timings):
            return _promote_to_free(before, rt, config, now, notes)
        left = _remaining_s(rt.clear_since, timings.clear_before_free, now)
        return _decide(
            before,
            rt,
            now,
            TransitionReason.AWAITING_CLEAR_WINDOW,
            f"{rt.host} reads clear; {left:.0f}s more before the top rung is earned",
            notes=notes,
        )

    settle_until = rt.settle_until
    if settle_until is None:
        settle_until = now + timings.settle
        rt = rt.evolve(settle_until=settle_until)
    if now < settle_until:
        return _decide(
            before,
            rt,
            now,
            TransitionReason.SETTLING,
            f"settling on {rt.host}: {(settle_until - now).total_seconds():.0f}s before we size "
            "anything against their job",
            notes=notes,
        )

    # The settle window *was* the wait, so this first sizing bypasses hysteresis.
    available = rt.window[-1].available_gb
    rung = select_rung(config.rungs, available, HostState.SHARING)
    after = _commit_rung(
        _enter(rt, HostState.SHARING, now, settle_until=None), rung, available, now
    )
    return _decide(
        before,
        after,
        now,
        TransitionReason.SETTLED,
        f"their job settled at {sample.used_gb:.1f} GB on {rt.host}; {available:.1f} GB free "
        f"fits {_name(rung)} while keeping {HostState.SHARING.headroom_gb:.0f} GB back",
        notes=notes,
    )


def _promote_to_free(
    before: HostRuntime,
    rt: HostRuntime,
    config: HostConfig,
    now: datetime,
    notes: tuple[str, ...],
) -> Decision:
    """Clear and unclaimed for the whole window. Only now do we take the top rung."""
    available = rt.window[-1].available_gb
    rung = select_rung(config.rungs, available, HostState.FREE)
    after = _commit_rung(_enter(rt, HostState.FREE, now, settle_until=None), rung, available, now)
    return _decide(
        before,
        after,
        now,
        TransitionReason.CARD_CLEAR,
        f"{rt.host} clear and unclaimed for the full window; promoting "
        f"{_name(before.rung)} -> {_name(rung)}",
        notes=notes,
    )


def _move_rung(
    before: HostRuntime,
    rt: HostRuntime,
    config: HostConfig,
    sample: GpuSample,
    now: datetime,
    timings: StateTimings,
    notes: tuple[str, ...],
    promote_ok: bool,
) -> Decision:
    """Rung movement inside FREE or SHARING. Hysteresis governs it -- with one exception.

    Hysteresis governs *voluntary* rung changes. A breached headroom is not voluntary.
    Anyone "simplifying" this function by folding the emergency branch below into the
    sustained path would reintroduce the exact contradiction docs/03 section 4.6 admits
    against its own acceptance test 3: a job whose VRAM ramps must make us drop a rung in
    seconds, and waiting out the 60 s sustain window can be precisely the OOM we exist to
    prevent. Pessimism is immediate; optimism is earned over the whole window.

    There is no voluntary-demotion branch, and that is not an omission -- see
    `_promotion_candidate`.
    """
    state = rt.state
    available = rt.window[-1].available_gb

    # EMERGENCY. `breaches_headroom` gets the raw reading, not `available`: the question
    # is what is left for the person right now, with our model resident.
    if breaches_headroom(rt.rung, sample.free_gb, state):
        target = select_rung(config.rungs, available, state)
        after = _commit_rung(rt, target, available, now)
        return _decide(
            before,
            after,
            now,
            TransitionReason.HEADROOM_BREACH,
            f"only {sample.free_gb:.1f} GB left on {rt.host} against a "
            f"{state.headroom_gb:.0f} GB floor; {_name(rt.rung)} -> {_name(target)} now, "
            "bypassing hysteresis",
            emergency=True,
            notes=notes,
        )

    if _footprint(select_rung(config.rungs, available, state)) <= _footprint(rt.rung):
        return _decide(
            before,
            rt,
            now,
            TransitionReason.HELD,
            f"{rt.host} steady on {_name(rt.rung)} with {available:.1f} GB available",
            notes=notes,
        )

    if not promote_ok:
        left = _remaining_s(rt.clear_since, timings.clear_before_free, now)
        return _decide(
            before,
            rt,
            now,
            TransitionReason.AWAITING_CLEAR_WINDOW,
            f"{rt.host} reads clear and a bigger rung would fit, but the top rung waits "
            f"{left:.0f}s more for the card to stay clear",
            notes=notes,
        )

    worst = min(r.available_gb for r in rt.window)
    earned = _promotion_candidate(rt, config, now, timings)
    if earned is None:
        span = (now - rt.window[0].at).total_seconds()
        return _decide(
            before,
            rt,
            now,
            TransitionReason.HYSTERESIS_SUPPRESSED,
            f"a bigger rung fits on {rt.host} at this instant, but the worst reading across "
            f"the last {span:.0f}s is {worst:.1f} GB and that window must span "
            f"{timings.sustain.total_seconds():.0f}s; staying on {_name(rt.rung)}",
            notes=notes,
        )

    drift = abs(worst - rt.rung_available_gb)
    if drift < timings.rung_change_gb:
        return _decide(
            before,
            rt,
            now,
            TransitionReason.HYSTERESIS_SUPPRESSED,
            f"{_name(earned)} would fit on {rt.host}, but free VRAM has moved only "
            f"{drift:.1f} GB since we chose {_name(rt.rung)} "
            f"(< {timings.rung_change_gb:.0f} GB); not swapping models for that",
            notes=notes,
        )

    after = _commit_rung(rt, earned, worst, now)
    return _decide(
        before,
        after,
        now,
        TransitionReason.SUSTAINED_CHANGE,
        f"at least {worst:.1f} GB has been available on {rt.host} for the whole "
        f"{timings.sustain.total_seconds():.0f}s window; {_name(rt.rung)} -> {_name(earned)}",
        notes=notes,
    )


def _promotion_candidate(
    rt: HostRuntime,
    config: HostConfig,
    now: datetime,
    timings: StateTimings,
) -> Rung | None:
    """A bigger rung we have *earned*, or None to stay put.

    Judged against the worst reading in the sustain window, never the latest: optimism
    must be earned, and a card that dipped once in the last minute has not proved it is
    free (docs/08 section 4.4).

    Why there is no matching voluntary-demotion branch. Given `ladder.py`'s single
    inequality, `select_rung` stops returning the resident rung at exactly the free-VRAM
    level where `breaches_headroom` starts firing: `footprint + headroom <= free +
    footprint` reduces to `headroom <= free`, which is the negation of the breach test.
    The rung boundary *is* the headroom floor, so every demotion is an emergency
    demotion and takes the branch in `_move_rung`; the sweep in test_state.py's
    `test_every_demotion_is_a_breach_so_there_is_no_voluntary_one` proves it rather
    than asserting it here. docs/08 section 4.4's four-row table
    lists "free VRAM falls, headroom still intact" as a separate, hysteresis-governed
    case; under the constraint that table means to implement, that row is unreachable.
    An unreachable branch here would look like protection without being any.
    """
    if not rt.window or now - rt.window[0].at < timings.sustain:
        return None  # too little history to justify optimism
    worst = min(r.available_gb for r in rt.window)
    from_worst = select_rung(config.rungs, worst, rt.state)
    return from_worst if _footprint(from_worst) > _footprint(rt.rung) else None


# --------------------------------------------------------------------------- rendering


def status(
    runtime: HostRuntime,
    config: HostConfig,
    now: datetime,
    *,
    timings: StateTimings = DEFAULT_TIMINGS,
) -> HostStatus:
    """Render for `/fleet/status` and the dashboard."""
    sample = runtime.last_sample
    held = runtime.reservation
    return HostStatus(
        host=runtime.host,
        state=runtime.state,
        free_gb=sample.free_gb if sample is not None else 0.0,
        total_gb=sample.total_gb if sample is not None else config.total_vram_gb,
        current_rung=runtime.rung.name if runtime.rung is not None else None,
        reserved_by=held.holder if held is not None else None,
        reserved_since=held.acquired_at if held is not None else None,
        last_sample_at=sample.sampled_at if sample is not None else None,
        message=status_line(runtime, now, timings=timings),
    )


def status_line(
    runtime: HostRuntime, now: datetime, *, timings: StateTimings = DEFAULT_TIMINGS
) -> str:
    """The one line docs/08 section 10.2 says nobody should have to guess at.

    It never says `ready` on the strength of an API response -- only a measurement gets
    it there -- and it never says `released` where it means `ready`, because "released"
    is something the platform did and "ready" is a statement about the user's card.
    """
    held = runtime.reservation
    if runtime.state is HostState.UNKNOWN:
        return "unknown - assuming in use"
    if held is not None and held.idle_warned:
        return f"auto-release in {auto_release_in_minutes(runtime, now, timings=timings):.0f} min"
    if runtime.state is HostState.YIELDING:
        settle_until = runtime.settle_until
        if settle_until is not None and now < settle_until:
            return f"settling... {(settle_until - now).total_seconds():.0f}s"
        return "ready"
    if held is not None:
        return f"YOURS - {(now - held.acquired_at).total_seconds() / 60:.0f} min - AI on leftovers"
    return f"rung: {_name(runtime.rung)}"


def auto_release_in_minutes(
    runtime: HostRuntime, now: datetime, *, timings: StateTimings = DEFAULT_TIMINGS
) -> float:
    """Minutes before an idle claim releases itself. 0.0 when nothing is claimed."""
    held = runtime.reservation
    if held is None:
        return 0.0
    left = timings.reservation_idle_release - (now - held.last_cuda_at)
    return max(0.0, left.total_seconds() / 60)
