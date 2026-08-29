# 17 — Evaluation

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> How we make quality a number instead of an argument. This document implements **N7** from
> [`00-goals-and-constraints.md`](./00-goals-and-constraints.md) and supplies the acceptance evidence
> for **N3**, **N4** and **N9**.
>
> Note: `00`'s N7 row points at `16-evaluation.md`, an earlier numbering. This file is the one it
> means; fix the cross-reference when `00` is next revised.

---

## Concept

## 1. Write the questions before you write the retriever

This is the whole argument of the document, so it goes first.

[`delivery-plan.md`](./delivery-plan.md) §7 starts eval-set authoring at **M1**, in parallel with host
setup — four to six weeks before the retriever it will judge exists. That is not scheduling
convenience. It is the only way the eval set stays honest.

Build the retriever first and you will write the eval set second, and you will write it *while
looking at retrieval output*. Every question you draft will be one you have half-watched the system
answer. The chunk boundaries you chose will shape which facts feel like "a question". The queries
that embarrassed you in manual testing will quietly not get written down. The result looks like an
eval set, produces a number, and measures nothing except your own memory of what already worked.

This failure is not a discipline problem. It happens to careful people, because the bias runs through
what feels like a *reasonable* question — and by then the implementation has already defined
reasonable.

So: **the eval set is authored against the corpus, by a person reading source documents, with no
retrieval system in existence.** It needs the corpus, not the code — which is exactly why it can run
in parallel while `.87` and `.226` are still being brought up.

Three further reasons it is worth doing early:

- **It is the only artifact here that outlives every component.** The embedding model will change.
  The chunker will change. The reranker, the gate threshold, the generator model — all of them will
  change, several times. A fixed eval set is what makes those changes comparable instead of
  successive fresh starts.
- **It converts M5 from a judgement call into a ratchet.** [`delivery-plan.md`](./delivery-plan.md)
  gives M5 eight to twelve days and calls it a third of the project. Without a fixed target, "is
  retrieval good enough yet?" gets answered by whoever is most tired.
- **It forces you to read the corpus.** An afternoon with the actual PDFs teaches more about chunking
  strategy than a week of tuning would.

**The cost is one to two days of one person's attention.** It is the most commonly skipped preparation
in RAG projects and the one that most reliably pays for itself.

---

## 2. What gets measured, and against which requirement

Four separable layers. Keep them separable — a single "quality" number tells you nothing about where
to spend the next day.

| Layer | The question it answers | Metric | Requirement | Automatable? |
|---|---|---|---|---|
| **Retrieval** | Did the right page reach the model at all? | recall@k, MRR | **N7** | Fully |
| **Grounding** | Does the cited page actually support the claim? | citation precision | F6 | Partly — final call is human |
| **Refusal** | Do we say "I don't know" when we should? | gate precision / recall | F7 | Fully, given labels |
| **Latency** | Is it fast enough to use? | TTFT, tok/s, p95 | **N3**, **N4** | Fully |

Retrieval and latency are objective and cheap, so they run on every change. Grounding is expensive and
human, so it runs per release on a sample. §9 is honest about why we do not automate that last one
with an LLM judge.

---

## Build

## 3. Building the eval set

### 3.1 Composition — about 50 pairs

Fifty is chosen deliberately: large enough that a category has enough members to say something, small
enough that one person can author it in a day or two and a human can spot-check its answers in under
an hour. It is **not** large enough for tight confidence intervals — see §11.

| # | Category | Count | What it stresses | Failure it catches |
|---|---|---|---|---|
| A | **Easy lookup** | 12 | The baseline path | Anything fundamentally broken — chunking, embedding, page mapping |
| B | **Multi-hop** | 8 | Retrieval breadth at k | Top-1 tuning that starves synthesis; over-aggressive reranking |
| C | **Exact-token** | 10 | The lexical half of hybrid search | Dense-only retrieval — part numbers, error codes, acronyms |
| D | **Paraphrase** | 10 | The dense half | Lexical-only retrieval — the user's words never appear on the page |
| E | **Unanswerable** | 8 | The relevance gate (F7) | Confident fabrication; a threshold set too low |
| F | **Near-miss** | 2 | The gate's hardest case | Grounding on a page that *discusses* the topic but does not answer |

That is 50 exactly. The counts are a starting shape, not a rule — if the corpus is mostly parts
catalogues, category C deserves more.

**How to write each category:**

- **A — Easy lookup.** One fact, one page, stated plainly. "What is the maximum operating temperature
  of the X unit?" These should never fail. When one does, something structural is wrong and the whole
  run is suspect.

