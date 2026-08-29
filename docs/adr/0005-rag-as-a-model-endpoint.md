# ADR-0005 — Expose the RAG service as an OpenAI-compatible model

**Status:** Accepted

## Context

The RAG service must be reachable from three different clients — Open WebUI, OpenCode in the
terminal, and Cline/Roo Code in VS Code. Each has its own extension mechanism: Open WebUI has
Pipelines and Functions, the coding agents have their own plugin models. Implementing against each
one separately triples the work and triples the maintenance.

Meanwhile, every one of those clients already speaks one protocol fluently: OpenAI-compatible
`/v1/chat/completions`, pointed at whatever base URL we give it.

## Decision

The RAG service **is a model**. It exposes `/v1/chat/completions` with streaming, and is registered
in the LiteLLM catalog under a name like `team-docs`. Internally it embeds the query, runs hybrid
retrieval, reranks, applies the relevance gate, builds a grounded prompt, and streams the answer back
from a generation model — but from the outside it is indistinguishable from any other model.

Users select it with the ordinary model picker.

## Consequences

- **+** Works in every client at once, with no per-client integration code (F10).
- **+** Not coupled to Open WebUI's plugin API, which changes between releases. If we ever replace the
  chat UI, the RAG service is untouched.
- **+** Testable in isolation with `curl`. Retrieval can be debugged without a browser, and regression
  tests are plain HTTP.
- **+** The model picker becomes the routing UI for free — "chat with docs" versus "general chat" needs
  no explanation, because it looks like choosing a model, which everyone already understands.
- **+** The gateway's fallback and routing logic applies to it like anything else.
- **−** The OpenAI chat schema has no field for citations. We attach them as structured data in the
  streamed response and also render them inline in the text, so they survive clients that ignore
  extra fields.
- **−** Some Open WebUI features that assume a plain LLM (regeneration, editing an earlier turn) need
  the service to be stateless and idempotent per request. That is a design constraint on the service,
  and a reasonable one.
- **−** One extra network hop versus running retrieval inside the chat app.

## Alternatives considered

- **An Open WebUI Pipeline / Function** — the documented extension path, and the most integrated
  result. Rejected: it works only in Open WebUI, leaving the terminal agent and the editor extension
  with no document access, and it couples us to an API that churns.
- **An MCP tool only** (`search_documents`, no model endpoint) — good for agentic use, and we ship
  this *as well*. Rejected as the sole mechanism because it makes the model decide when to retrieve,
  which weaker local models do unreliably; the dedicated endpoint guarantees retrieval happens.
- **Retrieval inside each client** — three implementations, three sets of bugs, three chances for the
  citation logic to differ. Rejected immediately.

## Note

We ship **both** surfaces: the model endpoint for "I want to ask my documents", and the
`search_documents` MCP tool for "an agent decided it needs context mid-task". They share one
retrieval implementation; only the entry point differs.
