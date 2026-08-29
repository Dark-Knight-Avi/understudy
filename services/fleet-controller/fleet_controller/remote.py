"""Reading a GPU that is on another machine.

`LocalSampler` runs `nvidia-smi` in its own process, which is what the per-host
agent does. The controller lives on the hub and cannot see anyone else's card, so
it asks each host's agent over HTTP instead. This module is that client.

Everything here fails toward `UNKNOWN`. A host that does not answer is not a host
with a free GPU -- it is a host we cannot make promises about.
"""

from __future__ import annotations

import logging

import httpx
from pydantic import ValidationError

from fleet_controller.models import GpuSample, HostConfig
from fleet_controller.nvidia import NvidiaSmiError, NvidiaSmiUnavailable

_log = logging.getLogger(__name__)

DEFAULT_AGENT_PORT = 8099
"""See docs/ports.md. The one port a host exposes for the platform's own benefit
rather than for a user-facing service, which is why it is the one most often
missed in a firewall rule -- and a missed rule reads as UNKNOWN forever."""

AGENT_PATH = "/gpu"


class RemoteSampler:
    """Fetches samples from per-host agents.

    Timeouts are short by design. The control loop ticks every ~2 s, and a sampler
    that can block for ten seconds turns one unreachable host into a stalled fleet.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        port: int = DEFAULT_AGENT_PORT,
        timeout_s: float = 1.5,
    ) -> None:
        self._client = client
        self._port = port
        self._timeout = timeout_s

    def url_for(self, host: HostConfig) -> str:
        return f"http://{host.address}:{self._port}{AGENT_PATH}"

    async def __call__(self, host: HostConfig) -> GpuSample:
        url = self.url_for(host)
        try:
            response = await self._client.get(url, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise NvidiaSmiUnavailable(f"agent timed out: {url}") from exc
        except httpx.HTTPStatusError as exc:
            raise NvidiaSmiUnavailable(f"agent returned {exc.response.status_code}: {url}") from exc
        except httpx.HTTPError as exc:
            raise NvidiaSmiUnavailable(f"agent unreachable: {url} ({exc})") from exc
        except ValueError as exc:  # malformed JSON
            raise NvidiaSmiUnavailable(f"agent sent invalid JSON: {url}") from exc

        try:
            sample = GpuSample.model_validate(payload)
        except ValidationError as exc:
            # A response we cannot parse is not a reading. Refusing it sends the
            # host to UNKNOWN, which never promotes -- far better than inventing
            # a number the ladder would then act on.
            raise NvidiaSmiUnavailable(f"agent sent an unusable sample: {exc}") from exc

        if sample.host != host.name:
            # An agent answering for the wrong host means a misconfigured address,
            # and acting on it would apply one machine's ladder to another's card.
            raise NvidiaSmiUnavailable(
                f"agent at {url} reports host {sample.host!r}, expected {host.name!r}"
            )
        return sample


__all__ = ["AGENT_PATH", "DEFAULT_AGENT_PORT", "NvidiaSmiError", "RemoteSampler"]
