#!/usr/bin/env bash
# M0 spike 4 -- can .149 talk to .32.x, and how fast?
#
# The three-host design assumes it can. .149 is on a different subnet and a
# different floor. If this fails, .149 drops out of the fleet and becomes a
# manually-used image box -- see docs/04-m0-spikes.md spike 4.
#
#   PASS      reachable, latency < 5 ms, >= 500 Mbit/s both directions
#   DEGRADED  reachable but slow -> image generation only, nothing latency-sensitive
#   FAIL      unreachable or firewalled
#
# Run FROM .87 (the hub). Start `iperf3 -s` on the target first.
#
#   ./scripts/spike_04_network.sh 10.0.1.149

set -uo pipefail

TARGET="${1:-10.0.1.149}"
OUT="${2:-results/spike04-network-${TARGET//./_}.txt}"
mkdir -p "$(dirname "$OUT")"

{
  echo "M0 spike 4 -- network to $TARGET"
  echo "from $(hostname) at $(date -Is)"
  echo

  echo "--- reachability ---"
  # ICMP is blocked on some of these hosts, so a failed ping is NOT a failed
  # spike. TCP is the authority; ping is only here for the latency number.
  if ping -c 10 -W 2 "$TARGET"; then
    echo "PING: ok"
  else
    echo "PING: no reply -- ICMP is likely filtered. Not a failure on its own;"
    echo "      judge reachability by the TCP probe below."
  fi
  echo
  echo "--- TCP reachability (authoritative) ---"
  TCP_OK=0
  for p in 3389 445 22; do
    if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$TARGET/$p" 2>/dev/null; then
      echo "  $p open -- host is reachable"
      TCP_OK=1
      break
    fi
  done
  [[ $TCP_OK -eq 1 ]] || echo "  no common port answered -- possible genuine FAIL, check firewalls"
  echo

  echo "--- route ---"
  command -v traceroute >/dev/null && traceroute -w 2 -m 10 "$TARGET" || echo "traceroute not installed"
  echo

  echo "--- bandwidth (needs 'iperf3 -s' running on the target) ---"
  if command -v iperf3 >/dev/null; then
    echo "> forward"
    iperf3 -c "$TARGET" -t 30 -f m || echo "iperf3 forward FAILED (is the server running?)"
    echo "> reverse"
    iperf3 -c "$TARGET" -t 30 -f m -R || echo "iperf3 reverse FAILED"
  else
    echo "iperf3 not installed: sudo apt install -y iperf3"
  fi
  echo

  echo "--- service ports we will actually need ---"
  # gateway, ragflow, fleet controller, comfyui, vllm
  for p in 4000 8188 8000 9000 5432; do
    timeout 3 bash -c "cat < /dev/null > /dev/tcp/$TARGET/$p" 2>/dev/null \
      && echo "  $p open" || echo "  $p closed/filtered"
  done

  echo
  echo "Record the verdict in docs/04-m0-spikes.md."
} 2>&1 | tee "$OUT"

echo
echo "wrote $OUT"
