# 07 — Inference Servers

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.
>
> Milestones **M1** (fast tier) and **M4** (deep tier). What actually runs the models: vLLM on all
> three hosts, and `ik_llama.cpp` on `.226` for the 235B deep tier. Depends on
> [`05-host-setup.md`](./05-host-setup.md); the gateway in front of it is
> [`06-model-gateway.md`](./06-model-gateway.md).
>
> The deep tier is a different engine with a different failure mode and a different gate on it. It
> starts at §10 and is deliberately kept separate.

---

## Concept

### 1. Two engines, because they solve different problems

| | **vLLM** — fast tier | **`ik_llama.cpp`** — deep tier |
|---|---|---|
| Hosts | `.226`, `.87`, `.149` | `.226` only |
| Where the weights live | GPU VRAM | ~130 GB in system RAM; attention + KV on GPU |
| Bound by | VRAM capacity, then compute | **Memory bandwidth** |
| Quant format | AWQ / GPTQ-Int4 / FP8 | GGUF (MoE-tuned quant types) |
| Concurrency | Continuous batching, 2–4 streams comfortably | Effectively one serious request at a time |
| Yields the GPU | **Yes — sleep mode**, seconds | Not meaningfully; it is gated instead |
| Speed | ~60–85 tok/s (estimate, [`02`](./02-hardware-and-fleet.md) §4) | ~10–20 tok/s (estimate, unmeasured) |
| Competes with | The user's GPU work — solved by [`03`](./03-gpu-sharing-policy.md) | The long-running simulation runs, for RAM bandwidth — **not** solved by `03` |

The sharing policy exists because of the last row on the vLLM side. The deep tier's gate (§14) exists
because that policy does not extend to memory bandwidth.

### 2. Quantisation: the trap worth restating

Published `Q4_K_M` sizes on model cards are **GGUF** — llama.cpp and Ollama. vLLM's production path is
**AWQ, GPTQ-Int4 or FP8**. A GGUF on disk does not make a model servable by vLLM in any way you want
to depend on.

| Engine | Formats to look for | Where it bites |
|---|---|---|
| vLLM | `-AWQ`, `-GPTQ-Int4`, `-FP8` repos | If no such build exists for a model, that model is not on the fast tier. Check *before* committing to it |
| `ik_llama.cpp` | GGUF, including MoE-specific types | Quant naming varies between the fork and mainline; a file that loads in one may not in the other |

So: **before adding any model to [`06`](./06-model-gateway.md)'s catalog, confirm a build exists in
the format the server actually loads**, and pin its exact revision in the host's `.env`
([`delivery-plan.md`](./delivery-plan.md) §8).

### 3. Sizing context, from the model's own config

[`02`](./02-hardware-and-fleet.md) §2 gives the KV formula. Do not copy per-model numbers from
anywhere — including this document — read them from the model's `config.json`:

```python
# per-token KV cost for one sequence
import json
c = json.load(open("config.json"))
layers  = c["num_hidden_layers"]
kv_head = c.get("num_key_value_heads", c["num_attention_heads"])
head_d  = c.get("head_dim", c["hidden_size"] // c["num_attention_heads"])
for bytes_per_el, label in ((2, "fp16"), (1, "fp8")):
    per_tok = 2 * layers * kv_head * head_d * bytes_per_el
    print(f"{label}: {per_tok/2**20:.3f} MiB/token  ->  "
          f"{10 * 2**30 / per_tok:,.0f} tokens per 10 GiB of KV cache")
```

Then fill this in per rung, on the host, with the real numbers:

| Rung | Weights | KV budget left | MiB/token (fp8) | `--max-model-len` | `--max-num-seqs` |
|---|---|---|---|---|---|
| Qwen3-Coder-30B-A3B Int4 | ~17 GB | | compute | | |
| Qwen3-14B Int4 | ~9 GB | | compute | | |
| Qwen3-8B Int4 | ~5.5 GB | | compute | | |
| Qwen3-4B Int4 | ~3 GB | | compute | | |

Two judgements to make while filling it in. **Context length is a budget, not a maximum** — asking
for 128k on the 30B rung leaves no room for concurrency, and N4 wants 2–4 streams before it wants a
long context. And **agentic clients are context-hungry**: Cline in particular will fill whatever it is
given ([`tech-stack.md`](./tech-stack.md) §4), so the `coder` rung wants a longer context than `chat`
does even though they run on the same card.

