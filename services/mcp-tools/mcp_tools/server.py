"""The MCP server: five tool registrations and nothing else.

Three clients times five capabilities is fifteen integrations built the obvious
way and five built here. That arithmetic is the whole reason this service exists
(docs/14, Reflect), and it only holds while the tools stay identical for every
client -- which means the logic must not live in this file.

So this module is deliberately a skin. Each function below validates nothing,
decides nothing, and calls one function in `mcp_tools.tools.*`. MCP is a young
protocol and all three clients implement it slightly differently; when a revision
breaks something, this is the only file that should have to change.

**Naming, for anyone arriving from the docs.** docs/14 says FastMCP. In the
installed SDK (`mcp` 2.x) FastMCP was renamed to `MCPServer` and
`mcp.server.fastmcp` now raises on import with a pointer to the migration guide.
Same framework, same decorators, different name.

**Docstrings are the wire format.** FastMCP/MCPServer derives each tool's
description from its docstring, and that description is injected into the model's
context on every turn, forever. One line each. The long explanation goes in a
comment like this one, where the model never sees it.
"""

from __future__ import annotations

import hmac
import logging
from functools import lru_cache
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_tools.config import Settings, get_settings
from mcp_tools.tools import CircuitBreaker, RecentContext
from mcp_tools.tools import documents as documents_tool
from mcp_tools.tools import image as image_tool
from mcp_tools.tools import pdf as pdf_tool
from mcp_tools.tools import pptx as pptx_tool
from mcp_tools.tools import websearch as websearch_tool

log = logging.getLogger(__name__)

mcp: MCPServer[Any] = MCPServer(
    "ai-platform-tools",
    version="0.1.0",
    instructions=(
        "Internal documents, the public web, and three artefact generators. "
        "Prefer search_documents over web_search for anything about this team's work."
    ),
)


# ----------------------------------------------------------- process-wide state

RECENT_CONTEXT = RecentContext()
"""Shared between `search_documents` and `web_search` -- see docs/16 section 6.1.

The only mutable state in the server, and it is a heuristic cache rather than
session state: losing it on restart costs one missed refusal, not a conversation.
docs/14 section 3 wants the server stateless enough that restarting mid-chat
costs nothing, and this clears that bar.
"""


@lru_cache(maxsize=1)
def _breakers() -> dict[str, CircuitBreaker]:
    settings = get_settings()
    return {
        name: CircuitBreaker(
            failures=settings.breaker_failures, cooldown_s=settings.breaker_cooldown_s
        )
        for name in ("ragflow", "searxng")
    }


@lru_cache(maxsize=1)
def _retrieval_backend() -> documents_tool.RetrievalBackend:
    settings = get_settings()
    return documents_tool.RagflowBackend(
        base_url=settings.ragflow_url,
        api_key=settings.ragflow_api_key,
        datasets=settings.ragflow_datasets,
        timeout_s=settings.timeout_fast_s,
    )


@lru_cache(maxsize=1)
def _websearch_backend() -> websearch_tool.WebSearchBackend:
    settings = get_settings()
    return websearch_tool.SearxngBackend(
        base_url=settings.searxng_url, timeout_s=settings.timeout_fast_s
    )


@lru_cache(maxsize=1)
def _fleet_backend() -> image_tool.FleetBackend:
    settings = get_settings()
    return image_tool.FleetBackend(
        base_url=settings.fleet_controller_url, timeout_s=settings.timeout_fast_s
    )


@lru_cache(maxsize=1)
def _comfy_backend() -> image_tool.ComfyBackend:
    settings = get_settings()
    return image_tool.ComfyBackend(
        base_url=settings.comfyui_url, timeout_s=settings.timeout_image_s
    )


@lru_cache(maxsize=1)
def _image_queue() -> image_tool.ImageQueue:
    return image_tool.ImageQueue(max_pending=get_settings().image_queue_depth)


# ------------------------------------------------------------------- five tools

# `structured_output=False` on every tool. The alternative emits a JSON output
# schema alongside each tool definition, and every byte of a tool definition is
# spent in every prompt on a model that has far less context than the ones most
# MCP examples were written against (docs/14 section 2). The envelope is stable
# and documented; the schema for it is not worth what it costs.


@mcp.tool(structured_output=False)
async def search_documents(
    query: str, top_k: int = 5, workspace: str = "default"
) -> dict[str, Any]:
    """Search the team's indexed internal documents. Returns passages with document and page citations."""  # noqa: E501
    return await documents_tool.search_documents(
        query,
        top_k,
        workspace,
        backend=_retrieval_backend(),
        recent=RECENT_CONTEXT,
        breaker=_breakers()["ragflow"],
    )


