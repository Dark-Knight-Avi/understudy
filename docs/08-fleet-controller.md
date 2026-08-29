# 08 — Fleet Controller

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> The service that arbitrates GPU use across three workstations people are sitting at. One of the
> three things we build ourselves ([ADR-0002](./adr/0002-assemble-vs-build.md)). Its functional
> specification is [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) — this document is how
> that policy becomes a running service, not a second opinion about the policy.
>
> Sections 1–6 are **Concept** — the model of the thing. Sections 7–14 are **Build** — layout,
> config, API, wrapper, failure handling, and the tests that decide whether it worked.
>
> **Prerequisites:** M0 spikes 5 and 6. Spike 5 sets the ladder rungs and the settle delay; spike 6
> sets the toggle's wait. Building this against guessed numbers wastes the milestone.

---

## 1. What the controller is, and what it is not

A small Python + FastAPI service on `.87`. It polls every host's GPU, decides which model each host
should be running, makes that happen, and serves a page that tells people what is going on.

| It owns | It does not own |
|---|---|
| The measured state of every GPU in the fleet | Serving tokens — that is vLLM's job |
| The Free / Yielding / Sharing state machine | Which *tier* a user picked — that is LiteLLM's catalog |
| Rung selection from measured free VRAM | Killing anybody's process. It has no such authority |
| Driving vLLM sleep/wake and model restarts | Scheduling or queueing user work |
| Leases — who claimed which host, since when | Authentication. M2 is cooperative, not adversarial |
| Telling LiteLLM which backends are usable | Being load-bearing when it is dead — see §13 |

**Three non-goals, stated so nobody adds them later.** It is not a scheduler: no queue, no
fair-share, no priority beyond "the human wins." It is not a monitoring system: it keeps a few
minutes of samples for the dashboard and nothing more. And it is not adversarial — a colleague who
wants to defeat it can, trivially, and that is the correct amount of engineering for three people
sharing three machines ([`03`](./03-gpu-sharing-policy.md) §6).

**Size expectation.** [`tech-stack.md`](./tech-stack.md) §3 budgets this at roughly 400 lines of
FastAPI plus a page. That is about right for the controller. The per-host agent (§7) adds maybe 120
lines, and `gpu-run` is 40 lines of shell. If the controller is heading past a thousand lines,
something has been invented that the policy did not ask for.

---

## 2. The control loop

Everything the service does hangs off one loop, running per host, concurrently.

```
  every ~2 s, per host
  ------------------------------------------------------------------
   1. SAMPLE     GET agent /gpu  ->  total / used / free MB,
                                     compute-apps list (best effort),
                                     console-session flag
   2. ATTRIBUTE  foreign_used = used - platform_used - baseline
   3. CLASSIFY   lease held?  foreign process?  still settling?
                 -> FREE | YIELDING | SHARING | UNKNOWN
   4. DECIDE     target = largest rung where
                 footprint + headroom(state) <= free
                 ...filtered through hysteresis (4.4)
   5. ACTUATE    if target != current:   sleep / wake / restart
                 if routing != desired:  update LiteLLM
   6. PUBLISH    update in-memory state; push SSE event to dashboards
```

**Three cadences, deliberately different.** Sampling is fast (~2 s) because preemption latency is
what users feel. Deciding is continuous but almost always concludes "no change." Actuating is *rare*
— hysteresis (§4.4) exists precisely to keep step 5 idle, because step 5 is the expensive one.

One `asyncio` task per host with a shared `httpx.AsyncClient`; not a thread pool, not cron. Three
hosts at 2 s is 1.5 requests per second in total. This loop must never be the reason anything is
slow.

---

## 3. The three states — and the fourth you have to build

### 3.1 The specified three

| State | Entry condition | Headroom | Platform behaviour |
|---|---|---|---|
| **Free** | No lease, no foreign CUDA process | ~1 GB | Top rung the card can hold |
| **Yielding** | Lease taken, foreign process appears, or a new console login | — | Sleep to zero VRAM within seconds; pull out of LiteLLM routing |
| **Sharing** | ~60 s after entering Yielding, user's job still resident | ~3 GB | Largest rung that fits alongside them |

```
                lease taken / foreign proc / new console login
       +---------------------------------------------------------+
       |                                                          v
  +---------+                                               +-----------+
  |  FREE   |                                               | YIELDING  |
  | top rung|                                               |  0 VRAM   |
  +---------+                                               +-----------+
       ^                                                          |
       |  no lease AND card clear for ~5 min                      | ~60 s settle
       |                                                          v
       |                                                    +-----------+
       +----------------------------------------------------|  SHARING  |
          lease released AND foreign_used ~= 0               | rung <= N |
                                                             +-----------+
                                                               |     ^
                                    their usage grows -------> |     | their usage falls,
                                    drop a rung                |     | sustained ~60 s
                                                               v     |
                                                        (rung moves within SHARING)
```

### 3.2 Why the settle delay is 60 s and not zero

PyTorch's caching allocator does not return memory to the driver, and it grows through warm-up. A job
that reads 4 GB at launch is routinely 11 GB a minute later. Sizing a rung against the first reading
claims VRAM the user's job is about to ask for — and then *the platform* is the reason their run
died. That is the exact failure this whole design exists to avoid.

So the sequence on a claim is: **yield first, measure later.** Go to zero immediately, wait out the
settle window while the card belongs entirely to them, then measure and re-enter.

Set `settle_s` from M0 spike 5's "how fast it ramps to peak" column. 60 s is the spec's placeholder,
not a measurement.

### 3.3 Headroom is asymmetric on purpose

~1 GB when free, ~3 GB when sharing. The two errors are not equally bad:

| Getting it wrong | Cost |
|---|---|
| Too conservative | A smaller model answers. Chat is worse for half an hour |
| Too greedy | An eight-hour modelling run dies at hour six |

No symmetric number respects that. Encode the asymmetry as two config keys
(`headroom_free_mb`, `headroom_shared_mb`) so it stays visible and tunable rather than buried in an
expression.

### 3.4 The fourth state: Unknown

The spec has three states. A distributed system has four, because *a host you cannot reach is not the
same as a host that is free*. **Unknown** is entered after three consecutive failed samples (~6 s)
and behaves as the most pessimistic state available:

