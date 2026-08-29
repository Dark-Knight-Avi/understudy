"""Tests for `generate_image` -- admission control, then ComfyUI.

Two properties matter more than the rest, and both are about *not* doing
something. The tool must decline in milliseconds when `.226` is claimed rather
than submitting a job and waiting three minutes (docs/14 section 4.4 rule 3,
M6 acceptance test 6). And it must never preempt a coding session for a picture
(docs/03) -- so `TestNeverPreempts` asserts that no request the fleet controller
receives is a reservation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp_tools.tools.image import (
    ASPECTS,
    NODE_EMPTY_LATENT,
    NODE_POSITIVE_PROMPT,
    NODE_SAMPLER,
    QUALITY_STEPS,
    ComfyBackend,
    FleetBackend,
    HostAdmission,
    ImageQueue,
    Rung,
    admit,
    build_workflow,
    generate_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


def fleet_status(*, state: str = "free", free_mb: int = 20000, reachable: bool = True) -> dict:
    return {
        "hosts": [
            {
                "id": "226",
                "reachable": reachable,
                "state": state,
                "gpu": {"vram_total_mb": 24564, "vram_free_mb": free_mb},
                "rung": {"id": "coder"},
            }
        ]
    }


def workflow_dir(tmp_path: Path) -> Path:
    """A workflow directory holding one API-format graph per rung."""
    directory = tmp_path / "comfy"
    directory.mkdir()
    graph = {
        NODE_SAMPLER: {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20}},
        NODE_EMPTY_LATENT: {"class_type": "EmptyLatentImage", "inputs": {"width": 1, "height": 1}},
        NODE_POSITIVE_PROMPT: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    }
    for rung in ("flux-schnell", "sd35-medium"):
        (directory / f"{rung}.json").write_text(json.dumps(graph), encoding="utf-8")
    return directory


class TestAdmissionBands:
    """The bands in docs/03. Pure arithmetic, so sweep it rather than spot-check."""

    @pytest.mark.parametrize(
        ("free_gb", "expected"),
        [
            (24.0, "flux-schnell"),
            (15.0, "flux-schnell"),
            (14.9, "sd35-medium"),
            (9.0, "sd35-medium"),
            (8.9, None),
            (0.0, None),
        ],
    )
    def test_rung_by_free_vram(self, free_gb: float, expected: str | None) -> None:
        rung, _ = admit(HostAdmission(reachable=True, state="sharing", free_gb=free_gb))
        assert (rung.name if rung else None) == expected

    def test_below_the_bottom_band_it_queues_rather_than_refusing_outright(self) -> None:
        """The distinction changes what the agent does next, so it is asserted."""
        _, reason = admit(HostAdmission(reachable=True, state="sharing", free_gb=4.0))
        assert reason == "queued"

    def test_a_claimed_host_loads_nothing_however_much_vram_is_free(self) -> None:
        rung, reason = admit(HostAdmission(reachable=True, state="yielding", free_gb=24.0))
        assert rung is None
        assert reason == "claimed"

    @pytest.mark.parametrize(
        "host",
        [
            HostAdmission(reachable=False, state="free", free_gb=24.0),
            HostAdmission(reachable=True, state="unknown", free_gb=24.0),
        ],
        ids=["unreachable", "unknown"],
    )
    def test_a_host_we_cannot_see_is_assumed_to_be_in_use(self, host: HostAdmission) -> None:
        """Absence of evidence is not evidence of a free GPU."""
        assert admit(host)[0] is None


class TestWorkflowPatching:
    def test_patches_by_node_id_and_never_generates_the_graph(self, tmp_path: Path) -> None:
        patched = build_workflow(
            Rung("flux-schnell", "FLUX.1-schnell", 15.0, False),
            prompt="a cat",
            aspect="16:9",
            seed=7,
            steps=4,
            workflow_dir=workflow_dir(tmp_path),
        )
        assert patched[NODE_POSITIVE_PROMPT]["inputs"]["text"] == "a cat"
        assert patched[NODE_EMPTY_LATENT]["inputs"]["width"] == ASPECTS["16:9"][0]
        assert patched[NODE_EMPTY_LATENT]["inputs"]["height"] == ASPECTS["16:9"][1]
        assert patched[NODE_SAMPLER]["inputs"]["seed"] == 7
        assert patched[NODE_SAMPLER]["inputs"]["steps"] == 4

    def test_the_file_on_disk_is_not_mutated(self, tmp_path: Path) -> None:
        """A patched-in-place graph would leak one request's prompt into the next."""
        directory = workflow_dir(tmp_path)
        rung = Rung("flux-schnell", "FLUX.1-schnell", 15.0, False)
        build_workflow(rung, prompt="first", aspect="1:1", seed=1, steps=4, workflow_dir=directory)
        again = build_workflow(
            rung, prompt="second", aspect="1:1", seed=2, steps=4, workflow_dir=directory
        )
        assert again[NODE_POSITIVE_PROMPT]["inputs"]["text"] == "second"

    def test_a_renumbered_graph_fails_loudly(self, tmp_path: Path) -> None:
        """Re-exporting from the UI renumbers nodes; a mis-patched graph looks fine."""
        directory = tmp_path / "comfy"
        directory.mkdir()
        (directory / "flux-schnell.json").write_text(
            json.dumps({"99": {"inputs": {}}}), encoding="utf-8"
        )
        with pytest.raises(Exception, match="node"):
            build_workflow(
                Rung("flux-schnell", "FLUX.1-schnell", 15.0, False),
                prompt="x",
                aspect="1:1",
                seed=1,
                steps=4,
                workflow_dir=directory,
            )


