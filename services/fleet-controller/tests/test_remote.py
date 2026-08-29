"""Reading another machine's GPU over HTTP.

Every test here is about a failure. The happy path is one line; what matters is
that no failure mode ever produces a *plausible but wrong* sample, because the
ladder acts on whatever it is handed. A host we cannot read must become UNKNOWN,
which never promotes -- not a host that looks empty.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fleet_controller.models import GpuSample, HostConfig, Rung
from fleet_controller.nvidia import NvidiaSmiError
from fleet_controller.remote import RemoteSampler

HOST = HostConfig(
    name="226",
    address="10.0.0.226",
    total_vram_gb=24.0,
    rungs=(Rung(name="chat", served_model="q", footprint_gb=9.0),),
)
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def good_payload(host: str = "226") -> dict[str, object]:
    return GpuSample(
        host=host, total_gb=24.0, used_gb=9.0, foreign_pids=(), sampled_at=NOW
    ).model_dump(mode="json")


def sampler(handler: object) -> RemoteSampler:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return RemoteSampler(httpx.AsyncClient(transport=transport))


def fetch(s: RemoteSampler) -> GpuSample:
    return asyncio.run(s(HOST))


class TestHappyPath:
    def test_parses_a_sample(self) -> None:
        s = sampler(lambda _r: httpx.Response(200, json=good_payload()))
        assert fetch(s).free_gb == pytest.approx(15.0)

    def test_url_uses_the_agent_port(self) -> None:
        s = sampler(lambda _r: httpx.Response(200, json=good_payload()))
        assert s.url_for(HOST) == "http://10.0.0.226:8099/gpu"


class TestFailuresBecomeUnknown:
    """All of these must raise, so the loop records no sample at all."""

    def test_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(NvidiaSmiError, match="timed out"):
            fetch(sampler(handler))

    def test_agent_returns_503(self) -> None:
        """What our own agent sends when nvidia-smi is untrustworthy."""
        s = sampler(lambda _r: httpx.Response(503, json={"error": "NvidiaSmiParseError"}))
        with pytest.raises(NvidiaSmiError, match="503"):
            fetch(s)

    def test_connection_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(NvidiaSmiError, match="unreachable"):
            fetch(sampler(handler))

    def test_html_error_page_instead_of_json(self) -> None:
        """A proxy or captive portal answering on the agent's port."""
        s = sampler(lambda _r: httpx.Response(200, text="<html>Gateway</html>"))
        with pytest.raises(NvidiaSmiError, match="invalid JSON"):
            fetch(s)

    def test_json_that_is_not_a_sample(self) -> None:
        s = sampler(lambda _r: httpx.Response(200, json={"hello": "world"}))
        with pytest.raises(NvidiaSmiError, match="unusable sample"):
            fetch(s)

    def test_wrong_host_is_refused(self) -> None:
        """A misconfigured address would apply one machine's ladder to another's card.

        The values would be perfectly well-formed, which is exactly why this has to
        be checked rather than trusted.
        """
        s = sampler(lambda _r: httpx.Response(200, json=good_payload(host="87")))
        with pytest.raises(NvidiaSmiError, match="expected '226'"):
            fetch(s)


class TestNothingLooksEmptyByAccident:
    def test_no_failure_yields_a_zero_used_sample(self) -> None:
        """The failure we most need to avoid: a broken read that reports a free card.

        Any of these returning a GpuSample with used_gb=0 would have the controller
        load a model on top of somebody's job.
        """
        handlers = [
            lambda _r: httpx.Response(200, text="not json"),
            lambda _r: httpx.Response(200, json={}),
            lambda _r: httpx.Response(500),
            lambda _r: httpx.Response(200, json={"host": "226"}),
        ]
        for handler in handlers:
            with pytest.raises(NvidiaSmiError):
                fetch(sampler(handler))
