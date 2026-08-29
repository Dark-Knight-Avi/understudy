# ADR-0007 — Adopt RAGFlow for retrieval instead of building it

**Status:** Accepted — amends [ADR-0002](./0002-assemble-vs-build.md)

## Context

[ADR-0002](./0002-assemble-vs-build.md) said we would build the RAG service ourselves, on the grounds
that off-the-shelf RAG uses "naive chunking, no reranking, and citations that are not trustworthy
enough to build on."

**That reasoning was too broad.** It is an accurate description of Open WebUI's *built-in* RAG, and it
was generalised to the entire category without checking. Two mature open-source platforms —
**RAGFlow** and **Onyx** — do hybrid retrieval, contextual chunking, reranking and cited answers, and
both accept any OpenAI-compatible endpoint, meaning they run on our local models through the LiteLLM
gateway unmodified.

Two things have also changed since ADR-0002:

- **The corpus is user-uploaded**, not a curated set we ingest once. That requires an upload UI and
  per-user document isolation, neither of which the original design had.
- **Multi-tenancy became a security requirement** rather than a structural nicety, because every user
  brings their own files.

Meanwhile M5 — building retrieval — is 8–12 days of a 6–9 week plan, carried by a single operator
whose bus factor is the project's stated top risk.

## Decision

**Adopt RAGFlow as the retrieval layer**, behind the seam that
[ADR-0005](./0005-rag-as-a-model-endpoint.md) already defines. RAGFlow is registered in the LiteLLM
catalog as the `team-docs` model, exactly as our own service would have been.

Validate first with a **one-day spike at M1.5**, after the gateway exists and before any of M5 is
built. Three pass/fail criteria:

| # | Check | Why it could fail |
|---|---|---|
| 1 | **Does it refuse?** Answers absent from the corpus must be *labelled ungrounded* (F7) | Many RAG tools always answer |
| 2 | **Per-user document isolation on the free tier** | Users upload their own files; a silent cross-tenant read is the worst failure available |
| 3 | **Reachable over MCP**, so OpenCode and Cline can query it | Otherwise F10 fails and documents are chat-only |

**Fallback is partial, not all-or-nothing.** If it fails *only* on criterion 1, put a thin relevance
gate in front of it — roughly a day — rather than building the whole pipeline. Their parser, our
trust guarantee. Build our own only if it fails structurally on 2 or 3.

## Consequences

- **+** Removes the largest single block of work in the plan, roughly halving time to something the
  team actually uses.
- **+** Document parsing is the one area where off-the-shelf genuinely beats us. Table and layout
  extraction from messy PDFs is specialised work with years behind it; two weeks of one person's time
  produces something worse.
- **+** Closes the upload-path gap discovered after the corpus decision — RAGFlow ships that UI.
- **+** Cuts the bus-factor risk: "build three things" becomes two, and the two remaining are the ones
  nothing off the shelf does.
- **+** The swap is a LiteLLM config change, not a redesign, because ADR-0005 made retrieval a model
  endpoint rather than a chat-UI plugin. That decision was made for unrelated reasons and pays off here.
- **−** We lose control over chunking strategy, and the hybrid + RRF + rerank design in `12` becomes
  reference rather than implementation.
- **−** Two UIs: chat in Open WebUI, document management in RAGFlow. A UX wart, and it needs explaining
  to users.
- **−** A dependency on someone else's release cadence and defaults for the most trust-sensitive part
  of the product.
- **−** Its licence, isolation model and refusal behaviour are unverified until the spike. This ADR is
  contingent on that spike passing.

## Alternatives considered

- **Build it ourselves** (the ADR-0002 plan) — maximum control and the best learning. Rejected on
  cost: 8–12 days for a worse parser, while the differentiated value of this platform is "our
  documents, our network", not "we wrote our own chunker". Retained as the fallback if the spike fails
  on isolation or MCP access.
- **Onyx** — a stronger team-search platform with 40+ connectors and an MIT community edition. Rejected
  for now on two counts: permissions and RBAC sit in the paid Enterprise edition, which conflicts with
  N2 and with the isolation requirement; and it would replace Open WebUI wholesale rather than sitting
  at the document layer. Reconsider if RAGFlow fails the spike.
- **Open WebUI's built-in RAG** — rejected, and this part of ADR-0002 stands: basic chunking, no
  reranking, citations too weak to build a trust guarantee on.
- **A framework (LlamaIndex, Haystack)** — these are libraries for assembling a RAG app, not a
  finished one. Choosing them still means building the service, the upload UI and the tenancy model,
  so it saves less than it appears to.

## Note on what does not change

`10-data-layer.md` through `13-rag-service-api.md` are retained as **reference and fallback**. They
document the design we would build if the spike fails, and the relevance-gate work in `12` is directly
reusable as the wrapper described above. The eval set in `17-evaluation.md` becomes *more* important,
not less: it is how we judge whether RAGFlow is good enough, and it is the only thing that makes the
spike's first criterion answerable.
