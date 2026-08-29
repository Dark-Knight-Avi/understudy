# ADR-0002 — Build three components; assemble everything else

**Status:** Accepted

## Context

The scope is large: chat, agentic coding in terminal and editor, multi-agent workflows, document RAG,
web search, PDF, PPTX and image generation. Built from scratch that is a multi-year project for a
team, let alone one person alongside other work.

Meanwhile mature open-source software already exists for nearly every item on that list, and all of
it speaks either an OpenAI-compatible HTTP API or the Model Context Protocol.

## Decision

Build only what is genuinely ours, assemble the rest.

**We build:**

| Component | Why it cannot be assembled |
|---|---|
| **RAG service** | Retrieval over *our* corpus with *our* quality bar. Off-the-shelf RAG (including Open WebUI's built-in) uses naive chunking, no reranking, and citations that are not trustworthy enough to build on |
| **MCP tool server** | The single tool surface shared by every client. Nothing off the shelf bundles our RAG, our renderers and our image host behind one protocol |
| **Fleet controller** | GPU arbitration against a specific social contract on specific shared machines. Inherently bespoke |

**We assemble:** LiteLLM (gateway), Open WebUI (chat), OpenCode (terminal agent), Cline / Roo Code
(VS Code), vLLM and `ik_llama.cpp` (inference), Infinity (embeddings), SearXNG (search), ComfyUI
(image), Typst and python-pptx (renderers), Postgres + pgvector.

## Consequences

- **+** A usable chat product exists in week one (M1) rather than month three. Adoption starts early,
  and feedback arrives while the design can still absorb it.
- **+** Effort concentrates where it is differentiated. Retrieval quality is the thing that decides
  whether this platform is better than a public chatbot; chat-history UI is not.
- **+** Each assembled piece is replaceable, because the seams are standard protocols rather than
  bespoke integrations.
- **+** The MCP server means one implementation of each tool serves the chat UI, the terminal agent
  and the editor extension simultaneously — the only realistic way to satisfy F10.
- **−** We inherit other projects' upgrade cycles, defaults and occasional breaking changes.
- **−** The UX is Open WebUI's, not ours. Some rough edges cannot be fixed without forking.
- **−** More moving parts to deploy and monitor than a single application would have.

## Alternatives considered

- **Build the whole product** (custom Next.js app, own chat UI, own agent loop) — maximum control and
  the best learning exercise, but realistically 2–3 months before the team would adopt it, and the
  differentiated work (retrieval) would be the *last* thing built rather than the first.
- **Assemble everything, build nothing** — running in a weekend using Open WebUI's built-in RAG.
  Rejected on quality: basic chunking, no reranking, no relevance gate, and citations too weak to
  trust. Trustworthy citations are the product.
- **Buy a commercial self-hosted platform** — violates the zero-cost constraint (N2).
