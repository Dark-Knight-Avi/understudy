"""Loading `deploy/fleet.yaml` into typed configuration.

Validation is deliberately strict and loud. A silently-wrong fleet config is one
of the few ways this service can do real damage: a footprint that is too small
makes the controller believe a rung fits when it does not, and the person whose
job gets squeezed has no way to know why. Failing to start is much better.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from fleet_controller.models import HostConfig
from fleet_controller.state import StateTimings


class ConfigError(RuntimeError):
    """Config is unusable. Always fatal -- never fall back to defaults.

    A default fleet is a fleet whose ladders do not match the hardware.
    """


class Tunables(BaseModel):
    """Fleet-wide knobs. Names match `deploy/fleet.yaml`."""

    model_config = {"frozen": True, "extra": "forbid"}

    poll_interval_s: float = Field(default=2.0, gt=0)
    settle_s: float = Field(default=60.0, ge=0)
    headroom_free_gb: float = Field(default=1.0, ge=0)
    headroom_sharing_gb: float = Field(default=3.0, ge=0)
    rung_change_gb: float = Field(default=2.0, ge=0)
    sustain_s: float = Field(default=60.0, ge=0)
    top_rung_clear_s: float = Field(default=300.0, ge=0)
    emergency_demote_bypasses_hysteresis: bool = True
    lease_idle_warn_s: float = Field(default=1500.0, ge=0)
    lease_idle_release_s: float = Field(default=1800.0, ge=0)
    yield_deadline_s: float = Field(default=10.0, gt=0)
    unreachable_after_polls: int = Field(default=3, ge=1)
    agent_autonomy_after_s: float = Field(default=15.0, ge=0)
    foreign_threshold_gb: float = Field(default=0.3, ge=0)

    def as_timings(self) -> StateTimings:
        """Translate the YAML's names into the state machine's.

        The two vocabularies differ -- `fleet.yaml` speaks of leases and polls, the
        state machine of reservations and samples -- and this is the only place that
        knows both. Keep the mapping here rather than renaming either side: the YAML
        is what an operator reads at 3am, and the state machine's names are what its
        200 tests assert on.
        """
        return StateTimings(
            settle=timedelta(seconds=self.settle_s),
            sustain=timedelta(seconds=self.sustain_s),
            clear_before_free=timedelta(seconds=self.top_rung_clear_s),
            reservation_idle_warning=timedelta(seconds=self.lease_idle_warn_s),
            reservation_idle_release=timedelta(seconds=self.lease_idle_release_s),
            rung_change_gb=self.rung_change_gb,
            unreachable_after_samples=self.unreachable_after_polls,
        )


class FleetConfig(BaseModel):
    """The whole fleet."""

    model_config = {"frozen": True}

    tunables: Tunables
    hosts: tuple[HostConfig, ...]

    def host(self, name: str) -> HostConfig | None:
        return next((h for h in self.hosts if h.name == name), None)


def _check_ladder_fits(host: HostConfig) -> list[str]:
    """Problems that are legal pydantic but wrong on the hardware.

    Every one of these is a config that would have the controller confidently
    plan something the card cannot hold.
    """
    problems: list[str] = []
    headroom = 3.0  # the stricter of the two; a rung that fails here is unusable

    always_on = sum(r.footprint_gb for r in host.rungs if r.always_on)
    if always_on + headroom > host.total_vram_gb:
        problems.append(
            f"{host.name}: always_on rungs total {always_on:.1f} GB, which leaves less "
            f"than {headroom:.1f} GB headroom on a {host.total_vram_gb:.1f} GB card. "
            f"They can never all be resident."
        )

    for rung in host.rungs:
        if rung.footprint_gb + headroom > host.total_vram_gb:
            problems.append(
                f"{host.name}/{rung.name}: {rung.footprint_gb:.1f} GB + {headroom:.1f} GB "
                f"headroom exceeds the card's {host.total_vram_gb:.1f} GB. This rung can "
                f"never be selected -- remove it or correct the footprint."
            )

    names = [r.name for r in host.rungs]
    if len(names) != len(set(names)):
        problems.append(f"{host.name}: duplicate rung names {names}")

    return problems


def load(path: Path) -> FleetConfig:
    """Parse and validate. Raises `ConfigError` with everything wrong, not just the first.

    Reporting one problem at a time turns a five-minute fix into five restarts.
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"fleet config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"fleet config is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"fleet config must be a mapping, got {type(raw).__name__}")

    try:
        cfg = FleetConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"fleet config failed validation:\n{exc}") from exc

    if not cfg.hosts:
        raise ConfigError("fleet config lists no hosts")

    names = [h.name for h in cfg.hosts]
    problems = [f"duplicate host names: {names}"] if len(names) != len(set(names)) else []
    for host in cfg.hosts:
        problems.extend(_check_ladder_fits(host))

    if problems:
        raise ConfigError(
            "fleet config is internally inconsistent:\n  - " + "\n  - ".join(problems)
        )

    return cfg
