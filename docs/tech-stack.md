# Tech Stack — Choices and Tradeoffs

> A reference, not a sequential unit. Every choice here, what it buys, what it costs, and what we'd
> use instead. The *why* for the big five lives in [`adr/`](./adr/); this is the whole picture on one
> page.
>
> **Read §8 before you install anything.** Three popular components have licence terms that matter
> for a company deployment, and one of them changes a recommendation.

---

## 1. Inference

| Layer | Choice | Why | What it costs |
|---|---|---|---|
| Fast-tier serving | **vLLM** | Continuous batching, OpenAI-compatible, **sleep mode** — which our whole yield policy depends on | Heavy install, CUDA-version sensitive, GGUF is not its native path, more config than alternatives |
| Deep-tier serving | **ik_llama.cpp** | MoE-specific quants and the best CPU expert-offload performance; `--n-cpu-moe` is the key flag | A community fork — fewer eyes, build from source, tracks upstream imperfectly |
| Embeddings serving | **Infinity** | Serves embeddings **and** reranking from one OpenAI-compatible server, dynamic batching | Smaller project than TEI; less battle-tested at scale |

### vLLM vs Ollama — the one people argue about

Ollama is dramatically easier: one binary, automatic model swapping, sane defaults. It loses roughly
10–15% throughput and handles concurrency notably worse. For a single user that's irrelevant; for
2–4 concurrent streams it isn't.