---

## Build — fast tier (vLLM)

### 4. Install and pin

Run vLLM from its official image rather than a pip install: the CUDA/PyTorch/vLLM version matrix is
the single most common source of "it worked yesterday" on this stack.

```yaml
# deploy/host-226/compose.yaml  (fragment)
services:
  vllm-fast:
    image: vllm/vllm-openai:vX.Y.Z          # PIN. Match the host's CUDA line (05 §6.4)
    restart: unless-stopped
    ipc: host                                # vLLM needs a large /dev/shm
    ports:
      - "0.0.0.0:8000:8000"
    volumes:
      - /srv/ai-platform/models:/models:ro
      - /srv/ai-platform/data/hf-cache:/root/.cache/huggingface
    environment:
      VLLM_SERVER_DEV_MODE: "1"              # exposes /sleep and /wake_up. See section 8
      HF_HUB_OFFLINE: "1"                    # weights are pre-downloaded; no egress at start-up
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: all, capabilities: [gpu] }]
    command: >
      --model /models/Qwen3-Coder-30B-A3B-Instruct-AWQ
      --served-model-name coder fast-tier
      ...
```

`HF_HUB_OFFLINE=1` is an egress control as much as a convenience — a container that cannot reach
Hugging Face cannot silently re-download a model over the 1 GbE link mid-incident, and it makes the
pinned-revision rule enforceable rather than aspirational.

### 5. `.226` — the fast tier

```bash
vllm serve /models/Qwen3-Coder-30B-A3B-Instruct-AWQ \
  --served-model-name coder fast-tier \
  --host 0.0.0.0 --port 8000 \
  --api-key "$VLLM_226_KEY" \
  --quantization awq_marlin \
  --dtype auto \
  --gpu-memory-utilization 0.90 \
  --max-model-len 65536 \
  --max-num-seqs 8 \
  --kv-cache-dtype fp8 \
  --enable-prefix-caching \
  --enable-sleep-mode \
  --swap-space 4 \
  --disable-log-requests
```

| Flag | Why | Watch out |
|---|---|---|
| `--served-model-name coder fast-tier` | Two aliases so the gateway's `coder` and `chat` entries hit one server ([`06`](./06-model-gateway.md) §5) | Whether multiple names are accepted has varied — **verify against your version**; if not, run the alias you need and add the second entry in LiteLLM |
| `--quantization awq_marlin` | The optimised Ada kernel for AWQ weights | Name differs by format (`gptq_marlin`, `fp8`). Wrong value = slow path or refusal to start |
| `--gpu-memory-utilization 0.90` | The size of the whole pool vLLM claims | **Fraction of what?** See §7. This one flag decides whether the sharing policy works |
| `--max-model-len 65536` | Context budget from §3 | Too high and KV cache crowds out concurrency; vLLM fails at start-up if it cannot fit at all |
| `--max-num-seqs 8` | Concurrency ceiling; N4 needs 2–4 | Higher is not free — each sequence draws from the same KV pool |
| `--kv-cache-dtype fp8` | Halves KV cost. Ada (CC 8.9) supports FP8 natively on `.226` and `.87` | Quality effect is small but not zero; some versions want calibration scales. A/B it on the eval set before making it permanent |
| `--enable-prefix-caching` | Agentic clients resend near-identical prefixes constantly; this is close to free latency | Uses KV pool space |
| `--enable-sleep-mode` | The entire GPU-sharing policy depends on it (§8) | Sleep level 1 offloads weights to **system RAM** — check that fits inside the `.wslconfig` cap |
| `--swap-space 4` | CPU swap space for preempted sequences, GiB | Counts against the WSL2 memory cap |
| `--disable-log-requests` | Prompts do not belong in container logs | Makes debugging harder; that is the correct trade for a colleague's chat |

**Sleep mode inside a 48 GB cap.** [`05`](./05-host-setup.md) §6.2 caps `.226`'s WSL2 guest at 48 GB.
A sleeping 30B-A3B parks ~17 GB of weights in that guest's RAM, plus vLLM's own footprint plus
`--swap-space`. It fits, but it is not spacious — and if you run two vLLM instances (§6) both
sleeping, add both. Measure real RSS while asleep and record it; this is the number that decides how
many rungs can stay resident.