- **B — Multi-hop.** The answer requires two or more pages, ideally in two or more documents. "Which
  of the units listed in the fleet register exceeds the temperature limit given in the spec?" Record
  **all** the pages needed, and score on retrieving *every* required page, not any of them — that is
  the point of the category. Multi-hop is where a reranker tuned for precision quietly hurts you,
  because it is very good at returning five chunks about the same thing.

- **C — Exact-token.** Scan the corpus for strings that no embedding model has ever usefully placed in
  a vector space: part numbers (`BRK-4471-C`), error codes (`E-0x2F14`), internal acronyms, surnames,
  standard references (`ISO 14971:2019`), version strings. Ask questions that hinge on the exact
  token. This category exists to prove the lexical half of hybrid retrieval is earning its keep — if
  removing `tsvector` from the fusion does not move these, the fusion is misconfigured.

- **D — Paraphrase.** Take a passage and ask about it using **none of its content words**. If the page
  says "thermal shutdown is initiated at 85 degrees C", ask "when does the device cut out from getting
  too hot?" Write these last, after a break, so you stop unconsciously reaching for the source's
  vocabulary. This is the category that justifies dense retrieval, and the mirror image of C.

- **E — Unanswerable.** The hardest to write well, and the most valuable. They must be **plausible and
  adjacent** — a question a real user would ask of this corpus, whose answer genuinely is not in it.
  "What is the warranty period on the X unit?" when the corpus is technical specifications with no
  commercial terms. Do **not** write absurd questions ("what is the capital of France?"): every gate
  rejects those, so they measure nothing and inflate the refusal score.

- **F — Near-miss.** The pathological case: a document that discusses exactly the topic at length
  without containing the answer. A troubleshooting guide covering the same error family but not this
  code. These are where retrieval confidently returns highly similar chunks and the gate has to refuse
  anyway. Two is enough — they are hard to find and unpleasant to write.

**Where the questions come from.** Read real documents from the real corpus. Better still, spend
twenty minutes asking two or three of the ten users what they would ask this thing. Their questions
are differently shaped from yours, and the difference is the point.

### 3.2 Record format

One JSONL file, one record per line, in the repo:

```jsonl
{"id": "A-01", "category": "easy", "answerable": true, "question": "What is the maximum operating temperature of the BRK-4471-C brake unit?", "expected": [{"doc": "brk-4471-spec-rev-c.pdf", "pages": [12]}], "answer": "85 degrees C, above which thermal shutdown is initiated.", "author": "ak", "written": "2026-09-04", "notes": "stated in the environmental table, not the prose"}
{"id": "B-03", "category": "multihop", "answerable": true, "question": "Which units in the 2025 fleet register exceed the thermal limit in the BRK-4471 spec?", "expected": [{"doc": "brk-4471-spec-rev-c.pdf", "pages": [12]}, {"doc": "fleet-register-2025.pdf", "pages": [4, 5]}], "answer": "Units 118 and 204.", "author": "ak", "written": "2026-09-04", "notes": "requires ALL listed pages; score strictly"}
{"id": "E-02", "category": "unanswerable", "answerable": false, "question": "What is the warranty period on the BRK-4471-C?", "expected": [], "answer": null, "author": "ak", "written": "2026-09-05", "notes": "plausible; corpus has no commercial terms at all"}
```

| Field | Why it exists |
|---|---|
| `id` | Stable across runs, so a regression names itself |
| `category` | Every metric is reported per-category; the aggregate hides the interesting failures |
| `answerable` | The label the relevance gate is calibrated against (§6) |
| `expected[].pages` | **Pages, not chunks.** Chunk boundaries move; page numbers do not |
| `answer` | The short reference answer, for human spot-checks — not for automated string matching |
| `notes` | Why this question is hard. You will not remember in six weeks |

**Pages, not chunk IDs, is load-bearing.** Score a hit as "a retrieved chunk whose `(document, page)`
is in `expected`". That keeps the eval set valid across every chunking change you will make — which is
the main thing you will be changing.

### 3.3 Rules while authoring

1. **No retrieval system may exist, or if it does, do not run it.** §1.
2. **Open the source document and read the page.** Copy the page number from the document, then check
   it against the PDF's *physical* page index — cover pages and roman-numeral front matter are the
   origin of the off-by-one bug in §5.
3. **Freeze it.** Once complete, it is version-controlled and does not change. Growth goes into
   `eval-set-v2.jsonl`; results are always reported against a named version.
4. **Never edit a question because the system fails it.** That is the exact bias §1 exists to prevent.
   If a question is genuinely wrong (bad page reference, ambiguous wording), fix it, note the fix in
   the commit message, and re-baseline everything.
