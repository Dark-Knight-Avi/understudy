# 03 — GPU Sharing Policy

> These are workstations people use every day, not servers. This document is how the platform stays
> welcome on them. It implements N5 and N6 from
> [`00-goals-and-constraints.md`](./00-goals-and-constraints.md).

---

## 1. The principle

**The person at the machine is the priority tenant. The platform lives on the leftovers.**

Not "the platform reserves X GB." Not "users get a quota." The user takes as much GPU as their work
needs, whenever they need it, and the platform fits into whatever remains — including nothing.

This is a deliberate inversion of the usual server mindset, and it exists because the failure mode we
care about is not "chat was slow." It is "the platform made my job crash, so I want it removed."

### Why not simply reserve memory permanently?

Because GPU contention here is **occasional**: roughly 30–60 minute blocks, a few hours a day. A
permanent 10 GB reservation on `.226` would cost the platform its best coding model 100% of the time
in order to serve a case that arises maybe 20% of the time. Running big by default and yielding fast
is strictly better — provided yielding is reliable, which is the rest of this document.

---

## 2. Three states per host

| State | Trigger | Platform behaviour |
|---|---|---|
| **Free** | No foreign CUDA process | Top rung — largest model that fits, ~1 GB margin |
| **Yielding** | Toggle flipped, or a foreign process appears | Sleep to zero within seconds; gateway reroutes |
| **Sharing** | ~60 s after the user's job settles | Measure actual free VRAM; load the largest rung that fits, leaving >= 3 GB headroom |
| **Unknown** | Host unreachable, or `nvidia-smi` unparseable | Hold current state, **never promote**. Absence of evidence is not evidence of a free GPU |

### The 60-second settle is not padding

PyTorch's caching allocator grows during warm-up. A job that reads as 4 GB at launch may be 11 GB a
minute later. Sizing against the first reading would let the platform reclaim memory the user's job
is about to need, and then *the platform* becomes the reason their run died. So: wait, measure,
re-measure continuously, and drop a rung whenever their usage grows.

**Headroom is asymmetric on purpose.** ~1 GB margin when the box is free; ~3 GB when sharing. The
cost of being wrong is not symmetric — a slightly smaller model is an inconvenience, someone's
eight-hour run dying at hour six is not.

**A rung change is a model swap, not a sleep.** Sleep/wake keeps one model's weights in RAM; moving
between rungs loads a *different* model, which means a server restart on that host. So continuity
during a rung change depends on **gateway failover to another host**, not on sleep mode. Configure
that failover before building the ladder, or every rung change will look like a controller bug.

---

## 3. The model ladder

The platform does not follow a fixed plan; it picks by **measured** free VRAM.

### The binding rule

Do not implement the tables below as ranges. Implement **one inequality**, and derive everything from
it:

```
    footprint + headroom  <=  measured free VRAM

    footprint = model weights + KV cache budget   (measure it; the figures below are
                                                   weights-only estimates)
    headroom  = 1 GB when the host is Free
                3 GB when the host is Sharing
```

Pick the largest rung that satisfies it. The tables are a *derived summary for humans*, not the
algorithm — if a table and this inequality ever disagree, the inequality wins.

### `.226` — RTX 4090, 24 GB

| Free VRAM | Platform loads | Effect on users of the platform |
|---|---|---|
| >= 20 GB | Qwen3-Coder-30B-A3B Int4 (~17 GB) | Full capability |
| 12–20 GB | Qwen3-14B Int4 (~9 GB) | Noticeably weaker at agentic coding |
| 8.5–12 GB | Qwen3-8B Int4 (~5.5 GB) | Chat fine, coding poor |
| 6–8.5 GB | Qwen3-4B Int4 (~3 GB) | Degraded; still answers |
| < 6 GB | Nothing | Gateway routes to `.87` / `.149` |

### `.87` — RTX 4070, 12 GB

| Free VRAM | Platform loads |
|---|---|
| >= 9.2 GB | Embeddings (~1.2 GB) + small chat model (~5 GB) |
| 4.2–9.2 GB | Embeddings only |
| < 4.2 GB | Embeddings fall back to **CPU** — slower, but RAG never breaks |

Embeddings are the one thing that must never disappear: with them down, ingestion stops and every RAG
query fails. The CPU fallback exists so that outcome is impossible — which is also why embeddings get
no special exemption from the headroom rule. Losing them costs us latency; starving the user's job
costs them a day.

### `.210` — RTX 4070, 12 GB (someone's daily workstation)

