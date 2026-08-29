"""Settings for the MCP tool server: environment in, one frozen object out.

Every value here is either an address of something we call or a number that
bounds how long we are willing to wait for it. Nothing in this file is a
credential with a default -- `mcp_token` and `ragflow_api_key` default to empty
and are read from `.env`, which is gitignored (docs/delivery-plan.md section 8).

The timeouts are *starting values, not measurements*. docs/14 section 4.2 is
explicit about this: replace them with numbers taken from real runs once M6 and
M7 are up, and record what was observed. They are here so that no call can hang
forever, which is a different goal from being well tuned.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_datasets() -> dict[str, list[str]]:
    """One workspace, every dataset the API key can see."""
    return {"default": []}


class Settings(BaseSettings):
    """Environment-derived configuration. Field name uppercased is the env var."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---------------------------------------------------------------- transport
    # Prefixed `mcp_` rather than a bare HOST/PORT: those two names are set by
    # enough unrelated tooling that a bare one is a bug waiting for a bad day.
    mcp_host: str = Field(
        # LAN service: the port is restricted to internal subnets by the host
        # firewall, not by the bind address (docs/14 section 3).
        default="0.0.0.0",
        description="Bind address. 0.0.0.0 because WSL2 mirrored networking needs it.",
    )
    mcp_port: int = 8080
    mcp_token: str = Field(
        default="",
        description=(
            "Shared bearer token. Empty means the server refuses to start unless "
            "mcp_allow_anonymous is set -- a LAN service with no token is an "
            "attribution hole, not a convenience."
        ),
    )
    mcp_allow_anonymous: bool = Field(
        default=False,
        description="Explicit opt-out of auth, for a laptop dev loop only.",
    )

    # ----------------------------------------------------------------- backends
    ragflow_url: str = Field(
        default="http://ragflow:9380",
        description="RAGFlow, adopted rather than built (ADR-0007). We do not implement retrieval.",
    )
    ragflow_api_key: str = ""
    ragflow_datasets: dict[str, list[str]] = Field(
        default_factory=_default_datasets,
        description=(
            "workspace name -> RAGFlow dataset ids, as JSON in RAGFLOW_DATASETS. "
            "An empty list means 'every dataset this key can see'. Config, not a "
            "tool argument: the model picks a workspace name, never a dataset id."
        ),
    )
    searxng_url: str = Field(
        default="http://searxng:8080",
        description="The only host we may talk to that has a route out (docs/16 section 4).",
    )
    comfyui_url: str = Field(
        default="http://10.0.0.226:8188",
        description="ComfyUI moved from .149 to .226; admission-controlled (docs/15 preamble).",
    )
    fleet_controller_url: str = Field(
        default="http://fleet-controller:9000",
        description="Asked before every image job so a claimed host refuses in milliseconds.",
    )
    fleet_image_host: str = Field(
        default="226",
        description="Host id in GET /fleet/status whose free VRAM admits image jobs.",
    )

    # ------------------------------------------------------------------- egress
    web_search_enabled: bool = Field(
        default=False,
        description="OFF by default (ADR-0004). Enabled per workspace, by a visible decision.",
    )
    web_search_max_query_chars: int = Field(
        default=200,
        description="docs/16 section 6.1 layer 1: a 600-character query is a paste, not a search.",
    )
    web_search_shingle_words: int = Field(
        default=12,
        description="docs/16 s6.1 layer 2: shared n-gram length that marks a query as copied.",
    )

    # --------------------------------------------------------------- artefacts
    artifact_dir: Path = Path("/data/artifacts")
    artifact_base_url: str = Field(
        default="https://ai.internal/artifacts",
        description="Tool results carry a URL, never bytes -- a base64 PNG costs the conversation.",
    )

    # --------------------------------------------------------------- renderers
    typst_bin: str = "typst"
    pptx_template_dir: Path | None = Field(
        default=None,
        description=(
            "Directory of operator-authored template decks. Unset falls back to "
            "python-pptx's bundled deck, which carries the same layout names."
        ),
    )
    comfy_workflow_dir: Path | None = Field(
        default=None,
        description=(
            "Directory of ComfyUI API-format workflows, one per rung. Must be exported "
            "from your own ComfyUI ('Save (API Format)') because the node ids we patch "
            "are your graph's, not ours. Unset means generate_image is unavailable."
        ),
    )

    # ---------------------------------------------------------------- timeouts
    timeout_fast_s: float = Field(default=10.0, description="RAGFlow, SearXNG, fleet controller.")
    timeout_render_s: float = Field(default=30.0, description="Typst compile, python-pptx save.")
    timeout_image_s: float = Field(default=180.0, description="ComfyUI queue wait plus sampling.")

    # -------------------------------------------------------------- degradation
    breaker_failures: int = Field(
        default=3,
        description="docs/14 section 4.4 rule 5: fail fast after this many consecutive failures.",
    )
    breaker_cooldown_s: float = 60.0
    image_queue_depth: int = Field(
        default=2,
        description="Pending image jobs we will hold. Beyond this, answer 'busy' immediately.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings.

    Cached rather than constructed at import so tests can build a Settings by
    hand and pass it in, instead of having to own the environment.
    """
    return Settings()
