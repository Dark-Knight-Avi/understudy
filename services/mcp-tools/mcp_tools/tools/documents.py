"""`search_documents` -- a thin delegation to RAGFlow. No retrieval lives here.

ADR-0007 replaced the RAG service we were going to build with RAGFlow, on the
grounds that document parsing is the one area where off-the-shelf genuinely beats
two weeks of our own work. So this module has exactly one job: turn a tool call
into a RAGFlow retrieval request and its answer into citable passages.

`RetrievalBackend` is the seam that ADR-0007's contingency needs. The ADR is
explicit that it is *contingent on the M1.5 spike passing* -- refusal behaviour,
per-user isolation, MCP reachability -- and that a structural failure on isolation
or MCP access means building our own after all, from the design retained in
docs/10-13. Keeping the protocol one function wide is what makes that a new class
here rather than a rewrite of the tool surface.
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

MAX_TOP_K = 10
"""Cap on what the model may ask for.

docs/14 section 4.3: a model that asks for 100 passages then drowns in them has
spent its context on a mistake it cannot undo. Capping server-side is cheaper
than teaching every client to behave.
"""


class Passage(BaseModel):
    """One retrieved chunk, in the shape a citation is built from."""

    model_config = {"frozen": True}

    text: str
    document: str
    page: int | None = None
    score: float | None = None


class RetrievalUnavailable(RuntimeError):
    """The backend could not answer. Caught in the tool; never reaches the protocol."""


class UnknownWorkspace(ValueError):
    """The caller named a workspace we have no dataset mapping for.

    Separate from `RetrievalUnavailable` because the envelopes differ and the
    difference changes agent behaviour: retrying an unavailable service is
    sensible, retrying a name that does not exist is a loop.
    """


class RetrievalBackend(Protocol):
    """One method wide, on purpose -- see the module docstring."""

    async def retrieve(self, query: str, *, top_k: int, workspace: str) -> list[Passage]: ...

    def workspaces(self) -> list[str]: ...


class RagflowBackend:
    """RAGFlow over its HTTP retrieval API.

    Endpoint and response shapes are **unverified against a running instance** --
    RAGFlow is pinned but its API is not something we control, and the M1.5 spike
    is where these get confirmed. The parsing below is deliberately tolerant of
    missing fields for that reason: a renamed key should cost a citation's page
    number, not the whole result.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        datasets: dict[str, list[str]],
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._datasets = datasets
        self._timeout_s = timeout_s
        self._client = client

    def workspaces(self) -> list[str]:
        return sorted(self._datasets)

    async def retrieve(self, query: str, *, top_k: int, workspace: str) -> list[Passage]:
        if workspace not in self._datasets:
            raise UnknownWorkspace(workspace)
        dataset_ids = self._datasets[workspace]

        payload: dict[str, Any] = {"question": query, "page": 1, "page_size": top_k}
        if dataset_ids:
            # Absent means "everything this key can see". Present means the
            # workspace is a closed set of datasets, which is how per-user
            # isolation (ADR-0007 criterion 2) is expressed from our side.
            payload["dataset_ids"] = list(dataset_ids)

        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            if self._client is not None:
                response = await self._client.post(
                    f"{self._base_url}/api/v1/retrieval",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout_s,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.post(
                        f"{self._base_url}/api/v1/retrieval", json=payload, headers=headers
                    )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RetrievalUnavailable(str(exc)) from exc

        if body.get("code") not in (0, None):
            raise RetrievalUnavailable(str(body.get("message", "RAGFlow returned an error")))
        return _to_passages(body.get("data") or {})


def _to_passages(data: dict[str, Any]) -> list[Passage]:
    """Map RAGFlow chunks onto our citation shape, tolerating absent fields."""
    passages: list[Passage] = []
    for chunk in data.get("chunks") or []:
        text = chunk.get("content") or chunk.get("content_with_weight") or ""
        if not text:
            continue
        passages.append(
            Passage(
                text=text,
                document=chunk.get("document_keyword") or chunk.get("docnm_kwd") or "unknown",
                page=_first_page(chunk.get("positions")),
                score=chunk.get("similarity"),
            )
        )
    return passages


def _first_page(positions: Any) -> int | None:
    """RAGFlow returns positions as [page, x1, x2, y1, y2] rows. We want the page.

    A citation without a page is still useful; a citation with the *wrong* page is
    worse than none, so anything that is not clearly a page number becomes None.
    """
    if not isinstance(positions, list) or not positions:
        return None
    first = positions[0]
    if isinstance(first, list | tuple) and first and isinstance(first[0], int):
        return int(first[0])
    if isinstance(first, int):
        return first
    return None


async def search_documents(
    query: str,
    top_k: int,
    workspace: str,
    *,
    backend: RetrievalBackend,
    recent: RecentContext,
    breaker: CircuitBreaker,
) -> ToolResult:
    """Retrieve passages, remember them, and never raise."""
    timer = Timer()
    query = query.strip()
    if not query:
        return refused("search_documents", "Provide a question or some search terms.")

    if not breaker.allow():
        # docs/14 section 4.4 rule 5. Ten seconds of nothing, once per turn, reads
        # to a user as the whole platform being slow.
        log_tool_call("search_documents", outcome="breaker_open", duration_ms=timer.ms)
        return unavailable(
            "ragflow", "Document search has been failing; it is being retried shortly."
        )

    try:
        hits = await backend.retrieve(query, top_k=min(top_k, MAX_TOP_K), workspace=workspace)
    except UnknownWorkspace:
        # Rule 5 of the schema constraints: validate server-side and answer with
        # what is valid, rather than spending context on an enum in every prompt.
        valid = ", ".join(backend.workspaces())
        log_tool_call(
            "search_documents", outcome="refused", duration_ms=timer.ms, workspace=workspace
        )
        return refused("search_documents", f"Unknown workspace '{workspace}'. Valid: {valid}.")
    except RetrievalUnavailable as exc:
        breaker.record_failure()
        log_tool_call("search_documents", outcome="unavailable", duration_ms=timer.ms)
        return unavailable("ragflow", f"Document search is offline: {exc}")

    breaker.record_success()
    # The passages are what `web_search` is checked against, so this has to happen
    # before any chance the same turn reaches for the web (docs/16 section 6.1).
    recent.remember([hit.text for hit in hits])

    # Log the query, never the passages: duplicating document text into a log file
    # guarded less carefully than the database undoes the point of the project.
    log_tool_call(
        "search_documents",
        outcome="ok",
        duration_ms=timer.ms,
        workspace=workspace,
        results=len(hits),
    )

    if not hits:
        return ok(results=[], note="No indexed document matched this query.")
    return ok(results=[hit.model_dump() for hit in hits])
