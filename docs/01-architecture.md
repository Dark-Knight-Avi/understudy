# 01 — Architecture

> The component map, and the three decisions everything else follows from. Read
> [`00-goals-and-constraints.md`](./00-goals-and-constraints.md) first.

---

## 1. The three load-bearing decisions

### D1 — Partition by service; do not cluster

We have three GPUs across three machines. The instinct is to pool them into one big model. Don't.

Tensor- and pipeline-parallel inference move activations between devices on every layer, which needs
InfiniBand-class interconnect. Over 1 GbE it is not slow-but-workable, it is unusable. Instead each
host runs the services its hardware suits, and a gateway makes the fleet look like one model catalog.

A side benefit worth naming: no single point of failure for chat. When one box is claimed by the
person sitting at it, the gateway routes to another.

Recorded as [ADR-0001](./adr/0001-partition-by-service.md).

### D2 — The platform is a guest that only uses the empty room

These are workstations people use daily, not servers. The platform runs at full size when a card is
idle, releases it entirely within seconds when someone claims it, then re-enters whatever VRAM that
person's job leaves unused. No fixed reservation, no cap on the user.

This is the whole of [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md), and it is the
requirement most likely to decide whether the platform survives contact with its users.

### D3 — Build three things; assemble the rest

Almost every capability on the list already exists as mature open-source software. What does not
exist is *our* retrieval over *our* documents, one tool surface shared by every client, and an
arbiter that keeps the platform out of the way.

| We build | We assemble |
|---|---|
| RAG service | LiteLLM (gateway) |
| MCP tool server | Open WebUI (chat) |
| Fleet controller | OpenCode (terminal agent) |
| | Cline / Roo Code (VS Code) |
| | vLLM, `ik_llama.cpp` (inference) |
| | SearXNG (search) |
| | ComfyUI (image) |
| | Typst, python-pptx (renderers) |
| | Postgres + pgvector |

Recorded as [ADR-0002](./adr/0002-assemble-vs-build.md).

**The MCP server is the lever.** Open WebUI, OpenCode and Cline all speak the Model Context Protocol.
One tool server therefore makes web search, document RAG, PDF, PPTX and image generation appear in
the chat UI, the terminal and the editor simultaneously. Build once, available everywhere — this is
what makes F10 achievable rather than a three-way reimplementation.

---

## 2. Component map

```
  Clients — all off the shelf, all pointed at one URL
    Open WebUI ......... chat, accounts, history      }
    OpenCode ........... terminal agent               }  ==>  LiteLLM Gateway  (.87)
    Cline / Roo Code ... VS Code extension            }       one OpenAI-compatible API
    Aider / OpenHands .. optional                     }       catalog / routing / fallback
                                                                    |
                                                      Fleet controller (.87)
                                                 toggle / preemption / ladder / status
                                                                    |
              +-----------------------------------------------------+--------------------------+
              v                                                     v                          v
       .226  RTX 4090 24 GB                              .87  RTX 4070 12 GB          .149  RTX 5080 16 GB
       -- fast tier, GPU-resident                        -- always-on small models     -- image generation
          Qwen3-Coder-30B-A3B  ~17 GB                       embeddings      ~1.2 GB       FLUX.1-schnell ~12 GB
          ladder: 14B -> 8B -> 4B -> off                    small chat      ~5 GB         SD3.5-medium ~6 GB
       -- deep tier, GPU + 256 GB RAM
          Qwen3-235B-A22B Q4   ~130 GB RAM               CPU side (128 GB, 24 cores):
          via ik_llama.cpp expert offload                  reranker (bge-reranker-v2-m3)
                                                           Postgres 17 + pgvector
                                                           LiteLLM / Open WebUI / Caddy / SearXNG
                                                           +------------------------------+
                                                           | RAG service       WE BUILD   |
                                                           | MCP tool server   WE BUILD   |
                                                           | Fleet controller  WE BUILD   |
                                                           +------------------------------+
```

Every component speaks an OpenAI-compatible HTTP API or MCP. Nothing is coupled to a vendor SDK, so
any piece can be replaced without touching the others.

---

## 3. Component responsibilities

