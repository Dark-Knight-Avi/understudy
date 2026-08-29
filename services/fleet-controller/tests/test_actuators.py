"""Tests for the actuation surface.

Everything here runs against `httpx.MockTransport`, because no host in the fleet
is reachable from a development machine and this is the only way the code that
touches them gets exercised before M2.

Async tests are driven with `asyncio.run` rather than a plugin: the project pins
its dependencies deliberately (docs/delivery-plan.md section 4) and one helper
function is a cheaper price than another package in the lockfile.

Two properties are worth more than the rest and are asserted repeatedly:

  * **Nothing raises into the control loop.** Every failure path returns an
    `ActuationResult`. The loop runs every ~2 s per host and cannot afford an
    exception escaping step 5.
  * **A 404 blames the target's version, not us.** That message is the only thing
    standing between an operator and half an hour of grepping this package for a
    bug that is not in it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

import httpx
import pytest
from fleet_controller.actuators import (
    ActuationOutcome,
    ActuationResult,
    Actuator,
    AgentEndpoints,
    ComfyEndpoints,
    FakeActuator,
    HostEndpoints,
    HttpActuator,
    LiteLlmEndpoints,
    RetryPolicy,
    RoutingTarget,
    VllmEndpoints,
    keep_demoted,
)
from fleet_controller.models import HostConfig, HostState, Rung

T = TypeVar("T")

# The .226 ladder from docs/03 section 3, weights-only estimates pending measurement.
CODER = Rung(name="coder", served_model="qwen3-coder-30b-a3b-int4", footprint_gb=17.0)
CHAT8 = Rung(name="chat-small", served_model="qwen3-8b-int4", footprint_gb=5.5)

# Image rungs, also docs/03 section 3. With SHARING headroom these reproduce the
# published admission table: FLUX at >= 15 GB free, SD3.5 at >= 9 GB, queue below.
FLUX = Rung(name="flux", served_model="flux.1-schnell-fp8", footprint_gb=12.0)
SD35 = Rung(name="sd35", served_model="sd3.5-medium", footprint_gb=6.0)
IMAGE_RUNGS = (FLUX, SD35)

VLLM_URL = "http://10.0.0.226:8000"
AGENT_URL = "http://10.0.0.226:9101"
COMFY_URL = "http://10.0.0.226:8188"
GATEWAY_URL = "http://10.0.0.87:4000"

HOST_226 = HostConfig(
    name="226",
    address="10.0.0.226",
    total_vram_gb=24.0,
    rungs=(CODER, CHAT8),
    vllm_url=VLLM_URL,
)

ENDPOINTS = {
    "226": HostEndpoints(agent_base_url=AGENT_URL, comfy_base_url=COMFY_URL),
}

GATEWAY = LiteLlmEndpoints(base_url=GATEWAY_URL, master_key="sk-master")  # type: ignore[arg-type]

# One immediate retry, no sleeping: the retry path must be exercised without the
# test suite paying a backoff for it.
FAST_RETRY = RetryPolicy(attempts=2, backoff_s=0.0)

GB = 1024**3


def run(coro: Awaitable[T]) -> T:
    """Drive one coroutine to completion. Cheaper than an asyncio plugin."""
    return asyncio.run(_await(coro))


async def _await(coro: Awaitable[T]) -> T:
    return await coro


class Recorder:
    """A MockTransport handler that records requests and replays scripted replies.

    `routes` maps a URL path to a callable returning a response, so a test can
    make the second call to the same path behave differently -- which is how the
    retry and idempotency cases are expressed.
    """

    def __init__(self, routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"detail": "no such route"})
        return handler(request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


def const(status: int, payload: Any = None) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


def sequence(*responses: int) -> Callable[[httpx.Request], httpx.Response]:
    """Return each status in turn, repeating the last one forever."""
    remaining: Iterator[int] = iter(responses)
    last = [responses[-1]]

    def handler(_request: httpx.Request) -> httpx.Response:
        with contextlib.suppress(StopIteration):
            last[0] = next(remaining)
        return httpx.Response(last[0], json={})

    return handler


def boom(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def build(
    routes: dict[str, Callable[[httpx.Request], httpx.Response]],
    **kwargs: Any,
) -> tuple[HttpActuator, Recorder]:
    recorder = Recorder(routes)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    kwargs.setdefault("endpoints", ENDPOINTS)
    kwargs.setdefault("litellm", GATEWAY)
    kwargs.setdefault("retry", FAST_RETRY)
    return HttpActuator(client, **kwargs), recorder


# --------------------------------------------------------------------------- sleep/wake


class TestSleep:
    def test_success_reports_vram_released(self) -> None:
        actuator, rec = build(
            {"/is_sleeping": const(200, {"is_sleeping": False}), "/sleep": const(200, {})}
        )
        result = run(actuator.sleep(HOST_226))
        assert result.outcome is ActuationOutcome.OK
        assert result.ok
        assert result.vram_released is True
        assert keep_demoted(result) is False
        assert "/sleep" in rec.paths()

    def test_level_goes_in_the_query_by_default(self) -> None:
        actuator, rec = build(
            {"/is_sleeping": const(200, {"is_sleeping": False}), "/sleep": const(200, {})}
        )
        run(actuator.sleep(HOST_226))
        sleep_request = next(r for r in rec.requests if r.url.path == "/sleep")
        assert sleep_request.url.params["level"] == "1"

    def test_level_can_be_moved_into_the_body(self) -> None:
        """The two docs disagree on this, which is why it is config."""
        actuator, rec = build(
            {"/is_sleeping": const(200, {"is_sleeping": False}), "/sleep": const(200, {})},
            vllm=VllmEndpoints(level_in_query=False),
        )
        run(actuator.sleep(HOST_226))
        sleep_request = next(r for r in rec.requests if r.url.path == "/sleep")
        assert "level" not in sleep_request.url.params
        assert b'"level": 1' in sleep_request.content.replace(b'"level":1', b'"level": 1')

    def test_paths_are_configurable(self) -> None:
        """Nothing is hardcoded: a renamed endpoint is a config change."""
        actuator, rec = build(
            {"/v1/sleep": const(200, {})},
            vllm=VllmEndpoints(sleep_path="/v1/sleep", is_sleeping_path=None),
        )
        result = run(actuator.sleep(HOST_226))
        assert result.ok
        assert rec.paths() == ["/v1/sleep"]

    def test_every_call_carries_an_explicit_deadline(self) -> None:
        """A hung actuator must not stall the control loop."""
        actuator, rec = build(
            {"/is_sleeping": const(200, {"is_sleeping": False}), "/sleep": const(200, {})}
        )
        run(actuator.sleep(HOST_226))
        for request in rec.requests:
            timeout = request.extensions["timeout"]
            assert timeout["connect"] is not None
            assert timeout["read"] is not None


class TestIdempotency:
    def test_sleeping_an_asleep_server_is_a_quiet_success(self) -> None:
        actuator, rec = build({"/is_sleeping": const(200, {"is_sleeping": True})})
        result = run(actuator.sleep(HOST_226))
        assert result.outcome is ActuationOutcome.ALREADY
        assert result.ok
        assert result.vram_released is True
        assert rec.count("/sleep") == 0, "must not re-issue a sleep it does not need"

    def test_double_sleep_survives_a_server_that_rejects_the_second(self) -> None:
        """Some versions 4xx a redundant sleep. The loop replays this constantly."""
        state = {"asleep": False}

        def probe(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"is_sleeping": state["asleep"]})

        def do_sleep(_request: httpx.Request) -> httpx.Response:
            if state["asleep"]:
                return httpx.Response(400, json={"detail": "already sleeping"})
            state["asleep"] = True
            return httpx.Response(200, json={})

        actuator, _ = build({"/is_sleeping": probe, "/sleep": do_sleep})
        first = run(actuator.sleep(HOST_226))
        second = run(actuator.sleep(HOST_226))
        assert first.outcome is ActuationOutcome.OK
        assert second.outcome is ActuationOutcome.ALREADY
        assert second.ok and second.vram_released is True

    def test_double_sleep_is_quiet_without_the_probe_endpoint(self) -> None:
        """Idempotency must not depend on a route some versions do not have."""
        actuator, _ = build(
            {"/sleep": sequence(200, 200)}, vllm=VllmEndpoints(is_sleeping_path=None)
        )
        assert run(actuator.sleep(HOST_226)).ok
        assert run(actuator.sleep(HOST_226)).ok

    def test_waking_an_awake_server_is_a_quiet_success(self) -> None:
        actuator, rec = build({"/is_sleeping": const(200, {"is_sleeping": False})})
        result = run(actuator.wake(HOST_226))
        assert result.outcome is ActuationOutcome.ALREADY
        assert rec.count("/wake_up") == 0

    def test_wake_never_claims_vram_was_released(self) -> None:
        """Waking takes VRAM. It can never be the reason we stop demoting a host."""
        actuator, _ = build(
            {"/is_sleeping": const(200, {"is_sleeping": True}), "/wake_up": const(200, {})}
        )
        result = run(actuator.wake(HOST_226))
        assert result.ok
        assert result.vram_released is False
        assert keep_demoted(result) is True


class TestVersionMismatch:
    """A 404 is a version difference on the target. Say so, in those words."""

    def test_sleep_404_blames_the_version_and_not_our_code(self) -> None:
        actuator, _ = build({"/is_sleeping": const(404), "/sleep": const(404)})
        result = run(actuator.sleep(HOST_226))
        assert result.outcome is ActuationOutcome.UNSUPPORTED
        assert result.version_mismatch is True
        assert not result.ok
        lowered = result.detail.lower()
        assert "version" in lowered
        assert "not a fault in fleet_controller" in lowered
        assert "vllm_server_dev_mode" in lowered
        assert "--enable-sleep-mode" in lowered

    def test_404_leaves_the_host_demoted(self) -> None:
        """We could not confirm an unload, so assume the model is still resident."""
        actuator, _ = build({"/is_sleeping": const(404), "/sleep": const(404)})
        assert keep_demoted(run(actuator.sleep(HOST_226))) is True

    def test_404_is_not_retried(self) -> None:
        """A missing route will still be missing in 500 ms."""
        actuator, rec = build({"/sleep": const(404)}, vllm=VllmEndpoints(is_sleeping_path=None))
        result = run(actuator.sleep(HOST_226))
        assert result.attempts == 1
        assert rec.count("/sleep") == 1

    def test_litellm_404_points_at_the_gateway_version(self) -> None:
        actuator, _ = build({})
        result = run(actuator.sync_routing(HOST_226, [], []))
        assert result.version_mismatch is True
        assert "version" in result.detail.lower()
        assert "store_model_in_db" in result.detail
        assert "not a fault in fleet_controller" in result.detail.lower()

    def test_agent_404_points_at_the_agent_version(self) -> None:
        actuator, _ = build({})
        result = run(actuator.change_rung(HOST_226, CHAT8))
        assert result.version_mismatch is True
        assert "verb list" in result.detail


class TestTimeoutsAndRetries:
    def test_timeout_returns_a_result_and_does_not_burn_the_budget(self) -> None:
        actuator, rec = build(
            {"/sleep": boom(httpx.ReadTimeout("too slow"))},
            vllm=VllmEndpoints(is_sleeping_path=None),
            retry=RetryPolicy(attempts=3, backoff_s=0.0),
        )
        result = run(actuator.sleep(HOST_226))
        assert result.outcome is ActuationOutcome.TIMEOUT
        assert result.attempts == 1, "retrying a deadline multiplies the stall we forbid"
        assert rec.count("/sleep") == 1
        assert keep_demoted(result) is True
        assert "deadline" in result.detail

    def test_timeout_retries_when_the_policy_asks_for_it(self) -> None:
        actuator, rec = build(
            {"/sleep": boom(httpx.ReadTimeout("too slow"))},
            vllm=VllmEndpoints(is_sleeping_path=None),
            retry=RetryPolicy(attempts=2, backoff_s=0.0, retry_on_timeout=True),
        )
        result = run(actuator.sleep(HOST_226))
        assert result.attempts == 2
        assert rec.count("/sleep") == 2

    def test_500_is_retried_and_can_succeed(self) -> None:
        actuator, rec = build(
            {"/sleep": sequence(500, 200)}, vllm=VllmEndpoints(is_sleeping_path=None)
        )
        result = run(actuator.sleep(HOST_226))
        assert result.outcome is ActuationOutcome.OK
        assert result.attempts == 2
        assert rec.count("/sleep") == 2
        assert result.vram_released is True

    def test_persistent_500_exhausts_a_bounded_budget(self) -> None:
        actuator, rec = build(
            {"/sleep": const(500, {"detail": "engine busy"})},
            vllm=VllmEndpoints(is_sleeping_path=None),
            retry=RetryPolicy(attempts=3, backoff_s=0.0),
        )
        result = run(actuator.sleep(HOST_226))
        assert result.outcome is ActuationOutcome.FAILED
        assert result.attempts == 3
        assert rec.count("/sleep") == 3
        assert keep_demoted(result) is True

    def test_transport_error_is_unreachable_not_an_exception(self) -> None:
        actuator, _ = build(
            {"/sleep": boom(httpx.ConnectError("no route to host"))},
            vllm=VllmEndpoints(is_sleeping_path=None),
        )
        result = run(actuator.sleep(HOST_226))
        assert result.outcome is ActuationOutcome.UNREACHABLE
        assert keep_demoted(result) is True


# --------------------------------------------------------------------------- rung change


class TestChangeRung:
    def test_a_rung_change_restarts_the_server_and_is_not_a_wake(self) -> None:
        """The distinction that causes the most confusion in this system."""
        actuator, rec = build({"/actuate/restart": const(200, {})})
        result = run(actuator.change_rung(HOST_226, CHAT8, gpu_memory_utilization=0.45))
        assert result.ok
        assert rec.paths() == ["/actuate/restart"]
        assert "/sleep" not in rec.paths()
        assert "/wake_up" not in rec.paths()
        body = rec.requests[0].content.decode()
        assert CHAT8.served_model in body
        assert "gpu_memory_utilization" in body

    def test_a_rung_change_gets_a_much_longer_deadline_than_a_wake(self) -> None:
        """A restart is a model load, not a wake; the budgets differ by an order."""
        actuator, rec = build(
            {
                "/actuate/restart": const(200, {}),
                "/is_sleeping": const(200, {"is_sleeping": True}),
                "/wake_up": const(200, {}),
            }
        )
        run(actuator.change_rung(HOST_226, CHAT8))
        run(actuator.wake(HOST_226))
        restart = next(r for r in rec.requests if r.url.path == "/actuate/restart")
        wake = next(r for r in rec.requests if r.url.path == "/wake_up")
        assert restart.extensions["timeout"]["read"] > wake.extensions["timeout"]["read"] * 5

    def test_the_off_rung_stops_the_server_and_releases_vram(self) -> None:
        actuator, rec = build({"/actuate/stop": const(200, {})})
        result = run(actuator.change_rung(HOST_226, None))
        assert result.ok
        assert result.vram_released is True
        assert rec.paths() == ["/actuate/stop"]

    def test_a_failed_rung_change_keeps_the_host_demoted(self) -> None:
        actuator, _ = build({"/actuate/restart": const(500, {})})
        result = run(actuator.change_rung(HOST_226, CHAT8))
        assert not result.ok
        assert keep_demoted(result) is True

    def test_the_agents_verbs_are_configurable(self) -> None:
        """The vocabulary is fixed; where it lives is not."""
        actuator, rec = build(
            {"/v1/restart": const(200, {})},
            agent=AgentEndpoints(restart_path="/v1/restart", token="agent-token"),  # type: ignore[arg-type]
        )
        result = run(actuator.change_rung(HOST_226, CHAT8))
        assert result.ok
        assert rec.paths() == ["/v1/restart"]
        assert rec.requests[0].headers["authorization"] == "Bearer agent-token"

    def test_missing_agent_config_returns_a_result(self) -> None:
        actuator, _ = build({}, endpoints={})
        result = run(actuator.change_rung(HOST_226, CHAT8))
        assert result.outcome is ActuationOutcome.FAILED
        assert "agent base URL" in result.detail


# --------------------------------------------------------------------------- routing


CHAT_226 = RoutingTarget(
    deployment_id="chat-226",
    public_name="chat",
    served_model="fast-tier",
    api_base=f"{VLLM_URL}/v1",
    api_key="os.environ/VLLM_226_KEY",
)
CODER_226 = RoutingTarget(
    deployment_id="coder-226",
    public_name="coder",
    served_model="coder",
    api_base=f"{VLLM_URL}/v1",
    api_key="os.environ/VLLM_226_KEY",
)
OWNED = ("chat-226", "coder-226")


def _catalog(*ids: str) -> dict[str, Any]:
    return {"data": [{"model_name": i.split("-")[0], "model_info": {"id": i}} for i in ids]}


class TestRouting:
    def test_adds_missing_and_removes_stale_deployments(self) -> None:
        actuator, rec = build(
            {
                "/model/info": const(200, _catalog("coder-226")),
                "/model/new": const(200, {}),
                "/model/delete": const(200, {}),
            }
        )
        result = run(actuator.sync_routing(HOST_226, [CHAT_226], OWNED))
        assert result.outcome is ActuationOutcome.OK
        assert rec.count("/model/new") == 1
        assert rec.count("/model/delete") == 1
        deleted = next(r for r in rec.requests if r.url.path == "/model/delete")
        assert b"coder-226" in deleted.content

    def test_replaying_the_same_desired_state_is_a_no_op(self) -> None:
        """The controller replays this every loop; it must not duplicate anything."""
        actuator, rec = build(
            {
                "/model/info": const(200, _catalog("chat-226")),
                "/model/new": const(200, {}),
                "/model/delete": const(200, {}),
            }
        )
        result = run(actuator.sync_routing(HOST_226, [CHAT_226], OWNED))
        assert result.outcome is ActuationOutcome.ALREADY
        assert rec.count("/model/new") == 0
        assert rec.count("/model/delete") == 0

    def test_it_never_touches_a_deployment_this_host_does_not_own(self) -> None:
        actuator, rec = build(
            {
                "/model/info": const(200, _catalog("chat-87", "chat-226")),
                "/model/delete": const(200, {}),
            }
        )
        run(actuator.sync_routing(HOST_226, [], OWNED))
        deleted = [r.content for r in rec.requests if r.url.path == "/model/delete"]
        assert len(deleted) == 1
        assert b"chat-226" in deleted[0]

    def test_the_master_key_is_sent_and_no_backend_secret_is(self) -> None:
        actuator, rec = build({"/model/info": const(200, _catalog()), "/model/new": const(200, {})})
        run(actuator.sync_routing(HOST_226, [CHAT_226], OWNED))
        added = next(r for r in rec.requests if r.url.path == "/model/new")
        assert added.headers["authorization"] == "Bearer sk-master"
        assert b"os.environ/VLLM_226_KEY" in added.content

    def test_a_partial_failure_is_reported_not_raised(self) -> None:
        actuator, _ = build({"/model/info": const(200, _catalog()), "/model/new": const(500, {})})
        result = run(actuator.sync_routing(HOST_226, [CHAT_226, CODER_226], OWNED))
        assert result.outcome is ActuationOutcome.FAILED
        assert "routing changes failed" in result.detail
        assert "fallbacks" in result.detail, "say that the pull path still covers this"

    def test_an_unreadable_catalog_stops_before_it_changes_anything(self) -> None:
        actuator, rec = build(
            {"/model/info": boom(httpx.ConnectError("gateway down")), "/model/new": const(200)}
        )
        result = run(actuator.sync_routing(HOST_226, [CHAT_226], OWNED))
        assert result.outcome is ActuationOutcome.UNREACHABLE
        assert rec.count("/model/new") == 0

    def test_no_gateway_configured_returns_a_result(self) -> None:
        actuator, _ = build({}, litellm=None)
        result = run(actuator.sync_routing(HOST_226, [CHAT_226], OWNED))
        assert result.outcome is ActuationOutcome.FAILED
        assert "LiteLLM" in result.detail


# --------------------------------------------------------------------------- admission


def _stats(free_bytes: float) -> dict[str, Any]:
    return {"devices": [{"name": "cuda:0", "vram_total": 24 * GB, "vram_free": free_bytes}]}


class TestImageAdmission:
    @pytest.mark.parametrize(
        ("free_gb", "expected"),
        [(20.0, "flux"), (15.0, "flux"), (14.9, "sd35"), (9.0, "sd35"), (8.9, None), (0.0, None)],
    )
    def test_reproduces_the_published_admission_table(
        self, free_gb: float, expected: str | None
    ) -> None:
        actuator, _ = build({"/system_stats": const(200, _stats(free_gb * GB))})
        answer = run(actuator.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert (answer.rung.name if answer.rung else None) == expected
        assert answer.admitted is (expected is not None)

    def test_a_queued_job_says_it_is_waiting_for_the_gpu(self) -> None:
        """The coder (~17 GB) and FLUX (~12 GB) cannot co-reside. Queue, never evict."""
        actuator, rec = build({"/system_stats": const(200, _stats(5 * GB))})
        answer = run(actuator.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert answer.admitted is False
        assert "waiting for GPU" in answer.reason
        assert all(r.method == "GET" for r in rec.requests), "admission never actuates"

    def test_an_unreachable_comfy_refuses_rather_than_guesses(self) -> None:
        actuator, _ = build({"/system_stats": boom(httpx.ConnectError("down"))})
        answer = run(actuator.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert answer.admitted is False
        assert answer.result.outcome is ActuationOutcome.UNREACHABLE
        assert "assuming it is in use" in answer.reason

    def test_an_unrecognised_stats_shape_refuses_and_names_the_version(self) -> None:
        actuator, _ = build({"/system_stats": const(200, {"devices": [{"foo": 1}]})})
        answer = run(actuator.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert answer.admitted is False
        assert "stats route has moved between versions" in answer.result.detail

    def test_a_404_names_the_version_and_still_returns_an_answer(self) -> None:
        actuator, _ = build({})
        answer = run(actuator.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert answer.admitted is False
        assert answer.result.version_mismatch is True

    def test_the_smallest_reading_wins(self) -> None:
        """Underestimating free VRAM queues an image; overestimating it costs a job."""
        actuator, _ = build(
            {
                "/system_stats": const(
                    200,
                    {"devices": [{"vram_free": 20 * GB, "torch_vram_free": 8 * GB}]},
                )
            }
        )
        answer = run(actuator.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert answer.admitted is False

    def test_stats_path_is_configurable(self) -> None:
        actuator, rec = build(
            {"/api/system_stats": const(200, _stats(20 * GB))},
            comfy=ComfyEndpoints(stats_path="/api/system_stats"),
        )
        answer = run(actuator.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert answer.admitted is True
        assert rec.paths() == ["/api/system_stats"]


# --------------------------------------------------------------------------- no raising


class TestNothingEscapesIntoTheControlLoop:
    """Every failure path returns a result. This is the invariant, so sweep it."""

    @pytest.mark.parametrize(
        "failure",
        [
            httpx.ConnectError("no route"),
            httpx.ReadTimeout("slow"),
            httpx.ConnectTimeout("slow"),
            httpx.RemoteProtocolError("garbage"),
            httpx.ReadError("reset"),
        ],
    )
    def test_transport_failures_never_raise(self, failure: Exception) -> None:
        routes = dict.fromkeys(
            [
                "/is_sleeping",
                "/sleep",
                "/wake_up",
                "/actuate/restart",
                "/actuate/stop",
                "/model/info",
                "/model/new",
                "/model/delete",
                "/system_stats",
            ],
            boom(failure),
        )
        actuator, _ = build(routes)
        assert isinstance(run(actuator.sleep(HOST_226)), ActuationResult)
        assert isinstance(run(actuator.wake(HOST_226)), ActuationResult)
        assert isinstance(run(actuator.change_rung(HOST_226, CHAT8)), ActuationResult)
        assert isinstance(run(actuator.change_rung(HOST_226, None)), ActuationResult)
        assert isinstance(run(actuator.sync_routing(HOST_226, [CHAT_226], OWNED)), ActuationResult)
        answer = run(actuator.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert answer.admitted is False

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429, 500, 503])
    def test_every_status_code_becomes_a_result(self, status: int) -> None:
        actuator, _ = build(
            {"/sleep": const(status, {"detail": "no"})},
            vllm=VllmEndpoints(is_sleeping_path=None),
        )
        result = run(actuator.sleep(HOST_226))
        assert isinstance(result, ActuationResult)
        assert result.ok is False
        assert keep_demoted(result) is True

    def test_a_malformed_body_does_not_raise(self) -> None:
        def not_json(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>nope</html>")

        actuator, _ = build({"/is_sleeping": not_json, "/sleep": const(200, {})})
        assert run(actuator.sleep(HOST_226)).ok


# --------------------------------------------------------------------------- fake


class TestFakeActuator:
    def test_it_satisfies_the_protocol(self) -> None:
        assert isinstance(FakeActuator(), Actuator)

    def test_double_sleep_and_double_wake_are_quiet(self) -> None:
        fake = FakeActuator()
        assert run(fake.sleep(HOST_226)).outcome is ActuationOutcome.OK
        assert run(fake.sleep(HOST_226)).outcome is ActuationOutcome.ALREADY
        assert run(fake.wake(HOST_226)).outcome is ActuationOutcome.OK
        assert run(fake.wake(HOST_226)).outcome is ActuationOutcome.ALREADY

    def test_it_counts_actuations(self) -> None:
        """The no-flapping test's pass criterion is a number, not an impression."""
        fake = FakeActuator()
        run(fake.sleep(HOST_226))
        run(fake.change_rung(HOST_226, CHAT8))
        assert fake.calls == [("sleep", "226"), ("change_rung", "226")]
        assert fake.rungs["226"] == "chat-small"

    def test_injected_failures_return_results_and_keep_the_host_demoted(self) -> None:
        fake = FakeActuator(fail_actions=["sleep"])
        result = run(fake.sleep(HOST_226))
        assert result.outcome is ActuationOutcome.FAILED
        assert keep_demoted(result) is True

    def test_admission_uses_the_same_ladder_as_the_real_thing(self) -> None:
        fake = FakeActuator(free_gb={"226": 16.0})
        answer = run(fake.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert answer.admitted is True
        assert answer.rung == FLUX

        fake.free_gb["226"] = 5.0
        refused = run(fake.image_admission(HOST_226, IMAGE_RUNGS, HostState.SHARING))
        assert refused.admitted is False
        assert "waiting for GPU" in refused.reason

    def test_only_the_off_rung_releases_vram(self) -> None:
        fake = FakeActuator()
        assert run(fake.change_rung(HOST_226, CODER)).vram_released is False
        assert run(fake.change_rung(HOST_226, None)).vram_released is True

    def test_routing_replays_are_quiet(self) -> None:
        fake = FakeActuator()
        assert run(fake.sync_routing(HOST_226, [CHAT_226], OWNED)).outcome is ActuationOutcome.OK
        second = run(fake.sync_routing(HOST_226, [CHAT_226], OWNED))
        assert second.outcome is ActuationOutcome.ALREADY


class TestHttpActuatorSatisfiesTheProtocol:
    def test_it_does(self) -> None:
        actuator, _ = build({})
        assert isinstance(actuator, Actuator)