| Free VRAM | Platform loads |
|---|---|
| >= 8.5 GB | Embeddings replica (~1.2 GB) + Qwen3-8B Int4 (~5.5 GB) |
| 4.2–8.5 GB | Embeddings replica only |
| < 4.2 GB | Nothing |

Treat this host as the strictest case. Its owner has first claim, uses it daily, and did not ask for
a platform to live on it. Prefer demoting early here over squeezing an extra rung.

### `.149` — RTX 5080, 16 GB

ComfyUI has **no sleep mode**, and its VRAM use is transient — it allocates per job and releases
afterwards. So this host's rung is enforced at **job admission**, not by what is resident: check free
VRAM when a request arrives and refuse or downgrade it there.

| Free VRAM at admission | Platform accepts |
|---|---|
| >= 15 GB | FLUX.1-schnell FP8 (~12 GB) |
| 9–15 GB | SD3.5-medium / SDXL-Turbo (~6 GB) |
| < 9 GB | Refuse; the MCP tool returns a clear "unavailable, host in use" |

### Why this is safe overall

All four hosts are rarely claimed at once, and the odds improve with every host added. With the
gateway aware of every host's state there is almost always somewhere to route — which is what turns
contention from an outage into a capacity dip. The fourth host also gives embeddings a real GPU
failover rather than only a CPU one.

---

## 4. The mechanisms

### 4.1 The toggle — the primary path

The fleet dashboard carries one switch per host: **"I'm using this GPU."** Flip it before starting
work, watch it move `releasing... -> ready`, then launch and take as much as you need. No amount to
declare, no quota to reason about.

```
   GPU fleet                                            [ dashboard ]

   .226  RTX 4090   ##########....  17.2 / 24 GB   Qwen3-Coder-30B
                    I'm using this  ( o--)                  ready in ~8s

   .87   RTX 4070   ###...........   1.2 / 12 GB   embeddings only
                    I'm using this  (--* )         YOURS - 22 min - AI on leftovers

   .149  RTX 5080   ########......  12.0 / 16 GB   FLUX.1-schnell
                    I'm using this  ( o--)

   .210  RTX 4070   ####..........   5.5 / 12 GB   embeddings + Qwen3-8B
                    I'm using this  ( o--)              a colleague's machine
```

**The status line is the entire point.** Nobody should have to guess whether the VRAM is actually
free, and nobody should have to learn a command. Serve the page from the fleet controller, bookmark
it on each machine's desktop, and mirror it inside Open WebUI so it sits where people already are.

Design requirements for the page:
- Live VRAM per host, refreshed every few seconds.
- Which model is currently loaded, and which rung that is.
- Whether a toggle is held, by whom, and for how long.
- An explicit `ready` state — never just "released".

### 4.2 Auto-release

A toggle held with **no CUDA process for ~30 minutes** releases itself, after a visible warning.
Forgetting to flip it back is the obvious failure mode, so the system handles it rather than relying
on anyone's discipline.

### 4.3 The preflight wrapper — the same thing for scripts

```
gpu-run python my_model.py
```

Calls the same API as the toggle, blocks until release completes, execs the real command, releases on
exit. For batch and scheduled work where nobody is sitting there to flip a switch. Ship it as a shell
alias so it costs nothing to adopt.

### 4.4 Automatic preemption — the safety net

For anyone who uses neither. The fleet controller polls `nvidia-smi` on each host every 2–3 seconds;
a foreign CUDA process triggers immediate demotion and reroutes the gateway.

**Interactive-login detection is per-host config, and off by default on `.87`.** A naive "someone is
logged in" trigger would pin `.87` to its bottom rung permanently — it is the hub, it hosts the
controller itself, and something is always logged into it. Where the trigger is enabled, it must fire
on a *new* session, not on the existence of one.

Be clear about its limit: **it cannot prevent an OOM already in flight.** A job that allocates
instantly on launch may still fail once, before the controller has reacted. That is exactly why the
toggle exists. Preemption recovers the situation within seconds; the toggle prevents it.

### 4.5 Sleep, do not reload

vLLM's sleep mode offloads weights to system RAM rather than discarding them. With 256 GB on `.226`,
demote and promote are seconds rather than a cold model load from NVMe. This is the mechanism that
makes the whole policy feel instant instead of annoying.

### 4.6 Hysteresis, so it does not flap

