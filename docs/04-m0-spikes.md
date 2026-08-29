# 04 — M0 Spikes

> **Everything gates on this document.** Seven measurements, each with a pass/fail criterion. Nothing
> in M1 onward gets built until these numbers exist, because several of them can invalidate the
> design outright.
>
> Budget: 1–2 days. Record every result in the table at the bottom.

---

## Why spike at all

Three assumptions in this design are load-bearing and unverified:

1. That WSL2 does not eat a large fraction of the 4090's VRAM (a documented problem on some
   architectures).
2. That CUDA is stable under WSL2 on an **AMD** host (NVIDIA documents a cache-coherency fault on
   Ryzen).
3. That a 235B-class MoE runs at a usable speed on `.226`'s CPU+RAM.

If (1) or (2) fail, the model ladder shifts down and the platform gets meaningfully weaker. If (3)
fails, the deep tier — the only thing that gets us near frontier quality — does not exist. Better to
know in two days than in two months.

---

## Spike 1 — `.226` usable VRAM under WSL2

**Question:** how much of the 4090's 24 GB can a process actually allocate from inside WSL2?

**Setup note:** inside WSL2, install **only the CUDA toolkit**. Do *not* install a Linux NVIDIA
driver — the driver comes through from Windows, and installing one inside the guest breaks the
passthrough.

```bash
# 1. Confirm the GPU is visible and note driver + CUDA versions
nvidia-smi
nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free,driver_version --format=csv

# 2. Measure what can actually be allocated
python3 - <<'PY'
import torch
assert torch.cuda.is_available(), "CUDA not visible inside WSL2"
free, total = torch.cuda.mem_get_info()
print(f"reported free: {free/2**30:.2f} GiB of {total/2**30:.2f} GiB")

blocks, step = [], 256 * 2**20          # 256 MiB at a time
try:
    while True:
        blocks.append(torch.empty(step, dtype=torch.uint8, device="cuda"))
except RuntimeError:
    pass
print(f"ACTUALLY ALLOCATED: {len(blocks)*step/2**30:.2f} GiB")
PY
```

**Pass:** allocatable >= 21 GiB.
**Soft fail (20–21 GiB):** proceed, but shift every ladder rung down by the shortfall.
**Hard fail (< 20 GiB):** the WSL2 overhead problem is real on Ada too. Options, in order: run the
platform's `.226` services under native Linux on a spare disk; drop to a 14B top rung permanently;
move fast-tier serving to `.149`.

---

## Spike 2 — `.226` CUDA stability soak (the AMD risk)

**Question:** does CUDA under WSL2 stay up for hours on a Threadripper?

```bash
# Continuous load for 2 hours; watch for hangs, XID errors, or falling utilisation
python3 - <<'PY' &
import torch, time
a = torch.randn(8192, 8192, device="cuda", dtype=torch.float16)
t0 = time.time()
while time.time() - t0 < 7200:
    a = (a @ a).clamp_(-1, 1)
    torch.cuda.synchronize()
print("soak completed cleanly")
PY

# In another shell, log every 10 s
nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used,temperature.gpu \
  --format=csv -l 10 | tee ~/soak-226.csv
```

Also check the Windows event log and `dmesg` afterwards for XID errors.

**Pass:** 2 hours, no hang, no XID, no crash.
**Fail:** the AMD cache-coherency issue is live. Do not build on WSL2 here — move `.226` serving to
native Linux, or accept `.149` (native Ubuntu) as the primary serving host and demote `.226` to
deep-tier-only via `ik_llama.cpp`, which is far less CUDA-intensive.

---

## Spike 3 — `.149`: native Ubuntu and Blackwell support

`.149` has **no WSL installed**, and the reported WSL2 CUDA memory-overhead problem is specific to
Blackwell / `sm_120` — which is exactly this GPU. **Install native Ubuntu and skip the whole class of
problem.** Confirm first that you are allowed to repartition this machine and that it stays powered
on (see Open Items).

```bash
nvidia-smi
python3 -c "import torch; print(torch.cuda.get_device_capability())"   # expect (12, 0)

# Verify the toolchain actually ships sm_120 kernels
python3 -c "import torch; print(torch.cuda.get_arch_list())"           # expect sm_120 present
```

**Pass:** `sm_120` present in the arch list, and a trivial matmul runs.
**Fail:** your PyTorch/vLLM/ComfyUI build predates Blackwell support. Upgrade to builds that include
it — do not attempt to work around this with `TORCH_CUDA_ARCH_LIST` hacks.

---

## Spike 4 — Network: can `.149` talk to `.32.x`?

The whole three-host design assumes it can. Verify before designing around it.

```bash
# From .87 (the hub) to .149
ping -c 10 10.0.1.149
traceroute 10.0.1.149

# Bandwidth, both directions
iperf3 -s                      # on .149
iperf3 -c 10.0.1.149 -t 30   # from .87
iperf3 -c 10.0.1.149 -t 30 -R
```

**Pass:** reachable, latency < 5 ms, >= 500 Mbit/s both ways.
**Degraded:** reachable but slow — keep `.149` for image generation only (large payloads are rare and
async); do not put anything latency-sensitive there.
**Fail:** unreachable or firewalled — `.149` drops out of the fleet and becomes a manually-used image
box. Update `02-hardware-and-fleet.md` accordingly.

---

## Spike 5 — Profile the simulation workloads

