# Port Allocation — the authoritative table

> **This file wins.** Where a numbered doc disagrees with it, this table is correct and
> the doc is stale. Six conflicts existed across the design docs before this was written,
> because the docs were authored in parallel and each picked a plausible number.
>
> The compose files in [`../deploy/`](../deploy/) are generated against this table; if you
> change a port, change it here and in the compose file in the same commit.

---

## Why this exists

Ports are the one piece of configuration that every doc touches and no doc owns. `docs/05`
listed a port table, `docs/06`, `docs/07`, `docs/08` and `docs/14` each assumed their own,
and the root `.env.example` had two of its own again. None of them was wrong in isolation.
Together they had the MCP server colliding with Open WebUI on `.87`, and three different
numbers for the fleet agent.

One table, one owner. That is the entire fix.

---

## `.87` — the hub

| Port | Service | Published? | Notes |
|---|---|---|---|
| 80 / 443 | Caddy | yes | The only front door. TLS with an internal CA |
| 4000 | LiteLLM gateway | yes | OpenAI-compatible; every client points here |
| 5000 | Docker registry | yes | Local images, so pulls never leave the network |
| 5432 | Postgres + pgvector | yes | Platform state |
| 7997 | Infinity — embeddings | yes | GPU |
| 7998 | Infinity — reranker | yes | **CPU**, per docs/03 |
| 8002 | MCP tool server | yes | Host 8002 → container 8080. **The collision** — docs/14 published 8080, which Open WebUI already uses |
| 8090 | Fleet controller | yes | Dashboard + `/fleet/status`. Root `.env.example` said 9000, `docs/08` said 8088 |
| 8099 | Fleet agent | yes | Reports `nvidia-smi`. `docs/08` said 9101 and wrote its firewall rule for it |
| 8888 | SearXNG | yes | **The only container with an egress route** |
| 9380 | RAGFlow | yes | See ADR-0007; its own stack brings more |
| — | Open WebUI | **no** | Deliberately unpublished. Reached only through Caddy |

## `.226` — models

| Port | Service | Notes |
|---|---|---|
| 8000 | vLLM — top rung | The swappable rung |
| 8001 | vLLM — floor rung | **Undocumented anywhere before now.** `docs/07 §6` requires a second server so the ladder can sleep/wake between two rungs instead of cold-restarting, but no port table listed it |
| 8081 | `ik_llama.cpp` — deep tier | Root `.env.example` said 8080 |
| 8188 | ComfyUI | Image generation. Moved here from `.149`, which is deferred |
| 8099 | Fleet agent | |

## `.210` — overflow

| Port | Service | Notes |
|---|---|---|
| 8000 | vLLM — small chat | |
| 7997 | Infinity — embeddings replica | The GPU failover for the one service that must never die |
| 8099 | Fleet agent | |

## `.149` — deferred

Not deployed. If it is ever added back as a dedicated image host it takes 8188 and 8099,
and ComfyUI moves off `.226`.

---

## Firewall

Each Windows host needs **two** rules per port set, not one — confirmed on `.210` during M0:

1. `New-NetFirewallRule` — Windows Firewall
2. `New-NetFirewallHyperVRule` with the WSL `VMCreatorId` — the Hyper-V layer that actually
   gates WSL2 under mirrored networking

Layer 1 alone leaves every service unreachable *even from its own host*, while the rules
look correct in the UI. Recipe and diagnosis in [`05-host-setup.md`](./05-host-setup.md) §9.
Scope both to the internal subnets only — and **derive the prefix from each host
rather than assuming `/24`**. Ours are `/23`; a `/24` rule covers half the subnet
and looks correct doing it. `ip -4 addr | grep inet` on each host, and read the
network off the broadcast address.

The fleet agent port (8099) is the one most likely to be missed, because it is the only
port a host exposes *for the platform's own benefit* rather than for a user-facing service.
Miss it and that host reads as `UNKNOWN` forever — which, correctly, means the platform
never uses it.

---

## Changing a port

1. Edit this table.
2. Edit the compose file and the host's `.env.example`.
3. Update the firewall rule.
4. `grep -rn ':<old-port>' docs/ deploy/ .env.example` and fix every straggler.

Step 4 is the one that gets skipped, and it is why this file exists.
