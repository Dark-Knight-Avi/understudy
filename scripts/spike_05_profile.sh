#!/usr/bin/env bash
# M0 spike 5 -- profile the long-running simulation workloads.
#
# This is the input the whole GPU sharing policy is tuned against, and it is
# currently the biggest unknown in the project. Start it BEFORE launching a real
# modelling run, leave it running for the whole job, then read the summary.
#
# What we need out of this (docs/04-m0-spikes.md spike 5):
#   - peak VRAM               -> sets whether the ladder headroom is enough
#   - how fast it ramps       -> sets the 60 s settle delay
#   - session length          -> confirms the "30-60 min block" assumption
#   - peak system RAM         -> decides whether the deep tier can coexist at all
#
#   ./scripts/spike_05_profile.sh              # runs until Ctrl-C
#   ./scripts/spike_05_profile.sh 3600         # or a fixed number of seconds

set -uo pipefail

DUR="${1:-0}"                       # 0 = until Ctrl-C
OUT_DIR="${OUT_DIR:-results}"
STAMP="$(date +%Y%m%d-%H%M%S)"
GPU_CSV="$OUT_DIR/spike05-gpu-$STAMP.csv"
SYS_CSV="$OUT_DIR/spike05-sys-$STAMP.csv"
mkdir -p "$OUT_DIR"

echo "profiling -> $GPU_CSV"
echo "            $SYS_CSV"
echo "start your modelling run now. Ctrl-C when it finishes."
echo

# Per-process GPU memory. --query-compute-apps only lists processes actually
# holding VRAM, which is exactly what the fleet controller will key on.
nvidia-smi --query-compute-apps=timestamp,pid,process_name,used_gpu_memory \
           --format=csv -l 5 > "$GPU_CSV" &
GPU_PID=$!

# Whole-GPU totals plus system memory, so we can see the ramp.
{
  echo "timestamp,gpu_used_mib,gpu_total_mib,gpu_util_pct,ram_used_mib,ram_total_mib"
  while true; do
    read -r used total util < <(nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu \
                                --format=csv,noheader,nounits | tr -d ' ' | tr ',' ' ')
    read -r rt ru < <(free -m | awk '/^Mem:/{print $2, $3}')
    echo "$(date -Is),$used,$total,$util,$ru,$rt"
    sleep 5
  done
} > "$SYS_CSV" &
SYS_PID=$!

cleanup() {
  kill "$GPU_PID" "$SYS_PID" 2>/dev/null
  wait "$GPU_PID" "$SYS_PID" 2>/dev/null
  echo
  echo "=============================================================="
  echo "SUMMARY"
  echo "=============================================================="
  if [[ -s "$SYS_CSV" ]]; then
    awk -F, 'NR>1 {
        if ($2+0 > peakg) peakg = $2+0
        if ($5+0 > peakr) peakr = $5+0
        n++
      }
      END {
        if (n == 0) { print "no samples captured"; exit }
        printf "samples              %d  (~%.1f min)\n", n, n*5/60
        printf "peak GPU memory      %.1f GiB\n", peakg/1024
        printf "peak system RAM      %.1f GiB\n", peakr/1024
      }' "$SYS_CSV"
    echo
    echo "Ramp -- first 2 minutes of GPU use (this sets the settle delay):"
    awk -F, 'NR>1 && NR<=25 {printf "  %s  %.1f GiB\n", $1, $2/1024}' "$SYS_CSV"
  fi
  echo
  echo "Record peak VRAM, ramp time, session length and peak RAM in"
  echo "docs/04-m0-spikes.md, then re-check the ladder rungs in 03-gpu-sharing-policy.md."
  exit 0
}
trap cleanup INT TERM

if [[ "$DUR" -gt 0 ]]; then sleep "$DUR"; cleanup; else wait; fi
