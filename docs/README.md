# Self-hosted AI Platform — Design & Engineering Log

This folder is both the **design record** and the **build guide** for a fully self-hosted AI platform
running on three GPU workstations we own.

## The product, in one sentence

> A team-wide assistant that does what Claude and Claude Code do — chat, agentic coding in the
> terminal and in VS Code, retrieval over our own documents, web search, and PDF/PPT/image
> generation — on local open-weight models, with no paid components and no documents leaving our
> network.

## How to read this

Docs are numbered and written **per milestone**, as that milestone is built — so they describe what
actually shipped rather than what we guessed. Each follows the same rhythm:

1. **Concept** — what this piece is and why it's built this way.
2. **Build** — the steps and configuration, with reasoning.
3. **Reflect** — what we traded away, and what we'd revisit.

Read `00` through `04` before touching anything. They're the contract the rest of the work is
measured against.

## Map

| Doc | Milestone | What it covers |
|---|---|---|
| [`00-goals-and-constraints.md`](./00-goals-and-constraints.md) | — | Requirements with measurable acceptance criteria; explicit non-goals |
| [`01-architecture.md`](./01-architecture.md) | — | Component map, the three load-bearing decisions, what we build vs assemble |
| [`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) | — | The three hosts, memory budgets, model tiers, why each host has its role |
| [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) | — | How the platform shares GPUs with the people using these workstations |
| [`04-m0-spikes.md`](./04-m0-spikes.md) | **M0** | The measurements that gate everything. Run these first |
| `05-host-setup.md` | M1 | WSL2 on `.226`/`.87`, native Ubuntu on `.149`, CPU/RAM caps, networking, boot |
| `06-model-gateway.md` | M1 | LiteLLM catalog, routing, fallback |
| `07-inference-servers.md` | M1/M4 | vLLM config; `ik_llama.cpp` hybrid CPU offload for the deep tier |
| `08-fleet-controller.md` | M2 | The service that arbitrates GPU use |
| `09-coding-agents.md` | M3 | OpenCode, Cline/Roo wiring |
| `10-data-layer.md` | M5 | Postgres + pgvector schema, hybrid search |
| `11-ingestion.md` | M5 | Parsing, chunking, page provenance |
| `12-retrieval-and-rerank.md` | M5 | Hybrid search, reranking, the relevance gate |
| `13-rag-service-api.md` | M5 | The OpenAI-compatible contract |
| `14-mcp-tool-server.md` | M6 | One tool surface for every client |
| `15-generation-tools.md` | M6/M7 | PDF, PPTX, image |
| `16-web-search-and-egress.md` | M6 | SearXNG, and the egress boundary |
| `17-evaluation.md` | M8 | Eval set, recall@k, groundedness |
| `18-operations.md` | M8 | Backups, monitoring, runbook |
| [`delivery-plan.md`](./delivery-plan.md) | — | How this gets built and deployed: sequencing, per-host bring-up, release mechanics, rollback |
| [`tech-stack.md`](./tech-stack.md) | — | Every stack choice with tradeoffs, alternatives, and **licence gotchas** |
| [`adr/`](./adr/) | — | Architecture Decision Records — the *why* behind each choice |

## Milestones

| | Outcome |
|---|---|
| **M0** | Spikes: measure before committing. **Everything gates on this** |
| **M1** | Chat online — the team can log in and use it |
| **M2** | Coexistence — platform and user GPU jobs share machines safely |
| **M3** | Coding — OpenCode + Cline on local models |
| **M4** | Deep tier — near-frontier models via CPU offload |
| **M5** | RAG — grounded answers with citations |
| **M6** | Tools — web search, PDF, PPTX via MCP |
| **M7** | Image generation |
| **M8** | Hardening — SSO, backups, monitoring, egress proof |

## Ground rules

- **No paid components.** Open-weight models, open-source software.
- **No documents or code leave the network.** Search queries may. See
  [ADR-0004](./adr/0004-egress-policy.md).
- **The people at these workstations have priority.** The platform is a guest. See
  [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md).
- **Credentials never go in this repo.** Host passwords, API tokens and secrets live in a gitignored
  secrets file. If you find one committed, rotate it.

## Status

| Milestone | State |
|---|---|
| Design (00–04) | Written |
| M0 spikes | **Not started — do this next** |
| M1 onward | Blocked on M0 |