def run_tool(
    tmp_path: Path,
    *,
    fleet_body: dict[str, Any] | None = None,
    fleet_status_code: int = 200,
    comfy_handler: Any = None,
    prompt: str = "a diagram of the fleet",
    aspect: str = "16:9",
    quality: str = "fast",
    with_workflows: bool = True,
) -> tuple[dict[str, Any], list[httpx.Request]]:
    fleet_seen: list[httpx.Request] = []

    def fleet_transport(request: httpx.Request) -> httpx.Response:
        fleet_seen.append(request)
        return httpx.Response(fleet_status_code, json=fleet_body or fleet_status())

    def default_comfy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p1"})
        if request.url.path.startswith("/history/"):
            return httpx.Response(
                200,
                json={
                    "p1": {
                        "outputs": {
                            "9": {
                                "images": [{"filename": "a.png", "subfolder": "", "type": "output"}]
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, content=PNG)

    result = asyncio.run(
        generate_image(
            prompt,
            aspect,
            quality,
            fleet=FleetBackend(
                base_url="http://fleet:9000",
                timeout_s=2.0,
                client=httpx.AsyncClient(transport=httpx.MockTransport(fleet_transport)),
            ),
            comfy=ComfyBackend(
                base_url="http://comfy:8188",
                timeout_s=5.0,
                client=httpx.AsyncClient(
                    transport=httpx.MockTransport(comfy_handler or default_comfy)
                ),
            ),
            queue=ImageQueue(max_pending=2),
            host_id="226",
            workflow_dir=workflow_dir(tmp_path) if with_workflows else None,
            artifact_dir=tmp_path / "artifacts",
            artifact_base_url="https://ai.internal/artifacts",
            seed=1,
        )
    )
    return result, fleet_seen


class TestHappyPath:
    def test_a_free_host_renders_at_full_quality_and_says_which_model(self, tmp_path: Path) -> None:
        """docs/15 acceptance test 6."""
        result, _ = run_tool(tmp_path)
        assert result["ok"] is True
        assert result["model"] == "FLUX.1-schnell"
        assert "note" not in result
        assert (tmp_path / "artifacts" / f"{result['file_id']}.png").read_bytes() == PNG

    def test_a_partly_claimed_host_downgrades_and_explains_why(self, tmp_path: Path) -> None:
        """docs/15 acceptance test 7. An unexplained quality drop erodes trust."""
        result, _ = run_tool(tmp_path, fleet_body=fleet_status(state="sharing", free_mb=10 * 1024))
        assert result["ok"] is True
        assert result["model"] == "SD3.5-medium"
        assert "Reduced quality" in result["note"]


class TestNeverPreempts:
    """docs/03: an image request queues behind a coding session, it never evicts one."""

    def test_the_tool_only_ever_reads_fleet_state(self, tmp_path: Path) -> None:
        _, fleet_seen = run_tool(tmp_path)
        assert [r.method for r in fleet_seen] == ["GET"]
        assert all("reserve" not in str(r.url) for r in fleet_seen)

    def test_a_busy_host_is_told_that_nothing_was_interrupted(self, tmp_path: Path) -> None:
        result, _ = run_tool(tmp_path, fleet_body=fleet_status(state="sharing", free_mb=4 * 1024))
        assert result["error"] == "unavailable"
        assert "Nothing was interrupted" in result["detail"]


class TestDeclinesPromptly:
    def test_a_claimed_host_refuses_before_reaching_comfyui(self, tmp_path: Path) -> None:
        """M6 acceptance test 6: clear message, almost immediately, no hang."""

        def tripwire(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"submitted a job to a claimed host: {request.url}")

        result, _ = run_tool(
            tmp_path, fleet_body=fleet_status(state="yielding"), comfy_handler=tripwire
        )
        assert result["error"] == "unavailable"
        assert "in use by its owner" in result["detail"]

    def test_an_unreachable_fleet_controller_declines_rather_than_guessing(
        self, tmp_path: Path
    ) -> None:
        result, _ = run_tool(tmp_path, fleet_status_code=503)
        assert result["error"] == "unavailable"
        assert result["service"] == "fleet-controller"

    def test_a_host_missing_from_the_status_document_declines(self, tmp_path: Path) -> None:
        result, _ = run_tool(tmp_path, fleet_body={"hosts": []})
        assert result["error"] == "unavailable"


class TestArgumentValidation:
    """Validated server-side, with the valid values in the message, not as an enum."""

    def test_unknown_aspect_is_refused_and_lists_what_is_valid(self, tmp_path: Path) -> None:
        result, seen = run_tool(tmp_path, aspect="21:9")
        assert result["error"] == "refused"
        assert "16:9" in result["detail"]
        assert seen == [], "a bad argument must not cost a fleet call"

    def test_unknown_quality_is_refused(self, tmp_path: Path) -> None:
        result, _ = run_tool(tmp_path, quality="ultra")
        assert result["error"] == "refused"
        assert all(name in result["detail"] for name in QUALITY_STEPS)

    def test_blank_prompt_is_refused(self, tmp_path: Path) -> None:
        assert run_tool(tmp_path, prompt="  ")[0]["error"] == "refused"


class TestComfyFailures:
    def test_missing_workflows_are_a_configuration_message_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        result, _ = run_tool(tmp_path, with_workflows=False)
        assert result["error"] == "unavailable"
        assert "misconfigured" in result["detail"]

    def test_a_full_comfy_queue_is_busy_not_offline(self, tmp_path: Path) -> None:
        """'Busy' tells the agent to come back; 'offline' tells it to stop."""
        result, _ = run_tool(
            tmp_path, comfy_handler=lambda _: httpx.Response(429, json={"error": "full"})
        )
        assert result["error"] == "unavailable"
        assert "busy" in result["detail"]

    def test_a_job_that_finishes_with_no_image_is_unavailable(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "p1"})
            return httpx.Response(200, json={"p1": {"outputs": {"9": {"images": []}}}})

        result, _ = run_tool(tmp_path, comfy_handler=handler)
        assert result["error"] == "unavailable"

    def test_a_timeout_cancels_the_job_rather_than_orphaning_it(self, tmp_path: Path) -> None:
        """Otherwise the host keeps working for us after we have given up on it."""
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "p1"})
            if request.url.path == "/interrupt":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={})

        result = asyncio.run(
            generate_image(
                "a cat",
                "1:1",
                "fast",
                fleet=FleetBackend(
                    base_url="http://fleet:9000",
                    timeout_s=2.0,
                    client=httpx.AsyncClient(
                        transport=httpx.MockTransport(
                            lambda _: httpx.Response(200, json=fleet_status())
                        )
                    ),
                ),
                comfy=ComfyBackend(
                    base_url="http://comfy:8188",
                    timeout_s=0.01,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                ),
                queue=ImageQueue(),
                host_id="226",
                workflow_dir=workflow_dir(tmp_path),
                artifact_dir=tmp_path / "artifacts",
                artifact_base_url="https://ai.internal/artifacts",
                seed=1,
            )
        )
        assert result["error"] == "unavailable"
        assert "/interrupt" in paths


class TestQueue:
    def test_beyond_the_depth_cap_callers_are_told_busy_immediately(self) -> None:
        """A chatty agent must not be able to wedge the host for its owner."""
        queue = ImageQueue(max_pending=1)

        async def scenario() -> str:
            async with queue.slot():
                try:
                    async with queue.slot():
                        return "admitted"
                except Exception as exc:  # noqa: BLE001 - the type is the assertion
                    return type(exc).__name__

        assert asyncio.run(scenario()) == "ComfyBusy"

    def test_a_slot_is_released_even_when_the_job_fails(self) -> None:
        queue = ImageQueue(max_pending=1)

        async def scenario() -> str:
            for _ in range(3):
                try:
                    async with queue.slot():
                        raise RuntimeError("render blew up")
                except RuntimeError:
                    continue
            async with queue.slot():
                return "admitted"

        assert asyncio.run(scenario()) == "admitted"
