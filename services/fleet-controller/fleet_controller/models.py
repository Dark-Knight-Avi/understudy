"""Core types for the fleet controller.

The governing rule, from docs/03-gpu-sharing-policy.md section 3:

    footprint + headroom <= measured free VRAM

Everything else in this service is bookkeeping around that inequality. The band
tables in the docs are a human-readable summary of it, not the algorithm -- when
they disagreed, the tables were wrong.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

# Headroom we leave for the person at the machine. Asymmetric on purpose: a
# smaller model is an inconvenience, an eight-hour run dying at hour six is not.
HEADROOM_FREE_GB = 1.0
HEADROOM_SHARING_GB = 3.0


class HostState(StrEnum):
    """Per-host state machine. See docs/03 section 2."""

    FREE = "free"
    """No foreign CUDA process. Platform may take the top rung."""

    YIELDING = "yielding"
    """Someone claimed the GPU. Sleep to zero; the gateway routes elsewhere."""

    SHARING = "sharing"
    """Their job has settled. Fit into what is genuinely spare."""

    UNKNOWN = "unknown"
    """Host unreachable or nvidia-smi unparseable.

    Never promotes. Absence of evidence is not evidence of a free GPU -- if we
    cannot see the card, we must assume someone is using it.
    """

    @property
    def headroom_gb(self) -> float:
        """VRAM to leave untouched for the machine's owner."""
        if self is HostState.FREE:
            return HEADROOM_FREE_GB
        return HEADROOM_SHARING_GB


class Rung(BaseModel):
    """One step on a host's model ladder."""

    model_config = {"frozen": True}

    name: str = Field(description="Catalog name exposed to users, e.g. 'coder'")
    served_model: str = Field(description="Model id the inference server loads")
    footprint_gb: float = Field(
        gt=0,
        description=(
            "Weights PLUS the KV cache budget. The doc figures are weights-only "
            "estimates; replace them with measured values (docs/07)."
        ),
    )
    always_on: bool = Field(
        default=False,
        description=(
            "Survives demotion where possible, e.g. embeddings, whose loss stops "
            "ingestion and every RAG query."
        ),
    )


class HostConfig(BaseModel):
    """Static description of one host."""

    model_config = {"frozen": True}

    name: str
    address: str
    total_vram_gb: float = Field(gt=0)
    rungs: tuple[Rung, ...] = Field(description="Any order; the selector sorts them")
    vllm_url: str | None = None

    trust_process_enumeration: bool = Field(
        default=True,
        description=(
            "Whether nvidia-smi on this host can identify processes. FALSE under "
            "WSL2, where it reports '[Not Found]' for every process_name and "
            "'[N/A]' for per-process memory -- so ownership cannot be decided by "
            "name or pid, and enumeration yields ONLY false positives: the "
            "platform sees its own parked engine's CUDA context, calls it "
            "somebody's job, and sizes itself around work that does not exist. "
            "When false, ownership is decided by subtraction instead."
        ),
    )
    cuda_context_gb: float = Field(
        default=1.5,
        ge=0,
        description=(
            "VRAM the driver and CUDA context hold for a live vLLM process even "
            "when its weights are parked -- it is released only when the process "
            "exits, never by sleep. Subtraction must allow for it or a parked "
            "model reads as somebody else holding 1.4 GB forever. M0 spike 1 "
            "measured 1.49 on .226 and 1.24 on .87 and .210."
        ),
    )

    detect_interactive_login: bool = Field(
        default=False,
        description=(
            "Off by default. On the hub this would pin the host to its bottom rung "
            "forever, since something is always logged into it."
        ),
    )
    notes: str = ""

    @field_validator("rungs")
    @classmethod
    def _rungs_fit(cls, v: tuple[Rung, ...], info: object) -> tuple[Rung, ...]:
        if not v:
            raise ValueError("a host needs at least one rung")
        return v


# Below this, "foreign" VRAM is noise -- a desktop compositor, a driver allocation.
# Yielding a whole host to 200 MiB of window manager would pin it off permanently,
# the same failure shape as naive interactive-login detection.
FOREIGN_NOISE_FLOOR_GB = 0.3


class GpuSample(BaseModel):
    """One reading of a GPU, as nvidia-smi reports it."""

    model_config = {"frozen": True}

    host: str
    total_gb: float
    used_gb: float
    foreign_pids: tuple[int, ...] = ()
    """CUDA processes that are not ours, by enumeration."""
    foreign_used_gb: float | None = None
    """VRAM in foreign hands by *subtraction* (`used - ours - baseline`), if known.

    Two signals, because either alone is blind somewhere. Enumeration misses
    processes the sampler cannot see -- under WSL2 the Linux-side process list does
    not show Windows-side CUDA work, so a card genuinely holding someone's 8 GB job
    can enumerate as empty. Subtraction catches that but cannot say whose it is, and
    it misreads if our own footprint is misconfigured.

    `None` means the sampler could not compute it, which is not the same as zero and
    must never be read as "nothing foreign".
    """
    sampled_at: datetime

    @property
    def free_gb(self) -> float:
        return max(0.0, self.total_gb - self.used_gb)

    @property
    def has_foreign_process(self) -> bool:
        """Either signal is enough. Yielding wrongly costs capability; not yielding
        costs somebody their job, so the disjunction is the safe direction."""
        if self.foreign_pids:
            return True
        return self.foreign_used_gb is not None and self.foreign_used_gb > FOREIGN_NOISE_FLOOR_GB


class HostStatus(BaseModel):
    """What the dashboard and /fleet/status render."""

    host: str
    state: HostState
    free_gb: float
    total_gb: float
    current_rung: str | None
    reserved_by: str | None = None
    reserved_since: datetime | None = None
    last_sample_at: datetime | None = None
    message: str = ""