The decider isn't throughput though — it's **sleep mode**. vLLM can park weights in system RAM and
free VRAM in seconds, which is the mechanism behind the entire GPU-sharing policy
([`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md)). Ollama's unload is a full teardown and
reload. On `.226` with 256 GB of RAM, that difference is the difference between yielding feeling
instant and feeling broken.

**Keep Ollama in your pocket:** if M0 spike 2 shows CUDA instability under WSL2 on the Threadripper,
Ollama on llama.cpp is a far less CUDA-intensive fallback.

### ik_llama.cpp vs mainline llama.cpp vs KTransformers

| | Pros | Cons |
|---|---|---|
| **ik_llama.cpp** ✅ | MoE-tuned quant types (`q3_k_r4`, `q2_k_r4`); reported to fit a 671B MoE under 256 GB + 24 GB | Fork; must build; smaller community |
| Mainline llama.cpp | Safest, huge community, now has `--n-cpu-moe` | Somewhat slower on MoE offload |
| KTransformers | Fastest reported MoE offload numbers | Research-grade; fragile; narrower model support |

Start on **mainline** to prove the concept in M0 spike 7, then move to `ik_llama.cpp` if you need the
extra throughput. Failing over to mainline is always available.

---

## 2. Models

| Role | Choice | Licence | Why | Tradeoff |
|---|---|---|---|---|
| Agentic coding | Qwen3-Coder-30B-A3B Int4 | Apache-2.0 | Best coder that fits 24 GB; MoE so ~3B active → fast | Clearly below frontier on hard tasks |
| Chat / ladder rungs | Qwen3-14B / 8B / 4B Int4 | Apache-2.0 | Graceful degradation when the GPU is shared | Quality drops visibly at lower rungs |
| Deep tier | Qwen3-235B-A22B Q4 | Apache-2.0 | Near-frontier, runs from RAM | ~10–20 tok/s; competes with modelling runs for bandwidth |
| Max tier | DeepSeek-V3/R1-class Q2–Q3 | MIT-style | Closest to frontier available | Single-digit tok/s; needs `.226` to itself |
| Embeddings | Qwen3-Embedding-0.6B | Apache-2.0 | Strong MTEB for 0.6B; flexible dims 32–1024; only ~1.2 GB | Changing it later = **re-embed the entire corpus** |
| Reranking | bge-reranker-v2-m3 | MIT | ~568M so CPU-viable; multilingual; proven | Adds 200–500 ms per query |
| Image | **FLUX.1-schnell** | **Apache-2.0** | Commercially usable, fast | Lower fidelity than FLUX.1-dev — see §8 |

### Embedding model — the choice with the longest shadow

Qwen3-Embedding-0.6B over bge-m3 on three grounds: it scores higher on MTEB, it's half the memory,
and it supports Matryoshka dimensions so we can shrink vectors later without re-training. bge-m3's
edge is native hybrid retrieval (dense + sparse + ColBERT in one model) — but we get hybrid from
Postgres full-text search instead, so we don't need it in the model.

The 8B variant is the quality leader and we can't use it: 7.6 GB on a 12 GB card that also has to
share with its user.

**This is the one decision that's expensive to reverse.** Different embedding models produce
incompatible vector spaces, so switching means re-embedding every chunk. Pin the dimension as one
config constant consumed by both the schema and the embed wrapper, so the blast radius stays small.

---

## 3. Gateway and orchestration

| Layer | Choice | Why | What it costs |
|---|---|---|---|
| Model gateway | **LiteLLM proxy** | One OpenAI-compatible endpoint for the whole fleet; catalog, routing, fallback, usage tracking | Another moving part; config sprawl; occasional breaking changes |
| GPU arbitration | **Fleet controller** (we build) | Nothing off the shelf implements our social contract | ~400 lines of FastAPI plus a page to maintain |
| Per-node swapping | vLLM sleep/wake, driven by the controller | Seconds, not a cold load | Endpoint names have moved between vLLM versions — pin yours |
| Containers | **Docker Compose** | Right size for three hosts | No cross-host scheduling — which we explicitly don't want ([ADR-0001](./adr/0001-partition-by-service.md)) |
| Reverse proxy / TLS | **Caddy** | Automatic TLS with an internal CA; tiny config | Less ubiquitous than nginx if you need obscure features |

**Why not llama-swap instead of the fleet controller?** llama-swap does on-demand model loading well,
but it swaps on *requested model name*. Ours must swap on *measured free VRAM and whether a human is
using the machine* — a different trigger entirely. llama-swap remains a reasonable helper for the
deep tier, where the trigger really is just "which model was asked for".

**Why not k3s?** Cross-host scheduling is the thing we deliberately don't want. Three Compose files
and a gateway is the correct amount of infrastructure.

---

## 4. Clients

| Surface | Choice | Why | Tradeoff |
|---|---|---|---|
| Chat UI | **Open WebUI** | Most active; accounts, RBAC, model picker, MCP support | Opinionated UX; built-in RAG is weak (we bypass it); licence clause — see §8 |
| Terminal agent | **OpenCode** | The de-facto open Claude Code equivalent; MIT; connects to any local endpoint | Ships releases daily — pin a version |
| VS Code | **Cline** (or Roo Code) | Most popular; model-agnostic; MCP support | **Token-hungry prompts** — a real problem on a local model's limited context |
| Alternatives kept in reserve | Aider, OpenHands, Goose, Qwen Code | Aider is more mature and git-centric; OpenHands sandboxes properly | Each is a different agent philosophy, not a drop-in |

**The Cline caveat deserves emphasis.** Cline's system prompts and file-context strategy assume a
frontier model with a big cheap context window. Against a local 30B with a constrained KV cache, it
can burn the entire context before doing useful work. Budget time in M3 for prompt/context tuning, and
consider Roo Code, which exposes more knobs for exactly this.

---

## 5. Retrieval stack

| Layer | Choice | Why | Tradeoff |
|---|---|---|---|
| Store | **Postgres 17 + pgvector** | One datastore for relational *and* vector; SQL joins for tenancy; transactional consistency | Not a purpose-built vector DB; index tuning is on us |
| Index | **HNSW** | Better recall and query latency | Slower build, more memory than IVFFlat |
| Lexical half | **Postgres `tsvector` + GIN** | BM25-ish search with no extra service | Weaker than a real BM25 engine |
| Fusion | **Reciprocal Rank Fusion** | No weights to tune; robust across query types | Slightly less optimal than a well-tuned weighted blend |
| Rerank | bge-reranker-v2-m3 on CPU | Largest single quality lever; costs no GPU | 200–500 ms; competes with modelling runs for CPU |

### Why one database instead of a dedicated vector DB

Qdrant and Weaviate have better vector features — richer filtering, quantisation options, snapshots.
They also mean a **second datastore to keep in sync** with the relational data, and two things to back
up and secure. At our corpus size, pgvector with HNSW is comfortably fast enough, and being able to
join chunks to documents to permissions in one query is worth more than the extra vector features.

Revisit if the corpus passes a few million chunks, or if per-query metadata filtering becomes complex.

### Why hybrid instead of dense-only

Dense embeddings miss exact tokens — part numbers, acronyms, error codes, surnames. Lexical search
misses paraphrase. RRF over both costs one extra SQL query and consistently beats either alone.
Elasticsearch would do the lexical half better and cost us an entire additional service; the
tsvector is good enough for this corpus size.

---

## 6. Tools and generation

| Capability | Choice | Why | Tradeoff |
|---|---|---|---|
| Tool protocol | **MCP** (FastMCP / official Python SDK) | Open WebUI, OpenCode and Cline all speak it — build once, available everywhere | Young protocol; specs still moving |
| Web search | **SearXNG** | Free, self-hosted, no API key, aggregates engines, strips identifying headers | Scraping is fragile; engines rate-limit; **queries leave the network** ([ADR-0004](./adr/0004-egress-policy.md)) |
| PDF generation | **Typst** | Single binary, fast, modern syntax an LLM emits correctly far more often than LaTeX | Smaller ecosystem than LaTeX; fewer templates |
| PPTX generation | **python-pptx** | Produces genuinely editable `.pptx` | Verbose API; layout is manual work |
| Image generation | **ComfyUI** | Graph-based, proper API mode, best performance, widest model support | Steep learning curve; workflow JSON is fiddly to generate |
| PDF parsing | **pypdfium2 / pdftext**, Docling for layout | Apache-licensed; fast; Docling handles tables well | See §8 — this is a licence-driven choice |

**Typst over LaTeX** is worth a sentence: an LLM asked for LaTeX produces subtly broken LaTeX
routinely, and a full TeX install is gigabytes. Typst is one binary and its error messages are
actionable, which matters when a model is generating the source.

---

## 7. Services we build

| Service | Language | Why |
|---|---|---|
| RAG service | **Python + FastAPI** | Parsing, tokenisers and rerankers are all Python; matches Open WebUI |
| MCP tool server | **Python + FastMCP** | Reference MCP SDK is Python |
| Fleet controller | **Python + FastAPI** | Shares the ecosystem; `nvidia-smi` parsing and async polling are trivial here |

**Python over TypeScript**, despite your other project being TS. The entire document-parsing,
embedding, reranking and MCP ecosystem lives in Python; doing this in TS means either reimplementing
or shelling out to Python anyway. Runtime speed is irrelevant — every one of these services spends
its time waiting on a model.

The cost is real: two languages across your projects, and Python packaging is worse than npm. Use
`uv` and pin everything.

---

## 8. Licence gotchas — read this before installing

Three widely-recommended components have terms that matter for a company deployment.

### FLUX.1-dev is **not** licensed for commercial use — use FLUX.1-schnell

This corrects an earlier recommendation in these docs. **FLUX.1 [dev]** ships under a
non-commercial licence; commercial use requires a paid licence from Black Forest Labs with usage
tracking through their API — which fails both N1 (egress) and N2 (zero cost).

**FLUX.1 [schnell] is Apache-2.0** and is fine for commercial self-hosting. It's a few-step distilled
model: faster, somewhat lower fidelity, and the right default here. If image quality later proves
insufficient, SDXL (OpenRAIL) and SD3.5 (Stability community licence — check the revenue threshold)
are the next options.

### PyMuPDF is AGPL-3.0

The fastest Python PDF library is AGPL, with a commercial licence sold separately by Artifex. For a
purely internal tool the AGPL's network clause is generally not triggered, but it's a landmine if this
ever gets exposed beyond the company, and "we depend on an AGPL library" is a conversation nobody
wants to have retroactively.

Use **pypdfium2** (Apache-2.0) or **pdftext** built on it — Apache-licensed, fast, and adequate for
text extraction. **Docling** (MIT) handles layout and tables when you need them. **pdfplumber** (MIT)
is the slow-but-precise option for tricky documents.

### Open WebUI's licence has a branding clause

Since April 2025 Open WebUI ships under a custom licence, not plain BSD-3. Deployments exceeding
**50 users in a rolling 30-day window** must retain Open WebUI branding. At 10 seats you're well
under it and may even rebrand freely — but note the threshold now, because crossing it silently later
would put you out of compliance.

### The models themselves are fine

Qwen3 family: Apache-2.0. DeepSeek: MIT-style. BGE rerankers: MIT. No issues — this is a genuine
strength of the Qwen-centric choice.

---

## 9. What would change our mind

| If this happens | We'd switch to |
|---|---|
| M0 spike 2 shows CUDA unstable under WSL2 on `.226` | Ollama/llama.cpp instead of vLLM; or move serving to `.149` |
| M0 spike 7 shows deep tier starves the modelling runs | Fast tier only; set expectations accordingly |
| Corpus grows past a few million chunks | Qdrant alongside Postgres |
| Reranking on CPU proves too slow under load | Move it to `.87`'s GPU and drop the small chat model |
| Cline's context appetite proves unworkable | Roo Code, or Aider's more economical diff-based flow |
| A second GPU becomes available | Split always-on serving from bursty work; most swapping disappears |
| Query disclosure via search becomes a concern | Local crawl-and-index of a fixed set of reference sources |

---

## Reflect

Two themes run through these choices.

**Prefer the boring, protocol-compatible option.** vLLM, Postgres, Docker Compose and MCP are all
chosen partly because they're replaceable. The seams — OpenAI-compatible HTTP, MCP, SQL — are what
make every row in this table a decision we can revisit cheaply. That matters more than any individual
pick, because the model landscape turned over several times in the past year and will again.

**Licences are part of the engineering.** "Free and open source" is the project's premise, and two of
the most obvious picks (FLUX.1-dev, PyMuPDF) quietly aren't. Checking that up front cost an hour;
discovering it during a security review would cost considerably more.