5. **Record the corpus state.** Which documents were present when the set was written. A question
   becomes silently unanswerable when someone removes a document.

### 3.4 Where it lives

Add to the repo layout in [`delivery-plan.md`](./delivery-plan.md) §2:

```
ai-platform/
  eval/
    eval-set-v1.jsonl          # frozen; the questions
    corpus-manifest-v1.txt     # sha256 + filename of every doc present when authored
    run_retrieval_eval.py      # section 4
    calibrate_gate.py          # section 6
    bench_latency.py           # section 7
    compare_models.py          # section 8
    results/
      2026-10-14-retrieval.txt
      2026-10-14-gate-scores.csv
      ...
```

Results are committed. They are small, and the history is the point — a git log of eval results is the
record of whether the project got better.

---

## 4. Retrieval metrics (N7)

### 4.1 Definitions

| Metric | Definition here | Why this one |
|---|---|---|
| **recall@k** | Fraction of answerable questions where the expected `(doc, page)` appears in the top *k*. For multi-hop, **all** expected pages must appear | The primary number. If the page never reaches the model, nothing downstream can fix it |
| **recall@5** | k = 5 | **The headline (N7).** 5 is roughly what fits a local 14–30B model's context alongside a question and an answer |
| **recall@30** | k = 30, measured **before** reranking | The *ceiling*. A reranker can only reorder what retrieval found. If this is low, stop tuning the reranker |
| **MRR** | Mean of 1/rank of the first correct page | Position sensitivity. recall@5 cannot tell 1st place from 5th; MRR can, and rank 1 gets read more carefully by a small model |

Report all four, **per category**. The aggregate is for the changelog; the per-category table is what
tells you what to do next.

### 4.2 The harness

`eval/run_retrieval_eval.py` calls the RAG service's retrieval path directly — not
`/v1/chat/completions` — so generation cost and non-determinism stay out of the measurement. Expose a
`/v1/retrieve` debug endpoint on the RAG service that returns ranked candidates with scores and skips
generation entirely. It is worth building for this alone; it is also the endpoint you will use to
debug every retrieval complaint for the life of the project.

```python
# eval/run_retrieval_eval.py  (sketch - the shape, not the finished code)
import json, httpx, statistics
from collections import defaultdict

RAG = "http://10.0.0.87:8100"
MODES = ["dense", "lexical", "hybrid", "hybrid+rerank"]   # ablation: each isolates one component

def hit(expected, got, k):
    """got: [{'doc':..,'page':..}, ...] ranked. Multi-hop needs every expected page."""
    top  = {(c["doc"], c["page"]) for c in got[:k]}
    need = {(e["doc"], p) for e in expected for p in e["pages"]}
    return need.issubset(top)

def first_rank(expected, got):
    need = {(e["doc"], p) for e in expected for p in e["pages"]}
    for i, c in enumerate(got, 1):
        if (c["doc"], c["page"]) in need:
            return i
    return None

def run(mode, questions):
    per_cat = defaultdict(lambda: {"n": 0, "r5": 0, "r30": 0, "rr": []})
    with httpx.Client(timeout=60) as http:
        for q in questions:
            if not q["answerable"]:
                continue                 # answerable-only; unanswerable are scored in section 6
            got = http.post(f"{RAG}/v1/retrieve",
                            json={"query": q["question"], "mode": mode, "k": 30}
                            ).json()["candidates"]
            c = per_cat[q["category"]]
            c["n"]   += 1
            c["r5"]  += hit(q["expected"], got, 5)
            c["r30"] += hit(q["expected"], got, 30)
            rank = first_rank(q["expected"], got)
            c["rr"].append(1.0 / rank if rank else 0.0)
    return per_cat

if __name__ == "__main__":
    qs = [json.loads(l) for l in open("eval/eval-set-v1.jsonl")]
    for mode in MODES:
        for cat, c in sorted(run(mode, qs).items()):
            print(f"{mode:14} {cat:12} n={c['n']:3}  "
                  f"recall@5={c['r5']/c['n']:.2f}  recall@30={c['r30']/c['n']:.2f}  "
                  f"MRR={statistics.mean(c['rr']):.2f}")
```

```bash
# Run the full ablation and keep the output
python eval/run_retrieval_eval.py | tee eval/results/$(date +%F)-retrieval.txt
```

**Why the four-mode ablation matters.** Running only `hybrid+rerank` gives one number and no
direction. Running all four tells you which component is actually contributing, and it is the only way
to notice that — for example — the lexical half has been silently broken since a schema migration,
because the aggregate barely moved while category C collapsed.

