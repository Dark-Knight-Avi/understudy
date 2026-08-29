# ADR-0003 — Two quality tiers, and an elastic model ladder

**Status:** Accepted

## Context

The requirement is models "as powerful as possible" — ideally frontier-class — that are open-weight,
free, and run locally. Two facts collide with that:

1. Nothing that fits in 24 GB of VRAM approaches frontier quality. The open-weight models that
   genuinely compete are 235B–1T MoEs. The aggregate gap between the best open-weight models and the
   closed frontier is still roughly 14 points on composite indices.
2. `.226` is shared. Its GPU can be claimed by its user at any moment, so no model can be assumed
   permanently resident.

But `.226` also has **256 GB of 8-channel DDR5-5600 on a Threadripper PRO**. MoE models activate only
a fraction of their weights per token, so experts can live in system RAM while attention and KV cache
stay on the GPU — making throughput bandwidth-bound rather than VRAM-bound. Reported on comparable
hardware: >15 tok/s at 64k context, and a 671B MoE fitting under 256 GB RAM + 24 GB VRAM.

## Decision

**Two tiers, user-selectable per task**, plus **an elastic ladder within the fast tier**.

| Tier | Model | Lives in | Speed | Use for |
|---|---|---|---|---|
| Fast | Qwen3-Coder-30B-A3B Int4 | GPU, ~17 GB | ~60–85 tok/s | Interactive work |
| Deep | Qwen3-235B-A22B Q4 | GPU attention + ~130 GB RAM | ~10–20 tok/s | Hard problems worth waiting for |
| Max | DeepSeek-V3/R1-class, Q2–Q3 | ~230 GB RAM | single digits | Off-hours only |

The **ladder** governs the fast tier: the fleet controller measures actual free VRAM and loads the
largest rung that fits — 30B, then 14B, 8B, 4B, then nothing — rather than following a fixed plan.
Detail in [`03-gpu-sharing-policy.md`](../03-gpu-sharing-policy.md).

Model *names* here are recommendations to benchmark. The durable part of this decision is the
**selection method**: memory budget → candidate shortlist → measured on our own eval set.

## Consequences

- **+** Near-frontier quality is reachable on hardware we already own — the deep tier is the only
  path to it, and it exists only because of `.226`'s memory configuration.
- **+** The tier split matches how people actually work: fast for iterating, deep for the hard
  question. Better than one mediocre compromise model, and it directly satisfies F2.
- **+** The ladder means the platform degrades rather than fails when a host is claimed.
- **+** MoE architectures make both tiers viable; a dense model of either size would not work here.
- **−** Deep tier is **memory-bandwidth-bound**, competing directly with the long-running simulation
  runs for the resource they need. There is no sleep-mode equivalent for bandwidth, so this tier must
  be gated on modelling-job state — potentially off-hours only.
- **−** Answer quality varies with host contention. This must be surfaced in the UI; an unexplained
  quality drop erodes trust faster than an explained one.
- **−** Deep-tier weights are 100–250 GB each on disk, and switching tiers is a cold load.
- **−** We are honest that fast tier is clearly weaker than a frontier model. Expectation-setting is
  part of the deliverable, not an afterthought.

## Alternatives considered

- **One mid-size model for everything** (e.g. Qwen3-14B) — simplest, and always available. Rejected:
  it is simultaneously too weak for hard problems and needlessly slow-to-load for trivial ones, and it
  leaves the 256 GB of RAM entirely unused.
- **Fast tier only, no CPU offload** — avoids the bandwidth contention problem entirely, but caps
  quality well below what the hardware can reach. Kept as the fallback if M0 spike 7 fails.
- **Buy a second GPU / a 48 GB card** — the cleanest technical answer, rejected under the zero-budget
  constraint. Worth revisiting if the platform proves valuable.
- **Route hard questions to a hosted frontier API** — flatly incompatible with the egress constraint
  (ADR-0004) and with N2.