| Component | Owns | Does not own |
|---|---|---|
| **LiteLLM gateway** | The model catalog; routing a model name to a backend; fallback when a host is claimed | Prompting, retrieval, tools |
| **Fleet controller** | GPU arbitration: the toggle, preemption, the model ladder, sleep/wake, telling the gateway what is available | Serving tokens |
| **vLLM** (`.226`, `.87`, `.149`) | Fast-tier generation, GPU-resident, continuous batching | Which model is loaded — the controller decides |
| **ik_llama.cpp** (`.226`) | Deep-tier generation with experts in system RAM | Interactive latency |
| **Infinity** (`.87`) | Embeddings on GPU | Reranking (that is CPU) |
| **RAG service** | Ingestion, hybrid retrieval, reranking, the relevance gate, citation assembly. Exposed as an OpenAI-compatible *model* | Being an Open WebUI plugin |
| **MCP tool server** | One tool surface: `search_documents`, `web_search`, `generate_pdf`, `generate_pptx`, `generate_image` | The models |
| **Postgres + pgvector** | Chunks, embeddings, full-text index, document metadata | Vector search *strategy* — that is the RAG service |
| **Open WebUI** | Accounts, chat history, model picker, admin | Retrieval, tools |
| **Caddy** | TLS on the LAN, one entry point | Auth decisions |

### Why the RAG service is a *model*, not a plugin

The RAG service exposes `/v1/chat/completions` and is registered in the gateway as a model named
something like `team-docs`. This is deliberate:

- It is testable in isolation with `curl` — no UI needed to debug retrieval.
- It is not coupled to Open WebUI's plugin API, which changes.
- Users switch to it with the ordinary model picker, which needs no explanation.
- Every client that speaks OpenAI-compatible — terminal agent, editor extension — gets it free.
- If we ever replace the chat UI, the RAG service is untouched.

Recorded as [ADR-0005](./adr/0005-rag-as-a-model-endpoint.md).

---

## 4. The two critical flows

### Ingestion — offline, correctness over speed

```
Document arrives (upload, or watched folder)
  -> record row, status=pending
  -> parse to text + page numbers
  -> chunk, token-aware, with overlap, page provenance preserved
  -> embed each chunk (Infinity on .87, passage mode)      [batched]
  -> INSERT chunks: content, page, embedding, tsvector
  -> status=ready
```

Not user-blocking. Idempotency and correct page attribution matter far more than throughput — a
citation that points at the wrong page destroys trust in every other citation.

### Question — online, where the latency budget lives

```
Question arrives at the RAG service
  -> embed the query (query mode)                        ~50-150 ms
  -> hybrid search: pgvector cosine  +  Postgres full-text
     fused with Reciprocal Rank Fusion -> top ~30        ~tens of ms
  -> cross-encoder rerank on CPU -> top ~5               ~200-500 ms
  -> relevance gate: is the top rerank score above threshold?
       yes -> grounded prompt with context, cite sources
       no  -> drop context, answer from model knowledge,
              label the answer ungrounded
  -> stream from the gateway -> client                   <- first token < 2 s (N3)
  -> persist message + citations
```

**Why hybrid retrieval plus reranking, when frontier models cope with mediocre context:** ours can't.
A local 14–30B model recovers far less gracefully from irrelevant retrieved text. Retrieval quality
is the largest lever we have on answer quality, and it costs no GPU on the generation side. Detail in
[`12-retrieval-and-rerank.md`](./12-retrieval-and-rerank.md).

---

## 5. Trust and security model

- **The egress boundary is the product's justification.** Documents and code never leave. Search
  queries may. Enforced at the network layer, not by convention, and verified by packet capture.
  See [ADR-0004](./adr/0004-egress-policy.md).
- **Secrets live in a gitignored secrets file**, never in this repo, never in a committed compose file.
- **LAN and VPN only.** Caddy terminates TLS; the firewall restricts exposed ports to internal
  subnets. Nothing is published to the public internet.
- **Accounts are per-person** in Open WebUI, so usage is attributable and chat history is private.
- **The platform never runs as a privileged user** on the workstations, and never writes outside its
  own data directories.

---

## Reflect

The design's centre of gravity is *not* the models — those will be replaced within a year. It is the
three seams: one OpenAI-compatible API for models, one MCP surface for tools, one controller for
arbitration. Those seams are what let every other piece be swapped without a rewrite, and they are
what to defend in a review.

The most under-appreciated risk is social, not technical: three shared workstations, and a platform
that can annoy the people sitting at them. That is why D2 gets its own document and lands at M2,
before anyone comes to depend on the thing.

**Next:** [`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md).