- Never promote. Not to any rung, ever, on stale data.
- Pull the host out of LiteLLM routing.
- Show it as `unknown - assuming in use`, never as free.
- Meanwhile the host's own agent runs the demotion half of the policy locally (§7.3).

This is the first appearance of the invariant that governs the rest of the document:
**demotion is local and always allowed; promotion is central and requires fresh evidence.**

---

## 4. The model ladder

### 4.1 It is data, not code

N9 says model choice is config, not source. The ladder is a per-host list, ordered best to worst,
loaded from YAML. Adding or reordering a rung must never be a code change.

```yaml
# fleet.yaml - values are the spec's; footprints are hypotheses until measured
poll_interval_s:        2
settle_s:              60
headroom_free_mb:    1024
headroom_shared_mb:  3072
rung_change_mb:      2048
sustain_s:             60
top_rung_clear_s:     300
lease_idle_warn_s:   1500
lease_idle_release_s: 1800
yield_deadline_s:      10

hosts:
  - id: "226"
    address: 10.0.0.226
    agent_url: http://10.0.0.226:9101
    vram_total_mb: 24564
    baseline_mb: 900            # desktop + compositor at idle. MEASURE. do not guess
    login_triggers_demotion: true
    backend: { kind: vllm, base_url: http://10.0.0.226:8000 }
    rungs:                      # ordered best -> worst
      - { id: coder-30b, model: Qwen3-Coder-30B-A3B-Int4, footprint_mb: 17408, litellm: [qwen3-coder, qwen3-chat] }
      - { id: chat-14b,  model: Qwen3-14B-Int4,           footprint_mb:  9216, litellm: [qwen3-chat] }
      - { id: chat-8b,   model: Qwen3-8B-Int4,            footprint_mb:  5632, litellm: [qwen3-chat] }
      - { id: chat-4b,   model: Qwen3-4B-Int4,            footprint_mb:  3072, litellm: [qwen3-chat] }
      - { id: "off",     model: null,                     footprint_mb:     0, litellm: [] }
```

