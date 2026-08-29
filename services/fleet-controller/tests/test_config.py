"""Config loading, including the real deploy/fleet.yaml.

The last test is the important one: it loads the file that will actually ship. A
config that parses but describes a ladder the hardware cannot hold is one of the
few ways this service does real damage, and nobody would see it until a rung
failed to load on a host at 3am.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fleet_controller.config import ConfigError, Tunables, load

REPO = Path(__file__).resolve().parents[3]
FLEET_YAML = REPO / "deploy" / "fleet.yaml"

MINIMAL = """
tunables: {}
hosts:
  - name: "226"
    address: 10.0.0.226
    total_vram_gb: 24.0
    rungs:
      - {name: coder, served_model: q, footprint_gb: 17.0}
"""


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fleet.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestLoading:
    def test_minimal(self, tmp_path: Path) -> None:
        cfg = load(write(tmp_path, MINIMAL))
        assert [h.name for h in cfg.hosts] == ["226"]
        assert cfg.host("226") is not None
        assert cfg.host("nope") is None

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load(tmp_path / "absent.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not valid YAML"):
            load(write(tmp_path, "hosts: [unclosed"))

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            load(write(tmp_path, "- just\n- a list\n"))

    def test_no_hosts(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load(write(tmp_path, "tunables: {}\nhosts: []\n"))

    def test_unknown_tunable_is_rejected(self) -> None:
        """extra=forbid. A typo'd knob that silently does nothing is worse than a crash."""
        with pytest.raises(Exception, match="settle_seconds"):
            Tunables(settle_seconds=60)  # type: ignore[call-arg]


class TestLadderSanity:
    """Configs that are valid YAML and valid types but wrong about the hardware."""

    def test_rung_larger_than_the_card(self, tmp_path: Path) -> None:
        body = MINIMAL.replace("footprint_gb: 17.0", "footprint_gb: 30.0")
        with pytest.raises(ConfigError, match="can never be selected"):
            load(write(tmp_path, body))

    def test_always_on_cannot_all_fit(self, tmp_path: Path) -> None:
        body = """
tunables: {}
hosts:
  - name: "87"
    address: 10.0.0.87
    total_vram_gb: 12.0
    rungs:
      - {name: a, served_model: a, footprint_gb: 6.0, always_on: true}
      - {name: b, served_model: b, footprint_gb: 5.0, always_on: true}
"""
        with pytest.raises(ConfigError, match="never all be resident"):
            load(write(tmp_path, body))

    def test_duplicate_rung_names(self, tmp_path: Path) -> None:
        body = """
tunables: {}
hosts:
  - name: "226"
    address: 10.0.0.226
    total_vram_gb: 24.0
    rungs:
      - {name: chat, served_model: a, footprint_gb: 9.0}
      - {name: chat, served_model: b, footprint_gb: 5.0}
"""
        with pytest.raises(ConfigError, match="duplicate rung names"):
            load(write(tmp_path, body))

    def test_all_problems_reported_at_once(self, tmp_path: Path) -> None:
        """One at a time turns a five-minute fix into five restarts."""
        body = """
tunables: {}
hosts:
  - name: "226"
    address: 10.0.0.226
    total_vram_gb: 24.0
    rungs:
      - {name: huge, served_model: a, footprint_gb: 40.0}
      - {name: also-huge, served_model: b, footprint_gb: 50.0}
"""
        with pytest.raises(ConfigError) as exc:
            load(write(tmp_path, body))
        assert "huge" in str(exc.value) and "also-huge" in str(exc.value)


class TestTheRealFile:
    def test_deploy_fleet_yaml_loads_and_is_consistent(self) -> None:
        """The config that actually ships must satisfy every check above."""
        cfg = load(FLEET_YAML)
        assert {h.name for h in cfg.hosts} >= {"226", "87"}

    def test_hub_does_not_detect_interactive_login(self) -> None:
        """.87 is the hub and always logged into; the trigger would pin it off forever."""
        hub = load(FLEET_YAML).host("87")
        assert hub is not None
        assert hub.detect_interactive_login is False

    def test_tunables_translate_to_state_timings(self) -> None:
        """The YAML and the state machine use different vocabularies. Prove the bridge."""
        timings = load(FLEET_YAML).tunables.as_timings()
        assert timings.settle.total_seconds() > 0
        assert timings.clear_before_free > timings.sustain
