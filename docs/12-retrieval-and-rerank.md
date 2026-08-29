# 12 — Retrieval & Rerank

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> The quality engine: hybrid search, fusion, cross-encoder reranking on CPU, and the relevance gate
> that decides whether an answer is grounded or honestly labelled as not. This is where M5's value
> actually lives.
>
> Depends on [`10-data-layer.md`](./10-data-layer.md) for the search query and
> [`11-ingestion.md`](./11-ingestion.md) for what is in the index.

---

## Concept

### 1. Why retrieval quality matters more here than it would at a frontier lab

A frontier model handed five mediocre chunks will usually still produce a good answer. It notices the
retrieved text is off-topic, quietly ignores it, and falls back on what it knows. That robustness is
a *luxury of scale*, and we do not have it.

A local 14–30B model handed the same five mediocre chunks behaves differently. It tends to treat the
context as authoritative — it was put in the prompt, so it must be relevant — and it will assemble a
confident answer out of adjacent-but-wrong material. Irrelevant context does not merely fail to help;
it actively degrades an answer the model could have given from its own weights.

Two consequences follow, and they shape everything in this document:

1. **Retrieval quality is the largest lever we have on answer quality**, larger than the choice of
   generation model, and it costs no GPU on the generation side. Reranking on `.87`'s idle CPU buys
   more than promoting a 14B to a 30B does.
2. **Knowing when *not* to retrieve is a first-class feature**, not an edge case. Hence §6.

### 2. Why hybrid beats dense-only

Dense embeddings encode meaning. That is their strength and their failure mode: two texts that mean
the same thing land near each other, but *the exact string you typed* is not privileged in any way.

| Query | What dense does | What lexical does |
|---|---|---|
| `error code E-4471` | Retrieves passages about errors and diagnostics generally. The specific code is a few near-meaningless subword tokens | Exact match, rank 1 |
| `part number 8821-RC` | Same problem — identifiers have no semantic neighbourhood | Exact match |
| `what did Krishnamurthy conclude` | Surnames embed poorly; may retrieve a different author entirely | Exact match |
| `VISSIM calibration` | Depends entirely on whether the acronym was in the embedder's training data | Exact match |
| `how do we handle congestion at intersections` | Finds the section on "queue management at signalised junctions" — no shared vocabulary at all | Nothing. Zero term overlap |
| `is the model validated` | Finds discussion of goodness-of-fit and RMSE | Matches the literal word "validated" wherever it appears, usefully or not |

The pattern is clean: **dense misses exact tokens, lexical misses paraphrase.** Neither failure is
rare, and our corpus is full of exactly the identifier-heavy technical content where the dense arm is
weakest — part numbers, error codes, acronyms, model names, author surnames.

Running both costs one extra SQL query against an index we already have. It is the cheapest quality
improvement available anywhere in this system, which is why it is not optional and why
[`tech-stack.md`](./tech-stack.md) §5 chose Postgres full-text over a second search service.

**Being honest about the lexical arm:** Postgres `tsvector` with `ts_rank_cd` is not BM25. It has no
proper document-length normalisation and cruder term weighting, so it is measurably weaker than
Elasticsearch would be. It is also weaker than a dedicated engine at handling stemming edge cases. We
accept that: it is good enough at this corpus size, and it costs zero additional services to operate,
back up and secure. If the eval set ever shows the lexical arm as the binding constraint, that is the
moment to reconsider — and not before.

### 3. The pipeline

```
  question
     |
     v
  [1] embed query (query mode)                Infinity GPU, .87      ~50-150 ms
     |
     v
  [2] hybrid search                           Postgres, .87          ~tens of ms
        dense arm    : pgvector cosine  -> 60 candidates
        lexical arm  : tsvector + GIN   -> 60 candidates
        fuse         : Reciprocal Rank Fusion
     |
     v  top 30
  [3] cross-encoder rerank                    bge-reranker-v2-m3
        every (query, chunk) pair scored      CPU, .87               ~200-500 ms (estimate)
     |
     v  top 5, with calibrated scores
  [4] RELEVANCE GATE
        top score >= threshold  -> grounded   : build context, cite
        top score <  threshold  -> ungrounded : drop context, label the answer
     |
     v
  [5] prompt construction + token budgeting
     |
     v
  [6] stream from the gateway (.87:4000 -> .226)      first token < 2 s  (N3)
```

