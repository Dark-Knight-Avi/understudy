# M2 Runbook — coexistence

> **Goal:** someone claims a GPU, the platform is gone from it within seconds, and
> nobody using chat notices.
>
> **Prerequisites:** M1 complete. Each host's Docker daemon configured per
> [`05`](./05-host-setup.md) §5.3 — `insecure-registries`, `dns`, **and**
> `runtimes.nvidia`.
>
> **Status: the gate passes.** Measured on the real fleet, 2026-08-30, over a
> full claim -> yield -> release -> reclaim cycle with **zero failed requests**:
>
> | | |
> |---|---|
> | Claim -> card released | 22.6 GB free in ~12 s, flat for 2 min |
> | Wake from sleep | 1.36 s (weights + KV, vLLM's own figure) |
> | Release -> rung restored | ~5 min (the `clear_before_free` window, by design) |
> | Chat during all of it | 200 at every sample, from the standby on `.87` |
>
> The five minutes are deliberate: a host that snapped back the instant a job
> ended would thrash against anyone working in short bursts.

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

---

## Field incident — the fleet parked itself for 3½ days (2026-08-31 → 09-04)

Recorded here because every mechanism involved is this runbook's subject, and
because the controller behaved *correctly* throughout — the outage was blindness,
not misjudgement.

**Symptom.** Open WebUI returned `400 … no healthy deployments for model=chat`,
the LiteLLM catalog was empty, and nobody had reported it — the outage was found
by accident, starting the M1.5 preflight.

**Cause.** A Windows-side NVIDIA driver update (the fleet showed WSL UMD
`615.65.06` against KMD `616.56` afterwards) revoked GPU access from
**already-running containers**: inside any container created before the update,
`nvidia-smi` exits 255 with `Failed to initialize NVML: GPU access blocked by
the operating system`, while the WSL distro itself and **freshly created**
containers see the card normally. Processes that already held the device keep
it — which is why the sleeping standby kept its 1.25 GiB and looked alive.

The chain, reconstructed from logs:

1. `.87`'s fleet-agent (a pre-update container) went NVML-blind: `/gpu` → 503,
   while `/healthz` — which proves only that uvicorn is alive — stayed 200, so
   `docker compose ps` showed it **healthy**. `.226`'s agent was unreachable
   outright (that host's downtime window is not fully explained; its stack was
   observed freshly restarted during recovery).
2. Blind on both hosts, the controller did what the sharing guarantee demands:
   it withdrew every deployment and slept the `.87` standby
   (`POST /sleep` at 08-31 23:56). Empty catalog, chat down, **no alert** —
   the dashboard knew, but nothing tells a human.
3. For 3½ days the loop logged `sample failed` twice per second and nothing
   else, because nothing else was wrong.

**Recovery** — the part worth rereading, because the controller converged on its
own once it could see:

- `docker compose up -d --force-recreate fleet-agent` on `.87` → `/gpu` 200s.
- `--force-recreate vllm-small` (the old one held a slept engine from a blind
  era). The controller then slept the fresh engine while undecided
  (`rung=none reason=settled`), decided `rung=chat` for `.226` and
  `rung=chat-small` for `.87` (`reason=card_clear`), **woke both engines**, and
  re-registered the catalog without any manual `/model/new`. A gateway
  completion answered from the 14B minutes later.
- The `actuate change_rung … no agent base URL` ERRORs during convergence are
  the unbuilt ladder (see "the honest gap" above) being honestly logged, not a
  new defect.

**What this adds to the open list:**

| | |
|---|---|
| **Driver updates blind old containers** | After any Windows/NVIDIA update: `docker compose up -d --force-recreate` the agent and engines on that host. A container can be `Up (healthy)` and GPU-blind — `/healthz` does not sample the card |
| **A parked fleet is silent** | Chat was down 3½ days and no one was told. Alerting is scoped for M7; until then, treat "the model picker is empty" reports as this incident first |
| **`Up (unhealthy)` may mean "parked"** | vllm's compose healthcheck fails while an engine sleeps by design. Check `/is_sleeping` before treating it as broken |
| **Version skew** | `.226`'s agent ran `0.1.9` under a `0.1.10` controller for 4 days. Harmless this time; pin-bump both together |
| **`.226` downtime unexplained** | Why its agent was unreachable (stack down? since when?) was never established. Its boot task is documented but was never registered — see `ec781fe` |