**`footprint_mb` is weights plus KV cache plus CUDA context, not the model card's parameter count.**
Budget it with the method in [`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) §2, then
confirm it against `nvidia-smi` once the rung is actually running. A rung whose configured footprint
is smaller than its real one silently eats the headroom protecting the user's job — the one error
this service must not make.

Related: `--gpu-memory-utilization` is a *fraction*, and whether it is a fraction of total or of free
memory has varied across vLLM versions ([`02`](./02-hardware-and-fleet.md) §2). The controller
computes that fraction when it starts a rung, so pin the semantics for your version before trusting
any of this arithmetic.

### 4.2 Selection is a constraint, not a lookup table

```python
def choose_rung(host: Host, free_mb: int, state: State) -> Rung:
    headroom = (settings.headroom_free_mb if state is State.FREE
                else settings.headroom_shared_mb)
    for rung in host.rungs:                    # ordered best -> worst
        if rung.footprint_mb + headroom <= free_mb:
            return rung
    return host.rungs[-1]                      # the "off" rung always fits
```

Four lines, and they are the whole ladder. Note what is *not* there: no memory of what was loaded
before, no requested model name, no fixed plan. Measured free VRAM against a sorted list.

**This differs slightly from the spec's band tables, and the difference is deliberate.**
[`03`](./03-gpu-sharing-policy.md) §3 gives bands (`>= 20 GB -> 30B`, `12–20 GB -> 14B`,
`7–12 GB -> 8B`, `4–7 GB -> 4B`, `< 4 GB -> nothing`). At the *top* of each band the arithmetic
agrees; at the *bottom* it does not. With 7 GB free, the band says load the 8B — but
5.5 + 3 = 8.5 GB, so honouring the band leaves 1.5 GB of headroom, not 3 GB.

The constraint wins. `footprint + headroom <= free` is the rule the user's job depends on; the bands
are a readable summary of it. Effective thresholds for `.226` while sharing:

| Rung | Footprint | + 3 GB headroom | Loads at free VRAM of | Spec band said |
|---|---|---|---|---|
| Qwen3-Coder-30B-A3B Int4 | ~17 GB | ~20 GB | >= 20 GB | 20 GB — agrees |
| Qwen3-14B Int4 | ~9 GB | ~12 GB | >= 12 GB | 12 GB — agrees |
| Qwen3-8B Int4 | ~5.5 GB | ~8.5 GB | >= 8.5 GB | 7 GB — **1.5 GB stricter** |
| Qwen3-4B Int4 | ~3 GB | ~6 GB | >= 6 GB | 4 GB — **2 GB stricter** |
| nothing | 0 | — | below 6 GB | below 4 GB |

The lower rungs engage slightly later than the band table implies. That is the safe direction, it
costs a small model nobody would have enjoyed, and it keeps one rule in one place. **Surface the
effective thresholds on the dashboard** so the behaviour is inspectable rather than mysterious.

### 4.3 The other two hosts

**`.87` — RTX 4070, 12 GB.** Its rungs are compound, because embeddings are not optional.

| Rung | Contents | Footprint |
|---|---|---|
| `emb+chat` | Qwen3-Embedding-0.6B (~1.2 GB) + small chat (~5 GB) | ~6.2 GB |
| `emb` | Embeddings only | ~1.2 GB |
| `emb-cpu` | Embeddings on CPU | 0 GB VRAM |

The bottom rung is the point. Embeddings on CPU are slower, but ingestion keeps running and every RAG
query keeps working. There is no rung on `.87` that means "RAG is down", and there must never be one.
The same band-vs-constraint gap applies: with 3 GB headroom `emb+chat` needs ~9.2 GB free rather than
the spec's 8 GB, and `emb` needs ~4.3 GB rather than 2 GB — so the CPU fallback engages earlier than
the band table suggests. Again the safe direction.

`.87` also hosts the controller itself, plus Postgres, LiteLLM, Open WebUI, Caddy and the reranker.
**The controller must never sleep anything it depends on.** Only the two GPU models are
ladder-managed here; the CPU services are outside its authority entirely.

**`.149` — RTX 5080, 16 GB.** Different in kind, because ComfyUI is not vLLM.

| Rung | Model | Footprint |
|---|---|---|
| `flux` | FLUX.1-schnell FP8 | ~12 GB |
| `sd35` | SD3.5-medium / SDXL-Turbo | ~6 GB |
| `off` | Image generation unavailable | 0 GB |

Two differences worth building for. First, **ComfyUI has no sleep mode** — yielding is stopping the
process or its container, and re-entering is starting it, so `.149`'s demote/promote is seconds to
tens of seconds rather than the near-instant vLLM path. Second, ComfyUI's VRAM use is *transient*: it
peaks during a generation and falls between jobs, so continuous residency is the wrong mental model.
Enforce `.149`'s rung at **job admission** — the MCP `generate_image` tool asks the controller which
rung is live and gets back either a model to use or a clean "unavailable, host in use"
([`03`](./03-gpu-sharing-policy.md) §3) — rather than by policing residency every two seconds.
`--lowvram` / `--normalvram` give a third lever if you need one.

`.149` is also on a different subnet, so treat every sample from it as more likely to be late or
missing (§13).

### 4.4 Hysteresis — the rule that keeps step 5 idle

Three timers, all from [`03`](./03-gpu-sharing-policy.md) §4.6:

- **Change threshold** — ignore free-VRAM movements smaller than ~2 GB.
- **Sustain window** — a qualifying change must hold ~60 s before it can move a rung.
- **Top-rung wait** — after a card goes clear, wait ~5 minutes before returning to the top rung.

Implement with a rolling window (30 samples at 2 s = 60 s) and one asymmetric rule:

```python
# promote on the WORST reading in the window; demote on the LATEST
promote_basis = min(s.free_mb for s in window)   # optimism must be earned
demote_basis  = window[-1].free_mb               # pessimism is immediate
```

**There is a real tension here that the spec does not resolve, and you must.** §4.6 says change rung
only on a sustained change; §7 test 3 says a job whose VRAM ramps must make the platform drop a rung
*rather than letting it OOM*. Waiting 60 s to react to growth can be exactly the OOM. Resolve it with
an explicit bypass:

| Situation | Path | Latency |
|---|---|---|
| Free VRAM falls, headroom still intact | Hysteresis: > 2 GB, sustained 60 s | ~60 s |
| Free VRAM falls **below current footprint + headroom** | **Emergency demotion** — bypass all timers | next loop (~2 s) |
| Foreign process appears, or lease taken | Full yield — bypass all timers | next loop (~2 s) |
| Free VRAM rises | Hysteresis, plus the 5 min wait for the top rung | 60 s – 5 min |

So hysteresis governs *voluntary* rung changes in both directions. A breached headroom is not
voluntary. Put that sentence in a comment above the function, because it is exactly the rule someone
"simplifies" six months later.

---

## 5. Yield is not the same operation as re-rung

This distinction is not in the spec and it changes the design, so it gets its own section.

### 5.1 Two operations, two very different costs

| Operation | Mechanism | Cost | Triggered by |
|---|---|---|---|
| **Yield / resume** | vLLM sleep level 1 / wake — same model, weights parked in system RAM | seconds | Toggle, `gpu-run`, preemption |
| **Re-rung** | Stop the server, start it with a *different* model and memory fraction | tens of seconds, or a cold load | The ladder |

[`03`](./03-gpu-sharing-policy.md) §4.5 says "sleep, do not reload", and that is right for the
toggle. But the ladder loads a *different, smaller model*, and sleep/wake cannot change which model a
vLLM process holds or how much memory it reserved at startup. Going from the 30B to the 8B is a
process restart, not a wake.

This is the strongest argument for hysteresis, and the reason the loop's expensive step must stay
rare.

### 5.2 The optimisation `.226`'s 256 GB makes possible

Because sleep level 1 parks weights in **system RAM**, you can run several vLLM instances at once
with all but one asleep, and re-rung by waking a different process.

| Approach | Re-rung latency | Idle VRAM cost | Complexity |
|---|---|---|---|
| **A — one process, restart to re-rung** | Cold-ish load | None | Low. One unit to supervise |
| **B — one process per rung, all but one asleep** | Seconds | One resident CUDA context per sleeping process — **measure it** | Higher. N units, N ports, N health checks |

Weights for all four `.226` rungs total roughly 35 GB of system RAM, which is nothing against 256 GB.
B's real cost is not RAM, it is the resident CUDA context of each sleeping process, which comes
straight out of the headroom protecting the user's job.

**Recommendation: build A for M2.** Hysteresis makes re-runging rare, and A has one failure mode
instead of N. Measure per-process context overhead during spike 6; if re-rung latency turns out to
hurt in practice, move to B with **two** instances only — top rung plus one fallback rung — which
captures most of the benefit for a fraction of the idle cost.

### 5.3 What this means for acceptance test 1

Test 1 requires that chat "keeps answering from a lower rung throughout" the toggle test. Trace the
actual sequence:

```
  t+0s      toggle flipped     -> .226 vLLM sleep(1) begins
  t+~8s     VRAM free          -> dashboard says READY; user launches their job
  t+8..68s  settle window      -> .226 is deliberately empty. NOTHING runs there
  t+~70s    measure, pick rung -> start it
  t+~90s    lower rung live
```

For roughly 80 seconds `.226` answers nothing. The test passes because **LiteLLM fails over to
`.87`'s small chat model**, not because `.226` degraded gracefully. That is a fleet-level property
and a hard dependency: if `.87` has no chat rung live at that moment, test 1 fails and it will look
like a controller bug when it is a routing gap. Configure the fallback explicitly in
[`06-model-gateway.md`](./06-model-gateway.md) and verify it *before* running the test.

---

## 6. Actuation — what the controller can actually do

### 6.1 vLLM sleep and wake

vLLM's sleep endpoints are development endpoints and have moved between releases: the enabling
flag/env (`VLLM_SERVER_DEV_MODE`), the paths (`/sleep`, `/wake_up`), and the level semantics have all
changed. **Verify every one of them against your version** and record what you find in
[`07-inference-servers.md`](./07-inference-servers.md); M0 spike 6 exists to establish exactly this.

```bash
# shape only - confirm paths, payloads and level semantics for YOUR version
curl -sS -X POST http://10.0.0.226:8000/sleep -d '{"level": 1}'
curl -sS -X POST http://10.0.0.226:8000/wake_up
```

Level 1 offloads weights to CPU RAM (fast resume, costs system RAM); level 2 discards them (slower
resume, no RAM cost). With 256 GB on `.226`, level 1 is the whole point.

Wrap every call in a **deadline** (default 10 s, from spike 6) and verify the outcome with
`nvidia-smi`, not with the HTTP 200. The invariant is "VRAM is free", not "the API said OK". If VRAM
is not free by the deadline, escalate — stop the container — and put the real measured number in the
UI rather than a hopeful one.

### 6.2 LiteLLM routing

Two mechanisms, and you need both.

**Pull (correctness).** A sleeping or absent vLLM must make LiteLLM route elsewhere *without the
controller doing anything*. Configure fallbacks, `num_retries`, `allowed_fails` and `cooldown_time`
in the gateway config so a 503 from `.226` fails over to `.87` on its own. This is what keeps chat up
when the controller is dead.

**Push (speed).** The controller also announces a state change as it happens, so the first request
after a yield does not have to fail in order to discover it. LiteLLM's model-management surface
(`/model/new`, `/model/delete`, config reload) varies by version and by whether you run it
database-backed — **verify against your version**, and prefer whichever form is idempotent, because
the controller will replay it on every reconcile.

Push is an optimisation over a correct pull. Never build the reverse: a design where routing is
correct only while the controller is alive fails §13.

### 6.3 The embeddings CPU fallback on `.87`

The `emb-cpu` rung is a restart of the embeddings server with the device flipped to CPU. Two things
matter. The RAG service must not see an outage across the switch, so drain then swap. And the model
revision and dimension must be **identical** on both paths — a CPU fallback that silently produces
vectors from a different model poisons the index. Pin both in config and cross-reference the
embedding-model trap in [`delivery-plan.md`](./delivery-plan.md) §9.

### 6.4 What it may never do

The controller stops and starts **platform** processes only. It never kills, renices or signals a
user's process, and it exposes no remote-exec path. The agent in §7 offers a fixed verb list for
exactly this reason: the moment it can run arbitrary commands on somebody's workstation, this stops
being a tool people will accept on their machine.

---

## 7. Cross-host access — how it reaches `nvidia-smi`

The controller runs on `.87`. It needs GPU truth from `.226` and from `.149`, which is on another
subnet.

### 7.1 The options

| Option | How | Pros | Cons |
|---|---|---|---|
| **Tiny agent per host** ✅ | ~120-line FastAPI on each host, `GET /gpu` | Can also **act** (sleep, restart, stop) — one channel for read and write; can run on the **Windows** side where the process list is actually complete; one auth model; one TCP port to open across the subnet; trivially testable with `curl` | A third thing to deploy and keep alive on each host; needs its own boot story |
| SSH from the controller | `ssh host nvidia-smi ...` every 2 s | No new service to write | A connection every 2 s is heavy and fragile; needs keys and accounts on machines that belong to other people — credential sprawl on exactly the boxes where we promised none; Windows OpenSSH quirks; slow across the subnet |
| DCGM / `nvidia_gpu_exporter` + Prometheus | Scrape metrics | Standard, and history/graphs come free | Read-only — you still need an actuation path, so it is an *addition*, not a replacement. DCGM support on consumer GeForce cards is limited. Extra Prometheus to run |
| Remote NVML | — | — | NVML is a local library. There is no remote mode |
| WMI / perf counters on Windows | — | No extra dependency | Does not give per-process CUDA memory reliably |

**Recommendation: the agent.** The decider is not polling — it is that the controller must *act*, and
every other option gives you telemetry and leaves the actuation channel unsolved. A second reason
follows.

### 7.2 The WSL2 process-visibility problem

`.226` and `.87` run WSL2. **Whether `nvidia-smi` inside WSL2 enumerates Windows-side CUDA processes,
and whether `nvidia-smi.exe` on Windows enumerates WSL2 ones with usable names, is driver- and
version-dependent.** Do not design around an assumption here — establish it during M0 spike 5, which
is already logging `--query-compute-apps` against a real modelling run.

Then design so the answer barely matters:

```bash
# Aggregate - reliable everywhere
nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu \
           --format=csv,noheader,nounits

# Per-process - useful for the dashboard, NOT load-bearing
nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory \
           --format=csv,noheader,nounits
```

**Derive foreign usage by subtraction, not by enumeration:**

```
foreign_used = memory.used - platform_used - baseline_mb
```

`platform_used` is known because the agent started the platform's own processes and can read what
they hold. `baseline_mb` is the idle desktop and compositor — measure it once per host with a normal
session open and put it in config. The process list is then a nicety for the dashboard ("`python.exe`,
11 GB, 22 min") rather than the signal the policy depends on. Subtraction is driver-agnostic and
survives the WSL2 question entirely.

**Where to run the agent.** If spike 5 shows the WSL2-side process list is blind to Windows
processes, run the agent **natively on Windows** for `.226` and `.87` (Python + `nvidia-smi.exe`) and
have it reach into WSL only to control the platform's containers. On `.149`, native Ubuntu, there is
no such question. Keep one agent codebase with a per-platform shim; do not fork it.

### 7.3 The agent's second job: autonomous demotion

The agent is not only a sensor. It runs a local watchdog:

- If it has not been polled by the controller for ~15 s, it enters **autonomous mode**.
- In autonomous mode it will still **demote** — foreign process appears, sleep the local server — but
  it will **never promote**. Promotion needs the controller.
- It keeps its last known lease state, so a controller restart does not resurrect a claimed host.

This is what makes "the controller dies" a safe event rather than a squatting event (§13), and it is
the reason the agent is worth its keep over an exporter.

### 7.4 Wiring

| | Value |
|---|---|
| Port | `9101/tcp`, bound to the LAN interface, never `0.0.0.0` on a public NIC |
| Auth | `Authorization: Bearer $FLEET_AGENT_TOKEN`, shared secret from the host `.env` |
| Firewall | Allow `9101` from `10.0.0.87` only. `.149` needs a cross-subnet rule — see M0 spike 4 |
| Secrets | Token lives in the gitignored `.env`, generated per host. Never in the repo ([`delivery-plan.md`](./delivery-plan.md) §8) |
| Restart | `restart: unless-stopped` in Compose (`.149`), Windows service or Task Scheduler on `.226`/`.87` — it must return after a reboot without a human (N8) |

Run the agent on `.87` too, even though the controller could read that GPU locally. One code path is
worth more than one saved process, and it means `.87` is not a special case in any test.

---

## 8. Service layout and configuration

```
services/fleet-controller/
  app/
    main.py            # FastAPI app, lifespan starts the loops
    config.py          # pydantic-settings; loads fleet.yaml + env
    models.py          # pydantic: Sample, HostState, Lease, Rung
    poller.py          # one asyncio task per host; the loop in SS 2
    ladder.py          # choose_rung(), hysteresis window
    states.py          # the state machine, transitions, settle timer
    leases.py          # reserve/release, TTL, idle detection, persistence
    actuators/
      vllm.py          # sleep / wake / restart-with-model
      comfy.py         # stop / start / admission answer for .149
      litellm.py       # push routing updates; reconcile every loop
    api.py             # the HTTP surface in SS 9
    events.py          # SSE fan-out to dashboards
    static/index.html  # the dashboard. no build step
  agent/
    main.py            # the per-host agent from SS 7
  tests/
    test_ladder.py     # pure functions - the part worth unit-testing
    test_states.py
  pyproject.toml       # uv, everything pinned
  Dockerfile
```

`ladder.py` and `states.py` should be **pure functions over a sample list**. That is what makes the
ladder testable without a GPU, and rung selection is the logic you least want to debug live on
somebody's workstation.

### 8.1 The tunables, and where each number comes from

| Setting | Default | Source |
|---|---|---|
| `poll_interval_s` | 2 | [`03`](./03-gpu-sharing-policy.md) §4.4 |
| `settle_s` | 60 | [`03`](./03-gpu-sharing-policy.md) §2 — **re-set from spike 5** |
| `headroom_free_mb` | 1024 | [`03`](./03-gpu-sharing-policy.md) §2 |
| `headroom_shared_mb` | 3072 | [`03`](./03-gpu-sharing-policy.md) §2 |
| `rung_change_mb` | 2048 | [`03`](./03-gpu-sharing-policy.md) §4.6 |
| `sustain_s` | 60 | [`03`](./03-gpu-sharing-policy.md) §4.6 |
| `top_rung_clear_s` | 300 | [`03`](./03-gpu-sharing-policy.md) §4.6 |
| `lease_idle_warn_s` | 1500 (25 min) | Derived — a warning before the 30 min release |
| `lease_idle_release_s` | 1800 (30 min) | [`03`](./03-gpu-sharing-policy.md) §4.2 |
| `yield_deadline_s` | 10 | [`03`](./03-gpu-sharing-policy.md) §7 test 1 — **re-set from spike 6** |
| `unreachable_after_polls` | 3 (~6 s) | This document, §3.4 |
| `agent_autonomy_after_s` | 15 | This document, §7.3 |
| `foreign_threshold_mb` | 300 | This document, §7.2 — above `baseline_mb` |

Every one of these is config. None of them are literals in a function.

### 8.2 Persistence

Leases go in Postgres, which is already on `.87` — one table, not a new datastore.

```sql
CREATE TABLE gpu_lease (
  id           uuid PRIMARY KEY,
  host_id      text        NOT NULL,
  holder       text        NOT NULL,          -- name, or user@host from gpu-run
  source       text        NOT NULL,          -- 'toggle' | 'gpu-run'
  token_hash   text        NOT NULL,          -- release requires the token, or a logged force
  acquired_at  timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  released_at  timestamptz,
  release_kind text                           -- 'explicit' | 'idle' | 'expiry' | 'force'
);
CREATE UNIQUE INDEX ON gpu_lease (host_id) WHERE released_at IS NULL;
```

The partial unique index is doing real work: one live lease per host, enforced by the database rather
than by a race in Python.

**On restart, do not trust the table — reconcile against measurement.** Reload live leases, then take
a fresh sample of every host. If a lease exists but the card is clear, keep the lease (a person may
be about to launch) and let the idle timer handle it. If no lease exists but the card is busy, that is
Sharing, not Free. Measurement outranks memory, always.

---

## 9. The HTTP API

Small, boring, and the same surface for the toggle and for `gpu-run` — [`03`](./03-gpu-sharing-policy.md)
§4.3 requires that the wrapper calls the same API, which is also why there is only one code path to
get wrong.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/fleet/status` | Everything the dashboard needs, in one document |
| `GET` | `/fleet/hosts/{id}` | One host — what `gpu-run` polls |
| `POST` | `/fleet/hosts/{id}/reserve` | Claim a host. Returns immediately with a lease; yielding proceeds |
| `POST` | `/fleet/hosts/{id}/release` | Release a lease |
| `GET` | `/fleet/events` | SSE stream of state changes for the dashboard |
| `GET` | `/fleet/hosts/{id}/history?minutes=30` | Recent samples, for the VRAM sparkline |
| `GET` | `/healthz` | Liveness. No dependencies, never blocks |
| `GET` | `/readyz` | Readiness — at least one host sampled within 3 poll intervals |

### 9.1 `GET /fleet/status`

```json
{
  "generated_at": "2026-08-29T11:04:12Z",
  "controller": { "uptime_s": 84213, "poll_interval_s": 2, "singleton": true, "degraded": false },
  "hosts": [
    {
      "id": "226",
      "address": "10.0.0.226",
      "reachable": true,
      "state": "sharing",
      "state_since": "2026-08-29T10:41:55Z",
      "gpu": {
        "name": "NVIDIA GeForce RTX 4090",
        "vram_total_mb": 24564,
        "vram_used_mb": 17820,
        "vram_free_mb": 6744,
        "foreign_used_mb": 11024,
        "platform_used_mb": 5896,
        "utilisation_pct": 71,
        "sampled_at": "2026-08-29T11:04:11Z",
        "sample_age_s": 1.2
      },
      "foreign_processes": [
        { "pid": 41233, "name": "python.exe", "used_mb": 11024, "first_seen": "2026-08-29T10:41:02Z" }
      ],
      "settle_until": null,
      "lease": {
        "id": "3f6c...",
        "holder": "aritra",
        "source": "toggle",
        "held_for_s": 1330,
        "expires_at": "2026-08-29T14:41:02Z",
        "idle_warning": false
      },
      "rung": {
        "id": "chat-8b",
        "model": "Qwen3-8B-Int4",
        "footprint_mb": 5632,
        "headroom_mb": 3072,
        "since": "2026-08-29T10:43:10Z"
      },
      "target_rung": "chat-8b",
      "actuation": { "status": "ready", "last_action": "start", "last_action_ms": 21400, "error": null },
      "routing": { "litellm_enabled": true, "models": ["qwen3-chat"] },
      "next_promotion_eligible_at": null
    }
  ]
}
```

Every duration is computed from the **controller's** receipt time, never from a timestamp the agent
sent, so clock skew between three machines cannot produce a lease that has been held for minus four
minutes.

### 9.2 `POST /fleet/hosts/226/reserve`

```json
// request
{ "holder": "aritra", "source": "toggle", "reason": "training run", "ttl_s": 14400 }

// 202 Accepted
{
  "lease_id": "3f6c...",
  "token": "opaque-secret-return-this-to-release",
  "host": "226",
  "status": "releasing",
  "eta_s": 8,
  "poll": "/fleet/hosts/226"
}
```

**202 and poll, not 200 and block.** Reserving is not instantaneous — VRAM has to actually drain —
and a client that holds a socket open for the duration is a client that reports a timeout as a
failure. The caller polls `GET /fleet/hosts/{id}` until `actuation.status == "ready"`; `gpu-run` does
exactly that (§11). Add `?wait_s=N` as a long-poll convenience later if the polling annoys anyone.

`status` moves `releasing -> ready`. Those two words are the contract with the user
([`03`](./03-gpu-sharing-policy.md) §4.1) and the same strings appear in the UI and in the wrapper's
output. Do not let them drift into three different vocabularies.

**Idempotency.** Reserving a host that the same holder already holds returns the existing lease with
`200`, not an error. A double-clicked toggle and a re-run script must both be harmless.

### 9.3 `POST /fleet/hosts/226/release`

```json
// request
{ "lease_id": "3f6c...", "token": "opaque-secret-..." }
// or, from the dashboard, when someone releases a colleague's forgotten toggle:
{ "lease_id": "3f6c...", "force": true, "actor": "priya" }

// 200
{
  "host": "226",
  "status": "released",
  "state": "free",
  "top_rung_eligible_in_s": 300
}
```

`force` exists because a forgotten toggle at 6 p.m. must not require the person who set it. It is
logged with the actor's name, which is the whole enforcement mechanism a cooperative system needs.
`top_rung_eligible_in_s` reflects the 5-minute clear-card wait — expose it, so nobody files a bug
about the good model not coming straight back.

### 9.4 Errors and auth

Plain problem shapes: `404` unknown host, `409` held by someone else without `force`, `503` host
Unknown (reserve is refused because the controller cannot verify a yield), `502` actuation failed.

Auth is a shared bearer token in `.env` for the write endpoints; `GET` is open on the LAN because the
dashboard has to be frictionless. When SSO lands at M8, `holder` comes from the session instead of
from the request body — design the field so that swap is a one-line change.

---

## 10. The dashboard

### 10.1 What it looks like

```
   GPU fleet                                          updated 1s ago  [ dashboard ]

   .226  RTX 4090   #########.....  17.8 / 24.0 GB   Qwen3-8B  (rung 3 of 4)
                    I'm using this  (--*)   YOURS - 22 min - AI on leftovers
                    foreign: python.exe 11.0 GB          next rung up at 12.0 GB free

   .87   RTX 4070   ##............   1.2 / 12.0 GB   embeddings only  (rung 2 of 3)
                    I'm using this  ( o--)  ready

   .149  RTX 5080   ########......  12.0 / 16.0 GB   FLUX.1-schnell   (rung 1 of 3)
                    I'm using this  ( o--)  releasing... 6s
```

### 10.2 The status line is the product

Nobody should have to guess whether the VRAM is actually free, and nobody should have to learn a
command ([`03`](./03-gpu-sharing-policy.md) §4.1). The status line has exactly these states and no
others:

| Status | Meaning | Shown when |
|---|---|---|
| `ready` | The card is yours. VRAM is measurably free | Yield confirmed by `nvidia-smi`, not by an HTTP 200 |
| `releasing... Ns` | We are draining. Do not launch yet | Between reserve and confirmed free |
| `YOURS - N min - AI on leftovers` | You hold it; the platform is on a lower rung | Lease held, Sharing |
| `settling... Ns` | Measuring your job before re-entering | Inside the settle window |
| `in use by <name> - N min` | Somebody else holds it | Lease held by another |
| `auto-release in N min` | Held, but no CUDA process seen | After `lease_idle_warn_s` |
| `unknown - assuming in use` | We cannot see this host | Unknown state (§3.4) |
| `yield failed - see logs` | Deadline blown, escalation ran | Actuation error |

Two rules about it. It must never say `ready` on the strength of an API response alone — only a
measurement promotes it. And it must never say `released` where it means `ready`; "released" is
something the platform did, "ready" is a statement about the user's card, and only the second one is
what they asked.

### 10.3 Build it boring

One `index.html` served by FastAPI. `EventSource('/fleet/events')` for updates with a `fetch`
of `/fleet/status` every 5 s as a fallback, so a dropped SSE connection degrades to a slower page and
not a frozen one. No npm, no build step, no framework — this must still be editable at 3 a.m. by
whoever is on call, and [`delivery-plan.md`](./delivery-plan.md) §4 deliberately has no CI to build
assets with.

Mirror it inside Open WebUI as an iframe so it sits where people already are, and bookmark it on each
machine's desktop. A dashboard nobody opens does none of the work this design assigns to it.

Also show, per host: **current model and which rung that is** (`rung 3 of 4`), the free-VRAM
threshold for the next rung up, and a 30-minute VRAM sparkline from
`/fleet/hosts/{id}/history`. [`03`](./03-gpu-sharing-policy.md) §5 is explicit that an unexplained
quality drop erodes trust faster than an explained one — the rung indicator is what turns "chat got
worse" into "someone is using the 4090."

### 10.4 Who holds the toggle

There is no SSO until M8, so identity is cooperative:

1. The browser asks for a name once and keeps it in `localStorage`; it is sent as `holder`.
2. The agent reports the host's console user, shown alongside as a cross-check.
3. `gpu-run` sends `$USER@$(hostname)` automatically.

Do not build accounts for this. Three people who can see each other's names on a shared page is
sufficient social pressure, and it is exactly the amount this problem deserves.

### 10.5 Auto-release

A toggle held with **no CUDA process for ~30 minutes** releases itself, after a visible warning
([`03`](./03-gpu-sharing-policy.md) §4.2). Forgetting is the obvious failure mode, so the system
handles it rather than relying on discipline.

- At 25 min with `foreign_used_mb` under threshold: the row turns amber, `auto-release in 5 min`.
- At 30 min: release, log `release_kind='idle'`, and keep the row visibly marked for a few minutes so
  the person sees what happened rather than discovering it by OOM.
- The timer resets the instant any CUDA process appears. Someone who claims the card and then spends
  half an hour writing the script must not be punished for thinking first — but note that the 30
  minutes starts at *claim*, so on a slow day they may need one re-toggle. That is the right side to
  err on.

---

## 11. The `gpu-run` preflight wrapper

```
gpu-run python my_model.py
```

Same API as the toggle, blocks until release completes, execs the real command, releases on exit. For
batch and scheduled work where nobody is sitting there to flip a switch. Ship it as a shell alias so
it costs nothing to adopt.

```bash
#!/usr/bin/env bash
# scripts/gpu-run - claim this host's GPU, run a command, release it.
set -uo pipefail

: "${FLEET_URL:=http://10.0.0.87:8088}"
: "${FLEET_HOST_ID:=226}"
: "${GPU_RUN_TIMEOUT:=120}"

api="$FLEET_URL/fleet/hosts/$FLEET_HOST_ID"
holder="${USER}@$(hostname -s)"

claim=$(curl -sS -m 10 -X POST "$api/reserve" \
          -H 'content-type: application/json' \
          -d "{\"holder\":\"$holder\",\"source\":\"gpu-run\",\"ttl_s\":86400}") || claim=""

if [ -z "$claim" ]; then
  echo "gpu-run: fleet controller unreachable - running anyway, unprotected" >&2
  exec "$@"                                  # see 'the important decision' below
fi

lease=$(echo "$claim" | jq -r .lease_id)
token=$(echo "$claim" | jq -r .token)

release() {
  curl -sS -m 10 -X POST "$api/release" \
    -H 'content-type: application/json' \
    -d "{\"lease_id\":\"$lease\",\"token\":\"$token\"}" >/dev/null || true
}
trap release EXIT INT TERM

printf 'gpu-run: releasing GPU on %s' "$FLEET_HOST_ID"
deadline=$(( SECONDS + GPU_RUN_TIMEOUT ))
until [ "$(curl -sS -m 5 "$api" | jq -r .actuation.status)" = "ready" ]; do
  [ "$SECONDS" -ge "$deadline" ] && { echo " - TIMEOUT, aborting" >&2; exit 75; }
  printf '.'; sleep 1
done
echo " ready"

"$@"                                          # trap releases on any exit path
exit $?
```

Four properties that matter more than the code:

- **The trap is the point.** Release must happen on success, failure, `Ctrl-C` and `SIGTERM`. A
  wrapper that leaks leases is worse than no wrapper, because the auto-release timer then becomes the
  primary mechanism rather than the backstop.
- **The exit code passes through.** This runs inside other people's scripts and cron jobs; swallowing
  a non-zero exit turns a failed run into a silent one.
- **The wait is bounded**, at roughly ten times the measured yield time from spike 6. `75`
  (`EX_TEMPFAIL`) is a distinguishable exit code for "never got the GPU".
- **The important decision: if the controller is unreachable, warn and run anyway.** The alternative —
  refuse to launch — means one crashed FastAPI service blocks somebody's overnight batch job. That is
  the platform getting in the way, which is the precise thing this whole document exists to prevent.
  The platform's protection degrades to plain preemption (§12); the person's work does not degrade at
  all.

Install to `/usr/local/bin/gpu-run` with `FLEET_HOST_ID` set per host in `/etc/environment`, so
nobody has to remember which box they are on.

---

## 12. Automatic preemption, and its limit

For anyone who uses neither the toggle nor the wrapper. The loop already samples every ~2 s; a
foreign CUDA process above `foreign_threshold_mb`, or a new console login where
`login_triggers_demotion` is set, triggers immediate demotion and a routing update — no timers, no
hysteresis.

**Be clear about the limit: it cannot prevent an OOM already in flight.** A job that allocates its
full working set in the first second may still fail once, before the controller has reacted.
Preemption recovers the situation within seconds; only the toggle prevents it. Say this in the
dashboard footer and in whatever note goes round when the platform is introduced, because the honest
version — "flip the switch and you are safe; do not flip it and you may lose one launch" — is a
proposition people can act on. A vague promise of automatic protection is not, and it is the version
that gets the platform uninstalled the first time it is wrong.

**One caveat on the console-login trigger.** [`03`](./03-gpu-sharing-policy.md) §4.4 lists "an
interactive login" alongside a foreign CUDA process. On `.226` and `.149` that is right. On `.87` it
is not: `.87` is the hub, it hosts this controller and everything else, and its console session may
be logged in permanently — so a literal "a session exists" test would pin `.87` to its bottom rung
forever. Implement the trigger as **a new session since the last poll**, and make it per-host config
(`login_triggers_demotion`), defaulting on for `.226` and `.149` and off for `.87`.

---

## 13. Failure modes

The governing rule, and the one to test hardest: **the controller must fail safe. A failure must
never leave a host squatting VRAM that a person is waiting for.** Everything below is an application
of that.

| Failure | Detection | Behaviour | Invariant preserved |
|---|---|---|---|
| **Controller process dies** | Agent sees no poll for `agent_autonomy_after_s` | Agent goes autonomous: keeps demoting locally, refuses to promote. LiteLLM fails over on 503s (§6.2) | Never squats; chat survives |
| **Controller host reboots** | — | Leases reloaded from Postgres, then **reconciled against a fresh sample** (§8.2). Anything unexplained is treated as claimed | Measurement outranks memory |
| **Host agent unreachable** | 3 failed samples (~6 s) | Host -> Unknown. Pulled from routing. No promotion. Dashboard says `assuming in use` | Never promote blind |
| **`nvidia-smi` parse failure** | Non-numeric or short CSV row | Discard the sample; 3 consecutive -> Unknown. Never coerce a bad parse to `0` used | A bad parse cannot look like a free card |
| **`nvidia-smi` hangs** | Subprocess timeout in the agent (2 s) | Sample skipped, counted as a failure | The agent's loop cannot wedge |
| **Split brain — two controllers** | Postgres advisory lock (`pg_try_advisory_lock`) | Loser serves `/fleet/status` read-only and actuates nothing; `controller.singleton: false` on its page | Exactly one writer |
| **Split brain — human vs. detection** | Lease held *and* no foreign process, or vice versa | Union, never intersection: the host stays yielded while **either** signal holds | The permissive reading never wins |
| **Stale lease after a crash** | TTL plus the idle-release path | Same 30-minute mechanism as a forgotten toggle | Leases cannot be immortal |
| **LiteLLM out of sync** | Reconcile desired vs. actual routing every loop, not only on change | Push is repaired by the next loop; pull (fallbacks) covers the gap meanwhile | Routing is correct without the controller |
| **vLLM wake fails** | Wake errors, or health check never passes | Stay released, keep the host out of routing, retry with backoff, raise it on the dashboard | Never route to a dead backend |
| **Yield deadline blown** | VRAM not free after `yield_deadline_s` | Escalate to stopping the container; if that fails, say `yield failed` in the UI — loudly and honestly | The user is told the truth |
| **`.149` network partition** | Same as unreachable, but expected more often | Image generation reports "unavailable, host in use"; the rest of the fleet is unaffected | Cross-subnet flakiness is contained |
| **Clock skew across hosts** | — | All durations computed from the controller's receipt time | No negative lease ages |
| **Postgres unavailable** | Connection error | Serve from in-memory state, keep arbitrating, queue lease writes; degrade `/readyz`, not the loop | GPU arbitration does not depend on the database |

Two of these deserve a sentence more.

**Why an agent that goes autonomous is not itself a split brain.** The agent may only move in one
direction — down. Two components that can both demote can disagree about *when* but never produce an
over-allocation, so the disagreement is always resolved in the user's favour. Promotion has exactly
one writer, and that writer holds a database lock.

**Why the controller does not restart the platform on the way out.** A clean shutdown should sleep
every managed backend before exiting, not wake them. If the controller is going away, the state that
needs no supervision is the one holding no VRAM.

---

## 14. Acceptance tests

These are [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §7, restated as what to verify and
how. They are the M2 exit criteria in [`delivery-plan.md`](./delivery-plan.md) §6, and per §12 of that
plan they should be run by someone who did not build the service.

Run them with `nvidia-smi --query-gpu=memory.used --format=csv -l 2 | tee test-N.csv` going on the
host under test, so every claim below has a trace behind it rather than a recollection.

| # | Test | Verify |
|---|---|---|
| 1 | **The toggle — headline test.** 30B loaded on `.226`; flip *I'm using this GPU* | UI reaches `ready` within ~10 s; `nvidia-smi` shows the card essentially empty; a job needing **> 10 GB** starts with no OOM; **chat keeps answering throughout** — which requires `.87` fallback, see §5.3 |
| 2 | **Ladder correctness.** Jobs of ~6, ~12 and ~20 GB in turn | The right rung settles each time; **>= 3 GB headroom** measured, not assumed; the user's job is never squeezed. Check the rung shown in the UI matches the model LiteLLM actually routes to |
| 3 | **Growth handling.** A job whose VRAM ramps over several minutes | The platform drops a rung rather than letting it OOM. This is the emergency-demotion path (§4.4), so confirm the drop happens in seconds, not after the 60 s sustain window |
| 4 | **Scripted path.** `gpu-run` a job with no toggle | Same outcome, no manual step. Also check the lease is released on `Ctrl-C` and on a non-zero exit, and that the exit code passes through |
| 5 | **Neither used.** Launch a job directly | Auto-preemption demotes within seconds. The job may need one retry — **record whether it did**, because that number is what you tell people about §12's limit |
| 6 | **Both release paths.** Toggle off; and separately, a toggle left on with no CUDA process | Top rung returns after the ~5 min hysteresis window; the idle toggle warns and then auto-releases at ~30 min |
| 7 | **No flapping.** A job with bursty VRAM use | No repeated model reloads. Count actuations in the log — the pass criterion is a number, not an impression |
| 8 | **Don't-disturb (N6).** A full modelling run alongside the platform under load | Per-iteration time within noise of its ~48 min baseline. This is the test that decides whether the platform stays installed |

Add two that are not in the spec but that §13 makes necessary:

| # | Test | Verify |
|---|---|---|
| 9 | **Controller killed mid-lease.** `docker stop` the controller while `.226` is claimed | The card stays free; the agent keeps demoting; chat still answers via LiteLLM fallback; restarting the controller reconciles rather than resurrects |
| 10 | **Host agent killed.** Kill the agent on `.226` | Host goes Unknown within ~6 s, is pulled from routing, and is **never** promoted while unreachable |

---

## Reflect

The engineering here is modest — polling, a state machine, a switch — and the temptation is to treat
it as plumbing. It is not. This is the milestone that decides whether the platform is welcome on
machines that belong to other people, which is why it lands at M2, before anyone depends on it.

Three things surprised the design as it was written down, and they are the parts to watch when it is
actually built.

**Yielding and re-runging are different operations.** Sleep/wake is seconds; changing rung is a
model load. The spec's "sleep, do not reload" is true of the toggle and not of the ladder, and that
single distinction is what makes hysteresis load-bearing rather than a nicety.

**The bands in the spec are a summary, not the rule.** `footprint + headroom <= free` is the rule, and
at the bottom of each band it is stricter than the table. Keeping one rule in one place is worth more
than matching a table exactly, but the divergence should be visible on the dashboard rather than
buried here.

**Fail-safe is a property of the *agent*, not of the controller.** The controller is a single process
on one workstation and it will die at some point. Putting local, autonomous demotion in the agent —
demotion local, promotion central — is what turns that from an incident into a shrug, and it is the
main reason a tiny agent beats an off-the-shelf metrics exporter here.

What this milestone cannot fix is the honest limit in §12: preemption cannot stop an OOM already in
flight. The whole of the dashboard, the `ready` status line, and `gpu-run` exist to make the
preventable path the easy one. If one thing gets cut for time, cut the wrapper — not the toggle, and
not the status line.

**Next:** [`09-coding-agents.md`](./09-coding-agents.md) — M3, once the fleet is safe to share.
