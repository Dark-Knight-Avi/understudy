# =============================================================================
#  fleet-controller -- ONE image, two entrypoints (docs/08)
# =============================================================================
#  The controller (.87) and the agent (.226, .210) are two FastAPI apps in one
#  package, and this image carries both. Compose picks which one runs:
#
#      controller:  uvicorn fleet_controller.api:app   --port 8090
#      agent:       uvicorn fleet_controller.agent:app --port 8099
#
#  Deliberately not two images. The pair speaks a private wire protocol
#  (remote.py calls what agent.py serves), and a version skew between them is
#  silent: the controller reads a field the agent stopped sending, decides the
#  card looks free, and promotes a rung on top of somebody's job. Building both
#  from one source tree makes that skew impossible to deploy rather than merely
#  unlikely.
#
#  nvidia-smi is NOT installed here. The NVIDIA container runtime injects the
#  binary and its libraries at container start, matched to the host driver --
#  which is what we want, since a driver-mismatched nvidia-smi baked into the
#  image would report nothing useful. It follows that the agent needs the
#  runtime configured on its host (docs/05 section 6); the controller does not.
# =============================================================================

FROM python:3.12-slim AS build

# uv resolves and installs considerably faster than pip, and the lockfile is
# already how this repo pins dependencies everywhere else.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

WORKDIR /src
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# One install, reading pyproject.toml. Splitting dependencies into a separate
# cached layer would mean listing them here as well, and a second copy of a
# dependency list is a copy that goes stale -- the build keeps succeeding
# against versions the project no longer declares. The dependency set is five
# packages; the saved rebuild time is not worth that.
COPY pyproject.toml ./
COPY fleet_controller/ ./fleet_controller/
RUN uv venv /venv && VIRTUAL_ENV=/venv uv pip install --no-cache .

# --- runtime -----------------------------------------------------------------
FROM python:3.12-slim

# Runs as nobody. The agent reads /host/proc and talks to nvidia-smi; neither
# needs privilege, and docs/08 section 1 is explicit that this service has no
# authority to kill anyone's process. An unprivileged user makes that structural
# rather than a promise.
RUN useradd --system --uid 10001 --no-create-home understudy

COPY --from=build /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER understudy

# Compose overrides this for the agent. The default is the controller because
# that is the one whose port and dashboard people go looking for.
EXPOSE 8090
CMD ["uvicorn", "fleet_controller.api:app", "--host", "0.0.0.0", "--port", "8090"]
