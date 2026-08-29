"""Tests for `web_search` -- the platform's only outbound surface.

The important tests here are the negative ones. M6 acceptance test 7 and docs/16
section 7.5 criterion 8 both demand that a disabled workspace produces **no
outbound packet**, verified by capture rather than by reading the code. A packet
capture is not something a unit test can do, so the equivalent here is a
transport that fails the test if it is ever called.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from mcp_tools.tools import CircuitBreaker, RecentContext
from mcp_tools.tools.websearch import SearxngBackend, web_search

SEARX_BODY = {
    "results": [
        {
            "title": "IEEE 802.1Q-2022 overview",
            "url": "https://example.org/8021q",
            "content": "A summary of the VLAN tagging standard.",
            "engine": "duckduckgo",
        }
    ]
}


def _tripwire(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"a refused search still reached the network: {request.url}")


def backend(
    handler: httpx.MockTransport | None = None,
) -> tuple[SearxngBackend, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SEARX_BODY)

    transport = handler or httpx.MockTransport(record)
    return (
        SearxngBackend(
            base_url="http://searxng:8080",
            timeout_s=5.0,
            client=httpx.AsyncClient(transport=transport),
        ),
        seen,
    )


def call(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "query": "vlan tagging standard",
        "max_results": 5,
        "enabled": True,
        "max_query_chars": 200,
        "recent": RecentContext(),
        "breaker": CircuitBreaker(),
    }
    kwargs.update(overrides)
    query = kwargs.pop("query")
    max_results = kwargs.pop("max_results")
    return asyncio.run(web_search(query, max_results, **kwargs))


class TestTheSwitch:
    """ADR-0004: off by default, per workspace, and enforced before any I/O."""

    def test_disabled_refuses_without_touching_the_network(self) -> None:
        disabled, _ = backend(httpx.MockTransport(_tripwire))
        result = call(backend=disabled, enabled=False)
        assert result["error"] == "refused"
        assert "disabled" in result["detail"]

    def test_enabled_actually_searches(self) -> None:
        searx, seen = backend()
        result = call(backend=searx)
        assert result["ok"] is True
        assert result["results"][0]["url"] == "https://example.org/8021q"
        assert len(seen) == 1
        assert seen[0].url.params["format"] == "json"


class TestChunkInQuery:
    """docs/16 section 6.1 -- the leak that does not look like a mistake."""

    PASSAGE = (
        "Every duration is computed from the controller's receipt time, never from "
        "a timestamp the agent sent, so clock skew cannot produce a negative lease."
    )

    def test_a_query_lifted_from_a_retrieved_passage_is_refused(self) -> None:
        searx, _ = backend(httpx.MockTransport(_tripwire))
        recent = RecentContext(shingle_words=12)
        recent.remember([self.PASSAGE])
        result = call(
            backend=searx,
            recent=recent,
            query=(
                "every duration is computed from the controller's receipt time "
                "never from a timestamp"
            ),
        )
        assert result["error"] == "refused"
        assert "own words" in result["detail"]

    def test_a_long_query_is_refused_before_the_overlap_check_can_miss_it(self) -> None:
        """Layer 1 exists because layer 2 is a heuristic. A 600-char query is a paste."""
        searx, _ = backend(httpx.MockTransport(_tripwire))
        result = call(backend=searx, query="a " * 200)
        assert result["error"] == "refused"
        assert "200" in result["detail"]

    def test_an_unrelated_query_still_goes_out(self) -> None:
        """The guard must not make search useless -- that is what the switch is for."""
        searx, seen = backend()
        recent = RecentContext(shingle_words=12)
        recent.remember([self.PASSAGE])
        assert call(backend=searx, recent=recent, query="ntp clock skew best practice")["ok"]
        assert len(seen) == 1


class TestResults:
    def test_max_results_is_capped(self) -> None:
        many = {"results": [{"url": f"https://e/{i}", "title": str(i)} for i in range(50)]}
        searx, _ = backend(httpx.MockTransport(lambda _: httpx.Response(200, json=many)))
        assert len(call(backend=searx, max_results=99)["results"]) == 10

    def test_results_without_a_url_are_dropped(self) -> None:
        """A citation with no link is not a citation."""
        body = {"results": [{"title": "no link"}, {"title": "ok", "url": "https://e/1"}]}
        searx, _ = backend(httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
        assert len(call(backend=searx)["results"]) == 1


class TestDegradation:
    @pytest.mark.parametrize(
        "transport",
        [
            httpx.MockTransport(lambda _: httpx.Response(502)),
            httpx.MockTransport(lambda _: httpx.Response(200, text="<html>rate limited")),
        ],
        ids=["upstream-error", "unparseable"],
    )
    def test_searxng_failures_become_unavailable(self, transport: httpx.MockTransport) -> None:
        """SearXNG's upstream engines rate-limit routinely; this is a normal day."""
        searx, _ = backend(transport)
        result = call(backend=searx)
        assert result["error"] == "unavailable"
        assert result["service"] == "searxng"


class TestAuditTrail:
    """ADR-0004 promises every outbound query is logged, and the log is visible."""

    def test_the_query_is_logged_before_it_leaves(self, caplog: pytest.LogCaptureFixture) -> None:
        searx, _ = backend()
        with caplog.at_level("INFO", logger="mcp_tools.audit"):
            call(backend=searx, query="quantum networking 2026")
        outbound = [r for r in caplog.records if '"outcome": "outbound"' in r.getMessage()]
        assert len(outbound) == 1
        assert "quantum networking 2026" in outbound[0].getMessage()

    def test_a_refusal_is_logged_too(self, caplog: pytest.LogCaptureFixture) -> None:
        """Otherwise the log answers 'what left' but never 'what was stopped'."""
        searx, _ = backend(httpx.MockTransport(_tripwire))
        with caplog.at_level("INFO", logger="mcp_tools.audit"):
            call(backend=searx, enabled=False, query="anything")
        assert any("refused_disabled" in r.getMessage() for r in caplog.records)
