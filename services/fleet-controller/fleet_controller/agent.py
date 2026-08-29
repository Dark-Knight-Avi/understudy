"""The per-host agent: reports this machine's GPU, and nothing else.

Runs on every host in the fleet. Deliberately tiny and deliberately powerless --
it reads `nvidia-smi` and serves the result. It cannot load a model, cannot kill
a process, and has no authority over anything.

That powerlessness is the point. This process runs on machines other people own,
so the smaller its blast radius the easier it is to justify installing.

    uvicorn fleet_controller.agent:app --host 0.0.0.0 --port 8099
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Response

from fleet_controller.nvidia import LocalSampler, NvidiaSmiError, ProcessOwnership

_log = logging.getLogger(__name__)

HOST_NAME = os.environ.get("FLEET_HOST_NAME", "")
"""Must match the host's `name` in fleet.yaml.

The controller rejects a sample whose host disagrees with the one it asked, so a
misconfigured value here shows up as that host sitting in UNKNOWN rather than as
one machine's ladder being applied to another's card.
"""

OURS = ProcessOwnership(
    name_patterns=tuple(
        p.strip()
        for p in os.environ.get("FLEET_OUR_PROCESSES", "vllm,infinity,comfyui,llama").split(",")
        if p.strip()
    )
)

app = FastAPI(title="Understudy fleet agent", version="0.1.0")
_sampler = LocalSampler(host=HOST_NAME, ownership=OURS)


@app.get("/gpu")
def gpu() -> Response:
    """This host's current GPU reading.

    Returns 503 rather than a guess when `nvidia-smi` cannot be trusted. The
    controller turns that into UNKNOWN, which never promotes -- exactly what we
    want when we cannot see the card.
    """
    try:
        sample = _sampler.sample()
    except NvidiaSmiError as exc:
        _log.warning("sample failed: %s", exc)
        return Response(
            content=f'{{"error":"{type(exc).__name__}","detail":{exc!s:.200}!r}}',
            status_code=503,
            media_type="application/json",
        )
    return Response(content=sample.model_dump_json(), media_type="application/json")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness only. Never touches the GPU, so it cannot be made to hang by one."""
    return {"ok": True, "host": HOST_NAME}