### 4.3 Before and after reranking — the number that must be visible

[`tech-stack.md`](./tech-stack.md) §5 calls reranking "the largest quality lever" and prices it at
200–500 ms per query on `.87`'s CPU. That is a claim, and it costs real latency out of the N3 budget.
Measure it or drop it.

```
   retrieval stage        what recall@k means there
   ---------------------------------------------------------------
   hybrid, top 30    -->  recall@30 = the CEILING. Rerank cannot
                          recover a page that was never retrieved.
                          Low here => fix chunking / fusion / embeddings.
   rerank, top 5     -->  recall@5  = what the model actually sees.
                          The delta from hybrid's own recall@5 is
                          the reranker's entire contribution.
```

Report it as one line in every release note:

| Configuration | recall@5 | MRR | Added latency | Verdict |
|---|---|---|---|---|
| hybrid (RRF), no rerank | | | 0 ms | |
| hybrid + bge-reranker-v2-m3 | | | | |
| **Delta** | | | | |

**Pass:** the reranker improves recall@5 by more than run-to-run noise, and improves MRR noticeably
(it is a reordering component — MRR is where it should show most).
**Fail:** the delta is within noise. Then drop the reranker, reclaim 200–500 ms of the TTFT budget, and
put the effort into chunking instead. Shipping a 300 ms cost on faith is exactly what this document
exists to prevent.

### 4.4 Targets

Set the bar from the first honest run at M5, then treat it as a ratchet. Opening targets:

| Metric | Target | If missed, look at |
|---|---|---|
| recall@5, category A | ~1.00 | Something structural — page mapping, embedding, ingestion |
| recall@5, overall answerable | >= 0.85 | Chunk size and overlap first, then the fusion |
| recall@30, overall answerable | >= 0.95 | If this is low, nothing downstream can help |
| MRR, overall | >= 0.70 | Reranker configuration; candidate depth |
| recall@5, category C (exact-token) | >= 0.90 | The lexical half. Check the `tsvector` config and the RRF constant |
| recall@5, category D (paraphrase) | >= 0.80 | The dense half. Check query vs passage embedding modes |

These are targets to confirm or revise, **not measurements**. Leave the measured column blank until it
is measured. Do not fill this table with estimates — a fabricated baseline is worse than none, because
everything after it is compared against a fiction.

**A regression rule more useful than any absolute target:** a change that drops recall@5 on any
category by more than one or two questions does not ship until it is explained.

---

## 5. Groundedness and citation correctness

[`01-architecture.md`](./01-architecture.md) §4 states it: *a citation that points at the wrong page
destroys trust in every other citation.* A user who checks one citation, finds it wrong, and stops
checking is a user who has stopped trusting the whole system — and they will be right to.

### 5.1 The three failure modes, in increasing order of harm

| # | Failure | Why it is dangerous |
|---|---|---|
| 1 | Right answer, no citation | Annoying. Visible. Self-correcting |
| 2 | Right answer, **wrong page cited** | **The trust-destroyer.** The answer checks out, so nobody verifies the pointer — until someone does, and then everything is suspect |
| 3 | Wrong answer, confident citation | Worst outcome, but usually caught faster because the answer itself is wrong |

Mode 2 is why this section exists, and it is overwhelmingly caused by mechanical bugs rather than by
the model: off-by-one page indexing, PDF logical page labels (roman front matter) versus physical
indices, multi-column parse order, and chunks that span a page boundary and get attributed to the
wrong side.

### 5.2 The automated half — cheap, run always

Three assertions, as a `make test` target, run on every RAG service change:

```python
# tests/test_citations.py  (sketch)
def test_citation_resolves():
    """Every cited (doc, page) exists in the documents table and is within page_count."""

def test_citation_was_in_context():
    """Every cited page was actually among the chunks passed to the generator.
       A citation to a page the model never saw is a hallucinated pointer."""

def test_page_provenance_roundtrip():
    """For 10 known chunks: the text at chunks.page in the source PDF contains
       the chunk's first 60 characters. This is the off-by-one detector."""
```

The third is the highest-value test in the project's whole suite. Write it during M5 ingestion work,
not here.

### 5.3 The human half — the spot-check protocol

Automation cannot tell you whether a page *supports a claim*. A person can, in about 90 seconds.

**Per release, sample 20 answered eval questions**, stratified — 6 from A, 5 from B, 4 from C, 5 from
D. For each, open the cited page and score:

| Score | Meaning |
|---|---|
| **2** | The cited page directly supports the claim |
| **1** | Partially — supports part of it, or only together with an uncited page |
| **0** | Does not support the claim, or the page is simply wrong |

