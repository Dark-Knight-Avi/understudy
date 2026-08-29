# 13 — RAG Service API

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> The contract. The RAG service is a *model*, not a plugin: it speaks `/v1/chat/completions` with
> streaming, and is registered in the LiteLLM catalog as `team-docs`. This document is what clients
> can rely on, and what happens when a dependency is down.
>
> Implements [ADR-0005](./adr/0005-rag-as-a-model-endpoint.md). Depends on
> [`10`](./10-data-layer.md)–[`12`](./12-retrieval-and-rerank.md).

---

## Concept

### 1. It is a model

The RAG service exposes an OpenAI-compatible chat API and appears in the model picker as
`team-docs`. Internally it embeds the query, runs hybrid retrieval, reranks, applies the relevance
gate, builds a grounded prompt and streams the answer back from a generation model on `.226` — but
from the outside it is indistinguishable from any other model.

```
   Open WebUI  \
   OpenCode     >--- POST /v1/chat/completions ---> LiteLLM (.87:4000)
   Cline       /                                        |
                                          model == "team-docs"
                                                        v
                                          RAG service (.87:8001)
                                            |  embed_query   -> Infinity  (.87:7997)
                                            |  hybrid search -> Postgres  (.87:5432)
                                            |  rerank        -> Infinity  (.87:7998, CPU)
                                            |  gate + prompt
                                            +--> POST /v1/chat/completions
                                                 back to LiteLLM, model == "qwen3-14b"
                                                        v
                                                   vLLM (.226)
```

**Read the loop carefully: the RAG service calls the gateway that called it.** That is intentional —
it means the RAG service inherits the gateway's routing, fallback and ladder behaviour for free, and
never needs to know which host is currently serving `qwen3-14b`. It also means LiteLLM must not route
`team-docs` back to the RAG service from that inner call. Give the inner call an explicit model name
and, when your LiteLLM version supports it, a header or tag that prevents recursion. **Verify this
against your version, and test it deliberately** — an accidental loop here is a self-inflicted denial
of service that looks like a hang.

Everything else about why it is a model rather than an Open WebUI Pipeline is in ADR-0005 and not
repeated here: it works in every client at once, it is `curl`-testable, and it survives replacing the
chat UI.

### 2. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/chat/completions` | The contract. Streaming and non-streaming |
| `GET` | `/v1/models` | Advertises `team-docs`. Some clients probe this before anything else |
| `GET` | `/health` | Liveness. No dependency checks — this must not fail because Postgres blinked |
| `GET` | `/ready` | Readiness: database reachable, embedding model asserted, Infinity answering |
| `POST` | `/admin/documents` | Upload. Multipart; returns `{document_id, status}` immediately |
| `GET` | `/admin/documents/{id}` | Ingestion status, `last_error`, chunk count |
| `POST` | `/admin/documents/{id}/reingest` | Requeue a `failed` document |
| `POST` | `/admin/search` | Retrieval only, no generation. **The debugging endpoint** |

`/admin/search` earns its place: it returns candidates, RRF scores, rerank scores, the gate decision
and per-stage timings without generating anything. Nearly every retrieval question is answered with
one `curl` against it, in a second, with no model involved. Build it first.

---

## Build

### 3. Request and response

Requests are ordinary OpenAI chat completions:

```bash
curl -sN http://10.0.0.87:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "team-docs",
    "stream": true,
    "messages": [
      {"role": "user", "content": "What speed limit did the 2025 study recommend for Vidhana Soudha Road?"}
    ]
  }'
```

Parameters we honour and parameters we ignore, stated plainly so clients are not surprised:

| Parameter | Behaviour |
|---|---|
| `messages` | Required. The last `user` message is the retrieval query (§5) |
| `stream` | Both supported. Streaming is the path that matters for N3 |
| `temperature`, `top_p`, `max_tokens` | Passed through to the generation model |
| `model` | Must be `team-docs`. Which model *generates* is our config, not the caller's |
| `user` | Used for workspace resolution when present (§7) |
| `tools`, `functions` | **Not supported.** We return a clear 400 rather than pretending |
| `n > 1` | **Not supported.** Retrieval is per request; multiple samples would share one context anyway |

Rejecting loudly matters more than it looks. A client that sends `tools` and gets a silent no-op
produces a confusing failure three layers away; a 400 saying "team-docs does not support tool calling
— use the `search_documents` MCP tool instead" is a one-line fix for whoever hits it.

### 4. Citations

