# ADR-0006 — Python + FastAPI for the three services we build

**Status:** Accepted

## Context

We build exactly three components ([ADR-0002](./0002-assemble-vs-build.md)): the RAG service, the MCP
tool server, and the fleet controller. Everything else is assembled.

The choice was not obvious. The author's other active project is TypeScript, and using one language
across both would mean shared tooling, shared idioms, and no context-switching cost. Against that,
every library these three services actually depend on lives in the Python ecosystem.

## Decision

**Python + FastAPI** for all three services. FastMCP (or the official Python MCP SDK) for the tool
server. Dependencies managed with `uv`, every version pinned.

One language for all three, not a per-service choice — they share the retrieval core
([ADR-0005](./0005-rag-as-a-model-endpoint.md)) and a common client for the gateway, and splitting
languages would duplicate both.

## Consequences

- **+** The document-parsing stack (pypdfium2, pdftext, Docling), tokenisers, the reranker, and the
  MCP SDK are all Python-native. In TypeScript we would either reimplement them or shell out to
  Python anyway — inheriting the dependency without the ecosystem.
- **+** Open WebUI is Python, so debugging across the boundary uses one set of tools and one mental
  model.
- **+** `nvidia-smi` parsing and async polling — the fleet controller's whole job — are trivial here.
- **+** Runtime performance is irrelevant to this decision. All three services spend essentially all
  their wall-clock waiting on a model or on Postgres; the language is not on the critical path.
- **+** One retrieval implementation genuinely serves both the RAG endpoint and the `search_documents`
  MCP tool, because they are in the same process space rather than across a language boundary.
- **−** Two languages across the author's projects, with the context-switching cost that implies.
- **−** Python packaging is worse than npm. `uv` and pinned versions are the mitigation, not a fix.
- **−** Type safety is weaker than TypeScript's. Use type hints throughout and run a type checker in
  the deploy step, or the schemas in `13-rag-service-api.md` become documentation rather than
  contracts.

## Alternatives considered

- **TypeScript + Node** — one language across both projects, better type safety, and familiar
  tooling. Rejected because the parsing, tokenising, reranking and MCP libraries are not there. The
  decisive question was not "which language is nicer" but "where do the dependencies live", and the
  answer is unambiguous.
- **Go** — best deployment story of the three (a static binary would suit the per-host fleet agent
  particularly well) and genuinely good at the polling and concurrency the controller needs. Rejected
  for the same ecosystem reason, and because splitting the fleet controller into Go while the other
  two stay Python would fragment a small codebase for a marginal gain.
- **Per-service choice** — Python for RAG and MCP, TypeScript or Go for the controller. Rejected:
  three services, one operator, and a shared gateway client. Two toolchains is a real cost and the
  controller is not big enough to justify it.

## Note

If the per-host agent that reports `nvidia-smi` (see `08-fleet-controller.md` §7) turns out to be
awkward to deploy as Python on three differently-configured hosts, a single static Go binary for
*that component only* is a reasonable exception — it has no shared code with the rest and a trivial
interface.
