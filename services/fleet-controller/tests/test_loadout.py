"""Tests for whole-loadout selection.

These exist because of a bug found while writing the deploy config: docs/03 gives
`.87` a ">= 9.2 GB" band for "embeddings + small chat", which sums 1.2 + 5.0 + 3.0
and is correct -- but `select_rung` answers "which single model fits" and would
have picked the 5 GB chat model at 8 GB free, then had embeddings loaded on top,
leaving 1.8 GB instead of the promised 3 GB.

Same arithmetic error as the original band tables, one level up: it is not enough
for the rule to be right about one model, it has to be right about everything
resident at once.
"""

from __future__ import annotations

import pytest
from fleet_controller.ladder import loadout_footprint_gb, select_loadout, select_rung
from fleet_controller.models import HostState, Rung

# .87 -- embeddings stay resident alongside whatever chat model is chosen.
EMBED = Rung(name="embed", served_model="qwen3-embedding-0.6b", footprint_gb=1.2, always_on=True)
CHAT_SMALL = Rung(name="chat-small", served_model="qwen3-8b-int4", footprint_gb=5.0)
H87 = (EMBED, CHAT_SMALL)


class TestAlwaysOnIsPaidFor:
    def test_the_bug_that_prompted_this(self) -> None:
        """At 8 GB free, the pair does not fit -- only embeddings should load."""
        assert select_rung(H87, 8.0, HostState.SHARING) == CHAT_SMALL  # the wrong question
        assert select_loadout(H87, 8.0, HostState.SHARING) == (EMBED,)  # the right one

    def test_docs_03_band_edge(self) -> None:
        """docs/03: >= 9.2 GB gets embeddings + small chat. 1.2 + 5.0 + 3.0."""
        assert set(select_loadout(H87, 9.2, HostState.SHARING)) == {EMBED, CHAT_SMALL}
        assert select_loadout(H87, 9.1, HostState.SHARING) == (EMBED,)

    def test_embeddings_survive_when_chat_cannot(self) -> None:
        """Losing embeddings stops ingestion and every RAG query; losing chat costs quality."""
        assert select_loadout(H87, 4.2, HostState.SHARING) == (EMBED,)
        assert select_loadout(H87, 4.1, HostState.SHARING) == ()

    def test_headroom_holds_across_the_whole_loadout(self) -> None:
        """Sweep. The property the single-rung selector could not guarantee."""
        for tenth in range(0, 121):
            free = tenth / 10
            loadout = select_loadout(H87, free, HostState.SHARING)
            remaining = free - loadout_footprint_gb(loadout)
            if loadout:
                assert remaining >= HostState.SHARING.headroom_gb - 1e-9, (
                    f"at {free} GB free, loadout leaves only {remaining:.2f} GB"
                )

    def test_free_state_reaches_further(self) -> None:
        assert set(select_loadout(H87, 7.2, HostState.FREE)) == {EMBED, CHAT_SMALL}
        assert select_loadout(H87, 7.2, HostState.SHARING) == (EMBED,)


class TestNonLoadingStates:
    @pytest.mark.parametrize("state", [HostState.YIELDING, HostState.UNKNOWN])
    def test_loads_nothing(self, state: HostState) -> None:
        assert select_loadout(H87, 12.0, state) == ()


class TestHostsWithoutAlwaysOn:
    def test_matches_select_rung(self) -> None:
        """.226 has no always_on rung, so both selectors must agree."""
        coder = Rung(name="coder", served_model="qwen3-coder-30b-a3b-int4", footprint_gb=17.0)
        chat = Rung(name="chat", served_model="qwen3-14b-int4", footprint_gb=9.0)
        rungs = (coder, chat)
        for tenth in range(0, 241):
            free = tenth / 10
            single = select_rung(rungs, free, HostState.SHARING)
            loadout = select_loadout(rungs, free, HostState.SHARING)
            assert loadout == ((single,) if single else ())

    def test_only_one_optional_rung_is_chosen(self) -> None:
        """Never stack two chat models just because they both fit."""
        a = Rung(name="a", served_model="a", footprint_gb=3.0)
        b = Rung(name="b", served_model="b", footprint_gb=2.0)
        assert len(select_loadout((a, b), 24.0, HostState.FREE)) == 1