**The OpenAI chat schema has no citation field.** This is ADR-0005's main cost, and the mitigation is
belt and braces: attach structured data *and* render citations inline in the text.

**Inline**, so citations survive any client. Bracket markers appear in the streamed content exactly
where the model produced them, and a `Sources` block is appended at the end of the stream:

```
The 2025 study recommended reducing the limit to 40 km/h on the northern
approach [1], with a 30 km/h zone near the junction [2].

---
**Sources**
[1] Traffic Study 2025.pdf, p. 47
[2] Traffic Study 2025.pdf, pp. 51-52
```

**Structured**, so good clients can do better. A non-standard `citations` field on the final chunk,
plus service metadata under `x_rag`:

```json
{
  "id": "chatcmpl-01J...",
  "object": "chat.completion.chunk",
  "model": "team-docs",
  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
  "citations": [
    {
      "n": 1,
      "document_id": "9f2c...",
      "document_title": "Traffic Study 2025.pdf",
      "source_uri": "file:///srv/corpus/team-docs/traffic-study-2025.pdf",
      "page_start": 47,
      "page_end": 47,
      "chunk_id": 184213,
      "rerank_score": 0.81
    }
  ],
  "x_rag": {
    "grounded": true,
    "top_rerank_score": 0.81,
    "threshold": 0.42,
    "candidates": 30,
    "generation_model": "qwen3-14b",
    "timings_ms": {"embed": 88, "search": 31, "rerank": 342, "total_pre_generation": 470}
  }
}
```

**Why both, and why inline is the primary.** Unknown JSON fields are dropped by strict clients,
proxies and SDKs, and we cannot audit every client the team will point at this endpoint. Text always
survives. So the inline rendering is the contract and the structured data is the enhancement — never
the other way round. If you find yourself moving a fact *out* of the text and into `citations` only,
you have made it invisible to somebody.

**The ungrounded case is a text-level fact for the same reason.** Per
[`12-retrieval-and-rerank.md`](./12-retrieval-and-rerank.md) §6, the answer begins with:

```
**UNGROUNDED -- not based on team documents.**
```

and carries `"grounded": false` with an empty `citations` array. A client that drops `x_rag` still
shows the user the label. F7 says a non-grounded answer is *explicitly marked*; the mark has to be
where the user's eyes are.

**Numbering.** Citation numbers are assigned by rerank rank and are stable within one response.
Passages appear in the prompt in a different order (best-last, per §8 of doc 12) but keep their
numbers, so what the model emits matches what the `Sources` block says.

**Cite only what was sent.** Post-process the model's output: any `[n]` outside `1..N` is stripped,
and a warning is logged. A small model occasionally invents `[7]` when it saw five passages, and an
invented citation is exactly the failure that costs us the user's trust — see
[`11-ingestion.md`](./11-ingestion.md) §4.

### 5. Statelessness and idempotency

**The service holds no per-conversation state.** Every request carries its whole history; the service
reads it, retrieves, generates, and forgets. Nothing is keyed on a session id, because there is no
session.

This is a requirement, not an aesthetic. Open WebUI's regeneration and edit-an-earlier-turn features
resend a modified `messages` array and expect the service to behave as though that conversation had
always looked like that. A service holding hidden state would answer the *old* conversation.

Three rules make it hold:

**Rule 1 — the retrieval query is a pure function of `messages`.** Same array, same query, same
candidates, same order. That is why the SQL tie-breaks on `chunk_id`
([`10-data-layer.md`](./10-data-layer.md) §10) and why the rerank sort tie-breaks on `chunk_id`
([`12-retrieval-and-rerank.md`](./12-retrieval-and-rerank.md) §5). Ties broken arbitrarily are the
usual reason "the same question gave different sources".

**Rule 2 — follow-up questions get a deterministic rewrite.** "What about the southern approach?"
retrieves nothing useful on its own. So we build a standalone query from the last few turns — but
that rewrite must itself be deterministic, or Rule 1 collapses:

```python
# services/rag/rag/query.py
def build_retrieval_query(messages: list[dict], max_history: int = 4) -> str:
    """Deterministic. Same messages in, same query out, no model call in the default path."""
    user_turns = [m["content"] for m in messages if m["role"] == "user"]
    if len(user_turns) == 1:
        return user_turns[-1]
    # Concatenate the recent user turns, most recent last. Crude, and free.
    return "\n".join(user_turns[-max_history:])[-2000:]
```

