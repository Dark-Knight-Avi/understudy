"""Tests for rung selection.

Several of these encode a bug that was live in docs/03 for a day: the published
band tables promised 3 GB of headroom while their lower edges delivered as little
as 1 GB. They exist to stop that recurring, so do not relax them without changing
the policy doc first.
"""

from __future__ import annotations

import pytest
from fleet_controller.ladder import (
    breaches_headroom,
    describe_ladder,
    minimum_free_for,
    select_rung,
)
from fleet_controller.models import HostState, Rung

# The .226 ladder from docs/03 section 3. Footprints are weights-only estimates
# pending measurement in M0 spike 1 / M2.
CODER = Rung(name="coder", served_model="qwen3-coder-30b-a3b-int4", footprint_gb=17.0)
CHAT14 = Rung(name="chat", served_model="qwen3-14b-int4", footprint_gb=9.0)
CHAT8 = Rung(name="chat-small", served_model="qwen3-8b-int4", footprint_gb=5.5)
CHAT4 = Rung(name="chat-tiny", served_model="qwen3-4b-int4", footprint_gb=3.0)
H226 = (CODER, CHAT14, CHAT8, CHAT4)


class TestSharingThresholds:
    """Sharing keeps 3 GB free. These are the corrected band edges."""

    @pytest.mark.parametrize(
        ("free_gb", "expected"),
        [
            (24.0, "coder"),
            (20.0, "coder"),
            (19.9, "chat"),
            (12.0, "chat"),
            (11.9, "chat-small"),
            (8.5, "chat-small"),
            (8.4, "chat-tiny"),
            (6.0, "chat-tiny"),
            (5.9, None),
            (0.0, None),
        ],
    )
    def test_band_edges(self, free_gb: float, expected: str | None) -> None:
        got = select_rung(H226, free_gb, HostState.SHARING)
        assert (got.name if got else None) == expected

    def test_never_leaves_less_than_promised_headroom(self) -> None:
        """The property the old band table violated. Sweep, do not spot-check."""
        for tenth in range(0, 241):
            free = tenth / 10
            rung = select_rung(H226, free, HostState.SHARING)
            if rung is not None:
                remaining = free - rung.footprint_gb
                assert remaining >= HostState.SHARING.headroom_gb - 1e-9, (
                    f"at {free} GB free, {rung.name} leaves only {remaining:.2f} GB"
                )

    def test_old_band_table_would_have_failed(self) -> None:
        """Regression: docs/03 once said 7-12 GB -> the 5.5 GB model."""
        rung = select_rung(H226, 7.0, HostState.SHARING)
        assert rung is not None
        assert rung.name == "chat-tiny", "7 GB free must not load the 5.5 GB model"


class TestFreeState:
    """A free host only keeps 1 GB back, so it reaches further down the ladder."""

    def test_top_rung_needs_less_when_free(self) -> None:
        assert select_rung(H226, 18.0, HostState.FREE) == CODER
        assert select_rung(H226, 18.0, HostState.SHARING) == CHAT14

    def test_headroom_values(self) -> None:
        assert HostState.FREE.headroom_gb == 1.0
        assert HostState.SHARING.headroom_gb == 3.0


class TestNonLoadingStates:
    @pytest.mark.parametrize("state", [HostState.YIELDING, HostState.UNKNOWN])
    def test_loads_nothing(self, state: HostState) -> None:
        """UNKNOWN especially: an unseeable card is assumed to be in use."""
        assert select_rung(H226, 24.0, state) is None


class TestHelpers:
    def test_minimum_free_for(self) -> None:
        assert minimum_free_for(CHAT8, HostState.SHARING) == pytest.approx(8.5)
        assert minimum_free_for(CHAT8, HostState.FREE) == pytest.approx(6.5)

    def test_breaches_headroom_triggers_emergency_demotion(self) -> None:
        assert breaches_headroom(CHAT8, free_gb=1.0, state=HostState.SHARING) is True
        assert breaches_headroom(CHAT8, free_gb=4.0, state=HostState.SHARING) is False

    def test_breaches_headroom_is_false_when_holding_nothing(self) -> None:
        assert breaches_headroom(None, free_gb=0.0, state=HostState.SHARING) is False

    def test_describe_ladder_is_largest_first(self) -> None:
        described = describe_ladder(H226, HostState.SHARING)
        assert described[0] == ("coder", 20.0)
        assert [n for n, _ in described] == ["coder", "chat", "chat-small", "chat-tiny"]


class TestOtherHosts:
    def test_87_embeddings_get_no_headroom_exemption(self) -> None:
        """Losing embeddings costs us latency; starving their job costs them a day."""
        embed = Rung(name="embed", served_model="qwen3-embedding-0.6b", footprint_gb=1.2)
        small = Rung(name="chat-small", served_model="qwen3-8b-int4", footprint_gb=5.0)
        rungs = (embed, small)
        assert select_rung(rungs, 9.2, HostState.SHARING) == small
        assert select_rung(rungs, 4.2, HostState.SHARING) == embed
        assert select_rung(rungs, 4.1, HostState.SHARING) is None
