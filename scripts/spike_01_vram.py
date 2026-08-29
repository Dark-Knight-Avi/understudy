#!/usr/bin/env python3
"""M0 spike 1 -- how much VRAM can a process actually allocate?

Run on every host. On .226 and .87 this runs INSIDE WSL2; that is the whole point.

Why this exists: WSL2 is reported to impose a large CUDA driver memory overhead
compared to native Linux. The loudest reports are Blackwell-specific, but it is
unverified on Ada, and if it is real here then every rung of the model ladder in
docs/03-gpu-sharing-policy.md shifts down.

    PASS       allocatable >= 21 GiB on .226
    SOFT FAIL  20-21 GiB  -> proceed, shift ladder rungs down by the shortfall
    HARD FAIL  < 20 GiB   -> see docs/04-m0-spikes.md spike 1 for the fallbacks

Usage:
    uv run --group spikes scripts/spike_01_vram.py [--json results/spike01-226.json]
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

CHUNK_BYTES = 256 * 2**20  # 256 MiB


def probe() -> dict[str, object]:
    try:
        import torch
    except ImportError:
        sys.exit("torch is not installed. Run with: uv run --group spikes ...")

    if not torch.cuda.is_available():
        sys.exit(
            "CUDA is not visible.\n"
            "Inside WSL2, install ONLY the CUDA toolkit -- never a Linux NVIDIA driver. "
            "The driver passes through from Windows, and installing one in the guest "
            "breaks the passthrough chain."
        )

    name = torch.cuda.get_device_name(0)
    capability = torch.cuda.get_device_capability(0)
    free, total = torch.cuda.mem_get_info()

    # Allocate until the driver refuses. This is the number that matters: what the
    # runtime reports as free and what it will actually hand over are not the same.
    blocks: list[object] = []
    try:
        while True:
            blocks.append(torch.empty(CHUNK_BYTES, dtype=torch.uint8, device="cuda"))
    except (RuntimeError, torch.cuda.OutOfMemoryError):
        pass

    allocated = len(blocks) * CHUNK_BYTES
    del blocks
    torch.cuda.empty_cache()

    gib = 2**30
    return {
        "host": platform.node(),
        "gpu": name,
        "compute_capability": f"{capability[0]}.{capability[1]}",
        "torch_version": torch.version.__version__,
        "cuda_version": torch.version.cuda,
        "arch_list": torch.cuda.get_arch_list(),
        "total_gib": round(total / gib, 2),
        "reported_free_gib": round(free / gib, 2),
        "actually_allocatable_gib": round(allocated / gib, 2),
        "overhead_gib": round((total - allocated) / gib, 2),
    }


def verdict(r: dict[str, object]) -> str:
    alloc = float(r["actually_allocatable_gib"])  # type: ignore[arg-type]
    total = float(r["total_gib"])  # type: ignore[arg-type]
    # Thresholds in docs/04 are written for the 24 GB card; scale for the others.
    if total >= 22:  # .226, RTX 4090
        if alloc >= 21:
            return "PASS"
        return "SOFT FAIL" if alloc >= 20 else "HARD FAIL"
    # For the 12 GB and 16 GB cards, judge by proportional overhead instead.
    return "PASS" if (total - alloc) <= 2.0 else "INVESTIGATE"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, help="also write results here")
    args = ap.parse_args()

    r = probe()
    r["verdict"] = verdict(r)

    print(f"host                 {r['host']}")
    print(f"gpu                  {r['gpu']}  (CC {r['compute_capability']})")
    print(f"torch / cuda         {r['torch_version']} / {r['cuda_version']}")
    print(f"total VRAM           {r['total_gib']} GiB")
    print(f"reported free        {r['reported_free_gib']} GiB")
    print(f"ACTUALLY ALLOCATABLE {r['actually_allocatable_gib']} GiB")
    print(f"overhead             {r['overhead_gib']} GiB")
    print(f"VERDICT              {r['verdict']}")

    if r["verdict"] == "HARD FAIL":
        print("\nSee docs/04-m0-spikes.md spike 1. Do not build the ladder on this.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(r, indent=2) + "\n")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