Concatenation is deliberately unsophisticated. An LLM-based rewrite is better at genuine
coreference — and it adds a generation round trip inside the TTFT budget, and it is *not*
deterministic unless pinned to `temperature=0` and even then varies across the ladder rungs of
[`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md). Ship concatenation, measure it on
multi-turn eval questions, and only then decide whether coreference is costing enough to pay for.
This is the same argument as §6 of doc 12: do not put a weak local model in a decision path when a
deterministic function will do.

**Rule 3 — cache retrieval, not generation.** Regeneration should produce a *new answer* from the
*same evidence*. So a short-TTL cache keyed on the hash of `(workspace_id, retrieval_query,
retrieval_params)` stores the gate decision and passages:

```python
cache_key = sha256(f"{workspace_id}|{retrieval_query}|{RETRIEVAL_PARAMS_VERSION}".encode()).digest()
```

Regenerating five times cites the same five sources with five differently-worded answers — which is
what a user expects — and costs one retrieval instead of five. An in-process TTL cache (60 s, a few
hundred entries) is sufficient; do not add Redis for this.

### 6. Streaming

Standard SSE. The only unusual part is where the citations go.

```python
# services/rag/rag/api/chat.py
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()

@router.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, ctx: RequestContext = Depends(auth)):
    query    = build_retrieval_query(req.messages)
    decision = await retrieve_and_gate(ctx.workspace_id, query)   # docs 10 + 12
    prompt   = build_prompt(req.messages, decision)

    if not req.stream:
        return await complete_once(prompt, decision, req)

    async def event_stream():
        emitted = 0
        try:
            async for delta in stream_from_gateway(prompt, req):
                emitted += 1
                yield sse(chunk_with_delta(delta))

            yield sse(chunk_with_delta(render_sources_block(decision)))   # inline, always
            yield sse(final_chunk(decision))                              # citations + x_rag
            yield "data: [DONE]\n\n"
        except Exception as exc:
            # A stream that has already started cannot become an HTTP error code.
            log.exception("generation failed after %d deltas", emitted)
            yield sse(chunk_with_delta(f"\n\n[error: generation interrupted -- {kind(exc)}]"))
            yield sse(final_chunk(decision, finish_reason="stop"))
            yield "data: [DONE]\n\n"
        finally:
            await log_retrieval_event(ctx, decision)     # telemetry, never blocks the stream
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

Four details that are easy to get wrong:

- **The `Sources` block is streamed as content**, not appended by a client. It is text, so it survives
  everything.
- **Once the stream has started, HTTP status is spent.** A failure mid-stream must be delivered *as
  content* plus a clean terminator, or clients hang waiting for `[DONE]`. Never let an exception
  escape the generator.
- **`finally` logs the event regardless of outcome**, including client disconnects. Ungrounded
  answers and mid-stream failures are exactly the events worth having in
  `rag.retrieval_events`.
- **Flush promptly and do not buffer.** With Caddy in front, confirm proxy buffering is off for this
  route, or TTFT measurements will be a lie about the proxy rather than about the model.

### 7. Auth, tenancy, and secrets

Requests arrive through LiteLLM, which has already authenticated the caller against its own virtual
keys. The RAG service still requires its own bearer token — a service that trusts its network
position is one firewall rule away from being open.

```python
async def auth(request: Request) -> RequestContext:
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.rag_service_key):   # constant-time
        raise HTTPException(401, "invalid service key")
    subject      = request.headers.get("x-openwebui-user-email") or request.json_body.get("user")
    workspace_id = await resolve_workspace(subject)      # membership lookup, doc 10 section 6
    return RequestContext(subject=subject, workspace_id=workspace_id, request_id=uuid4())
```

**Be honest about the weak link.** Which header actually carries the end user's identity through Open
WebUI and LiteLLM is version-dependent — LiteLLM forwards metadata differently across releases, and
Open WebUI's user headers have moved. **Verify against your versions during M5** by logging the full
header set of one real request from each of the three clients, and write the answer into this
document afterwards. Until then, the safe default is a single default workspace with membership
enforced, and a loud warning in the logs when a subject cannot be resolved:

```python
if subject is None:
    log.warning("no subject on request %s; falling back to default workspace", ctx.request_id)
```

Never fail *open* into "search everything" — fall back to the default workspace, or 403. Silent
cross-workspace retrieval is a data leak that looks like a feature.

**Secrets.** `RAG_SERVICE_KEY`, `DATABASE_URL`, `LITELLM_MASTER_KEY` come from the host's gitignored
`.env`. Committed files contain `.env.example` with placeholder values only. Nothing in this repo is
ever a real credential; if you find one committed, rotate it
([README](./README.md) ground rules).

