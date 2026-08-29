# M0 spike scripts

Seven measurements that gate the whole project. Full rationale and pass/fail criteria are in
[`../docs/04-m0-spikes.md`](../docs/04-m0-spikes.md) — this is the operator's cheat sheet.

**Budget: 1–2 days.** Two of these can invalidate the model ladder outright, so run them before
building anything on top.

## Setup, on each host

> **Do not run a bare `uv sync` afterwards.** It is exact by default and will
> uninstall the ~2.5 GB CUDA PyTorch this pulls in. Use the command below, or
> `make setup-spikes`, which passes `--inexact`.

```bash
# once per host, inside WSL2 on .226 and .87; natively on .149
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <this repo> && cd ai-platform
uv sync --all-packages --group dev --group spikes --inexact
```

Inside WSL2, install **only the CUDA toolkit** — never a Linux NVIDIA driver. The driver passes
through from Windows and installing one in the guest breaks the passthrough chain.

## The runs

| # | Where | Command | Gate |
|---|---|---|---|
| 1 | all 3 | `uv run --group spikes scripts/spike_01_vram.py --json results/spike01-$(hostname).json` | >= 21 GiB allocatable on `.226` |
| 2 | `.226` | `uv run --group spikes scripts/spike_02_soak.py --hours 2` | 2 h, no hang or crash |
| 3 | `.149` | `uv run --group spikes scripts/spike_01_vram.py` — check `arch_list` | `sm_120` present |
| 4 | from `.87` | `./scripts/spike_04_network.sh 10.0.1.149` | < 5 ms, >= 500 Mbit/s |
| 5 | `.226` | `./scripts/spike_05_profile.sh` then start a real modelling run | peak VRAM bounded and known |
| 6 | `.226` | `uv run --group spikes scripts/spike_06_demotion.py --base http://10.0.0.226:8000` | VRAM free <= 10 s |
| 7 | `.226` | see [`04-m0-spikes.md`](../docs/04-m0-spikes.md) spike 7 | >= 10 tok/s at 8k |

Spike 4 needs `iperf3 -s` running on the target first. Spike 6 needs vLLM already serving a model in
dev mode.

## Order

**1 → 2 → 4 → 3** first: these gate M1. Then **5 → 6** before M2, since the sharing policy is tuned
against them. **7** before M4, and before the deep tier is promised to anyone.

Spike 5 is the one most likely to be skipped and the one whose absence hurts most — it is the only
evidence behind the ladder rungs, the settle delay and the headroom margin.

## Results

Everything writes to `results/`, which is committed deliberately — these measurements are evidence,
and several docs have blank tables waiting for exactly these numbers. Fill in the table at the bottom
of `04-m0-spikes.md` as you go.

## If something fails

Every spike has a documented fallback; none of them are dead ends. Check the relevant section of
`04-m0-spikes.md` before improvising, and record what you chose — a workaround nobody wrote down
becomes a mystery in three weeks.
