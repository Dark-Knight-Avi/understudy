#!/usr/bin/env python3
"""M0 spike 2 -- does CUDA stay up for hours under WSL2 on an AMD host?

Run on .226. This is the Threadripper, and NVIDIA documents a CUDA/WSL2
cache-coherency fault on AMD Ryzen that can hang or crash CUDA applications.
If that is live here, we cannot build the platform on WSL2 on this box.

    PASS  2 hours of continuous load, no hang, no crash, no XID
    FAIL  see docs/04-m0-spikes.md spike 2 -- move serving to native Linux or
          demote .226 to deep-tier-only via ik_llama.cpp

Afterwards, also check the Windows event log and `dmesg` for XID errors.

Usage:
    uv run --group spikes scripts/spike_02_soak.py --hours 2 --log results/soak-226.csv
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from pathlib import Path
from types import FrameType

_stop = False


def _handle(_sig: int, _frm: FrameType | None) -> None:
    global _stop
    _stop = True
    print("\ninterrupted -- finishing cleanly", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--size", type=int, default=8192, help="matmul edge length")
    ap.add_argument("--log", type=Path, default=Path("results/soak.csv"))
    args = ap.parse_args()

    try:
        import torch
    except ImportError:
        sys.exit("torch is not installed. Run with: uv run --group spikes ...")
    if not torch.cuda.is_available():
        sys.exit("CUDA not visible. See spike 1.")

    signal.signal(signal.SIGINT, _handle)
    args.log.parent.mkdir(parents=True, exist_ok=True)

    a = torch.randn(args.size, args.size, device="cuda", dtype=torch.float16)
    deadline = time.time() + args.hours * 3600
    iters = 0
    last_report = 0.0
    stalls: list[float] = []

    with args.log.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["elapsed_s", "iters", "iters_per_s", "vram_used_gib", "max_step_s"])
        start = time.time()

        while time.time() < deadline and not _stop:
            t0 = time.time()
            # clamp keeps values bounded so this runs indefinitely without inf/nan
            a = (a @ a).clamp_(-1.0, 1.0)
            torch.cuda.synchronize()
            step = time.time() - t0
            stalls.append(step)
            iters += 1

            elapsed = time.time() - start
            if elapsed - last_report >= 10:
                used = torch.cuda.memory_reserved() / 2**30
                w.writerow(
                    [
                        round(elapsed, 1),
                        iters,
                        round(iters / elapsed, 2),
                        round(used, 2),
                        round(max(stalls), 3),
                    ]
                )
                fh.flush()
                print(
                    f"\r{elapsed / 60:6.1f} min  {iters:7d} iters  "
                    f"{iters / elapsed:6.1f}/s  worst step {max(stalls):.2f}s",
                    end="",
                    flush=True,
                )
                last_report = elapsed
                stalls.clear()

    total = time.time() - start
    print(f"\n\ncompleted {total / 60:.1f} min, {iters} iterations")
    print(f"log: {args.log}")
    if _stop:
        print("VERDICT  INCONCLUSIVE (interrupted before the full window)")
    elif total >= args.hours * 3600 * 0.99:
        print("VERDICT  PASS -- no hang or crash")
        print("Now check the Windows event log and `dmesg` for XID errors before calling it clean.")
    else:
        print("VERDICT  FAIL -- exited early")


if __name__ == "__main__":
    main()
