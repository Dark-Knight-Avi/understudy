# ADR-0001 — Partition the fleet by service; do not cluster the GPUs

**Status:** Accepted

## Context

We have three GPUs totalling 52 GB VRAM, but they sit in three separate workstations connected by
1 GbE, across two subnets. The obvious wish is to pool them so that one large model can use all
52 GB — which would put frontier-class models within reach on GPU alone.

The hosts are also heterogeneous (24 / 12 / 16 GB, two architectures, two OS setups) and shared with
people doing other work, so any given host can vanish from the pool at any moment.

## Decision

**Do not attempt distributed inference.** Each host runs the services its hardware suits, and a
single **LiteLLM gateway** presents the fleet as one OpenAI-compatible model catalog.

- `.226` (24 GB, 256 GB RAM) — fast-tier generation, and the deep tier via CPU offload
- `.87` (12 GB, 128 GB RAM, least contended) — embeddings, small models, and all CPU services
- `.149` (16 GB, native Linux) — image generation

Capacity is scaled by **adding models to the catalog**, not by pooling memory.

## Consequences

- **+** Works at all. Tensor- and pipeline-parallel inference exchange activations every layer and
  need InfiniBand-class interconnect; over 1 GbE the result is unusable, not merely slow.
- **+** No single point of failure for chat. When one host is claimed by its user, the gateway routes
  to another — contention becomes a capacity dip instead of an outage.
- **+** Heterogeneous hardware becomes an asset: each host does what it is actually good at, and
  differing GPU generations never have to agree.
- **+** Hosts can join and leave the fleet freely, which is required by
  [`03-gpu-sharing-policy.md`](../03-gpu-sharing-policy.md).
- **−** No single model may exceed one host's memory. Frontier-scale models are reachable only
  through CPU offload on `.226` (ADR-0003), at reduced speed.
- **−** Aggregate VRAM is never usable as one pool; 52 GB is a fleet total, not a model budget.

## Alternatives considered

- **vLLM tensor/pipeline parallel over the LAN** — rejected. 1 GbE is roughly three orders of
  magnitude short of the interconnect bandwidth this needs. Not a tuning problem.
- **Ray cluster across the three hosts** — same physical limit; adds an orchestration layer without
  addressing the bottleneck.
- **Consolidate all three GPUs into one chassis** — technically the best answer, and the Threadripper
  PRO platform has the PCIe lanes for it. Rejected because it requires hardware spend, and because
  `.87` and `.149` must remain usable workstations where they are.
- **Use only `.226` and ignore the other two** — simpler, but discards 28 GB of VRAM and the
  routing-around-contention property that makes the platform usable on shared machines.