Numbers marked as estimates are estimates. Replace them with measurements from
[`17-evaluation.md`](./17-evaluation.md) when they exist.

---

## Build

### 4. Reciprocal Rank Fusion

The two arms produce scores that are not comparable. Cosine similarity lives in roughly 0.3–0.9 for
almost everything; `ts_rank_cd` is unbounded and depends on document length and term frequency.
Normalising them onto a common scale requires knowing each arm's score distribution — which shifts
with the query and with the corpus.

RRF sidesteps the problem entirely by throwing away the scores and keeping only the **ranks**:

```
        RRF(chunk) = sum over arms of  1 / (k + rank_in_that_arm)      k = 60
```

| Property | Consequence |
|---|---|
| Uses ranks, not scores | Immune to incomparable scales, and to either arm's score distribution drifting |
| No weights to tune | Nothing to overfit to the eval set — a real risk with a 50-question set |
| Agreement is rewarded | A chunk in both arms' top 10 outranks one that is rank 1 in a single arm. That is usually correct |
| `k = 60` flattens the head | The gap between ranks 1 and 2 is small; the gap between 1 and 20 is large |

The last row is the one that makes RRF the right choice *for us specifically*. RRF is not trying to
produce the final ordering — a cross-encoder does that in the next step. Its only job is to assemble
a candidate set of ~30 that contains the right chunks somewhere. Precision at rank 1 is not what we
are buying from it; recall at rank 30 is.

A tuned weighted blend (`0.7 * normalised_dense + 0.3 * normalised_lexical`) can beat RRF when tuned
well. It can also be badly wrong on query types the tuning set did not contain, and it needs
re-tuning whenever the corpus shifts. Untuned robustness is worth more than tuned optimality here,
because nobody is going to re-tune it in six months.

The SQL is in [`10-data-layer.md`](./10-data-layer.md) §10. Two parameters:

- **60 candidates per arm.** Enough that a chunk ranked poorly by one arm still enters the pool.
- **30 fused candidates out.** The reranker's input. See §5 for why 30 and not 100.

### 5. Cross-encoder reranking on CPU

**What a cross-encoder does differently.** The embedding model is a *bi-encoder*: query and chunk are
encoded separately, never seeing each other, and compared by cosine. That is what makes it fast
enough to index a whole corpus — vectors are computed once at ingest — and it is also what limits it.
A cross-encoder concatenates query and chunk and runs them through a transformer *together*, with
full attention between them, producing one relevance score. It can see that "the 2019 figure" in the
chunk refers to the year in the query. The bi-encoder structurally cannot.

The cost is that it cannot be precomputed: every `(query, chunk)` pair is a forward pass at query
time. That is why it runs over 30 candidates and not 500,000, and it is why the architecture is
retrieve-then-rerank rather than rerank-everything.

**This is the single largest quality lever in the system, and it costs no GPU.**

| | Bi-encoder (retrieval) | Cross-encoder (rerank) |
|---|---|---|
| Sees query and chunk together | No | **Yes** |
| Precomputable | Yes — at ingest | No — per query |
| Cost | One vector lookup per query | One forward pass per candidate |
| Scales to | Whole corpus | Tens of candidates |
| Quality | Good | Substantially better |

**Where it runs.** `bge-reranker-v2-m3` (MIT, ~568M parameters) on `.87`'s CPU. `.87` is an i9-14900K
with 24 cores and 128 GB, and is the least contended box in the fleet — its GPU is reserved for
embeddings and small always-on models, and its CPU is otherwise mostly idle. Reranking there is
close to free in the sense that matters: it consumes a resource we are not otherwise using and does
not touch the VRAM that [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) is fighting over.

Run it as a second Infinity container, CPU-only, on its own port:

```yaml
# deploy/host-87/compose.yaml  (excerpt)
services:
  infinity-embed:                       # GPU: embeddings
    image: michaelf34/infinity:latest   # pin a real tag; 'latest' is not a deploy
    command: >
      v2 --model-id Qwen/Qwen3-Embedding-0.6B --revision ${EMBED_REVISION}
         --device cuda --port 7997
    ports: ["7997:7997"]
    deploy:
      resources: { reservations: { devices: [{ capabilities: [gpu] }] } }

  infinity-rerank:                      # CPU: reranking
    image: michaelf34/infinity:latest
    command: >
      v2 --model-id BAAI/bge-reranker-v2-m3 --revision ${RERANK_REVISION}
         --device cpu --port 7998
    ports: ["7998:7998"]
    cpuset: "8-19"                      # 12 cores. Leave the rest of the box alone
    environment:
      OMP_NUM_THREADS: "12"
```

