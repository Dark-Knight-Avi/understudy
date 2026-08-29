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
| **Sharing** | ~60 s after the user's job settles | Measure actual free VRAM; load the largest rung that fits, leaving ~3 GB headroom |

### The 60-second settle is not padding

PyTorch's caching allocator grows during warm-up. A job that reads as 4 GB at launch may be 11 GB a
minute later. Sizing against the first reading would let the platform reclaim memory the user's job
is about to need, and then *the platform* becomes the reason their run died. So: wait, measure,
re-measure continuously, and drop a rung whenever their usage grows.

**Headroom is asymmetric on purpose.** ~1 GB margin when the box is free; ~3 GB when sharing. The
cost of being wrong is not symmetric — a slightly smaller model is an inconvenience, someone's
eight-hour run dying at hour six is not.

---

## 3. The model ladder

The platform does not follow a fixed plan; it picks by **measured** free VRAM.

### `.226` — RTX 4090, 24 GB

| Free VRAM | Platform loads | Effect on users of the platform |
|---|---|---|
| >= 20 GB | Qwen3-Coder-30B-A3B Int4 (~17 GB) | Full capability |
| 12–20 GB | Qwen3-14B Int4 (~9 GB) | Noticeably weaker at agentic coding |
| 7–12 GB | Qwen3-8B Int4 (~5.5 GB) | Chat fine, coding poor |
| 4–7 GB | Qwen3-4B Int4 (~3 GB) | Degraded; still answers |
| < 4 GB | Nothing | Gateway routes to `.87` / `.149` |

### `.87` — RTX 4070, 12 GB

| Free VRAM | Platform loads |
|---|---|
| >= 8 GB | Embeddings (~1.2 GB) + small chat model (~5 GB) |
| 2–8 GB | Embeddings only |
| < 2 GB | Embeddings fall back to **CPU** — slower, but RAG never breaks |

Embeddings are the one thing that must never disappear: with them down, ingestion stops and every RAG
query fails. The CPU fallback exists so that outcome is impossible.

### `.149` — RTX 5080, 16 GB

| Free VRAM | Platform loads |
|---|---|
| >= 14 GB | FLUX.1-schnell FP8 (~12 GB) |
| 7–14 GB | SD3.5-medium / SDXL-Turbo (~6 GB) |
| < 7 GB | Image generation offline; the MCP tool returns a clear "unavailable, host in use" |

### Why this is safe overall

All three hosts are rarely claimed at once. With the gateway aware of every host's state, there is
almost always somewhere to route — which is what turns contention from an outage into a capacity dip.

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
a foreign CUDA process or an interactive login triggers immediate demotion and reroutes the gateway.

Be clear about its limit: **it cannot prevent an OOM already in flight.** A job that allocates
instantly on launch may still fail once, before the controller has reacted. That is exactly why the
toggle exists. Preemption recovers the situation within seconds; the toggle prevents it.

### 4.5 Sleep, do not reload

vLLM's sleep mode offloads weights to system RAM rather than discarding them. With 256 GB on `.226`,
demote and promote are seconds rather than a cold model load from NVMe. This is the mechanism that
makes the whole policy feel instant instead of annoying.

### 4.6 Hysteresis, so it does not flap

- Only change rung on a **sustained** free-VRAM change: more than ~2 GB, held for ~60 s.
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
`autoMemoryReclaim=gradual`, so platform services cannot starve the modelling runs of cores. Details
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
