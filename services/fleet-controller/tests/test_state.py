"""Tests for the per-host state machine.

Time is a parameter here, never a wall clock, so a five minute promotion window and a
thirty minute idle release cost the same microseconds as anything else. There is no
`time.sleep` in this file and there must never be one: a test that sleeps is a test
nobody runs, and these are the timings that decide whether the platform is welcome on
somebody else's workstation.

`Fleet` below models the *whole* loop, including our own residency -- `used_gb` is
their job plus whatever rung we hold -- because half the arithmetic in `state.py`
exists to separate those two, and a fixture that reported only their usage would let
that arithmetic be wrong without any test noticing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fleet_controller.ladder import breaches_headroom, select_rung
from fleet_controller.models import GpuSample, HostConfig, HostState, Rung
from fleet_controller.state import (
    DEFAULT_TIMINGS,
    Decision,
    HostRuntime,
    ReleaseKind,
    StateTimings,
    TransitionReason,
    initial,
    observe,
    release,
    reserve,
    status,
    status_line,
)

# The .226 ladder from docs/03 section 3, matching tests/test_ladder.py.
CODER = Rung(name="coder", served_model="qwen3-coder-30b-a3b-int4", footprint_gb=17.0)
CHAT14 = Rung(name="chat", served_model="qwen3-14b-int4", footprint_gb=9.0)
CHAT8 = Rung(name="chat-small", served_model="qwen3-8b-int4", footprint_gb=5.5)
CHAT4 = Rung(name="chat-tiny", served_model="qwen3-4b-int4", footprint_gb=3.0)

H226 = HostConfig(
    name="226",
    address="10.0.0.226",
    total_vram_gb=24.0,
    rungs=(CODER, CHAT14, CHAT8, CHAT4),
    notes="RTX 4090. Baseline is taken out by the agent before it reports (docs/08 7.2).",
)

T0 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)
POLL_S = 2  # docs/08 section 8.1: poll_interval_s
THEIR_PID = 41233


class Fleet:
    """One host under test, driven by explicit time. Never sleeps, never guesses."""

    def __init__(
        self,
        config: HostConfig = H226,
        start: datetime = T0,
        timings: StateTimings = DEFAULT_TIMINGS,
    ) -> None:
        self.config = config
        self.timings = timings
        self.now = start
        self.runtime: HostRuntime = initial(config, start)
        self.decisions: list[Decision] = []

    # -- driving ---------------------------------------------------------------

    def poll(
        self,
        *,
        their_gb: float = 0.0,
        seconds: int = POLL_S,
        missing: bool = False,
        stale_by_s: int = 0,
    ) -> Decision:
        """One turn of the control loop. `their_gb` is the user's job, not ours."""
        self.now += timedelta(seconds=seconds)
        sample = None
        if not missing:
            ours = self.runtime.rung.footprint_gb if self.runtime.rung is not None else 0.0
            sample = GpuSample(
                host=self.config.name,
                total_gb=self.config.total_vram_gb,
                used_gb=their_gb + ours,
                foreign_pids=(THEIR_PID,) if their_gb > 0 else (),
                sampled_at=self.now - timedelta(seconds=stale_by_s),
            )
        return self._record(
            observe(self.runtime, self.config, sample, self.now, timings=self.timings)
        )

    def poll_for(self, seconds: int, *, their_gb: float = 0.0) -> Decision:
        """Poll every ~2 s for `seconds`, returning the last decision."""
        last = self.decisions[-1]
        for _ in range(seconds // POLL_S):
            last = self.poll(their_gb=their_gb)
        return last

    def poll_until(
        self, reason: TransitionReason, *, within_s: int, their_gb: float = 0.0
    ) -> Decision:
        """Poll until `reason` arrives, and fail loudly if it never does.

        Timing assertions want the tick a thing *happened* on, not whatever the loop
        was saying some polls later -- which is usually the placid "held".
        """
        for _ in range(within_s // POLL_S):
            d = self.poll(their_gb=their_gb)
            if d.reason is reason:
                return d
        raise AssertionError(f"{reason} did not happen within {within_s}s")

    def reserve(self, holder: str = "aritra") -> Decision:
        return self._record(reserve(self.runtime, holder, self.now, timings=self.timings))

    def release(self, kind: ReleaseKind = ReleaseKind.EXPLICIT) -> Decision:
        return self._record(release(self.runtime, self.now, kind=kind))

    def _record(self, decision: Decision) -> Decision:
        self.runtime = decision.runtime
        self.decisions.append(decision)
        return decision

    # -- reading ---------------------------------------------------------------

    @property
    def state(self) -> HostState:
        return self.runtime.state

    @property
    def rung(self) -> str | None:
        return self.runtime.rung.name if self.runtime.rung is not None else None

    def state_sequence(self) -> list[HostState]:
        """The states actually visited, with repeats collapsed."""
        seen: list[HostState] = []
        for d in self.decisions:
            if not seen or seen[-1] is not d.state:
                seen.append(d.state)
        return seen


def reach_free(f: Fleet) -> None:
    """Cold start -> UNKNOWN -> a fresh reading -> the clear window -> top rung."""
    f.poll()
    f.poll_for(seconds=302)
    assert f.state is HostState.FREE
    assert f.rung == "coder"


def reach_sharing(f: Fleet, their_gb: float, *, launch_gb: float = 1.0) -> None:
    """Top rung, then their job launches small, grows, and we settle around it."""
    reach_free(f)
    f.poll(their_gb=launch_gb)
    f.poll_for(seconds=64, their_gb=their_gb)
    assert f.state is HostState.SHARING


# --------------------------------------------------------------------------- 1. free -> yielding


class TestFreeToYielding:
    """docs/03 section 2: a foreign process or a flipped toggle. No delay, ever."""

    def test_foreign_process_yields_on_the_very_same_sample(self) -> None:
        f = Fleet()
        reach_free(f)
        before = f.now
        d = f.poll(their_gb=4.0)
        assert d.state is HostState.YIELDING
        assert d.reason is TransitionReason.FOREIGN_PROCESS
        assert d.previous_rung == CODER
        assert d.rung is None, "we must be at zero VRAM, not merely a smaller model"
        assert d.at - before == timedelta(seconds=POLL_S), "no hysteresis on the way down"

    def test_toggle_yields_immediately_without_waiting_for_a_sample(self) -> None:
        f = Fleet()
        reach_free(f)
        d = f.reserve("aritra")
        assert d.state is HostState.YIELDING
        assert d.reason is TransitionReason.RESERVED
        assert d.rung is None
        assert d.runtime.reservation is not None
        assert d.runtime.reservation.holder == "aritra"

    def test_a_claim_over_an_empty_card_never_re_enters_sharing(self) -> None:
        """They said they are using this GPU. The only respectful answer is zero."""
        f = Fleet()
        reach_free(f)
        f.reserve("aritra")
        f.poll_for(seconds=600)  # ten minutes, no job launched yet
        assert f.state is HostState.YIELDING
        assert f.rung is None


# --------------------------------------------------------------------------- 2. settle


class TestSettleDelay:
    """docs/03 section 2 and acceptance test 3: the 60 s settle is not padding."""

    def test_a_ramping_job_is_sized_after_it_grows_not_when_it_launches(self) -> None:
        f = Fleet()
        reach_free(f)

        f.poll(their_gb=4.0)  # launch: 4 GB, and we drop the coder
        assert f.state is HostState.YIELDING

        # Their allocator grows 4 -> 11 GB across the settle window.
        for step in range(1, 31):
            d = f.poll(their_gb=4.0 + 7.0 * step / 30)
            assert d.state is HostState.YIELDING, "we must hold at zero while they warm up"
            assert d.rung is None

        d = f.poll(their_gb=11.0)
        assert d.state is HostState.SHARING
        assert d.reason is TransitionReason.SETTLED
        assert d.rung == CHAT14, "13 GB free fits the 9 GB model with 3 GB spare"

        naive = select_rung(H226.rungs, 24.0 - 4.0, HostState.SHARING)
        assert naive == CODER, "sizing against the launch reading would have taken 17 GB"
        assert d.rung != naive, "which is the whole reason the settle window exists"

    def test_the_settle_window_starts_when_their_job_appears_not_at_the_claim(self) -> None:
        f = Fleet()
        reach_free(f)
        f.reserve("aritra")
        f.poll_for(seconds=600)  # they spend ten minutes preparing
        launched = f.now
        f.poll(their_gb=11.0)
        f.poll_for(seconds=56, their_gb=11.0)
        assert f.state is HostState.YIELDING, "still settling, measured from the launch"
        d = f.poll_for(seconds=8, their_gb=11.0)
        assert d.state is HostState.SHARING
        assert timedelta(seconds=58) <= d.at - launched <= timedelta(seconds=66)


# --------------------------------------------------------------------------- 3. sharing -> free


class TestSharingToFree:
    """docs/03 section 4.6: ~5 minutes of a clear card before the top rung returns."""

    def test_promotion_waits_the_full_clear_window(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        assert f.rung == "chat-small"

        f.poll(their_gb=0.0)  # their job exits
        exited_at = f.now
        d = f.poll_for(seconds=290)
        assert d.state is HostState.SHARING, "one clear minute is not five"
        assert f.rung == "chat-small"

        d = f.poll_until(TransitionReason.CARD_CLEAR, within_s=30)
        assert d.state is HostState.FREE
        assert d.rung == CODER
        waited = (d.at - exited_at).total_seconds()
        assert 300 <= waited <= 312, f"waited {waited}s; the clear window is 300 s"

    def test_a_restarted_job_resets_the_clear_window(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        f.poll(their_gb=0.0)
        f.poll_for(seconds=240)
        f.poll(their_gb=13.0)  # they start the next run
        f.poll(their_gb=0.0)
        d = f.poll_for(seconds=240)
        assert d.state is not HostState.FREE, "the window restarts, it does not accumulate"

    def test_releasing_a_toggle_does_not_itself_promote(self) -> None:
        f = Fleet()
        reach_free(f)
        f.reserve("aritra")
        d = f.release()
        assert d.reason is TransitionReason.RELEASED
        assert d.runtime.reservation is None
        assert d.rung is None, "an API call is not evidence; promotion needs a measurement"
        assert d.state is HostState.YIELDING


# --------------------------------------------------------------------------- 4. unknown


class TestUnknown:
    """docs/08 section 3.4: absence of evidence is not evidence of a free GPU."""

    def test_three_failures_are_an_outage_and_one_is_not(self) -> None:
        f = Fleet()
        reach_free(f)
        for n in (1, 2):
            d = f.poll(missing=True)
            assert d.reason is TransitionReason.SAMPLE_MISSED
            assert d.state is HostState.FREE, f"one dropped poll is a network ({n} so far)"
            assert d.rung == CODER
        d = f.poll(missing=True)
        assert d.state is HostState.UNKNOWN
        assert d.reason is TransitionReason.HOST_UNREACHABLE
        assert d.rung is None, "unreadable hosts are pulled out of routing"

    def test_a_stale_reading_counts_as_a_failure(self) -> None:
        f = Fleet()
        reach_free(f)
        for _ in range(3):
            d = f.poll(stale_by_s=60)
        assert d.state is HostState.UNKNOWN

    def test_unknown_never_promotes_however_long_it_lasts(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        for _ in range(3):
            f.poll(missing=True)
        assert f.state is HostState.UNKNOWN

        for _ in range(200):
            d = f.poll(missing=True)
            assert d.state is HostState.UNKNOWN
            assert d.rung is None

    def test_unknown_never_promotes_on_a_stale_reading_of_an_empty_card(self) -> None:
        """The tempting bug: a cached 'card is empty' reading looks like a free host."""
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        for _ in range(3):
            f.poll(missing=True)
        for _ in range(200):
            d = f.poll(their_gb=0.0, stale_by_s=60)
            assert d.state is HostState.UNKNOWN
            assert d.rung is None

    def test_recovery_yields_and_re_measures_rather_than_trusting_memory(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        for _ in range(3):
            f.poll(missing=True)

        d = f.poll(their_gb=0.0)
        assert d.state is HostState.YIELDING
        assert d.reason is TransitionReason.RECOVERED
        assert d.rung is None
        assert len(d.runtime.window) == 1, "pre-outage readings cannot earn a promotion"

        d = f.poll_for(seconds=290)
        assert d.state is HostState.YIELDING, "the clear window starts now, not before the outage"
        d = f.poll_for(seconds=20)
        assert d.state is HostState.FREE


# --------------------------------------------------------------------------- 5. hysteresis


class TestHysteresisSuppresses:
    """docs/03 section 4.6 and acceptance test 7: bursty usage must not reload models."""

    def test_a_small_sustained_gain_does_not_buy_a_model_swap(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=13.0)  # 11.0 GB available -> chat-small
        assert f.rung == "chat-small"
        assert f.runtime.rung_available_gb == pytest.approx(11.0)

        # They free 1.3 GB. A bigger rung now fits -- but only just.
        d = f.poll_for(seconds=900, their_gb=11.7)
        assert f.rung == "chat-small", "1.3 GB of movement is not worth a server restart"
        assert d.reason is TransitionReason.HYSTERESIS_SUPPRESSED
        assert "moved only" in d.detail

    def test_a_single_dip_inside_the_window_withdraws_the_promotion(self) -> None:
        """Promote on the worst reading in the window. Optimism must be earned."""
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        f.poll_for(seconds=56, their_gb=9.0)
        f.poll(their_gb=13.0)  # one bursty sample, right before it would have committed
        d = f.poll_for(seconds=56, their_gb=9.0)
        assert f.rung == "chat-small", "the dip resets the clock it does not shorten it"
        assert d.reason is TransitionReason.HYSTERESIS_SUPPRESSED

    def test_a_large_gain_held_for_the_window_does_move_the_rung(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        moved_at = f.now

        d = f.poll_for(seconds=56, their_gb=9.0)
        assert f.rung == "chat-small", "not yet -- the window has not filled"
        assert d.reason is TransitionReason.HYSTERESIS_SUPPRESSED

        d = f.poll_until(TransitionReason.SUSTAINED_CHANGE, within_s=30, their_gb=9.0)
        assert d.rung == CHAT14
        elapsed = (d.at - moved_at).total_seconds()
        assert 60 <= elapsed <= 75, f"promotion took {elapsed}s; the window is 60 s"

    def test_a_clear_card_still_waits_five_minutes_not_sixty_seconds(self) -> None:
        """The sustain window must not become a back door around the top-rung wait."""
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        f.poll(their_gb=0.0)
        d = f.poll_for(seconds=120)
        assert d.reason is TransitionReason.AWAITING_CLEAR_WINDOW
        assert f.rung == "chat-small"


class TestHysteresisBypass:
    """docs/03 section 4.6 and acceptance test 3: a breach is not a voluntary change."""

    def test_a_headroom_breach_demotes_on_the_next_sample(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=11.0)
        assert f.rung == "chat"  # 13 GB available -> the 9 GB model

        d = f.poll(their_gb=12.0)
        assert d.reason is TransitionReason.HELD, "exactly 3 GB left is the floor, not a breach"
        assert d.rung == CHAT14

        breached_at = f.now
        d = f.poll(their_gb=12.5)  # 2.5 GB left: below the floor
        assert d.reason is TransitionReason.HEADROOM_BREACH
        assert d.emergency is True
        assert d.rung == CHAT8, "drop a rung rather than let their job OOM"
        assert d.at - breached_at == timedelta(seconds=POLL_S), (
            "the emergency path must not wait out the 60 s sustain window"
        )

    def test_growth_keeps_dropping_rungs_all_the_way_to_nothing(self) -> None:
        """docs/03 section 3: below the bottom rung we load nothing and route elsewhere."""
        f = Fleet()
        reach_sharing(f, their_gb=11.0)
        for their in (12.5, 15.0, 18.0, 21.0):
            f.poll(their_gb=their)
        assert f.rung is None
        assert f.state is HostState.SHARING, "still sharing -- there is simply no rung that fits"

    def test_every_demotion_is_a_breach_so_there_is_no_voluntary_one(self) -> None:
        """The claim `state._promotion_candidate` makes in prose, swept not asserted.

        `footprint + headroom <= free + footprint` reduces to `headroom <= free`, so a
        resident rung stops fitting at exactly the free-VRAM level where the headroom
        floor is breached. docs/08 section 4.4 lists "free VRAM falls, headroom still
        intact" as a separate hysteresis-governed case; under the inequality that table
        means to implement, that row cannot occur. If this test ever fails, the two
        definitions have drifted apart and `_move_rung` needs a demotion branch after
        all -- which is exactly when someone would want to know.
        """
        for rung in (CODER, CHAT14, CHAT8, CHAT4):
            for tenth in range(0, 241):
                free = tenth / 10
                picked = select_rung(H226.rungs, free + rung.footprint_gb, HostState.SHARING)
                still_fits = picked is not None and picked.footprint_gb >= rung.footprint_gb
                assert still_fits is not breaches_headroom(rung, free, HostState.SHARING), (
                    f"{rung.name} at {free} GB free is neither clearly safe nor clearly breached"
                )

    def test_the_breach_bypass_beats_the_change_threshold_too(self) -> None:
        """A breach on a movement smaller than 2 GB must still act. Fail safe, not tidy."""
        f = Fleet()
        reach_sharing(f, their_gb=11.0)
        d = f.poll(their_gb=12.6)  # 1.6 GB of movement, but the floor is breached
        assert d.emergency is True
        assert d.rung == CHAT8


# --------------------------------------------------------------------------- 6. reservations


class TestReservations:
    """docs/03 section 4.2: forgetting the toggle is handled, not policed."""

    def test_an_idle_claim_warns_then_auto_releases(self) -> None:
        f = Fleet()
        reach_free(f)
        f.reserve("aritra")
        claimed_at = f.now

        warned: Decision | None = None
        released: Decision | None = None
        for _ in range(40):
            d = f.poll(seconds=60)
            if warned is None and d.notes:
                warned = d
            if d.reason is TransitionReason.RESERVATION_AUTO_RELEASED:
                released = d
                break

        assert warned is not None, "docs/03 4.2 requires a visible warning first"
        assert warned.at - claimed_at == timedelta(minutes=25)
        assert "auto-releases in 5 min" in warned.notes[0]
        assert warned.runtime.reservation is not None

        assert released is not None
        assert released.at - claimed_at == timedelta(minutes=30)
        assert released.runtime.reservation is None
        assert "no CUDA process" in released.detail

    def test_the_idle_timer_resets_the_instant_a_job_appears(self) -> None:
        f = Fleet()
        reach_free(f)
        f.reserve("aritra")
        for _ in range(25):
            f.poll(seconds=60)  # 25 idle minutes
        f.poll(seconds=60, their_gb=8.0)  # they finally launch
        assert f.runtime.reservation is not None

        for _ in range(25):
            f.poll(seconds=60, their_gb=8.0)
        assert f.runtime.reservation is not None, "a running job must never be auto-released"
        assert not f.runtime.reservation.idle_warned

    def test_auto_release_is_followed_by_the_ordinary_clear_window(self) -> None:
        f = Fleet()
        reach_free(f)
        f.reserve("aritra")
        for _ in range(31):
            f.poll(seconds=60)
        assert f.runtime.reservation is None
        assert f.state is HostState.YIELDING, "released, but the top rung is still not earned"
        d = f.poll_for(seconds=302)
        assert d.state is HostState.FREE
        assert d.rung == CODER

    def test_a_blind_controller_cannot_release_somebody_elses_claim(self) -> None:
        f = Fleet()
        reach_free(f)
        f.reserve("aritra")
        for _ in range(40):
            f.poll(seconds=60, missing=True)
        assert f.state is HostState.UNKNOWN
        assert f.runtime.reservation is not None, (
            "auto-release needs evidence of no CUDA process; an unreachable host gives none"
        )

    def test_reserving_twice_is_harmless(self) -> None:
        f = Fleet()
        reach_free(f)
        first = f.reserve("aritra")
        again = f.reserve("aritra")
        assert again.reason is TransitionReason.HELD
        assert not again.changed
        assert again.runtime.reservation == first.runtime.reservation

    def test_a_second_person_does_not_take_over_a_held_host(self) -> None:
        f = Fleet()
        reach_free(f)
        f.reserve("aritra")
        d = f.reserve("priya")
        assert d.reason is TransitionReason.HELD
        assert d.runtime.reservation is not None
        assert d.runtime.reservation.holder == "aritra"

    def test_releasing_an_unclaimed_host_is_a_no_op(self) -> None:
        f = Fleet()
        reach_free(f)
        d = f.release(kind=ReleaseKind.FORCE)
        assert d.reason is TransitionReason.HELD
        assert not d.changed


# --------------------------------------------------------------------------- 7. sequences


class TestSequences:
    """A day on .226, as a list of states. The test that catches ordering mistakes."""

    def test_a_full_session_visits_the_expected_states_in_order(self) -> None:
        f = Fleet()
        reach_free(f)  # cold start -> unknown -> yielding -> free
        f.reserve("aritra")  # toggle
        f.poll(their_gb=4.0)  # they launch
        f.poll_for(seconds=64, their_gb=11.0)  # ramp, settle, share
        f.poll(their_gb=12.5)  # growth -> emergency demotion
        f.release()  # toggle off
        f.poll_for(seconds=310)  # job gone, card clear

        assert f.state_sequence() == [
            HostState.YIELDING,  # first reading out of a cold UNKNOWN start
            HostState.FREE,
            HostState.YIELDING,
            HostState.SHARING,
            HostState.FREE,
        ]
        assert f.rung == "coder"

    def test_the_rungs_along_that_session_only_shrink_while_they_are_working(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=11.0)
        sizes = [f.runtime.rung]
        for their in (12.5, 15.0, 18.0):
            f.poll(their_gb=their)
            sizes.append(f.runtime.rung)
        footprints = [r.footprint_gb if r else 0.0 for r in sizes]
        assert footprints == sorted(footprints, reverse=True)
        assert footprints[0] > footprints[-1]

    def test_a_bursty_job_causes_no_actuation_storm(self) -> None:
        """Acceptance test 7. The pass criterion is a number, not an impression."""
        f = Fleet()
        reach_sharing(f, their_gb=13.0)
        start = len(f.decisions)
        for _ in range(60):
            f.poll(their_gb=13.0)
            f.poll(their_gb=12.2)
        actuations = [d for d in f.decisions[start:] if d.rung_changed]
        assert actuations == [], f"{len(actuations)} model swaps for a job that never grew"


# --------------------------------------------------------------------------- 8. explanations


class TestDecisionsExplainThemselves:
    """docs/03 section 5: an unexplained quality drop erodes trust faster than the drop."""

    def test_every_decision_carries_a_reason_and_a_sentence(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=11.0)
        f.poll(their_gb=12.5)
        f.poll(missing=True)
        for d in f.decisions:
            assert d.detail.strip(), f"{d.reason} shipped without an explanation"
            assert d.host == "226"
            assert isinstance(d.reason, TransitionReason)

    def test_a_rung_change_names_both_ends_of_the_swap(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=11.0)
        d = f.poll(their_gb=12.5)
        assert "chat" in d.detail and "chat-small" in d.detail

    def test_status_line_uses_the_vocabulary_the_dashboard_promises(self) -> None:
        f = Fleet()
        assert status_line(f.runtime, f.now) == "unknown - assuming in use"

        reach_free(f)
        assert status_line(f.runtime, f.now) == "rung: coder"

        f.reserve("aritra")
        f.poll(their_gb=8.0)
        assert status_line(f.runtime, f.now).startswith("settling...")

        f.poll_for(seconds=64, their_gb=8.0)
        assert "YOURS" in status_line(f.runtime, f.now)
        assert "AI on leftovers" in status_line(f.runtime, f.now)

    def test_status_reports_what_was_measured_not_what_was_hoped(self) -> None:
        f = Fleet()
        reach_sharing(f, their_gb=11.0)
        s = status(f.runtime, H226, f.now)
        assert s.state is HostState.SHARING
        assert s.current_rung == "chat"
        assert s.free_gb == pytest.approx(24.0 - 11.0 - 9.0)
        assert s.last_sample_at == f.now


# --------------------------------------------------------------------------- 9. guards


class TestGuards:
    def test_a_sample_from_the_wrong_host_is_a_bug_not_a_reading(self) -> None:
        f = Fleet()
        wrong = GpuSample(host="87", total_gb=12.0, used_gb=1.0, sampled_at=f.now)
        with pytest.raises(ValueError, match="fed to the '226' machine"):
            observe(f.runtime, H226, wrong, f.now)

    def test_a_cold_controller_starts_unknown_not_free(self) -> None:
        """docs/08 section 8.2: measurement outranks memory, including on restart."""
        rt = initial(H226, T0)
        assert rt.state is HostState.UNKNOWN
        assert rt.rung is None

    def test_timings_are_configurable_per_host(self) -> None:
        """docs/03 section 3: .210 is somebody's daily workstation. Let it be timid."""
        timid = StateTimings(settle=timedelta(seconds=120), clear_before_free=timedelta(minutes=15))
        f = Fleet(timings=timid)
        f.poll()
        f.poll_for(seconds=302)
        assert f.state is HostState.YIELDING, "15 minutes means 15 minutes"
        f.poll_for(seconds=600)
        assert f.state is HostState.FREE

    def test_decisions_are_immutable_and_carry_the_next_runtime(self) -> None:
        f = Fleet()
        d = f.poll()
        assert d.runtime is f.runtime
        with pytest.raises(ValueError, match="frozen"):
            d.runtime.state = HostState.FREE  # type: ignore[misc]