Two containers rather than one process inside the RAG service, for a reason worth stating: it keeps
the RAG service free of a multi-gigabyte model and of PyTorch's thread pools, so the service stays a
thin, fast-restarting, stateless thing — which is the property
[`13-rag-service-api.md`](./13-rag-service-api.md) depends on.

**The CPU contention risk, honestly.** [`tech-stack.md`](./tech-stack.md) §5 flags reranking as
competing with the long-running simulation runs for CPU. Those runs live on `.226`, so on today's
fleet layout the reranker on `.87` does not touch them. But two things keep this on the risk register:
`.87` is still somebody's workstation, and N6 ("the modelling runs are not measurably slowed") is a
harder requirement than any latency target here. So:

- **Cap the cores explicitly** with `cpuset` and `OMP_NUM_THREADS`. An uncapped PyTorch will happily
  take all 24 and make the machine feel unresponsive to the person sitting at it. Capping is cheap
  insurance and costs us only a slower rerank.
- **Never rerank on `.226`.** The one place it must not go.
- **The documented fallback** if CPU reranking proves too slow under load is
  [`tech-stack.md`](./tech-stack.md) §9's row: move the reranker to `.87`'s GPU and drop the small
  chat model. That trade is available; take it only with measurements in hand.

```python
# services/rag/rag/rerank.py
import math, httpx
from rag.config import RERANK

_URL = "http://10.0.0.87:7998/rerank"
_TIMEOUT = httpx.Timeout(connect=2.0, read=10.0, write=5.0, pool=2.0)

def _to_probability(raw: float) -> float:
    """bge-reranker emits a logit. Sigmoid puts it on 0..1 so a threshold is interpretable."""
    return 1.0 / (1.0 + math.exp(-raw))

async def rerank(query: str, candidates: list[Candidate], top_n: int = 5) -> list[Scored]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(_URL, json={
            "model": RERANK.name,
            "query": query,
            "documents": [c.content for c in candidates],
            "return_documents": False,
        })
        r.raise_for_status()
    results = r.json()["results"]
    scored = [
        Scored(candidate=candidates[item["index"]], score=_to_probability(item["relevance_score"]))
        for item in results
    ]
    scored.sort(key=lambda s: (-s.score, s.candidate.chunk_id))   # deterministic tie-break
    return scored[:top_n]
```

**Check whether your Infinity build already applies the sigmoid** before applying it again — a
double-sigmoid squashes everything into a narrow band around 0.5 and makes the threshold in §6
meaningless while still looking plausible. Print raw scores for ten queries the first time you run
this and confirm the range looks like probabilities.

**Why 30 in, 5 out.**

| Candidates in | Effect |
|---|---|
| 10 | Reranking cannot recover a chunk that hybrid search ranked 15th. Wastes the lever |
| **30** | **Chosen.** Deep enough to fix ordinary retrieval misses; ~30 forward passes is affordable on CPU |
| 100 | Latency grows roughly linearly on CPU; the marginal chunk rescued is rare. Revisit only with data |

| Chunks out | Effect |
|---|---|
| 3 | Tight and cheap; a multi-part question loses a needed source |
| **5** | **Chosen.** ~2,500 tokens of context, which fits a local model's budget with room for an answer |
| 10 | The tail chunks are usually weak, and a local model is *harmed* by weak context (§1) |

Both numbers are hypotheses until the eval set speaks. The asymmetry is deliberate: be generous
about what enters the reranker, stingy about what reaches the model.

### 6. The relevance gate

**The problem.** The retriever always returns something. Ask "what is the capital of France" of a
corpus of traffic engineering reports and you get five chunks — the least-irrelevant five, ranked
confidently. Hand those to a local model and it will write something that sounds sourced. That is the
worst possible failure of a citation system: not "I don't know", but a confident wrong answer
wearing references.

**The mechanism.** Compare the top rerank score against a threshold.