- Only change rung on a **sustained** free-VRAM change: more than ~2 GB, held for ~60 s.
- **Except downward, on a headroom breach.** If free VRAM falls below the headroom floor, demote
  immediately — no 60 s wait. Hysteresis exists to stop flapping, not to make us slow to get out of
  someone's way. Without this bypass the rule directly contradicts test 3 in §7.
- Wait ~5 minutes of a clear card before returning to the top rung.

With 30–60 minute user sessions this costs almost nothing and prevents a bursty job from triggering
a reload storm.

---

## 5. Two consequences worth stating plainly

**The reranker lives on CPU.** `.87`'s sharing state cannot hold it alongside embeddings, and the
reranker must not vanish mid-query. That is fine: `.87` has 24 cores and 128 GB of RAM and is the
least CPU-contended box, and reranking ~30 candidates costs a few hundred milliseconds there.
Embeddings stay on GPU because they are on the hot path for every query and every ingested chunk.

**Coding quality degrades during someone's session, and that is the right trade.** When `.226` is
claimed, Qwen3-Coder-30B unloads and whatever rung fits answers instead. A 30–60 minute window of
reduced quality a few times a day beats never having the good model at all. **Surface the current
rung in the UI** so the drop is visible rather than mysterious — an unexplained quality drop erodes
trust far faster than an explained one.

---

## 6. What this policy does *not* solve

**Memory bandwidth.** The deep tier ([`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) §4)
runs experts out of system RAM and is bandwidth-bound, competing directly with the simulation
modelling runs. There is no sleep mode for a memory controller. The deep tier therefore needs a
separate gate based on modelling-job state — not this ladder.

**CPU contention.** Cap WSL2 in `.wslconfig` (roughly 8 processors, 48 GB on `.226`) with
`autoMemoryReclaim=gradual`, so platform services cannot starve the modelling runs of cores.

> **Conflict to be aware of.** That 48 GB cap is incompatible with the deep tier, which needs ~130 GB
> of *guest* RAM on the same host for its experts ([`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md)
> §4). Both cannot hold at once. The resolution is two `.wslconfig` profiles — fast (8 cores / 48 GB)
> and deep (24 cores / 180 GB) — switched with `wsl --shutdown`, which restarts every container on the
> host. That restart cost is precisely why the deep tier is a **gated session**, not a permanent
> catalog entry. See [`07-inference-servers.md`](./07-inference-servers.md).

Details
in [`05-host-setup.md`](./05-host-setup.md).

**Anyone determined to break it.** This is a cooperative system for colleagues, not an adversarial
scheduler. That is the correct amount of engineering for the situation.

---

## 7. Acceptance tests

These are the tests that decide whether the platform is welcome. Run them at M2, before anyone comes
to depend on the platform.

1. **The toggle — headline test.** With the 30B coder loaded on `.226`, flip *I'm using this GPU*.
   The UI reaches `ready` within ~10 s, `nvidia-smi` shows the card essentially empty, a job needing
   **more than 10 GB** runs without OOM, and chat keeps answering from a lower rung throughout.
2. **Ladder correctness.** Run jobs of ~6, ~12 and ~20 GB in turn. The platform settles on the right
   rung each time, leaves >= 3 GB headroom, and never squeezes the user's job.
3. **Growth handling.** A job whose VRAM ramps over several minutes — the platform drops a rung
   rather than letting it OOM.
4. **Scripted path.** `gpu-run` a job with no toggle: same outcome, no manual step.
5. **Neither used.** Launch directly. Auto-preemption demotes within seconds; the job may need one
   retry; the platform recovers with no intervention.
6. **Both release paths.** Toggling off restores the top rung after the hysteresis window; a toggle
   left on with no CUDA process auto-releases after ~30 min, with a warning first.
7. **No flapping.** A job with bursty VRAM use does not cause repeated model reloads.
8. **Don't-disturb.** A full modelling run alongside the platform under load: per-iteration time
   stays within noise of its ~48 min baseline (N6).

---

## Reflect

The engineering here is modest — polling, a state machine, a switch. What makes it matter is that it
encodes a social contract in software: *your work comes first, and you do not have to argue with a
machine about it.* Most shared-GPU setups fail not because the scheduling is wrong but because the
sharing is invisible and unpredictable. The dashboard and the explicit `ready` state are doing as
much work here as the preemption logic.

If one thing gets cut for time, cut the preflight wrapper, not the toggle or the dashboard.

**Next:** [`04-m0-spikes.md`](./04-m0-spikes.md) — measure all of this before building on it.
