# 02 — Hardware & Fleet

> What we have, what each machine is for, how much memory each model actually needs, and an honest
> answer to "how close to frontier can we get?"

---

## 1. The fleet

| | `.226` | `.87` | `.149` | `.210` |
|---|---|---|---|---|
| **Role** | Fast tier + deep tier | Hub: services, embeddings | Image generation | Overflow + embeddings failover |
| **GPU** | RTX 4090, **24 GB** | RTX 4070, **12 GB** | RTX 5080, **16 GB** | RTX 4070, **12 GB** |
| **Arch / CC** | Ada, 8.9 | Ada, 8.9 | **Blackwell, 12.0** | Ada, 8.9 |
| **CUDA** | 13.1 | 12.6 | 13.1 | 13.1 |
| **CPU** | TR PRO 9975WX, 32c/64t | i9-14900K, 24c/32t | i9-14900K, 24c/32t | i9-14900K, 24c/32t |
| **RAM** | **256 GB DDR5-5600** | 128 GB DDR5-4000 | 32 GB DDR5-5600 | 96 GB DDR5-4000 |
| **Storage** | 8 TB + 2x4 TB NVMe | 2x2 TB NVMe | 2 TB NVMe | 2x2 TB NVMe |
| **OS** | Windows + WSL2/Ubuntu | Windows + WSL2/Ubuntu | Windows, **no WSL yet** | Windows + WSL2/Ubuntu |
| **Also used for** | Long long-running simulation runs | Light | Lab workstation | **Someone's daily workstation** |

Total: **64 GB VRAM**, **512 GB RAM**, four separate machines on 1 GbE, two subnets
(`10.0.0.x` and `10.0.1.x`).

### Why each host has the role it does

**`.226` runs the models.** Most VRAM, and — more importantly — 256 GB of 8-channel DDR5. That memory
bandwidth is what makes both vLLM sleep-mode (weights parked in RAM, so demote/promote is seconds)
and the deep tier (experts resident in RAM) possible. No other box can do either.

**`.87` is the hub despite the weakest GPU.** Its job is CPU and RAM work: Postgres, the gateway, the
reranker, the app services. Its 12 GB carries only the small models that must never be evicted —
embeddings above all, since nothing in RAG works without them. It is also the least contended box, so
it is where anything that must stay up belongs.

**`.149` does image generation.** 32 GB of system RAM rules out hosting data services, and it sits on
another subnet. Image generation is bursty, self-contained, and tolerates both.

**`.210` is elastic overflow, and deliberately holds nothing critical.** It is assigned to a named
person and is their daily machine, which makes it the least reliable host in the fleet -- not because
of its hardware, but because its owner has first claim on it and will exercise that claim without
warning. So it gets work that can vanish mid-flight: burst chat capacity, and a **GPU replica of the
embeddings service**. That replica is the real prize. Embeddings are the one thing that must never
die, and until now their only fallback was CPU on `.87`; a second GPU copy turns a degraded fallback
into a proper failover.

What it must *not* get is the deep tier. Its 96 GB could hold a mid-size MoE, but CPU offload is
memory-bandwidth-hungry and would make the machine sluggish for the person sitting at it -- which is
exactly the outcome the sharing policy exists to prevent.

### Two hardware facts that change decisions

**`.149` is Blackwell (CC 12.0).** The widely reported WSL2 CUDA memory-overhead problem is specific
to `sm_120`. This box has no WSL installed yet, so **install native Ubuntu on it** and the problem
never arises. Also verify your PyTorch / vLLM / ComfyUI builds actually ship `sm_120` kernels —
Blackwell support arrived later than Ada's.

**`.226` is AMD.** NVIDIA documents a CUDA-under-WSL2 cache-coherency fault on AMD Ryzen that can
hang or crash CUDA applications. This is a Threadripper. M0 soak-tests for it before anything is
built on top.

---

## 2. Memory budgeting method

Do not trust a model card's parameter count. Budget like this, per host:

```
usable VRAM  =  nominal VRAM
              - driver/runtime overhead   (measure it, do not assume ~1 GB)
              - other resident processes
              - safety margin             (~1 GB)

model budget =  usable VRAM - KV cache budget

KV bytes/token = 2 (K and V)
               x n_layers
               x n_kv_heads
               x head_dim
               x bytes_per_element   (2 for FP16, 1 for FP8)
```

Worked example, Qwen3-14B class (40 layers, 8 KV heads, head_dim 128), FP16 KV:

```
2 x 40 x 8 x 128 x 2  =  163,840 bytes/token  ~=  0.16 MB/token
```

So 10 GB of KV cache buys roughly 64k tokens total across all concurrent sequences — four streams at
16k context each. Halve the cost with `--kv-cache-dtype fp8`, which Ada (CC 8.9) supports natively on
`.226` and `.87`.

**Two traps.**

*Quantisation format.* Published `Q4_K_M` sizes are GGUF — that is llama.cpp and Ollama. vLLM's
production path is AWQ, GPTQ-Int4 or FP8. Before committing to any model, confirm a build exists in
the format your server actually loads.

*`--gpu-memory-utilization` semantics.* It is a fraction, but whether it is a fraction of *total* or
*free* memory has varied across vLLM versions. Pin the behaviour of your version in
[`07-inference-servers.md`](./07-inference-servers.md) — the whole sharing policy depends on it.

---

## 3. The fast tier — GPU-resident

Everything here is sized for `.226`'s 24 GB and swapped by the fleet controller according to the
ladder in [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md).