```python
# services/rag/rag/gate.py
from dataclasses import dataclass
from rag.config import RELEVANCE_THRESHOLD     # a calibrated constant, see section 7

@dataclass(frozen=True)
class GateDecision:
    grounded: bool
    top_score: float
    threshold: float
    passages: list[Scored]        # empty when not grounded -- the context is DROPPED

def apply_gate(scored: list[Scored], threshold: float = RELEVANCE_THRESHOLD) -> GateDecision:
    top = scored[0].score if scored else 0.0
    if top < threshold:
        return GateDecision(False, top, threshold, [])
    keep = [s for s in scored if s.score >= threshold * 0.6]    # keep the supporting tail
    return GateDecision(True, top, threshold, keep or scored[:1])
```

Above the threshold: build a grounded prompt with those passages, answer from them, cite document and
page. Below: **drop the context entirely** and answer from the model's own knowledge, with the answer
explicitly labelled as not grounded in our documents.

**Dropping the context is the part people get wrong.** The tempting alternative is to include the
weak chunks anyway "just in case, the model can decide". It cannot — see §1. Weak context makes a
local model's answer worse than no context, because it anchors on the irrelevant material rather than
ignoring it. If the gate says no, the context goes in the bin.

This is requirement **F7** from [`00-goals-and-constraints.md`](./00-goals-and-constraints.md), and
the gate is the entire mechanism by which we satisfy it.

**Why a deterministic threshold beats an LLM router.**

| | Threshold on a calibrated score | Asking an LLM "is this relevant?" |
|---|---|---|
| Reliability | Same input, same decision, always | A local 14B follows this kind of meta-instruction unreliably; a 4B ladder rung, worse |
| Cost | Free — the score already exists | An extra generation round trip inside the latency budget |
| Tunable | One number, calibrated against the eval set | Prompt engineering, re-validated per model |
| Debuggable | The score is logged; every decision is explainable after the fact | "The model felt it wasn't relevant" |
| Fails how | Predictably, in a direction you chose | Unpredictably, and differently after a model swap |
| Survives a model change | Yes — it does not involve the generation model | No. Every rung of the ladder is a different router |

That last row is decisive given [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md). The
generation model changes underneath us as the ladder demotes from 30B to 14B to 8B to 4B when someone
claims the GPU. A routing decision made by that model would become less reliable exactly when the
platform is already degraded. The reranker score does not move, because the reranker is not on the
ladder. **Grounding behaviour must not depend on which rung we are on.**

The general principle: when a decision can be made by a deterministic function of a measured score,
do not delegate it to a language model. Reserve the model for what only a model can do.

**Two refinements worth building in from the start:**

- **Report the score.** Every response carries `top_rerank_score` and `threshold` in its metadata
  ([`13-rag-service-api.md`](./13-rag-service-api.md) §6). A user reporting "it said it didn't know
  but the answer is definitely in the handbook" becomes a diagnosable event with a number attached,
  and a row in `rag.retrieval_events` to look up.
- **Keep the supporting tail, not just the top chunk.** Once grounded, include chunks scoring above a
  softer secondary threshold (`0.6 * threshold` above). A multi-part question needs its second and
  third sources; a single strong chunk plus four weak ones should not drag four weak ones in.