Report two numbers:

- **Citation precision** = (count of 2s) / 20 — the fraction of citations that hold up.
- **Answer correctness** = fraction of the 20 answers a competent reader judges correct against the
  reference answer in the eval record.

**Pass:** citation precision >= 0.95. Below that is almost certainly a mechanical provenance bug, not
model quality — go find it.

Log the run: date, git tag, generator model, reviewer, both numbers, and the IDs of anything scored 0.
Twenty questions at 90 seconds is half an hour per release. That is the correct price.

---

## 6. Calibrating the relevance gate

The gate is the mechanism behind **F7** — "when the corpus doesn't contain the answer, the response is
explicitly marked as not grounded rather than silently guessed."
[`01-architecture.md`](./01-architecture.md) §4 defines it as: *is the top rerank score above a
threshold?* This section is how that threshold stops being a guess.

### 6.1 Why the eval set is the right instrument

Because it already carries the labels. Categories A–D are `answerable: true`; E and F are
`answerable: false`. That is a labelled binary classification dataset for the decision "should this
question be answered from the corpus?" — 42 positives, 8 negatives. Small, and much better than the
alternative, which is picking a round number because it looked reasonable in a REPL.

**This is why category E is not optional.** Without unanswerable questions you can measure only how
often the gate lets things through, never how often it should have stopped them — and a threshold of
zero scores perfectly.

### 6.2 The procedure

```bash
# 1. Dump the top-1 rerank score for all 50 questions, with the answerable label
python eval/calibrate_gate.py --dump > eval/results/$(date +%F)-gate-scores.csv
# columns: id,category,answerable,top1_rerank_score,top1_doc,top1_page

# 2. Sweep the threshold and print the confusion matrix at each step
python eval/calibrate_gate.py --sweep --from 0.0 --to 1.0 --step 0.02
```

```python
# eval/calibrate_gate.py  --sweep  (the part that matters)
for t in thresholds:
    tp = sum(1 for r in rows if r.answerable     and r.score >= t)   # correctly grounded
    fp = sum(1 for r in rows if not r.answerable and r.score >= t)   # FABRICATION RISK
    tn = sum(1 for r in rows if not r.answerable and r.score <  t)   # correctly refused
    fn = sum(1 for r in rows if r.answerable     and r.score <  t)   # needless refusal
    precision = tp / (tp + fp) if tp + fp else 1.0   # of what we ground, how much should be
    recall    = tp / (tp + fn) if tp + fn else 0.0   # of answerable questions, how many we answer
```

Fill in and keep:

| Threshold | TP (grounded, right) | FP (**grounded, wrong**) | TN (refused, right) | FN (refused, wrong) | Precision | Recall |
|---|---|---|---|---|---|---|
| 0.10 | | | | | | |
| 0.20 | | | | | | |
| 0.30 | | | | | | |
| 0.40 | | | | | | |
| 0.50 | | | | | | |
| 0.60 | | | | | | |
| 0.70 | | | | | | |

### 6.3 Reading the tradeoff, and which way to err

```
  threshold LOW                                     threshold HIGH
  |------------------------------------------------------------|
  answers everything                        refuses almost everything
  FP high: cites irrelevant pages           FN high: "not in the corpus"
  with full confidence                      for things that plainly are

  cost: a user is misled and                cost: a user is annoyed, rephrases,
        does not know it                          or opens the document
```

**Err toward refusing.** A false-ground is a confident answer built on irrelevant pages, and the user
has no signal that anything went wrong. A false-refuse is visible, self-correcting and merely
irritating. Concretely: find the knee of the precision/recall curve, then move **one step more
conservative** than the knee.

Two practical notes:

- **The mitigation for false-refusals is not a lower threshold.** It is the F7 behaviour itself: when
  the gate refuses, the RAG service still answers from model knowledge and *labels the answer
  ungrounded*. A refused question is not a dead end, it is an unsourced answer. That is what makes
  erring conservative cheap.
- **Score-distribution overlap is the real diagnostic.** Sort the top-1 scores and compare the
  answerable and unanswerable populations. Well separated, and any threshold in the gap works — the
  exact value hardly matters. Heavily overlapping, and **no threshold works**: the problem is
  retrieval, not the gate, and tuning the number is wasted effort. Check this before running the
  sweep.

### 6.4 The threshold is model-specific — recalibrate

`bge-reranker-v2-m3` scores are not calibrated probabilities, and whether the serving layer returns
raw logits or a sigmoid depends on configuration — **verify against your version of Infinity** and
record which it is next to the threshold in config. A threshold of 0.35 means nothing without that
context.

