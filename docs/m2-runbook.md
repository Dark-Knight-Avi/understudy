# M2 Runbook — coexistence

> **Goal:** someone claims a GPU, the platform is gone from it within seconds, and
> nobody using chat notices.
>
> **Prerequisites:** M1 complete. Each host's Docker daemon configured per
> [`05`](./05-host-setup.md) §5.3 — `insecure-registries`, `dns`, **and**
> `runtimes.nvidia`.
>
> **Status: the gate passes.** Measured on the real fleet, 2026-08-30.

---

## What M2 actually delivers

| Capability | State |
|---|---|
| Yield a whole card on claim | **Working.** 22.6 GB free within ~12 s, held flat |
| Reclaim after release | **Working**, after the 5-minute clear window |
| Chat survives a claimed host | **Working**, from the standby model on `.87` |
| Routing follows the fleet | **Working.** Controller owns the LiteLLM catalog |
| Ladder *down* on one host | **Not built** — see below |

**The ladder is not implemented, and this is the honest gap.** `change_rung` calls
`/actuate/restart` and `/actuate/stop` on the per-host agent; the agent implements
`/gpu` and `/healthz` and nothing else. A rung change is a server restart with
different weights, and none of that machinery exists.

Continuity therefore comes from a *second host*, not from swapping models on the
contended one. That is a better trade than it looks: it needs no new capability,
and specifically does not give a service the power to restart processes on
somebody's workstation, which [`08`](./08-fleet-controller.md) §6.4 treats as a
line worth not crossing.

---

## The catalog is one group, not two

Both hosts advertise the **same public name**, so `chat` is one model group with
two deployments:

```
chat   chat-226        order 1     <- .226, Qwen3-14B
chat   chat-small-87   order 2     <- .87,  Qwen3-4B, standby
```

When `.226` is claimed the controller deletes `chat-226`; the group survives and
serves from `.87`. When it is released, `chat-226` returns and `order` puts it
back in front.

**LiteLLM `fallbacks` cannot do this.** A fallback acts within a group that still
exists, and a group whose only deployment was deleted answers `model not found`
before any fallback runs — measured, HTTP 400, `Available Model Group
Fallbacks=None`. This was the first design and it was wrong.

---

## Bring-up

Build and push the image (the controller and the agent are one image, two
entrypoints):

```bash
cd ~/understudy && make test
docker build -t localhost:5000/fleet-controller:<version> services/fleet-controller
docker push localhost:5000/fleet-controller:<version>
```

> `localhost:5000`, not the hub's LAN address: under WSL2 a host cannot reach its
> own. See [`05`](./05-host-setup.md).

Set the tag and start, on `.87`:

```bash
cd ~/understudy/deploy/host-87
unset FLEET_CONTROLLER_TAG          # see the warning below
sed -i 's|^FLEET_CONTROLLER_TAG=.*|FLEET_CONTROLLER_TAG=<version>|' .env
docker compose --env-file .env up -d --force-recreate fleet-agent fleet-controller
docker compose ps fleet-controller | grep -o "fleet-controller:[0-9.]*"
```

> **Always confirm the tag that is actually running.** Compose prefers an
> exported shell variable over `--env-file`, and `set -a && . ./.env` exports
> everything. Three deploys in a row silently ran an old image this way, and the
> fix under test looked like it had not worked.

Then the agents on each model host:

```bash
cd ~/understudy/deploy/host-226 && docker compose --env-file .env up -d fleet-agent
```

---

## Verification

```bash
curl -s http://localhost:8090/fleet/status | python3 -m json.tool
```

Every host must report a real `free_gb` and a `current_rung`. A host stuck on
`unknown - assuming in use` is not being sampled — check its agent is up and that
`address` in `fleet.local.yaml` is reachable *from the controller's container*.

**The gate itself.** Claim a host, watch three things at once:

```bash
curl -s -X POST -H 'content-type: application/json' -d '{"holder":"test"}' \
  http://localhost:8090/fleet/hosts/226/reserve
```

1. `nvidia-smi` on that host — free VRAM climbs to ~22.6 GB within ~12 s **and
   stays there**. A host that re-takes the card after ~60 s is the bug in
   [`03`](./03-gpu-sharing-policy.md) §4: "leftovers" only means something once
   the job exists.
2. `/fleet/status` — `state: yielding`, `current_rung: null`.
3. Chat keeps answering, from the standby.

Then release, and confirm the host returns to its rung after the clear window.

---

## What is still open

| | |
|---|---|
| `gpu-run` wrapper | Written, never exercised against a real job |
| Auto-preemption | Untested: no run has yet started a GPU job *without* claiming first |
| Growth handling | Untested: no job whose VRAM ramps over minutes |
| Flapping | Untested: no bursty workload |
| `.210` | Not managed. No stack on it at all |
| Don't-disturb check | **The one that matters most.** A full modelling run alongside the platform, per-iteration time within noise of its baseline |

The first four are [`00`](./00-goals-and-constraints.md)'s acceptance tests and
need a real modelling job, not a synthetic one. Until they run, M2 is proven for
the *cooperative* path — someone who flips the toggle — and unproven for the
person who does not.
