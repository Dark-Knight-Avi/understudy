"""Tests for `search_documents`, mocked at RAGFlow's HTTP boundary.

The point of ADR-0007 is that we do not implement retrieval, so there is nothing
here about ranking or chunking. What is tested is the seam: that we send what
RAGFlow expects, that a citation survives the mapping, and that every way RAGFlow
can fail comes back as an envelope rather than an exception.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from mcp_tools.tools import CircuitBreaker, RecentContext
from mcp_tools.tools.documents import (
    MAX_TOP_K,
    RagflowBackend,
    search_documents,
)

CHUNK = {
    "content": "Headroom is 3 GB while sharing and 1 GB while free.",
    "document_keyword": "gpu-sharing-policy.pdf",
    "positions": [[4, 100, 400, 200, 260]],
    "similarity": 0.81,
}


def backend(
    handler: httpx.MockTransport | None = None,
    *,
    datasets: dict[str, list[str]] | None = None,
) -> tuple[RagflowBackend, list[httpx.Request]]:
    """A backend whose transport records what it was asked to send."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"chunks": [CHUNK]}})

    transport = handler or httpx.MockTransport(record)
    client = httpx.AsyncClient(transport=transport)
    return (
        RagflowBackend(
            base_url="http://ragflow:9380",
            api_key="test-key",
            datasets=datasets if datasets is not None else {"default": ["ds-1"]},
            timeout_s=5.0,
            client=client,
        ),
        seen,
    )


def call(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "query": "how much headroom",
        "top_k": 5,
        "workspace": "default",
        "recent": RecentContext(),
        "breaker": CircuitBreaker(),
    }
    kwargs.update(overrides)
    query = kwargs.pop("query")
    top_k = kwargs.pop("top_k")
    workspace = kwargs.pop("workspace")
    return asyncio.run(search_documents(query, top_k, workspace, **kwargs))


class TestHappyPath:
    def test_returns_passages_with_document_and_page(self) -> None:
        rag, _ = backend()
        result = call(backend=rag)
        assert result["ok"] is True
        assert result["results"][0]["document"] == "gpu-sharing-policy.pdf"
        assert result["results"][0]["page"] == 4

    def test_empty_corpus_is_a_success_with_a_note_not_an_error(self) -> None:
        """An unanswerable question is a fact about the corpus, not a failure."""
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": 0, "data": {"chunks": []}})
        )
        rag, _ = backend(transport)
        result = call(backend=rag)
        assert result["ok"] is True
        assert result["results"] == []
        assert "note" in result

    def test_retrieved_text_is_remembered_for_the_web_search_guard(self) -> None:
        """The whole mechanism of docs/16 section 6.1 depends on this happening here."""
        rag, _ = backend()
        recent = RecentContext(shingle_words=6)
        call(backend=rag, recent=recent)
        assert recent.overlaps("headroom is 3 gb while sharing and 1 gb while free")


class TestArgumentHandling:
    def test_top_k_is_capped_server_side(self) -> None:
        """A model that asks for 100 passages drowns in them; the cap is ours to keep."""
        rag, seen = backend()
        call(backend=rag, top_k=100)
        assert json.loads(seen[0].read())["page_size"] == MAX_TOP_K

    def test_dataset_ids_come_from_config_not_from_the_caller(self) -> None:
        """Per-user isolation is expressed by which datasets a workspace maps to."""
        rag, seen = backend(datasets={"default": ["ds-1", "ds-2"]})
        call(backend=rag)
        assert b'"ds-2"' in seen[0].read()

    def test_unknown_workspace_is_refused_with_the_valid_names(self) -> None:
        rag, seen = backend(datasets={"engineering": [], "sales": []})
        result = call(backend=rag, workspace="marketing")
        assert result["error"] == "refused"
        assert "engineering" in result["detail"] and "sales" in result["detail"]
        assert seen == [], "a name we cannot resolve must not reach RAGFlow"

    def test_blank_query_is_refused(self) -> None:
        rag, seen = backend()
        assert call(backend=rag, query="   ")["error"] == "refused"
        assert seen == []


class TestDegradation:
    """Every failure is an envelope. docs/14 section 4.4: never raise into the protocol."""

    @pytest.mark.parametrize(
        "transport",
        [
            httpx.MockTransport(lambda _: httpx.Response(503)),
            httpx.MockTransport(lambda _: httpx.Response(200, text="not json")),
            httpx.MockTransport(
                lambda _: httpx.Response(200, json={"code": 102, "message": "no such dataset"})
            ),
        ],
        ids=["http-error", "unparseable", "ragflow-error-code"],
    )
    def test_backend_failures_become_unavailable(self, transport: httpx.MockTransport) -> None:
        rag, _ = backend(transport)
        result = call(backend=rag)
        assert result["ok"] is False
        assert result["error"] == "unavailable"
        assert result["service"] == "ragflow"

    def test_connection_refused_becomes_unavailable_not_an_exception(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        rag, _ = backend(httpx.MockTransport(refuse))
        assert call(backend=rag)["error"] == "unavailable"

    def test_the_breaker_opens_and_stops_paying_the_timeout(self) -> None:
        rag, seen = backend(httpx.MockTransport(lambda _: httpx.Response(503)))
        breaker = CircuitBreaker(failures=3, cooldown_s=60.0)
        for _ in range(3):
            assert call(backend=rag, breaker=breaker)["error"] == "unavailable"
        calls_before = len(seen)
        result = call(backend=rag, breaker=breaker)
        assert result["error"] == "unavailable"
        assert len(seen) == calls_before, "an open breaker must not call the backend"