### 8. Errors, timeouts, and degradation

Every dependency gets an explicit timeout. A request with no timeout is a request that hangs forever
on a machine somebody just claimed for their own work.

| Dependency | Connect | Read | On failure |
|---|---|---|---|
| Postgres | 2 s | 5 s | 503, no answer attempted |
| Infinity — embeddings (`:7997`) | 2 s | 10 s | Degrade to lexical-only (below) |
| Infinity — reranker (`:7998`) | 2 s | 10 s | Degrade to RRF order, force ungrounded |
| LiteLLM gateway (`:4000`) | 2 s | 300 s total, 30 s idle | 502 with a plain-language message |

**The degradation matrix — what the user actually sees:**

| What is down | Behaviour | Rationale |
|---|---|---|
| **Embeddings server** | Lexical-only retrieval, still reranked and gated. Response notes `"degraded": "dense_unavailable"` | The lexical arm alone is genuinely useful for keyword questions. Half a retriever beats none |
| **Reranker** | Fall back to RRF order, and **force ungrounded** | Without a rerank score there is no calibrated number to compare to the threshold. Guessing at grounding is the one thing we must not do ([doc 12](./12-retrieval-and-rerank.md) §6) |
| **Postgres** | 503, retrieval impossible | Everything depends on it. Fail fast and honestly |
| **Gateway / all generation hosts** | 502, message naming the RAG service as healthy | Distinguishes "retrieval is broken" from "no model is available", which are different fixes |
| **Ladder demoted to a 4B** | Normal operation, `x_rag.generation_model` reports it | Grounding does not depend on the generation model. That is why §6 of doc 12 rejected an LLM router |

The reranker row is the interesting one, and it is worth stating as a principle: **when the mechanism
that establishes grounding is unavailable, the correct behaviour is to stop claiming grounding** —
not to lower the bar. The system says UNGROUNDED and answers from model knowledge, which is honest
and degraded, rather than confident and unverified.

Errors use the OpenAI error envelope so clients render them properly:

```json
{"error": {"message": "retrieval unavailable: database unreachable",
           "type": "service_unavailable", "code": "db_unavailable"}}
```

Include `request_id` in the message. Someone reporting a failure gives you a string that finds the
log line and the `rag.retrieval_events` row.

### 9. Registering with the gateway

```yaml
# deploy/host-87/litellm-config.yaml  (excerpt)
model_list:
  - model_name: team-docs                    # what users see in the picker
    litellm_params:
      model: openai/team-docs                # OpenAI-compatible passthrough
      api_base: http://rag:8001/v1
      api_key: os.environ/RAG_SERVICE_KEY
      timeout: 300
      stream_timeout: 60
    model_info:
      description: "Answers from the team's documents, with citations to page."
      mode: chat
```

Two operational notes:

- **No fallback for `team-docs`.** LiteLLM's fallback would route a failed RAG request to a plain
  chat model, which would answer *without citations and without the ungrounded label* while the user
  believes they are talking to the document assistant. That is a silent correctness failure. Let it
  fail visibly.
- **The inner generation model does have fallbacks**, and should — that is the whole point of routing
  through the gateway rather than pointing at `.226` directly. When `.226` is claimed by its user, the
  ladder demotes and `team-docs` keeps working on a smaller model.

### 10. One implementation, two entry points

M6 adds `search_documents` as an MCP tool ([`14-mcp-tool-server.md`](./14-mcp-tool-server.md)). It is
**the same retrieval code**, reached through a different door.

```
                     rag/retrieval.py :: retrieve_and_gate()
                     (embed -> hybrid -> rerank -> gate)
                                  ^          ^
                                  |          |
             POST /v1/chat/completions    MCP tool search_documents
             "I want to ask my docs"      "an agent needs context mid-task"
                     |                            |
              adds generation +            returns passages +
              citation rendering           scores as structured data
```

The shared function returns passages, scores and the gate decision. The chat endpoint adds prompt
construction, generation and citation rendering; the MCP tool returns the structured result and lets
the calling agent decide what to do with it.

```python
# services/rag/rag/retrieval.py  --  the shared core. Both entry points call exactly this.
async def retrieve_and_gate(workspace_id: UUID, query: str,
                            k_candidates: int = 30, k_final: int = 5) -> GateDecision:
    vector     = await embed_query(query)                      # doc 11 section 6
    candidates = await hybrid_search(workspace_id, vector, query, out_k=k_candidates)  # doc 10 s.10
    scored     = await rerank(query, candidates, top_n=k_final)                        # doc 12 s.5
    return apply_gate(scored)                                                          # doc 12 s.6
```