**Recalibrate whenever any of these change:** the reranker model, its serving config or normalisation,
the embedding model, the chunking strategy, or the candidate depth fed to the reranker. Make it a
checklist item in the runbook — a stale threshold degrades silently, which is the worst way for
anything to degrade.

---

## 7. Latency benchmarks (N3, N4)

### 7.1 What "warm" means, precisely

**N3 says TTFT < 2 s on the fast tier, warm.** Define warm so the number is reproducible:

- Model resident on the GPU — not slept, not loading.
- vLLM has served at least 5 requests since load, so CUDA graphs are captured and the allocator has
  settled.
- Postgres query plans cached; the RAG service's HTTP clients have live connections.
- **Discard the first 5 measurements of any run.** Report from the next 20.

Cold numbers are worth recording separately, but N3 is not a cold-start requirement and pretending
otherwise makes the target unachievable for the wrong reason.

### 7.2 Measure the stages, not just the total

A single TTFT number tells you that you regressed, not where. The RAG path has four stages and each
has a different owner:

```
  question in
    |
    |-- embed query (Infinity, .87 GPU) ............... target ~50-150 ms   [01 section 4]
    |-- hybrid SQL: pgvector + tsvector + RRF (.87) ... target tens of ms
    |-- rerank 30 candidates (bge, .87 CPU) .......... target 200-500 ms
    |-- gateway -> vLLM -> first token (.226) ........ the remainder
    v
  first token                                         TOTAL must be < 2 s (N3)
```

Have the RAG service emit these four timings as response headers or a trailing debug object. Then a
latency regression names its own cause, and you are not bisecting the stack at 11pm.

### 7.3 The benchmark

```bash
# Quick single-shot smoke check against the gateway
curl -s -N -X POST http://10.0.0.87:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen3-coder-30b","stream":true,
       "messages":[{"role":"user","content":"Write a haiku about brake systems."}]}' \
  | head -1
```

For the real measurement use a concurrency harness — `bench_latency.py` — that fires N streams
simultaneously and records per-stream TTFT and end-to-end time. Do not use a loop of `curl`; it
measures your shell, not the server.

```python
# eval/bench_latency.py  (sketch)
import asyncio, time, httpx, statistics

async def one(client, model, prompt):
    t0, ttft = time.perf_counter(), None
    async with client.stream("POST", f"{GW}/v1/chat/completions",
            json={"model": model, "stream": True,
                  "messages": [{"role": "user", "content": prompt}]}) as r:
        async for _ in r.aiter_lines():
            if ttft is None:
                ttft = time.perf_counter() - t0
    return ttft, time.perf_counter() - t0

async def at_concurrency(n, model, prompts, warmup=5, samples=20):
    async with httpx.AsyncClient(timeout=300) as c:
        await asyncio.gather(*(one(c, model, prompts[0]) for _ in range(warmup)))
        out = []
        for _ in range(samples):
            out += await asyncio.gather(*(one(c, model, p) for p in prompts[:n]))
    ttfts = sorted(t for t, _ in out)
    return {"n": n,
            "ttft_median": statistics.median(ttfts),
            "ttft_p95": ttfts[int(len(ttfts) * 0.95) - 1],
            "ttft_max": ttfts[-1]}
```

Use **varied prompts**, not the same string N times — vLLM's prefix caching will otherwise hand you a
flattering number no real user will ever see.

### 7.4 The tables to fill

**Fast tier chat, warm (N3, N4).** `.226`, top rung.

| Concurrent streams | TTFT median | TTFT p95 | tok/s per stream | tok/s aggregate | Pass? |
|---|---|---|---|---|---|
| 1 | | | | | < 2 s |
| 2 | | | | | < 2 s |
| 4 | | | | | < 2 s |
| 8 (overload probe) | | | | | informational |

**Pass (N3 + N4):** median TTFT < 2 s at 1, 2 and 4 concurrent, and nothing queued behind another
request at 4. The 8-stream row is not a requirement; run it to learn where the cliff is, because
knowing that number is what lets you answer "can we add five more seats?" without guessing.

**RAG endpoint, warm.** Same shape plus the stage breakdown from §7.2 — the RAG path adds embedding,
SQL and rerank before the generator is even called, so it has less than 2 s of budget for the model.

| Concurrent | Embed | SQL | Rerank | Gateway TTFT | Total TTFT | Pass? |
|---|---|---|---|---|---|---|
| 1 | | | | | | < 2 s |
| 2 | | | | | | < 2 s |
| 4 | | | | | | < 2 s |

