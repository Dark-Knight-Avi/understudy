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


def select_rung(rungs: tuple[Rung, ...], free_gb: float, state: HostState) -> Rung | None:
    """Largest rung that fits, or None if nothing does.

    UNKNOWN never loads anything: if we cannot see the card, assume it is in use.
    """
    if state in (HostState.YIELDING, HostState.UNKNOWN):
        return None
    headroom = state.headroom_gb
    for rung in sorted(rungs, key=lambda r: r.footprint_gb, reverse=True):
        if rung.footprint_gb + headroom <= free_gb:
            return rung
    return None


def minimum_free_for(rung: Rung, state: HostState) -> float:
    """Free VRAM this rung needs. The inverse of the rule, for the dashboard."""
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
