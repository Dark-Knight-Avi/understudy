"""Tests for the shared machinery: envelope, artefact store, breaker, shingles.

These are the pieces every tool depends on, so a bug here is five bugs. The
shingle tests in particular encode the only defence we have against the
chunk-in-query leak (docs/16 section 6.1), which is enforced by our own logic
rather than by the network -- the one place the egress guarantee degrades from
"structurally impossible" to "caught most of the time".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_tools.tools import (
    CircuitBreaker,
    RecentContext,
    ok,
    refused,
    safe_filename,
    store_artifact,
    truncate,
    unavailable,
)


class TestEnvelope:
    """One shape, success or failure, so no client has to special-case a tool."""

    def test_success_carries_ok_true(self) -> None:
        assert ok(results=[]) == {"ok": True, "results": []}

    def test_unavailable_names_the_service(self) -> None:
        result = unavailable("ragflow", "connection refused")
        assert result == {
            "ok": False,
            "error": "unavailable",
            "service": "ragflow",
            "detail": "connection refused",
        }

    def test_refused_is_distinct_from_unavailable(self) -> None:
        """Retrying an unavailable service is sensible; retrying a refusal is a loop."""
        assert refused("web_search", "disabled")["error"] == "refused"
        assert unavailable("searxng", "down")["error"] == "unavailable"


class TestArtifactStore:
    def test_writes_under_a_random_id_not_the_supplied_name(self, tmp_path: Path) -> None:
        """A model-supplied name must never reach the filesystem."""
        result = store_artifact(
            b"%PDF-1.7",
            suffix=".pdf",
            filename="../../etc/passwd",
            directory=tmp_path,
            base_url="https://ai.internal/artifacts",
        )
        written = list(tmp_path.iterdir())
        assert len(written) == 1
        assert written[0].name == f"{result['file_id']}.pdf"
        assert ".." not in result["filename"]
        assert result["url"].endswith(f"{result['file_id']}.pdf")
        assert result["bytes"] == 8

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Quarterly report", "Quarterly report.pdf"),
            ("../../etc/passwd", "etc passwd.pdf"),
            ("report.pdf", "report.pdf"),
            ("", "artifact.pdf"),
            ("///", "artifact.pdf"),
        ],
    )
    def test_display_names_cannot_act_as_paths(self, raw: str, expected: str) -> None:
        assert safe_filename(raw, ".pdf") == expected


class TestTruncate:
    def test_leaves_short_text_alone(self) -> None:
        assert truncate("short", 20) == "short"

    def test_caps_with_an_ellipsis(self) -> None:
        capped = truncate("x" * 200, 140)
        assert len(capped) == 140
        assert capped.endswith("…")


class TestCircuitBreaker:
    """docs/14 section 4.4 rule 5: fail fast rather than wait out every timeout."""

    def test_opens_after_the_threshold_and_recovers_after_the_cooldown(self) -> None:
        breaker = CircuitBreaker(failures=3, cooldown_s=60.0)
        assert breaker.allow()
        breaker.record_failure(now=0.0)
        breaker.record_failure(now=0.0)
        assert breaker.allow(now=0.0), "two failures is not yet a pattern"
        breaker.record_failure(now=0.0)
        assert not breaker.allow(now=0.0)
        assert not breaker.allow(now=59.0)
        assert breaker.allow(now=60.0)

    def test_one_success_resets_the_count(self) -> None:
        """Otherwise three failures spread over a day would open the breaker."""
        breaker = CircuitBreaker(failures=3, cooldown_s=60.0)
        breaker.record_failure(now=0.0)
        breaker.record_failure(now=0.0)
        breaker.record_success()
        breaker.record_failure(now=0.0)
        assert breaker.allow(now=0.0)


class TestRecentContext:
    """The chunk-in-query heuristic. Approximate by construction -- see docs/16 s6.1."""

    PASSAGE = (
        "The gateway routes a request to whichever host the fleet controller "
        "reports as free, falling back to the deep tier when none is."
    )

    def test_catches_a_sentence_lifted_from_a_retrieved_passage(self) -> None:
        recent = RecentContext(shingle_words=12)
        recent.remember([self.PASSAGE])
        assert recent.overlaps(
            "the gateway routes a request to whichever host the fleet controller reports as free"
        )

    def test_ignores_a_query_the_model_phrased_itself(self) -> None:
        recent = RecentContext(shingle_words=12)
        recent.remember([self.PASSAGE])
        assert not recent.overlaps("how does load balancing work between GPU hosts")

    def test_short_queries_never_match(self) -> None:
        """A query shorter than the shingle cannot contain one, so it cannot be a paste."""
        recent = RecentContext(shingle_words=12)
        recent.remember([self.PASSAGE])
        assert not recent.overlaps("gateway routing")

    def test_matching_is_case_and_punctuation_insensitive(self) -> None:
        """A paste that survives a capitalisation is still a paste."""
        recent = RecentContext(shingle_words=12)
        recent.remember([self.PASSAGE])
        assert recent.overlaps(
            "THE GATEWAY ROUTES A REQUEST TO WHICHEVER HOST THE FLEET CONTROLLER REPORTS AS FREE!"
        )

    def test_forgets_old_passages(self) -> None:
        """Bounded memory: this is a heuristic cache, not a session store."""
        recent = RecentContext(shingle_words=12, max_passages=2)
        recent.remember([self.PASSAGE])
        recent.remember(["word " * 20, "other " * 20])
        assert not recent.overlaps(
            "the gateway routes a request to whichever host the fleet controller reports as free"
        )