```python
# services/mcp_tools/tools/search.py  (M6)
@mcp.tool()
async def search_documents(query: str, workspace: str | None = None, k: int = 5) -> dict:
    """Search the team's documents. Returns passages with document, page, and relevance score."""
    decision = await retrieve_and_gate(await resolve_workspace(workspace), query, k_final=k)
    return {
        "grounded": decision.grounded,
        "top_score": decision.top_score,
        "passages": [
            {"text": p.candidate.content, "document": p.candidate.document_title,
             "page_start": p.candidate.page_start, "page_end": p.candidate.page_end,
             "score": p.score}
            for p in decision.passages
        ],
    }
```

**Why this matters beyond saving code.** Three separate retrieval implementations means three
citation formats, three page-numbering conventions, and three places for the gate to be
subtly different — so an agent in VS Code and a user in the chat UI would get different answers to
the same question with no way to tell why. ADR-0005's closing note ("they share one retrieval
implementation; only the entry point differs") is a correctness requirement, not a tidiness
preference.

The one thing the tool surface deliberately does *not* inherit is the guarantee of retrieval. The
model endpoint always retrieves; the MCP tool retrieves when an agent decides to call it, and weaker
local models decide that unreliably. That is exactly why ADR-0005 ships both.

### 11. Testing it

The service is `curl`-testable end to end, which is the practical dividend of ADR-0005:

```bash
# 1. Retrieval only -- no model involved. The fastest possible debugging loop.
curl -s localhost:8001/admin/search \
  -H "Authorization: Bearer $RAG_SERVICE_KEY" \
  -d '{"query":"recommended speed limit northern approach","k":5}' | jq '{
        grounded: .grounded, top: .top_score,
        hits: [.passages[] | {doc: .document, p: .page_start, s: .score}]}'

# 2. The gate's negative case -- must come back ungrounded.
curl -s localhost:8001/admin/search -H "Authorization: Bearer $RAG_SERVICE_KEY" \
  -d '{"query":"what is the capital of France"}' | jq '.grounded'    # expect false

# 3. Full path through the gateway, streaming, as a client sees it.
curl -sN localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_KEY" \
  -d '{"model":"team-docs","stream":true,
       "messages":[{"role":"user","content":"What speed limit was recommended?"}]}'

# 4. Idempotency -- the same messages must cite the same sources.
for i in 1 2 3; do
  curl -s localhost:8001/admin/search -H "Authorization: Bearer $RAG_SERVICE_KEY" \
    -d '{"query":"speed limit"}' | jq -c '[.passages[].chunk_id]'
done            # three identical lines, or Rule 1 of section 5 is broken
```

Test 4 is the one that catches the subtle regressions. Put it in the regression suite alongside the
eval set from [`17-evaluation.md`](./17-evaluation.md).

---

## Reflect

**What we traded away.** Being a model rather than a plugin costs one extra network hop, a citation
format that has to be smuggled through a schema with no room for it, and a statelessness constraint
that rules out the obvious implementation of conversational follow-ups. In exchange we get every
client at once, `curl`-based debugging, and independence from Open WebUI's plugin API. Given F10 and
given that chat UIs churn faster than protocols, that trade looks right — and ADR-0005 records it so
a future reader can re-open it deliberately rather than by accident.

**The thing most likely to bite.** Identity propagation. Everything else in this document is under
our control; which header carries the end user's identity through Open WebUI and LiteLLM is not, it
varies by version, and the failure mode is a workspace boundary that quietly does not hold. The
mitigations are in §7 — never fail open, warn loudly on unresolved subjects — but the real fix is to
verify it against the deployed versions early in M5 and re-verify after any upgrade of either
component. Put it on the M8 hardening checklist as an explicit test, not an assumption.

**What we would revisit first.** The follow-up query rewrite. Concatenating recent user turns is
crude and will visibly fail on genuine coreference ("what about that one?"), and multi-turn document
questions are exactly how people actually use a document assistant. The reason to ship the crude
version anyway is that it is deterministic, and determinism is what makes regeneration, editing an
earlier turn, and the idempotency test above all work. If measurement shows coreference is costing
real quality, the upgrade path is a pinned, cached, `temperature=0` rewrite whose output is part of
the cache key — so determinism survives the improvement.

**Next:** [`14-mcp-tool-server.md`](./14-mcp-tool-server.md).