If the total misses, the stage table says whether to attack the reranker (move it to `.87`'s GPU —
[`tech-stack.md`](./tech-stack.md) §9 lists this as a prepared response), the SQL (index tuning), or
the generator (a smaller rung).

**Deep tier.** Report separately and **do not apply N3 to it.**
[`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) §4 sizes it at ~10–20 tok/s and it is
explicitly the "worth waiting for" tier. What matters is that the UI sets the expectation.

| Context | TTFT | tok/s | Concurrent | Notes |
|---|---|---|---|---|
| 8k | | | 1 | |
| 64k | | | 1 | |

**Rung-change latency.** The number users judge the sharing policy by
([`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §4.1; M0 spike 6).

| Transition | What is timed | Target | Measured |
|---|---|---|---|
| Toggle flip -> VRAM free | `nvidia-smi` reports the card essentially empty | ~10 s | |
| Toggle flip -> `ready` shown | Dashboard state, end to end | ~10 s | |
| Sleep -> wake, top rung serving | The load itself, excluding the ~5 min hysteresis window | ~15 s | |
| Rung drop -> first token from new rung | User-visible: chat keeps answering | < 30 s | |

Measure these with the same discipline as TTFT: five times, report median and worst. The worst case is
the one someone will hit and complain about.

---

## 8. Model comparison harness (N9)

### 8.1 The design requirement that makes this cheap

**The RAG service must accept the generator model as a request parameter**, defaulting to the
configured one. Without that, comparing fast tier against deep tier means editing config and
restarting between runs — which nobody will do repeatedly, which means it gets done once and never
again.

```jsonc
// POST /v1/chat/completions to the RAG service
{
  "model": "team-docs",
  "messages": [ /* ... */ ],
  "metadata": { "generator": "qwen3-235b-a22b" }   // overrides the default generator
}
```

Retrieval, reranking and the gate are held **identical** across the comparison. Only the generator
varies. That is what makes the resulting difference attributable rather than merely observed.

### 8.2 What to compare

| Configuration | Host | Why it is in the matrix |
|---|---|---|
| `qwen3-coder-30b-a3b` (fast, top rung) | `.226` | The default experience |
| `qwen3-14b` (ladder rung 2) | `.226` | What users get when someone claims the 4090 — how bad is the drop, really? |
| `qwen3-4b` (`.87` small) | `.87` | The fallback of last resort |
| `qwen3-235b-a22b` (deep) | `.226` | Is the wait worth it, and by how much? |

That second row is the point. "Coding quality degrades during someone's session, and that is the right
trade" ([`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §5) is currently an assertion. This
harness turns it into a measured gap you can show people — which is also how you decide whether the
ladder's rungs are set at the right models.

### 8.3 Running it

```bash
python eval/compare_models.py \
  --models qwen3-coder-30b-a3b,qwen3-14b,qwen3-4b,qwen3-235b-a22b \
  --set eval/eval-set-v1.jsonl \
  --out eval/results/$(date +%F)-model-comparison.json
```

| Model | Answer correctness (human, n=20) | Citation precision | Refusal correctness (cat E+F) | TTFT median | tok/s | Wall time, 50 Q |
|---|---|---|---|---|---|---|
| qwen3-coder-30b-a3b | | | | | | |
| qwen3-14b | | | | | | |
| qwen3-4b | | | | | | |
| qwen3-235b-a22b | | | | | | |

**Refusal correctness deserves its own column.** Smaller models are markedly worse at honouring
"answer only from the provided context, and say so if it is not there." It is entirely possible for a
4B model to score acceptably on the answerable questions and badly on E and F — and that failure
matters more than a few points of correctness, because it is the one that produces confident fiction.

Retrieval metrics are **not** in this table. They cannot vary — retrieval is held fixed. If they move
between runs, the harness is broken.

### 8.4 The N9 swap test

N9 says model choice is swappable — config, not code. Verify it with the harness rather than by
inspection:

```bash
git status --porcelain                       # 1. clean tree; note the commit
$EDITOR deploy/host-226/.env                 # 2. change MODEL_FAST to the new model + pinned revision
make deploy HOST=226                         # 3. restart. No source edits allowed
python eval/compare_models.py --models new-model --set eval/eval-set-v1.jsonl
git status --porcelain                       # 4. still clean (.env is gitignored)
```

**Pass (N9):** the new model appears in the catalog, serves, and produces a complete eval result, with
`git status` on tracked source files clean.
**Fail:** any source file needed editing. Find the hardcoded model name and move it to config — a small
fix now, an expensive one after three more models have accreted around it.

Run the full comparison after every model swap. That is both the N9 verification and the regression
check, and it is the cheapest moment to discover that the new model is worse at refusing.