**The failure this does not catch:** a chunk that is *topically* relevant but does not contain the
answer. The reranker scores it highly and correctly — it is about the right subject — and the model
then produces a confident answer the passage does not actually support. The gate is a relevance gate,
not a sufficiency gate. Mitigate it in the prompt (§8: "if the passages do not contain the answer,
say so and cite nothing") and measure it as groundedness in
[`17-evaluation.md`](./17-evaluation.md). Do not claim the gate solves it.

### 7. Calibrating the threshold

The threshold is a number we must earn, not guess. Guessing produces one of two bad platforms: too
low and ungrounded answers get dressed in citations; too high and the system says "not in your
documents" about things that are plainly in the documents, which is the faster route to abandonment.

**The eval set makes this possible, and it must contain both kinds of question.**
[`delivery-plan.md`](./delivery-plan.md) §7 says to write ~50 Q&A pairs starting in M1 — before the
retriever exists, so the questions cannot be unconsciously fitted to the implementation. Extend that:

| Class | Count | Expected gate decision |
|---|---|---|
| Answerable from the corpus, with a known source page | ~35 | grounded |
| Plausible-sounding but genuinely absent from the corpus | ~15 | **ungrounded** |

The second class is the one everybody forgets and the one that makes the threshold calibratable at
all. Write questions that *sound* like corpus questions — right vocabulary, right domain, wrong
subject — because those are what the gate has to catch.

```python
# scripts/calibrate_threshold.py -- sweep, then choose with your eyes open
import numpy as np

scores  = np.array([...])   # top rerank score per eval question, after retrieval+rerank
labels  = np.array([...])   # True = the corpus really can answer it

print(f"{'thr':>6} {'grounded%':>10} {'false_grnd':>11} {'false_ungrnd':>13}")
for thr in np.arange(0.10, 0.90, 0.02):
    grounded      = scores >= thr
    false_grnd    = int(( grounded & ~labels).sum())   # cited an answer it could not support
    false_ungrnd  = int((~grounded &  labels).sum())   # refused an answer it had
    print(f"{thr:6.2f} {grounded.mean()*100:9.1f}% {false_grnd:11d} {false_ungrnd:13d}")
```

**How to read the sweep.** The two error columns move in opposite directions. Pick the lowest
threshold at which `false_grnd` reaches zero, *then* look at what `false_ungrnd` costs you there. If
that cost is unacceptable, the honest conclusion is that retrieval is not good enough yet — fix
chunking or rerank depth — rather than that the threshold should be lowered.

**Which error to prefer.** A false-grounded answer is worse than a false-ungrounded one, and not by a
little. A wrong citation, once noticed, poisons every correct citation the system ever produces
([`11-ingestion.md`](./11-ingestion.md) §4 makes the same argument about page numbers). A
false-ungrounded answer is merely unhelpful, is visibly labelled, and the user can rephrase. **Bias
the threshold high.**

**A starting hypothesis, marked as such.** With sigmoid-normalised bge-reranker-v2-m3 scores, a
threshold somewhere in **0.3–0.5** is a plausible first guess — but that is a guess about a score
distribution we have not observed, on a corpus that does not exist yet. Do not ship it as a constant
without running the sweep. Record the sweep table in [`17-evaluation.md`](./17-evaluation.md) when it
exists, and put the chosen number and the date next to it.

**Recalibrate when** the embedding model changes, the reranker changes, chunk size changes, or the
corpus composition shifts substantially. `rag.retrieval_events` gives you the production score
distribution for free — if the fraction of ungrounded answers drifts, that is the signal.

### 8. Prompt construction and token budgeting

The context is assembled, not concatenated. Four decisions:

**Order matters, and the intuitive order is wrong.** Models attend less reliably to the middle of a
long context than to either end. Our best chunk therefore goes **last**, immediately before the
question — the position with the strongest recency effect — with weaker chunks earlier. Citation
numbers stay stable regardless of position, so the numbering the user sees still matches rank.

**Every passage is labelled with its citation number and its provenance**, in the context itself.
This is what makes the model able to write `[2]` correctly, rather than us trying to attach citations
post hoc by string matching.

**Neighbouring chunks from the same document are merged** when adjacent by `ordinal`. Two overlapping
512-token chunks share ~64 tokens; presenting both wastes budget and reads like repetition, which
some models mirror.

**The prompt states the ungrounded rule explicitly**, because the gate handles the "no relevant
documents at all" case but not the "relevant topic, missing fact" case (§6).

```python
# services/rag/rag/prompt.py
GROUNDED_SYSTEM = """\
You are answering questions about the team's internal documents.

Use ONLY the passages below. Each is labelled [n] with its document and page.

- Cite every factual claim inline with its bracket number, e.g. [2].
- If the passages do not contain the answer, say so plainly and cite nothing.
  Do not fill the gap from general knowledge.
- Do not invent citation numbers. Only [1]..[N] below exist.
- Quote exact figures, identifiers and names from the passages rather than paraphrasing them.
"""

UNGROUNDED_SYSTEM = """\
You are answering from general knowledge. Nothing in the team's documents was relevant
to this question.

Begin your reply with exactly: **UNGROUNDED -- not based on team documents.**
Then answer normally. Do not fabricate citations or references to internal documents.
"""

def build_context(passages: list[Scored]) -> str:
    blocks = []
    for i, s in enumerate(passages, start=1):
        c = s.candidate
        page = (f"p. {c.page_start}" if c.page_start == c.page_end
                else f"pp. {c.page_start}-{c.page_end}")
        blocks.append(f"[{i}] {c.document_title}, {page}\n{c.content}")
    return "\n\n---\n\n".join(blocks)
```

**The token budget.** Context is not free, and on a local model it is doubly not free: KV cache is
VRAM, and VRAM is what [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) is rationing. Per
[`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) §2, a Qwen3-14B-class model costs roughly
0.16 MB per token of KV cache, so four concurrent 16k-context streams is a real 10 GB commitment.
Every token we spend on a marginal chunk is a token unavailable to another user.

Budget for a 16k working window and enforce it, rather than discovering it at generation time:

| Component | Budget | Note |
|---|---|---|
| System prompt | ~250 | Fixed |
| Retrieved context | **~3,000** | 5 chunks x ~512 tokens, plus labels |
| Conversation history | ~2,000 | Most recent turns; oldest dropped first |
| Current question | ~200 | |
| Reserved for the answer | **~2,000** | Reserved *first*, never encroached upon |
| Headroom | remainder | Absorbs tokeniser disagreement between our count and the server's |

```python
def fit_context(passages, budget_tokens: int = 3000):
    """Drop the weakest passages until the context fits. Never truncate mid-passage:
    a half-passage can be cited, and a citation to text the model never saw is a wrong citation."""
    kept, used = [], 0
    for s in passages:                          # already sorted best-first
        n = s.candidate.token_count + 24        # +label overhead
        if used + n > budget_tokens:
            break
        kept.append(s); used += n
    return kept
```

**Reserve the answer allocation first.** The failure mode of not doing so is a truncated answer,
which is worse than a slightly thinner context: the user sees a sentence stop mid-word and loses
confidence in the whole system, whereas they cannot see the fourth chunk that was dropped.

**Never truncate a passage mid-way to make it fit.** Drop it. A partially-included passage still
carries citation number `[4]`, and the model may cite `[4]` for a claim from the part that was cut —
producing a citation to a page whose relevant text was never in the prompt. That is the wrong-page
failure from [`11-ingestion.md`](./11-ingestion.md) §4 arriving through a different door.

### 9. Latency budget

N3 requires time-to-first-token under 2 s on the fast tier, warm. Retrieval spends its share before
the generation model has seen anything:

| Stage | Estimate | Where it runs | If it must be cut |
|---|---|---|---|
| Embed query | 50–150 ms | Infinity GPU, `.87` | Cache embeddings of repeated queries |
| Hybrid search | tens of ms | Postgres, `.87` | Lower `hnsw.ef_search`; fewer candidates per arm |
| Rerank 30 candidates | 200–500 ms | CPU, `.87` | Rerank 20 instead of 30 — the last thing to cut |
| Gate + prompt assembly | <10 ms | RAG service | — |
| **Retrieval subtotal** | **~0.3–0.7 s** | | |
| Generation TTFT | remainder | `.226` via the gateway | Prompt caching if vLLM supports it for your config |

**Every number here is an estimate.** Measure them per stage and log them to
`rag.retrieval_events.timings_ms`, so that when someone says "it feels slow" there is a per-stage
breakdown rather than a debate. That table exists for exactly this.

Note the shape of the budget: reranking is the largest retrieval cost and also the largest quality
gain. If the budget is tight, cut candidate count before cutting the reranker — going from 30 to 20
candidates costs a third of the rerank time and a small amount of recall, whereas removing the
reranker entirely costs the single biggest quality lever we have.

---

## Reflect

**What we traded away.** A tuned weighted fusion probably beats RRF on our corpus by a small margin,
and we gave that up for robustness and for not having a weight to overfit. A real BM25 engine beats
`tsvector`, and we gave that up to avoid operating Elasticsearch. Both trades exchange a few points
of measured quality for durability and one less service — the right call at 10 seats, and both are
worth revisiting only with eval numbers pointing at them.

**The thing most likely to bite.** The threshold. It is one scalar standing between "grounded answer
with citations" and "honest admission of ignorance", it is calibrated on a ~50-question set which is
small enough that a handful of mislabelled questions move it meaningfully, and it will need
recalibrating whenever the corpus composition changes. The mitigation is not a better number — it is
the logging: score, threshold and decision on every request, so drift is visible rather than
inferred from complaints.

**What we would revisit first.** A sufficiency check distinct from the relevance check. The gate
answers "is this passage about the right thing", which is not the same question as "does this passage
contain the answer", and the gap between those two is where our remaining confident-but-wrong answers
will live. A second cheap pass — a small model asked only "does this passage state the answer, yes or
no" — is tempting, but it reintroduces exactly the LLM-as-judge unreliability §6 rejected. The
honest first step is to *measure* how often it happens, in M8, before designing a fix for it.

**Next:** [`13-rag-service-api.md`](./13-rag-service-api.md).