@mcp.tool(structured_output=False)
async def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the public web for current information. The query text leaves the network; document text never does."""  # noqa: E501
    # The second sentence is deliberate. It is read by the model *and* shown to
    # the user in the client's tool-approval prompt, which is exactly where the
    # egress contract should be visible.
    settings = get_settings()
    return await websearch_tool.web_search(
        query,
        max_results,
        backend=_websearch_backend(),
        enabled=settings.web_search_enabled,
        max_query_chars=settings.web_search_max_query_chars,
        recent=RECENT_CONTEXT,
        breaker=_breakers()["searxng"],
    )


@mcp.tool(structured_output=False)
async def generate_pdf(title: str, body_markdown: str, template: str = "report") -> dict[str, Any]:
    """Write a report to PDF from markdown. Returns a file id and a URL."""
    settings = get_settings()
    return await pdf_tool.generate_pdf(
        title,
        body_markdown,
        template,
        typst_bin=settings.typst_bin,
        timeout_s=settings.timeout_render_s,
        artifact_dir=settings.artifact_dir,
        artifact_base_url=settings.artifact_base_url,
    )


@mcp.tool(structured_output=False)
async def generate_pptx(
    title: str, slides: list[pptx_tool.Slide], template: str = "default"
) -> dict[str, Any]:
    """Build an editable PowerPoint deck from a slide outline. Returns a file id and a URL."""
    settings = get_settings()
    return await pptx_tool.generate_pptx(
        title,
        slides,
        template,
        template_dir=settings.pptx_template_dir,
        artifact_dir=settings.artifact_dir,
        artifact_base_url=settings.artifact_base_url,
    )


@mcp.tool(structured_output=False)
async def generate_image(
    prompt: str, aspect: str = "16:9", quality: str = "fast"
) -> dict[str, Any]:
    """Generate an image from a text description. Returns a file id and a URL."""
    settings = get_settings()
    return await image_tool.generate_image(
        prompt,
        aspect,
        quality,
        fleet=_fleet_backend(),
        comfy=_comfy_backend(),
        queue=_image_queue(),
        host_id=settings.fleet_image_host,
        workflow_dir=settings.comfy_workflow_dir,
        artifact_dir=settings.artifact_dir,
        artifact_base_url=settings.artifact_base_url,
    )


# The SDK's custom_route decorator carries no return annotation, so mypy sees
# an untyped decorator here. The handler itself is fully typed.
@mcp.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
async def healthz(_: Request) -> Response:
    """Liveness. No dependencies, never blocks -- the same contract as the fleet controller's."""
    return JSONResponse({"status": "ok"})


# -------------------------------------------------------------------------- auth


class BearerTokenMiddleware:
    """One shared token in the `Authorization` header (docs/14 section 3).

    This is a LAN service, so the token is about attribution and
    accident-prevention, not about defeating an attacker on the wire. It is an
    ASGI middleware rather than the SDK's OAuth machinery because a shared secret
    is what the three clients are configured with, and because a pure-ASGI
    wrapper does not interfere with the streaming responses underneath it.
    """

    def __init__(self, app: ASGIApp, *, token: str, exempt: frozenset[str]) -> None:
        self._app = app
        self._token = token
        self._exempt = exempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._exempt:
            await self._app(scope, receive, send)
            return
        if not self._authorised(scope):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _authorised(self, scope: Scope) -> bool:
        for name, value in scope.get("headers") or []:
            if name.lower() != b"authorization":
                continue
            presented = value.decode("latin-1").removeprefix("Bearer ").strip()
            # Constant-time: the comparison is cheap and the habit is what stops
            # the one place it matters from being the place it was forgotten.
            return hmac.compare_digest(presented, self._token)
        return False


def create_app(settings: Settings | None = None) -> ASGIApp:
    """The ASGI application: the MCP streamable-HTTP app behind the token check."""
    settings = settings or get_settings()
    app: Starlette | ASGIApp = mcp.streamable_http_app(host=settings.mcp_host)
    if settings.mcp_allow_anonymous:
        log.warning("MCP_ALLOW_ANONYMOUS is set: this server accepts unauthenticated calls")
        return app
    if not settings.mcp_token:
        raise RuntimeError(
            "MCP_TOKEN is empty. Generate one per environment and put it in .env "
            "(gitignored), or set MCP_ALLOW_ANONYMOUS=true for a local dev loop."
        )
    return BearerTokenMiddleware(app, token=settings.mcp_token, exempt=frozenset({"/healthz"}))


def main() -> None:
    """Serve on `.87`. Streamable HTTP, one shared instance, bound to the LAN."""
    import uvicorn

    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    uvicorn.run(create_app(settings), host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
