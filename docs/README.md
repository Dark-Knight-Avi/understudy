# Self-hosted AI Platform — Design & Engineering Log

This folder is both the **design record** and the **build guide** for a fully self-hosted AI platform
running on three GPU workstations we own.

## The product, in one sentence

> A team-wide assistant that does what Claude and Claude Code do — chat, agentic coding in the
> terminal and in VS Code, retrieval over our own documents, web search, and PDF/PPT/image
> generation — on local open-weight models, with no paid components and no documents leaving our
> network.

## How to read this

Each doc follows the same rhythm:

1. **Concept** — what this piece is and why it's built this way.
2. **Build** — the steps and configuration, with reasoning.
3. **Reflect** — what we traded away, and what we'd revisit.

Read `00` through `04` before touching anything — they're the contract the rest is measured against.
Docs `05`–`18` are **pre-build drafts**: written ahead of their milestone as build guides, to be
revised after each ships so they describe what actually shipped rather than what we planned.

## Map

| Doc | Milestone | What it covers |
|---|---|---|
| [`00-goals-and-constraints.md`](./00-goals-and-constraints.md) | — | Requirements with measurable acceptance criteria; explicit non-goals |
| [`01-architecture.md`](./01-architecture.md) | — | Component map, the three load-bearing decisions, what we build vs assemble |
| [`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) | — | The three hosts, memory budgets, model tiers, why each host has its role |
| [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) | — | How the platform shares GPUs with the people using these workstations |
| [`04-m0-spikes.md`](./04-m0-spikes.md) | **M0** | The measurements that gate everything. Run these first |
| [`05-host-setup.md`](./05-host-setup.md) | M1 | WSL2 on `.226`/`.87`, native Ubuntu on `.149`, caps, networking, boot |
| [`06-model-gateway.md`](./06-model-gateway.md) | M1 | LiteLLM catalog, routing, fallback, virtual keys |
| [`07-inference-servers.md`](./07-inference-servers.md) | M1/M4 | vLLM config; `ik_llama.cpp` hybrid CPU offload for the deep tier |
| [`08-fleet-controller.md`](./08-fleet-controller.md) | M2 | The service that arbitrates GPU use |
| [`09-coding-agents.md`](./09-coding-agents.md) | M3 | OpenCode, Cline/Roo, and the context-economics problem |
| [`10-data-layer.md`](./10-data-layer.md) | M5 | Postgres + pgvector schema, HNSW, hybrid search SQL |
| [`11-ingestion.md`](./11-ingestion.md) | M5 | Parsing, chunking, page provenance, idempotency |
| [`12-retrieval-and-rerank.md`](./12-retrieval-and-rerank.md) | M5 | RRF, reranking, the relevance gate |
| [`13-rag-service-api.md`](./13-rag-service-api.md) | M5 | The OpenAI-compatible contract and citations |
| [`14-mcp-tool-server.md`](./14-mcp-tool-server.md) | M6 | One tool surface for every client |
| [`15-generation-tools.md`](./15-generation-tools.md) | M6/M7 | PDF (Typst), PPTX (python-pptx), image (ComfyUI) |
| [`16-web-search-and-egress.md`](./16-web-search-and-egress.md) | M6 | SearXNG, and proving the egress boundary |
| [`17-evaluation.md`](./17-evaluation.md) | M8 | Eval set, recall@k, gate calibration, groundedness |
| [`18-operations.md`](./18-operations.md) | M8 | Backups, boot resilience, monitoring, runbook |
| [`delivery-plan.md`](./delivery-plan.md) | — | How this gets built and deployed: sequencing, per-host bring-up, release mechanics, rollback |
| [`ports.md`](./ports.md) | — | **Authoritative** port allocation. Wins over any numbered doc |
| [`tech-stack.md`](./tech-stack.md) | — | Every stack choice with tradeoffs, alternatives, and **licence gotchas** |
| [`adr/`](./adr/) | — | Architecture Decision Records — the *why* behind each choice |

## Milestones

| | Outcome | State |
|---|---|---|
| **M0** | Spikes: measure before committing. **Everything gates on this** | Not started — **do this next** |
| **M1** | Chat online — the team can log in and use it | Blocked on M0 |
| **M1.5** | RAGFlow spike — adopt retrieval or build it ([ADR-0007](./adr/0007-adopt-ragflow-for-retrieval.md)) | Blocked on M1 |
| **M2** | Coexistence — platform and user GPU jobs share machines safely | Blocked on M0/M1 |
| **M3** | Coding — OpenCode + Cline on local models | Blocked |
| **M4** | Deep tier — near-frontier models via CPU offload | Blocked on M0 spike 7 |
| **M5** | RAG — integrate RAGFlow, gate wrapper if needed | Blocked on M1.5 |
| **M6** | Tools — web search, PDF, PPTX, image via MCP | Blocked |
| **M7** | Hardening — SSO, backups, monitoring, egress proof | Blocked |

Design docs for every milestone are written. **No code exists yet** — deliberately, because M0's
measurements can still change the model ladder, the WSL2 layout, and whether the deep tier is viable
at all.

## Start here

1. Run [`04-m0-spikes.md`](./04-m0-spikes.md) — 1–2 days, seven measurements with pass/fail gates.
2. **Install uv inside WSL2** on `.226`, `.87` and `.210` so the spikes can run there.
3. **Start the ~50-question eval set** ([`17-evaluation.md`](./17-evaluation.md)). It must be authored
   before the retriever exists, or it only measures what already worked.
4. **Rotate the three host passwords** — they were shared in plaintext during design.

## Ground rules

- **No paid components.** Open-weight models, open-source software.
- **No documents or code leave the network.** Search queries may. See
  [ADR-0004](./adr/0004-egress-policy.md) and [`16-web-search-and-egress.md`](./16-web-search-and-egress.md).
- **The people at these workstations have priority.** The platform is a guest, and must be removable
  with one command per host. See [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md).
- **Credentials never go in this repo.** `.env` is gitignored; `.env.example` holds placeholders only.