---

## 9. Be honest: automated answer scoring is hard

Retrieval metrics are objective. Whether a page appears in a ranked list is a set-membership test,
which is why §4 is fully automated and runs on every change.

**Answer quality is not like that, and the tempting shortcut is a trap here specifically.**

LLM-as-judge works reasonably when the judge is substantially stronger than the thing it judges. We do
not have that. The strongest model available is the one being evaluated, on the same hardware. Using a
local 14B to grade a local 14B has four concrete problems:

1. **Shared blind spots.** Judge and generator have similar training data, similar tokenizers and
   similar failure modes. A misreading the generator makes is one the judge is disposed to accept.
2. **Fluency bias.** Small models reliably score confident, well-formed wrong answers above hedged
   correct ones — the exact direction we least want to be wrong in (§5, failure mode 3).
3. **It measures agreement, not correctness.** A judge score of 0.82 is 0.82 *agreement with a model of
   unknown reliability*, and reporting it as quality launders a guess into a number. That is worse than
   having no number.
4. **It costs the GPU we are short of.** Fifty judged answers is fifty more generations competing for
   `.226` — against the modelling runs, and against actual users.

**What we do instead:**

| Layer | Method | Cost per release |
|---|---|---|
| Retrieval | Fully automated, objective (§4) | Minutes, unattended |
| Citation mechanics | Automated assertions (§5.2) | Seconds, in `make test` |
| Citation support, answer correctness | **Human spot-check, 20 sampled answers** (§5.3) | ~30 min |
| Refusal behaviour | Automated against the `answerable` label (§6) | Minutes |

Thirty minutes of human reading per release, on a project with one operator and ten users, is
affordable and produces a number you can defend. An automated judge produces one you cannot.

**If you use a judge at all, use it as a triage tool only.** Have it flag which answers a human should
read first — it is decent at spotting an answer that ignores its context — and never report its score
as a quality metric. If a judge is ever promoted to a reported number, it must be the deep tier judging
the fast tier, never a model judging itself, and the human sample stays as the calibration.

Report human numbers with their **n and date**: "citation precision 19/20, 2026-10-14, reviewer AK, tag
v0.5.2". That is a real measurement with visible uncertainty, which is exactly what it should look
like.

---

## 10. Cadence — when to run what

| Trigger | Run | Cost |
|---|---|---|
| Any RAG service change | Retrieval eval (§4), citation assertions (§5.2) | Minutes, unattended |
| Chunking, embedding model or reranker change | Full retrieval eval **and recalibrate the gate** (§6) | ~1 h incl. re-embedding |
| Model swap (N9) | Model comparison (§8) + latency (§7) | ~1 h |
| Any release | Retrieval eval + latency + **20-answer human spot-check** (§5.3) | ~1 h |
| Corpus grows, or documents are removed | Re-verify the set against `corpus-manifest`; retire questions whose source document is gone | ~30 min |
| Quarterly | Everything, plus 5–10 new questions harvested from the real query log | Half a day |

**That last row is how the eval set stays relevant.** The set written at M1 reflects what its author
thought to ask before anyone used the system. Real users ask differently. Mine the query log —
especially questions that produced a refusal or a complaint — and fold them in as a versioned
addition, never as an edit to v1.

---

## 11. Results log

Keep the current numbers here, so the doc is the record rather than a pointer to one.

| Date | Tag | Config | recall@5 | recall@30 | MRR | Gate threshold | Citation precision (n=20) | TTFT @4 |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

---

## Reflect

The eval set is fifty lines of JSON written by one person in two days, and it is the highest-leverage
artifact in this project. Every other component in these docs will be replaced — the models certainly,
the chunker probably, the vector store possibly. The eval set is what makes each of those replacements
a measured decision instead of a fresh start.

**Be honest about its limits.** Fifty questions is small. A one- or two-question swing on a category of
ten is noise, and treating it as signal will send you tuning against randomness. Act only on changes
that are large or explainable. Report counts alongside percentages — "43/48" is more honest than
"89.6%", and it makes the sample size impossible to forget.

**Its other limit is authorship.** It measures what its author thought to ask, which is a narrower
distribution than what ten people will ask. The quarterly harvest from the real query log is not
optional maintenance; it is what stops the number drifting away from the experience.

The measurement most worth defending is the **pre/post rerank delta** (§4.3). Everything else has an
obvious owner and an obvious fix. The reranker is the one place where the design commits 200–500 ms of
a 2 s budget on the strength of a claim — and it is the one number that, if skipped now, will simply
never be questioned again.

**Next:** [`18-operations.md`](./18-operations.md).
