"""Tests for the two-signal foreign-GPU-use detection.

Enumeration alone is blind under WSL2: the Linux-side process list does not show
Windows-side CUDA work, so a card genuinely holding someone's 8 GB job can
enumerate as completely empty. The controller would then read "free" and load a
17 GB model on top of their run. Subtraction catches that case; enumeration
catches the case where our own footprint is misconfigured. Neither is sufficient,
so the rule is a disjunction.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fleet_controller.ladder import always_on_rungs, select_rung
from fleet_controller.models import FOREIGN_NOISE_FLOOR_GB, GpuSample, HostState, Rung

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def sample(**kw: object) -> GpuSample:
    base: dict[str, object] = {
        "host": "226",
        "total_gb": 24.0,
        "used_gb": 0.0,
        "sampled_at": NOW,
    }
    base.update(kw)
    return GpuSample(**base)  # type: ignore[arg-type]


class TestTwoSignals:
    def test_enumeration_alone_triggers(self) -> None:
        assert sample(foreign_pids=(4321,)).has_foreign_process is True

    def test_subtraction_alone_triggers(self) -> None:
        """The WSL2 case: nothing enumerated, 8 GB plainly in use."""
        s = sample(used_gb=8.0, foreign_pids=(), foreign_used_gb=8.0)
        assert s.has_foreign_process is True

    def test_neither_signal_means_free(self) -> None:
        assert sample(foreign_used_gb=0.0).has_foreign_process is False

    def test_unknown_subtraction_is_not_zero(self) -> None:
        """None means 'could not compute', which must never read as 'nothing foreign'."""
        s = sample(foreign_used_gb=None)
        assert s.foreign_used_gb is None
        assert s.has_foreign_process is False  # enumeration still governs

    @pytest.mark.parametrize(
        ("foreign_gb", "expected"),
        [(0.0, False), (0.2, False), (FOREIGN_NOISE_FLOOR_GB, False), (0.5, True), (8.0, True)],
    )
    def test_noise_floor(self, foreign_gb: float, expected: bool) -> None:
        """A desktop compositor holding ~200 MiB must not pin a whole host off.

        Same failure shape as naive interactive-login detection: a permanently
        present process means a permanently unavailable machine.
        """
        assert sample(foreign_used_gb=foreign_gb).has_foreign_process is expected


class TestAlwaysOnIsNotACandidate:
    """always_on rungs are permanent residents, not alternatives to a chat model."""

    EMBED = Rung(name="embed", served_model="e", footprint_gb=1.2, always_on=True)
    CHAT = Rung(name="chat-small", served_model="c", footprint_gb=5.0)

    def test_select_rung_ignores_always_on(self) -> None:
        rungs = (self.EMBED, self.CHAT)
        assert select_rung(rungs, 9.0, HostState.SHARING) == self.CHAT

    def test_never_returns_an_always_on_rung(self) -> None:
        """Even when only the always_on rung would fit, it is not a 'choice'.

        Returning it would have the host report a rung change to embeddings, as
        though it had swapped its chat model for the embedder.
        """
        rungs = (self.EMBED, self.CHAT)
        assert select_rung(rungs, 4.5, HostState.SHARING) is None

    def test_always_on_rungs_listed_smallest_first(self) -> None:
        big = Rung(name="big", served_model="b", footprint_gb=4.0, always_on=True)
        assert always_on_rungs((big, self.EMBED, self.CHAT)) == (self.EMBED, big)

    def test_host_without_always_on_is_unaffected(self) -> None:
        coder = Rung(name="coder", served_model="q", footprint_gb=17.0)
        assert select_rung((coder,), 24.0, HostState.SHARING) == coder
