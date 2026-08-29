"""Rung selection: the one inequality the whole sharing policy reduces to.

    footprint + headroom <= measured free VRAM

Kept as pure functions with no I/O so the arithmetic can be tested exhaustively.
That matters more than it looks: the published band tables in docs/03 originally
contradicted this rule at the bottom of every band -- .226 at 7 GB free would have
loaded the 5.5 GB model and left 1.5 GB, not the promised 3 GB. Encoding the rule
once, here, is what stops that class of error recurring.
"""

from __future__ import annotations

from fleet_controller.models import HostState, Rung

# Binary floating point cannot represent most of these figures exactly, and the
# error accumulates once a loadout subtracts several footprints in sequence:
# 9.2 - 3.0 - 1.2 evaluates to 4.999999999999999, so a 5.0 GB model "does not fit"
# at exactly the band edge docs/03 publishes for `.87`. The symptom would have been
# a host that silently refuses its documented loadout on a boundary value.
#
# VRAM is measured in MiB, so anything below ~0.001 GB is already beneath the
# resolution of the input. 1e-6 is comfortably inside the noise and cannot let a
# rung through that meaningfully breaches headroom.
_EPSILON_GB = 1e-6


def _fits(footprint_gb: float, budget_gb: float) -> bool:
    return footprint_gb <= budget_gb + _EPSILON_GB


def select_rung(rungs: tuple[Rung, ...], free_gb: float, state: HostState) -> Rung | None:
    """Largest rung that fits, or None if nothing does.

    UNKNOWN never loads anything: if we cannot see the card, assume it is in use.
    """
    if state in (HostState.YIELDING, HostState.UNKNOWN):
        return None
    budget = free_gb - state.headroom_gb
    for rung in sorted(rungs, key=lambda r: r.footprint_gb, reverse=True):
        if _fits(rung.footprint_gb, budget):
            return rung
    return None


def select_loadout(rungs: tuple[Rung, ...], free_gb: float, state: HostState) -> tuple[Rung, ...]:
    """Every rung that should be resident, not just the biggest one.

    `select_rung` answers "which single model fits", which is the wrong question on
    any host carrying an `always_on` rung. On `.87`, embeddings (~1.2 GB) stay
    resident *alongside* whatever chat model is selected -- so the two footprints
    have to be paid together. Asking the single-rung question there would pick the
    5 GB chat model at 8 GB free, then load embeddings on top, and leave 1.8 GB
    rather than the promised 3 GB.

    That is the same arithmetic error the band tables in docs/03 originally had,
    reappearing one level up: it is not enough for the rule to be right about one
    model, it has to be right about the whole loadout. docs/03's ">= 9.2 GB" band
    for `.87` is correct precisely because it sums 1.2 + 5.0 + 3.0.

    always_on rungs are claimed first, smallest first, because losing embeddings
    stops ingestion and every RAG query while losing a chat rung only costs
    quality. Then the largest optional rung that fits the remainder.
    """
    if state in (HostState.YIELDING, HostState.UNKNOWN):
        return ()

    budget = free_gb - state.headroom_gb
    resident: list[Rung] = []

    for rung in sorted((r for r in rungs if r.always_on), key=lambda r: r.footprint_gb):
        if _fits(rung.footprint_gb, budget):
            resident.append(rung)
            budget -= rung.footprint_gb

    optional = sorted(
        (r for r in rungs if not r.always_on), key=lambda r: r.footprint_gb, reverse=True
    )
    for rung in optional:
        if _fits(rung.footprint_gb, budget):
            resident.append(rung)
            break

    return tuple(resident)


def loadout_footprint_gb(loadout: tuple[Rung, ...]) -> float:
    """Total VRAM a loadout occupies."""
    return sum(r.footprint_gb for r in loadout)


def minimum_free_for(rung: Rung, state: HostState) -> float:
    """Free VRAM this rung needs *on its own*. The inverse of the rule.

    For a host with `always_on` rungs this understates the requirement -- use
    `select_loadout` to reason about what is actually resident.
    """
    return rung.footprint_gb + state.headroom_gb


def breaches_headroom(rung: Rung | None, free_gb: float, state: HostState) -> bool:
    """True if what we hold no longer leaves the promised headroom.

    Drives *emergency* demotion, which deliberately bypasses hysteresis. Without
    that bypass the 60 s sustained-change rule contradicts the requirement to drop
    a rung before someone's job OOMs (docs/03 section 7, test 3).

    `free_gb` is what nvidia-smi reports free right now -- our own model is already
    resident and therefore already excluded from it.
    """
    if rung is None:
        return False
    return free_gb < state.headroom_gb


def describe_ladder(rungs: tuple[Rung, ...], state: HostState) -> list[tuple[str, float]]:
    """(rung name, free VRAM required) for every rung, largest first.

    Surface this on the dashboard. A user who can see why the platform picked a
    smaller model does not file a bug about it.
    """
    return [
        (r.name, minimum_free_for(r, state))
        for r in sorted(rungs, key=lambda r: r.footprint_gb, reverse=True)
    ]
