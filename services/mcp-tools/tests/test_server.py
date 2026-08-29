"""Tests for the tool surface itself: five tools, one token, one shape.

M6 acceptance test 1 is "list tools over HTTP and get exactly 5, descriptions as
written". The HTTP half needs a running server; the half that can regress
silently -- a sixth tool appearing, a docstring growing to three paragraphs, an
argument becoming required -- is checked here on every run.

docs/14 section 2 is explicit that the tool surface is a **context budget**: every
name, description and argument schema is injected into the model's context on
every turn, and the models this platform serves have far less of it to spend than
the ones most MCP examples were written against. So these are budget tests.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp_tools.config import Settings
from mcp_tools.server import BearerTokenMiddleware, create_app, mcp
from mcp_types import Tool

EXPECTED_TOOLS = {
    "search_documents",
    "web_search",
    "generate_pdf",
    "generate_pptx",
    "generate_image",
}


def tools() -> dict[str, Tool]:
    listed = asyncio.run(mcp.list_tools())
    return {tool.name: tool for tool in listed}


class TestTheFiveTools:
    def test_exactly_five_and_no_more(self) -> None:
        """Argue hard before adding a sixth; this test is where the argument happens."""
        assert set(tools()) == EXPECTED_TOOLS

    def test_every_description_is_one_line(self) -> None:
        """A three-paragraph docstring is three paragraphs in every prompt, forever."""
        for name, tool in tools().items():
            description = (tool.description or "").strip()
            assert description, f"{name} has no description"
            assert "\n" not in description, f"{name}'s description is multi-line"
            assert len(description) < 130, f"{name}'s description is {len(description)} chars"

    def test_the_two_searches_are_unambiguous(self) -> None:
        """Overlapping descriptions are chosen wrongly, and the failure is silent."""
        found = tools()
        assert "internal documents" in (found["search_documents"].description or "")
        assert "public web" in (found["web_search"].description or "")

    def test_web_search_states_the_egress_contract_where_a_user_will_see_it(self) -> None:
        """The client shows this in its tool-approval prompt -- ADR-0004 made visible."""
        description = tools()["web_search"].description or ""
        assert "leaves the network" in description
        assert "document text never does" in description

    def test_only_query_and_prompt_and_title_are_required(self) -> None:
        """Every required argument is another thing a local model can get wrong."""
        required = {name: set(t.input_schema.get("required", [])) for name, t in tools().items()}
        assert required["search_documents"] == {"query"}
        assert required["web_search"] == {"query"}
        assert required["generate_image"] == {"prompt"}
        assert required["generate_pdf"] == {"title", "body_markdown"}
        assert required["generate_pptx"] == {"title", "slides"}

    def test_no_tool_takes_a_free_form_object(self) -> None:
        """A model handed an open-ended dict will invent keys."""
        for name, tool in tools().items():
            for argument, schema in tool.input_schema.get("properties", {}).items():
                if schema.get("type") != "object":
                    continue
                assert schema.get("properties") or schema.get("$ref"), (
                    f"{name}.{argument} is an untyped object"
                )

    def test_no_long_enums_anywhere_in_the_schemas(self) -> None:
        """Long enums are where the token budget goes to die; validate server-side."""
        for name, tool in tools().items():
            for enum in _enums(tool.input_schema):
                assert len(enum) <= 4, f"{name} exposes a {len(enum)}-value enum"

    def test_generate_image_exposes_quality_and_never_a_model_name(self) -> None:
        """Which model is loaded is decided by free VRAM, so `model` would be a lie."""
        properties = tools()["generate_image"].input_schema["properties"]
        assert "quality" in properties
        assert "model" not in properties

    def test_no_output_schemas(self) -> None:
        """Output schemas are more tool definition to pay for on every turn."""
        assert all(tool.output_schema is None for tool in tools().values())

    def test_the_whole_surface_stays_small(self) -> None:
        """A crude proxy for the real measurement docs/14 section 2 asks for.

        The number to record when M6 ships is the token count under the fast
        tier's own tokeniser. Until then, serialised bytes at least fails loudly
        if someone pastes a paragraph into a docstring.
        """
        serialised = json.dumps(
            [t.model_dump(exclude_none=True) for t in asyncio.run(mcp.list_tools())]
        )
        assert len(serialised) < 6000, f"tool surface is {len(serialised)} bytes"


def _enums(schema: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(schema, dict):
        if isinstance(schema.get("enum"), list):
            found.append(schema["enum"])
        for value in schema.values():
            found.extend(_enums(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_enums(value))
    return found


# ---------------------------------------------------------------------- auth


async def _sentinel(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"reached"})


def probe(header: str | None, *, path: str = "/mcp") -> int:
    """Drive the middleware directly and report the status it produced."""
    app = BearerTokenMiddleware(_sentinel, token="s3cret", exempt=frozenset({"/healthz"}))
    headers = [(b"authorization", header.encode())] if header is not None else []
    statuses: list[int] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(
        app({"type": "http", "path": path, "method": "POST", "headers": headers}, receive, send)
    )
    return statuses[0]


class TestBearerToken:
    """One token, one place to rotate. Attribution, not defence against the wire."""

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Bearer s3cret", 200),
            ("s3cret", 200),
            ("Bearer wrong", 401),
            ("Bearer ", 401),
            ("", 401),
            (None, 401),
        ],
    )
    def test_only_the_right_token_gets_through(self, header: str | None, expected: int) -> None:
        assert probe(header) == expected

    def test_healthz_is_exempt(self) -> None:
        """Liveness must not depend on the caller holding a credential."""
        assert probe(None, path="/healthz") == 200


class TestStartup:
    def test_a_missing_token_stops_the_server_rather_than_serving_open(self) -> None:
        """Failing to start is loud; serving unauthenticated is silent."""
        with pytest.raises(RuntimeError, match="MCP_TOKEN"):
            create_app(Settings(mcp_token="", mcp_allow_anonymous=False))

    def test_anonymous_must_be_asked_for_explicitly(self) -> None:
        app = create_app(Settings(mcp_token="", mcp_allow_anonymous=True))
        assert not isinstance(app, BearerTokenMiddleware)

    def test_a_token_wraps_the_app(self) -> None:
        app = create_app(Settings(mcp_token="s3cret"))
        assert isinstance(app, BearerTokenMiddleware)
