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

    # Allocate until the driver stops giving us real VRAM.
    #
    # "Until it raises OOM" is NOT the test, and assuming it was produced a
    # measured 226 GiB on a 24 GB card. On WSL2 the driver's system-memory
    # fallback silently spills allocations into host RAM rather than failing, so
    # the loop runs until it has eaten the machine. A host with 256 GB of RAM
    # will happily report ten times its VRAM.
    #
    # The authority is the driver's own free-memory counter: keep allocating only
    # while `mem_get_info` shows free VRAM actually dropping by roughly what we
    # asked for. The moment it stops dropping, we have left the card.
    blocks: list[object] = []
    fallback_detected = False
    hard_cap = int(total * 1.10)  # nothing legitimate can exceed the card

    while len(blocks) * CHUNK_BYTES < hard_cap:
        before, _ = torch.cuda.mem_get_info()
        try:
            blocks.append(torch.empty(CHUNK_BYTES, dtype=torch.uint8, device="cuda"))
        except (RuntimeError, torch.cuda.OutOfMemoryError):
            break
        after, _ = torch.cuda.mem_get_info()
        # Allow slack for the allocator's own bookkeeping, but a chunk that costs
        # the card almost nothing came from somewhere else.
        if (before - after) < CHUNK_BYTES // 2:
            blocks.pop()
            fallback_detected = True
            break

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
        "system_memory_fallback": fallback_detected,
    }


def verdict(r: dict[str, object]) -> str:
    alloc = float(r["actually_allocatable_gib"])  # type: ignore[arg-type]
    total = float(r["total_gib"])  # type: ignore[arg-type]
    if alloc > total:
        # Should now be unreachable, but if it ever fires the number is fiction
        # and must never reach the ladder.
        return "INVALID (allocated more than the card holds -- fallback not caught)"
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
    if r["system_memory_fallback"]:
        print(
            "\nNOTE: system-memory fallback was hit, and the probe stopped at the"
            "\ncard's real limit. The figure above is true VRAM."
            "\n"
            "\nIt also means CUDA on this host spills into system RAM rather than"
            "\nraising OOM, so an oversized model runs pathologically slowly instead"
            "\nof failing fast. Worth knowing before blaming the model."
        )

    if r["verdict"] == "HARD FAIL":
        print("\nSee docs/04-m0-spikes.md spike 1. Do not build the ladder on this.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(r, indent=2) + "\n")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
