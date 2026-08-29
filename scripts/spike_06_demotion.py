#!/usr/bin/env python3
"""M0 spike 6 -- how long from "release the GPU" to "VRAM is actually free"?

This number sets the wait in the toggle and the gpu-run wrapper, and it is what
users will judge the sharing policy by. If it is slow, say the real number in
the UI rather than pretending.

    PASS      VRAM free within ~10 s; wake within ~15 s
    DEGRADED  > 30 s -> sleep mode is not viable; fall back to stop/restart and
              raise the wrapper's wait accordingly

IMPORTANT: vLLM's sleep endpoints and their level semantics have moved between
releases, and they usually require the server to be started in dev mode. Verify
both against YOUR version before trusting this, and record what you find in
docs/07-inference-servers.md.

    # on .226, with a model loaded:
    VLLM_SERVER_DEV_MODE=1 vllm serve <model> --gpu-memory-utilization 0.9
    uv run --group spikes scripts/spike_06_demotion.py --base http://10.0.0.226:8000
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def vram_used_mib() -> int:
    """Whole-GPU used memory, as the fleet controller will see it."""
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip().splitlines()[0])


def wait_until(pred, timeout: float, poll: float = 0.25) -> float | None:
    """Seconds until pred() is true, or None on timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return time.time() - t0
        time.sleep(poll)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:8000", help="vLLM base URL")
    ap.add_argument("--level", type=int, default=1, help="sleep level (1 = offload to RAM)")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    try:
        import httpx
    except ImportError:
        sys.exit("httpx is not installed. Run with: uv run --group spikes ...")

    baseline = vram_used_mib()
    print(f"VRAM before sleep      {baseline / 1024:.2f} GiB")
    if baseline < 2048:
        sys.exit("Less than 2 GiB in use -- is a model actually loaded? Nothing to measure.")

    # Anything under ~15% of the baseline counts as released.
    released_floor = max(512, int(baseline * 0.15))

    with httpx.Client(timeout=args.timeout) as c:
        t0 = time.time()
        try:
            r = c.post(f"{args.base}/sleep", json={"level": args.level})
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 -- endpoint name varies by version
            sys.exit(
                f"POST {args.base}/sleep failed: {e}\n"
                "The endpoint name and dev-mode requirement vary by vLLM version. "
                "Check your version's docs and update this script and 07-inference-servers.md."
            )
        ack = time.time() - t0
        freed = wait_until(lambda: vram_used_mib() <= released_floor, args.timeout)

        print(f"sleep acknowledged in  {ack:.2f} s")
        print(f"VRAM after sleep       {vram_used_mib() / 1024:.2f} GiB")
        print(f"time to actually free  {freed if freed is None else round(freed, 2)} s")

        t1 = time.time()
        try:
            c.post(f"{args.base}/wake_up").raise_for_status()
        except Exception as e:  # noqa: BLE001
            sys.exit(f"POST {args.base}/wake_up failed: {e}")
        woke = wait_until(lambda: vram_used_mib() >= baseline * 0.8, args.timeout)
        wake_total = time.time() - t1

    verdict = (
        "FAIL (never released)"
        if freed is None
        else "PASS"
        if freed <= 10
        else "ACCEPTABLE"
        if freed <= 30
        else "DEGRADED -- sleep mode not viable, fall back to stop/restart"
    )

    result = {
        "baseline_gib": round(baseline / 1024, 2),
        "sleep_ack_s": round(ack, 2),
        "time_to_free_s": None if freed is None else round(freed, 2),
        "wake_total_s": round(wake_total, 2),
        "wake_to_ready_s": None if woke is None else round(woke, 2),
        "verdict": verdict,
    }
    print(f"wake round-trip        {result['wake_total_s']} s")
    print(f"VERDICT                {verdict}")
    print("\nPut this number in the dashboard's status line -- users should not have to guess.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