**This sets the ladder rungs, the settle delay and the headroom margin.** It is the input the sharing
policy is tuned against, and currently the biggest unknown.

```bash
# Log GPU memory per process while a real modelling run executes
nvidia-smi --query-compute-apps=timestamp,pid,process_name,used_gpu_memory \
  --format=csv -l 5 | tee ~/workload-profile.csv

# System RAM and bandwidth pressure at the same time
vmstat 5 | tee ~/workload-vmstat.csv
```

Capture, for each tool that touches the GPU:

| Question | Why it matters |
|---|---|
| Peak VRAM | Sets whether the ladder's headroom is enough |
| How fast it ramps to peak | Sets the 60 s settle delay |
| Session length | Confirms the "30–60 min block" assumption |
| Frequency per day | Confirms contention is occasional, not constant |
| Peak system RAM | Decides whether the deep tier can coexist at all |
| Does it survive a transient allocation failure? | Decides how bad a missed preemption is |

**Pass:** contention is occasional and peak VRAM is bounded and known.
**Fail (near-continuous use):** the "big by default, yield fast" policy inverts — go back to a
permanent reservation, and expect the platform to run at a lower rung most of the time.

---

## Spike 6 — Demotion latency

**Question:** how long between "user flips the toggle" and "VRAM is actually free"? This number sets
the wrapper's wait and is what users will judge the system by.

```bash
# Start vLLM with sleep mode available (dev endpoints must be enabled;
# confirm the flag/env name against YOUR vLLM version before relying on it)
VLLM_SERVER_DEV_MODE=1 vllm serve <model> --gpu-memory-utilization 0.9 &

# Time the release
time curl -X POST localhost:8000/sleep -d '{"level": 1}'
nvidia-smi --query-gpu=memory.used --format=csv

# And the wake
time curl -X POST localhost:8000/wake_up
```

**Pass:** VRAM free within ~10 s; wake within ~15 s.
**Degraded (> 30 s):** sleep mode is not viable — fall back to stopping and restarting the server,
and raise the wrapper's wait accordingly. Say the real number in the UI.

Record the exact endpoint names and semantics for your version in
[`07-inference-servers.md`](./07-inference-servers.md); these have changed between releases.

---

## Spike 7 — Deep-tier feasibility (the important one)

**Question:** does a 235B-class MoE run at a usable speed on `.226`, and can it coexist with the
modelling runs?

```bash
# Build the MoE-optimised fork
git clone https://github.com/ikawrakow/ik_llama.cpp && cd ik_llama.cpp
cmake -B build -DGGML_CUDA=ON && cmake --build build -j

# Load a 235B-class MoE with experts on CPU, attention on the GPU.
# --n-cpu-moe is the key flag: how many MoE layers live in system RAM.
./build/bin/llama-bench -m /models/Qwen3-235B-A22B-Q4_K_M.gguf \
  --n-cpu-moe 90 -ngl 99 -p 512 -n 128

# Then a real run at long context
./build/bin/llama-server -m /models/Qwen3-235B-A22B-Q4_K_M.gguf \
  --n-cpu-moe 90 -ngl 99 -c 65536 --host 0.0.0.0 --port 8080
```

Measure and record:
- tokens/sec at 8k and at 64k context
- time to first token
- resident system RAM
- **the same numbers with a modelling run executing concurrently**

**Pass:** >= 10 tok/s at 8k with RAM headroom left for the modelling runs.
**Gated pass:** usable alone, but halves the modelling runs' throughput — deep tier becomes
**off-hours only**, enforced by the fleet controller.
**Fail (< 5 tok/s, or it starves the modelling runs):** no deep tier. The platform tops out at the
fast tier, and expectations with the team must be set accordingly — say so plainly rather than
shipping something nobody wants to wait for.

---

## Results table — fill this in

| # | Spike | Metric | Target | Measured | Verdict |
|---|---|---|---|---|---|
| 1 | `.226` usable VRAM | GiB allocatable | >= 21 | | |
| 2 | `.226` CUDA soak | 2 h clean | pass | | |
| 3 | `.149` Blackwell | `sm_120` present | pass | | |
| 4 | Cross-subnet link | Mbit/s, latency | >= 500, < 5 ms | | |
| 5 | Workload peak VRAM | GB | bounded + known | | |
| 5b | Workload frequency | blocks/day | occasional | | |
| 6 | Demotion latency | seconds | <= 10 | | |
| 7 | Deep tier @ 8k | tok/s | >= 10 | | |
| 7b | Deep tier vs modelling | modelling slowdown | < 10% | | |

---

## Gate

Proceed to M1 when spikes 1–4 pass or have an agreed workaround recorded here. Spikes 5–7 shape
later milestones but do not block M1:

- **Spike 5** must complete before M2 (the sharing policy is tuned against it).
- **Spikes 6** must complete before M2 (the toggle's wait depends on it).
- **Spike 7** must complete before M4, and before the deep tier is mentioned to anyone.

---

## Reflect

The value of M0 is not the numbers, it is the **cheapness of being wrong**. Two days here can
invalidate a design assumption that would otherwise be discovered in week six, after the team has
been told the platform is coming. Every spike above is written so that failing it produces a concrete
alternative rather than a dead end.

**Next:** M1 — [`06-model-gateway.md`](./06-model-gateway.md) and
[`07-inference-servers.md`](./07-inference-servers.md), written as they are built.