| Purpose | Model | Quant | VRAM | Host |
|---|---|---|---|---|
| Agentic coding | Qwen3-Coder-30B-A3B | Int4 (AWQ/GPTQ) | ~17 GB | `.226` |
| General chat | Qwen3-14B | Int4 | ~9 GB | `.226` |
| Ladder rung | Qwen3-8B | Int4 | ~5.5 GB | `.226` |
| Ladder rung | Qwen3-4B | Int4 | ~3 GB | `.226` |
| Fast / autocomplete | Qwen3-4B | Int4 | ~3 GB | `.87` |
| Embeddings | Qwen3-Embedding-0.6B | FP16 | ~1.2 GB | `.87` |
| Reranking | bge-reranker-v2-m3 | — | CPU | `.87` |
| Image, quality | FLUX.1-schnell (Apache-2.0) | FP8 | ~12 GB | `.149` |
| Image, fast | SD3.5-medium / SDXL-Turbo | — | ~6 GB | `.149` |

Qwen3-Coder-30B-A3B is a Mixture-of-Experts model: ~30B total parameters, ~3B active per token. That
is why it fits 24 GB at Int4 *and* decodes at near-small-model speed. MoE is the reason the fast tier
is as capable as it is.

---

## 4. The deep tier — how close to frontier can we actually get?

**Honest answer first: nothing that fits in 24 GB of VRAM is close to a frontier model.** The
open-weight models that genuinely compete — Kimi K3, DeepSeek V4-Pro, Qwen3-235B-A22B, MiniMax M3 —
are 235B to 1T parameter MoEs. They are competitive on individual benchmarks (Kimi K2.6 outscores
Claude Opus 4.6 on SWE-Bench Pro), but the aggregate gap to the closed frontier remains roughly
14 points, and none of them fit on a 4090.

**But `.226` is not merely a 4090.** It is a 4090 attached to 256 GB of 8-channel DDR5-5600. That is
precisely the configuration where those models become runnable, because MoE models activate only a
fraction of their weights per token:

- **Experts live in system RAM.** Only the few experts selected for the current token are read.
- **Attention and KV cache live on the GPU**, where latency matters most.
- **Throughput is then bounded by memory bandwidth**, not VRAM capacity — which is why 8-channel
  DDR5 on a Threadripper PRO is the enabling detail and a dual-channel desktop is not.

Reported on comparable hardware: a Threadripper PRO with 256 GB sustaining **over 15 tok/s at 64k
context**, and `ik_llama.cpp` fitting a **671B-parameter** MoE under 256 GB RAM plus 24 GB VRAM using
aggressive quantisation.

### The tiers, as the user sees them

| Tier | Model | Lives in | Speed | Quality |
|---|---|---|---|---|
| **Fast** | Qwen3-Coder-30B-A3B Int4 | GPU, ~17 GB | ~60–85 tok/s | Good; clearly below frontier |
| **Deep** | Qwen3-235B-A22B Q4 | GPU attention + ~130 GB RAM | ~10–20 tok/s | Near-frontier, Apache 2.0 |
| **Max** | DeepSeek-V3/R1-class 671B, Q2–Q3 | Fills the box, ~230 GB RAM | single digits tok/s | Closest available; needs `.226` to itself |

Fast tier for interactive work — autocomplete, quick questions, iterating. Deep tier for the things
worth waiting for: a hard bug, a design review, a long document. That split fits how people actually
work far better than one mediocre compromise model, and it is exactly the per-task switching we set
out to provide (F2).

### The catch, and it is a real one

Deep-tier inference is **memory-bandwidth-bound**, so it competes with the long-running simulation
runs for the exact resource those runs need. GPU contention is solvable with a toggle; RAM-bandwidth
contention is not — there is no sleep-mode equivalent for memory bandwidth, and no way to hand back
half a memory controller.

Second, it needs ~130 GB of *guest* RAM, which collides with the ~48 GB WSL2 cap that protects the
modelling runs from CPU starvation. Two `.wslconfig` profiles resolve it -- fast and deep -- switched
with `wsl --shutdown`, which restarts every container on the host. See `07-inference-servers.md`.

So the deep tier must be **gated on modelling-job state**: available when `.226` is quiet, queued or
refused when it is not. M0 spike 7 measures what the modelling runs actually consume in RAM and
bandwidth. **Do not promise this tier to anyone before that number exists.**

---

## 5. Storage layout

| Host | Path | Contents |
|---|---|---|
| `.226` | 8 TB NVMe | Model weights (deep-tier GGUFs are 100–250 GB each) |
| `.226` | 4 TB NVMe #2 | Container volumes, logs |
| `.87` | NVMe #1 | Postgres data directory — deliberately separate from anything else |
| `.87` | NVMe #2 | Ingested source documents, container volumes |
| `.149` | 2 TB NVMe | Image model weights, ComfyUI outputs |

Keep the Postgres data directory on its own device. IO isolation from other work on the box is worth
more than the capacity.

---

## Reflect

Two facts drive nearly every choice here. First, **MoE architectures are what make this project
possible at all** — a dense 235B model on this hardware would be unusable, while a 235B-A22B is not.
Second, **`.226`'s 256 GB of 8-channel memory is the single most valuable asset in the fleet**, worth
more to us than the 4090 it sits next to, because it is what turns "no frontier model fits" into
"a frontier model runs, slowly."

The numbers in this document are estimates until M0 measures them. Treat every VRAM figure and every
tok/s figure as a hypothesis with a test attached.

**Next:** [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md).