### 6. How the ladder actually changes rungs

[`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) says the controller loads "the largest rung
that fits". Mechanically there are three ways to do that, and the choice belongs here:

| Approach | Mechanism | Cost | Verdict |
|---|---|---|---|
| **Sleep/wake one server** | One vLLM per model; sleep the big one, wake the small one | Seconds. RAM for every resident model's weights | **Best for the top two rungs.** ~17 GB + ~5.5 GB of parked weights is affordable within the 48 GB cap |
| **Stop/start containers** | Controller restarts a container with different flags | Cold load from NVMe: tens of seconds, and in-flight requests die | Fine for rarely-used rungs (4B), unacceptable as the routine path |
| **Restart with a lower `--gpu-memory-utilization`** | Same model, smaller pool | Full restart, and the model may no longer fit | Avoid. Sleep is the mechanism, not resizing |

Recommended shape on `.226`: **two vLLM services** — the 30B `coder`/`chat` server and an 8B or 4B
floor — with the controller sleeping and waking them so exactly one is awake in the sharing state,
and both asleep when the host is fully claimed. Fall through to `.87`'s `chat-small` when neither
fits, which the gateway already does via `fallbacks`.

### 7. Pinning `--gpu-memory-utilization` semantics — do this before M2

It is a fraction, but **whether it is a fraction of *total* VRAM or of *free* VRAM has varied between
vLLM versions**, and the difference decides whether the platform politely takes 60% of what is left or
tries to take 90% of the whole card while someone's job is on it. Do not infer it from documentation.
Measure it:

```bash
# 1. Card empty. Note total.
nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv

# 2. Start vLLM with --gpu-memory-utilization 0.50 on the empty card. Note used.
#    If used ~= 0.50 * total, the fraction is of TOTAL.

# 3. Now occupy ~8 GB with a dummy allocation, restart vLLM with the same 0.50.
python3 -c "import torch,time; x=torch.empty(8*2**30,dtype=torch.uint8,device='cuda'); time.sleep(3600)" &
#    If used ~= 0.50 * (total - 8 GB), the fraction is of FREE.
```

Write the answer, the version tag, and the date in the shipped version of this document. The fleet
controller computes its `--gpu-memory-utilization` per rung from that result, so getting it wrong
means the platform either wastes the card or OOMs someone's job — the exact outcome
[`03`](./03-gpu-sharing-policy.md) exists to prevent.

### 8. Sleep and wake — the endpoints the policy rides on

```bash
# Requires --enable-sleep-mode AND VLLM_SERVER_DEV_MODE=1 in the environment.
# ENDPOINT NAMES AND PAYLOADS HAVE MOVED BETWEEN VERSIONS — verify against yours,
# then record the exact form here.

curl -X POST "http://10.0.0.226:8000/sleep?level=1"   # offload weights to system RAM
curl -X GET  "http://10.0.0.226:8000/is_sleeping"
curl -X POST "http://10.0.0.226:8000/wake_up"
nvidia-smi --query-gpu=memory.used --format=csv          # confirm the card is actually empty
```

| Level | What it does | Wake cost | Use when |
|---|---|---|---|
| **1** | Weights offloaded to CPU RAM, KV cache discarded | Seconds | The default. Someone claimed the GPU and will give it back |
| **2** | Weights discarded entirely | Cold reload from NVMe | Only when RAM is needed too — e.g. switching `.226` into the deep-tier profile |

Targets from M0 spike 6: **VRAM free within ~10 s, wake within ~15 s.** If the measured numbers are
worse than ~30 s, sleep mode is not viable and the fallback is stopping and restarting the server —
in which case say the real number in the dashboard rather than quietly making people wait.

Two operational notes. In-flight requests at sleep time are lost, so the controller should stop
routing (gateway first, §7 of [`06`](./06-model-gateway.md)) before sleeping. And a sleeping server
still answers HTTP — whether it also reports healthy is the open question in
[`06`](./06-model-gateway.md) §7, and the answer belongs in both documents.

### 9. `.87` and `.149`

**`.87` — the floor that never disappears.** A 4B Int4 model with a small context, sized so it
coexists with Infinity's embeddings on the 12 GB card:

```bash
vllm serve /models/Qwen3-4B-Instruct-AWQ \
  --served-model-name small-tier chat-small \
  --host 0.0.0.0 --port 8000 --api-key "$VLLM_87_KEY" \
  --quantization awq_marlin --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.45 \
  --max-model-len 16384 --max-num-seqs 4 \
  --enable-sleep-mode
```

The utilisation figure is low on purpose. Embeddings are the one service that must never vanish
([`03`](./03-gpu-sharing-policy.md) §3) — if the small chat model and Infinity ever contend, the chat
model loses. Infinity itself (embeddings on GPU, reranker on CPU) belongs to
[`12-retrieval-and-rerank.md`](./12-retrieval-and-rerank.md).

**`.149` — spare capacity, not a commitment.** Its job is image generation
([`15-generation-tools.md`](./15-generation-tools.md)). A vLLM instance there is a bonus for when
ComfyUI is idle, and it carries two caveats: the 16 GB is Blackwell, so **confirm your vLLM build
ships `sm_120` kernels** (M0 spike 3) before assuming anything runs; and `--kv-cache-dtype fp8` is
well-trodden on Ada but should be **verified on CC 12.0 for your version** rather than assumed. Also
confirm cross-subnet latency (spike 4) is good enough that routing chat there is not worse than
routing it to the 4B on `.87`.

### 9b. Verify the fast tier

```bash
# Loaded and named as the gateway expects
curl -s http://10.0.0.226:8000/v1/models -H "Authorization: Bearer $VLLM_226_KEY"

# Warm TTFT under 2 s at 1, 2 and 4 concurrent streams (N3, N4)
# Sleep frees the card within ~10 s and a >10 GB job then runs without OOM (03 §7 test 1)
# Survives a host reboot with no manual step (N8)
```

---

## Deep tier — `ik_llama.cpp` on `.226`

> Everything below is a **different engine, a different memory system and a different risk**. It is
> gated on M0 spike 7, and it must not be mentioned to users before that spike has a verdict
> ([`02`](./02-hardware-and-fleet.md) §4).

### 10. What it is

Qwen3-235B-A22B Q4: ~130 GB of weights, of which only ~22B parameters are active per token. The MoE
structure is what makes it possible — **experts live in system RAM and only the selected few are read
per token, while attention and the KV cache stay on the GPU** where latency matters. Throughput is
then bounded by memory bandwidth, which is why `.226`'s 8-channel DDR5-5600 is the enabling asset and
a dual-channel desktop would not do.

The cost is stated plainly in [ADR-0003](./adr/0003-model-tiers-and-ladder.md): this tier competes
with the long-running simulation runs for exactly the resource those runs need, and there is no
sleep mode for a memory controller.

### 11. Build

[`tech-stack.md`](./tech-stack.md) §1 says: prove the concept on **mainline llama.cpp** first — it is
safer, better-tested, and now has `--n-cpu-moe` too — then move to `ik_llama.cpp` for the extra MoE
throughput. Failing back to mainline stays available.

```bash
# Inside WSL2 on .226, in the DEEP-TIER profile (section 13)
sudo apt-get install -y build-essential cmake git libcurl4-openssl-dev

git clone https://github.com/ikawrakow/ik_llama.cpp
cd ik_llama.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 16

./build/bin/llama-server --version
```

`ik_llama.cpp` is a fork with its own flags and its own MoE-tuned quant types, and both drift from
mainline. **Read its README at the commit you build** rather than assuming mainline's options apply,
and record the exact commit hash — an unpinned fork is an unreproducible deploy.

### 12. Weights

100–250 GB per model, on `.226`'s 8 TB NVMe at `/srv/ai-platform/models/`, reached at native ext4
speed and not across the WSL filesystem boundary ([`05`](./05-host-setup.md) §6.3). Download once,
record the exact repo revision in the host's `.env`, and never pull weights during a deploy
([`delivery-plan.md`](./delivery-plan.md) §4). Start the download during M0 — it is bandwidth, not
attention, and it parallelises with everything else.

### 13. The memory-profile problem, and how it is handled

The fast-tier `.wslconfig` caps the guest at **48 GB**. The deep tier needs roughly **130 GB** of
guest RAM for its experts. Both statements are correct and they cannot both hold at once.

Resolution: **two `.wslconfig` profiles**, switched deliberately, never automatically.

```
  fast-tier profile   processors=8    memory=48GB    default. Modelling runs unaffected
  deep-tier profile   processors=24   memory=180GB   only while NO modelling job is running
```

```powershell
# Switching. Note: this restarts WSL2 and therefore every container on .226.
Copy-Item C:\Users\<you>\wslconfig-deep.ini  C:\Users\<you>\.wslconfig -Force
wsl --shutdown
wsl -d Ubuntu-24.04 --exec /bin/true
wsl -d Ubuntu-24.04 -- free -g          # verify ~180 available before loading 130 GB of weights
```

Consequences to accept up front:

- **Switching profiles is disruptive.** Fast-tier serving on `.226` goes down for the length of a WSL
  restart plus a model load. Route `chat` and `coder` to `.87` first ([`06`](./06-model-gateway.md)
  §6), then switch.
- **Therefore the deep tier is a session, not a service.** It is brought up for a block of work and
  taken down, rather than sitting in the catalog permanently. That fits its intended use — a hard bug
  or a long document, not autocomplete.
- **Thread count is a guess until measured.** 24 processors is a starting point. Deep-tier decode is
  bandwidth-bound, so beyond the point where the memory controllers saturate, extra threads add
  contention rather than tokens. Sweep `-t` in §15 and use the measured optimum, which may well be
  lower than 24.

If M0 spike 7 shows the deep tier starving the modelling runs even in a dedicated window, the honest
outcome is that this profile never gets used and the platform tops out at the fast tier. Say so
plainly rather than shipping something nobody will wait for.

### 14. Running it

```bash
./build/bin/llama-server \
  -m /srv/ai-platform/models/Qwen3-235B-A22B-Q4_K_M.gguf \
  --n-cpu-moe 90 \
  -ngl 99 \
  -c 65536 \
  -t 24 \
  --host 0.0.0.0 --port 8081 \
  --api-key "$VLLM_226_KEY" \
  --no-mmap
```

| Flag | Meaning | How to choose it |
|---|---|---|
| `--n-cpu-moe N` | How many MoE layers keep their experts in **system RAM** | **The flag that matters.** Raise it until the model fits in VRAM budget; lower it until it does not. Sweep it (§15) — the optimum is the smallest value that still fits, since every layer left on the GPU is faster |
| `-ngl 99` | Offload all layers to GPU — attention and shared weights land there; `--n-cpu-moe` claws the experts back | Used together, not as alternatives |
| `-c 65536` | Context. KV cache is on the GPU, so this competes with the 24 GB directly | Start at 32k, raise while VRAM allows. `-ctk`/`-ctv` quantised KV types buy more context at some quality cost — verify support in your build |
| `-t 24` | CPU threads for expert matmuls | Sweep. Bandwidth-bound work saturates before the core count does |
| `--no-mmap` | Read weights into RAM rather than memory-mapping the file | With ~180 GB available, resident is more predictable than page-cache behaviour. If start-up RAM pressure is a problem, drop it and let mmap page from the NVMe — measure both |
| `--api-key` | Same key discipline as vLLM ([`06`](./06-model-gateway.md) §8) | Do not leave this open on the LAN |

`ik_llama.cpp` adds fork-specific options (fused-MoE paths, run-time weight repacking, attention
buffer limits) that can matter a lot for throughput. **Their names and defaults differ from mainline
and change between commits** — take them from the README at your pinned commit, list the ones you
used in the shipped version of this doc, and record what each was worth in §15.

### 15. Benchmark before believing anything

No numbers are asserted here on purpose. Fill this in from your own hardware — this table *is* M0
spike 7's deliverable.

```bash
# Sweep the offload split and thread count
for ncm in 80 85 90 95; do
  for th in 8 16 24 32; do
    ./build/bin/llama-bench -m /srv/.../Qwen3-235B-A22B-Q4_K_M.gguf \
      --n-cpu-moe $ncm -ngl 99 -t $th -p 512 -n 128
  done
done
```

| `--n-cpu-moe` | `-t` | VRAM used | RSS | prompt tok/s | decode tok/s @ 8k | decode tok/s @ 64k | TTFT |
|---|---|---|---|---|---|---|---|
| 80 | 16 | | | | | | |
| 90 | 16 | | | | | | |
| 90 | 24 | | | | | | |
| 95 | 24 | | | | | | |

Then repeat **the whole table with a real modelling run executing concurrently**, and record the
modelling job's per-iteration time in both conditions. That second measurement is the one that
decides whether this tier exists at all.

Thresholds, from [`04-m0-spikes.md`](./04-m0-spikes.md) spike 7:

| Result | Meaning |
|---|---|
| >= 10 tok/s at 8k with RAM headroom left | Pass. Deep tier ships |
| Usable alone, but halves the modelling runs' throughput | Gated pass — **off-hours only**, enforced by the fleet controller |
| < 5 tok/s, or it starves the modelling runs | No deep tier. Remove `deep-slow` from the catalog and set expectations with the team |

### 16. The modelling-job gate

The GPU ladder does not apply here. The deep tier needs its own state machine, driven by whether the
box is doing its day job.

| Host state | Signal | Deep tier |
|---|---|---|
| **Modelling job running** | Known process names; sustained high CPU across many cores; large resident RAM | **Refused.** Gateway returns a clear message, no fallback ([`06`](./06-model-gateway.md) §6) |
| **Quiet, inside working hours** | No modelling process for ~15 min | **Available on request**, one request at a time, with a queue |
| **Off-hours window** | Configured schedule, e.g. after 18:00 and weekends | **Available**, and it is the window we advertise |
| **Deep session active** | The controller brought up the deep profile | Fast tier on `.226` is down; gateway serves `chat` from `.87` |

Detection is deliberately crude and conservative: a name-matched process list plus CPU and RAM
thresholds, with a bias toward false positives. **Getting this wrong in the "available" direction
breaks N6**, which is the requirement that decides whether the platform stays installed; getting it
wrong in the "refused" direction only annoys someone who wanted a slow model. Those costs are not
symmetric, so neither is the tuning.

Two more rules worth encoding:

- **A running deep session is never preempted mid-generation** — killing a four-minute answer at
  minute three wastes more than it saves. Instead, refuse *new* deep requests as soon as a modelling
  job appears, and let the current one finish.
- **Whoever runs the modelling jobs gets a veto**, exposed as one switch on the fleet dashboard
  alongside the per-host toggles from [`03`](./03-gpu-sharing-policy.md) §4.1. The social contract is
  the same; only the resource differs.

The controller implementing all of this is [`08-fleet-controller.md`](./08-fleet-controller.md).

### 17. Verify the deep tier

1. `llama-bench` numbers recorded in §15, alone and alongside a modelling run.
2. Modelling job's per-iteration time stays within noise of its ~48 min baseline while the deep tier
   is **idle** (N6). This is non-negotiable.
3. `deep-slow` answers through the gateway end to end, with `timeout: 1800`, and the stream survives
   Caddy — check the reverse-proxy timeouts as well as LiteLLM's.
4. With a modelling job running, `deep-slow` returns the human-readable refusal, **not** a fallback to
   a 4B model and **not** a 504.
5. Profile switch and switch-back both complete, and the fast tier returns on `.226` afterwards with
   no manual step.

---

## Reflect

**The fast tier is ordinary engineering; the interesting decisions are all in the flags.** Two of them
carry the weight: `--gpu-memory-utilization`, whose semantics decide whether the sharing policy is
safe, and `--kv-cache-dtype fp8`, which roughly doubles usable context on Ada for a small quality
cost. Both are worth an afternoon of measurement rather than an afternoon of reading.

**The deep tier is the part most likely not to survive contact with reality.** It is the only path to
near-frontier quality on hardware we own, and it is also the only component that competes with the
existing owner of `.226` for a resource that cannot be shared or yielded. We have designed it as a
gated session rather than a service precisely because we expect the gate to be closed a lot of the
time. If spike 7 comes back badly, deleting this half of the document is a legitimate outcome and a
cheap one — nothing else in the platform depends on it.

**The unresolved tension worth flagging to a reviewer:** [`03`](./03-gpu-sharing-policy.md) prescribes
a 48 GB WSL2 cap on `.226`, and [`02`](./02-hardware-and-fleet.md) prescribes a 130 GB deep tier on
the same host. The two-profile scheme in §13 reconciles them at the cost of a disruptive restart. If
that proves too disruptive in practice, the next option is not a bigger cap — it is a scheduled
nightly window where `.226` belongs to the deep tier and nothing else.

**Next:** [`08-fleet-controller.md`](./08-fleet-controller.md) — the service that drives the sleep,
wake and gating decisions described here.
