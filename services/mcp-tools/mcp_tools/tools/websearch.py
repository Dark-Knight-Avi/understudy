"""`web_search` -- the platform's entire outbound surface, via SearXNG.

Nothing else in this package makes an outbound call. That is not a coding
convention, it is the product: docs/16 section 1 argues that the one thing this
platform has that a hosted vendor does not is that our documents and our code
never leave the building, and everything else here is downstream of that.

The real enforcement is at the network layer -- `internal: true` on the platform
Docker network, so a platform container has no route out regardless of what its
code attempts (docs/16 section 4.1). The checks in this module are the second
layer: they exist to turn the *accidental* cases into refusals before a packet is
built, and to make sure the audit trail exists for the ones that do go out.

Order matters in `web_search`. Every refusal is decided before an HTTP client is
touched, because M6 acceptance test 7 verifies "no outbound packet" with a
capture, not by reading the code.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from mcp_tools.tools import (
    CircuitBreaker,
    RecentContext,
    Timer,
    ToolResult,
    log_tool_call,
    ok,
    refused,
    unavailable,
)

MAX_RESULTS = 10


class WebResult(BaseModel):
    """One search hit, in the citation shape docs/16 section 8 defines."""

    model_config = {"frozen": True}

    title: str
    url: str
    snippet: str = ""
    engine: str = ""


class SearchUnavailable(RuntimeError):
    """SearXNG could not answer -- upstream engines rate-limit routinely."""


class WebSearchBackend(Protocol):
    async def search(self, query: str, *, max_results: int) -> list[WebResult]: ...


class SearxngBackend:
    """SearXNG's JSON API. Requires `json` in `search.formats` (docs/16 section 3)."""

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

    async def search(self, query: str, *, max_results: int) -> list[WebResult]:
        params = {"q": query, "format": "json"}
        try:
            if self._client is not None:
                response = await self._client.get(
                    f"{self._base_url}/search", params=params, timeout=self._timeout_s
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.get(f"{self._base_url}/search", params=params)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailable(str(exc)) from exc
        return _to_results(body, max_results)


def _to_results(body: dict[str, Any], max_results: int) -> list[WebResult]:
    results: list[WebResult] = []
    for hit in (body.get("results") or [])[:max_results]:
        url = hit.get("url")
        if not url:
            continue
        results.append(
            WebResult(
                title=hit.get("title") or url,
                url=url,
                snippet=hit.get("content") or "",
                engine=hit.get("engine") or "",
            )
        )
    return results


async def web_search(
    query: str,
    max_results: int,
    *,
    backend: WebSearchBackend,
    enabled: bool,
    max_query_chars: int,
    recent: RecentContext,
    breaker: CircuitBreaker,
) -> ToolResult:
    """Search the public web, after four checks that can each stop the packet."""
    timer = Timer()
    query = query.strip()

    # 1. The switch (ADR-0004, docs/16 section 5). First, so a disabled workspace
    #    cannot reach the network even if every later check has a bug in it.
    if not enabled:
        log_tool_call("web_search", outcome="refused_disabled", duration_ms=timer.ms, query=query)
        return refused(
            "web_search",
            "Web search is disabled for this workspace. Answer from the documents, "
            "or say that you cannot.",
        )

    if not query:
        return refused("web_search", "Provide something to search for.")

    # 2. Length cap (docs/16 section 6.1 layer 1). Real queries are short; a long
    #    one is a paste, and refusing it costs nothing.
    if len(query) > max_query_chars:
        log_tool_call(
            "web_search",
            outcome="refused_too_long",
            duration_ms=timer.ms,
            query=query,
            flagged_long=True,
        )
        return refused(
            "web_search",
            f"Query is {len(query)} characters; the limit is {max_query_chars}. "
            "Search for a few keywords rather than a passage of text.",
        )

    # 3. Overlap with retrieved context (docs/16 section 6.1 layer 2). This is the
    #    leak most likely to actually happen and the one place the guarantee
    #    degrades from "structurally impossible" to "caught most of the time".
    if recent.overlaps(query):
        log_tool_call("web_search", outcome="refused_overlap", duration_ms=timer.ms, query=query)
        return refused(
            "web_search",
            "That query repeats text from a retrieved document, which must not leave the network. "
            "Rephrase it in your own words.",
        )

    if not breaker.allow():
        log_tool_call("web_search", outcome="breaker_open", duration_ms=timer.ms, query=query)
        return unavailable("searxng", "Web search has been failing; it is being retried shortly.")

    # 4. Everything above passed, so this query is about to leave the network.
    #    ADR-0004 promises every outbound query is logged; this is that line, and
    #    it is written *before* the call so a crash mid-request still records it.
    log_tool_call("web_search", outcome="outbound", duration_ms=timer.ms, query=query)

    try:
        results = await backend.search(query, max_results=min(max_results, MAX_RESULTS))
    except SearchUnavailable as exc:
        breaker.record_failure()
        log_tool_call("web_search", outcome="unavailable", duration_ms=timer.ms, query=query)
        return unavailable("searxng", f"Web search is offline: {exc}")

    breaker.record_success()
    log_tool_call(
        "web_search", outcome="ok", duration_ms=timer.ms, query=query, results=len(results)
    )
    return ok(results=[result.model_dump() for result in results])
