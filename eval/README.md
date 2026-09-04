# eval — the retrieval eval set

> Scaffolding for the ~50 Q&A pairs that make retrieval quality a number instead of an argument
> (**N7**). The methodology lives in [`../docs/17-evaluation.md`](../docs/17-evaluation.md); this
> README is the working guide for the person authoring the set. The one rule that outranks all
> others: **write the questions before the retriever exists** (17 §1). No retrieval system may be
> consulted while authoring — that is the entire reason this directory exists now, while `.87` is
> still being brought up, and not at M5.

---

## 1. What this set gates

| Gate | What it needs from the set |
|---|---|
| **M1.5 — RAGFlow spike** ([delivery-plan §6](../docs/delivery-plan.md)) | The refusal criterion. "Does it refuse?" ([ADR-0007](../docs/adr/0007-adopt-ragflow-for-retrieval.md) check 1) needs questions the corpus *cannot* answer — categories E and F below. If the spike runs before this set is authored, [the spike procedure's synthetic probes](../docs/m1.5-ragflow-spike.md) stand in (N1 keeps real corpus text out of the spike anyway), and E/F re-test refusal against the real corpus at M5 |
| **M5 — RAG acceptance** | "Cited answers; recall@5 on the eval set." Without this file, M5 acceptance is vibes |
| **Every later change** | Chunker, embedding model, reranker, gate threshold, generator — each will change, and the frozen set is what makes those changes comparable rather than fresh starts |

## 2. What is here

| File | Role |
|---|---|
| `questions.jsonl` | The working draft. Ships with **synthetic examples only** (`"example": true`) — delete them as real authoring starts. Gitignored: see §7 before adding a real question |
| `check.py` | Structural validator — schema, id uniqueness, label consistency, progress vs targets. `uv run python eval/check.py` |
| this README | Schema and workflow. Full methodology: [`17-evaluation.md`](../docs/17-evaluation.md) |

The measurement scripts from 17 §3.4 (`run_retrieval_eval.py`, `calibrate_gate.py`,
`bench_latency.py`, `compare_models.py`) arrive with M1.5/M5 — they need a retriever to run
against, and nothing that scores may exist while the questions are being written.

## 3. Composition — six categories, 50 pairs

From 17 §3.1. The counts are a starting shape, not a rule — if the corpus turns out to be mostly
survey tables, category C deserves more.

| # | Category | Target | What it stresses | The failure it catches |
|---|---|---|---|---|
| A | `easy` | 12 | The baseline path — one fact, one page, stated plainly | Anything structural: chunking, embedding, page mapping |
| B | `multihop` | 8 | Retrieval breadth — answer needs 2+ pages, ideally 2+ documents | Top-1 tuning that starves synthesis; over-aggressive reranking |
| C | `exact-token` | 10 | The lexical half of hybrid search — ids, codes, acronyms, standard refs | Dense-only retrieval quietly carrying everything |
| D | `paraphrase` | 10 | The dense half — none of the page's content words appear in the question | Lexical-only retrieval |
| E | `unanswerable` | 8 | The relevance gate (F7) — plausible, adjacent, genuinely not in the corpus | Confident fabrication; a threshold of zero scores perfectly without these |
| F | `near-miss` | 2 | The gate's hardest case — a document that *discusses* the topic without answering | Grounding on high-similarity chunks that do not contain the answer |

In generic RAG-eval terms: A, C and D are factoid variants (each isolating one retrieval
component), B is the synthesis / multi-document category, and E–F are the negative questions. The
finer split is deliberate — each category names the component that broke when it regresses.

**E is not optional.** Do not write absurd unanswerables ("what is the capital of France?") — every
gate rejects those, so they measure nothing. Write questions a real user of *this* corpus would
plausibly ask, whose answers genuinely are not in it.

## 4. Record schema

One JSON object per line. `check.py` enforces all of this mechanically; the examples in
`questions.jsonl` demonstrate it. The design goal is that scoring recall@5 against a record is a
set-membership test, nothing more.

| Field | Type | Rule |
|---|---|---|
| `id` | string | `X-NN` where `X` is the category letter (`A-01` … `F-02`). Unique, and stable forever — a regression names itself |
| `category` | string | One of the six names in §3; must match the id's letter |
| `answerable` | bool | `true` for A–D, `false` for E–F. This is the label the relevance gate is calibrated against (17 §6) |
| `question` | string | As a user would type it, not as the document phrases it |
| `expected` | array | `[{"doc": "<filename>", "pages": [<int ≥ 1>, …]}, …]`. **Every** page needed for the answer — multi-hop is scored on retrieving all of them. `[]` when unanswerable |
| `answer` | string \| null | Short reference answer, for the human spot-check (17 §5.3) — never for automated string matching. `null` when unanswerable |
| `author` | string | Initials |
| `written` | string | ISO date, `YYYY-MM-DD` |
| `notes` | string, optional | Why this question is hard. You will not remember in six weeks |
| `example` | bool, optional | `true` only on the shipped synthetic placeholders. `check.py` excludes them from progress counts and `--frozen` rejects them |

Two load-bearing choices:

- **`(doc, page)`, never chunk ids.** Chunk boundaries move with every chunking change, and the
  embedding-model trap ([delivery-plan §9](../docs/delivery-plan.md)) invalidates every vector —
  but page numbers survive both. That is what keeps one eval set valid across the changes it
  exists to judge.
- **`doc` is the source filename, exactly as ingested** — same case, same extension. Agree the
  canonical form once, at ingestion, or scoring degenerates into fuzzy matching.
- **`pages` are physical page indices** (1-based position in the PDF), not the printed page
  labels. Cover pages and roman-numeral front matter are the origin of the off-by-one citation
  bug in 17 §5 — check every page number against the PDF's physical index.

## 5. How recall@5 is computed

Mechanical, per 17 §4:

```
need(q)      = { (doc, p)  for e in q.expected  for p in e.pages }
top_k(q, k)  = { (doc, page) of the first k retrieved chunks }
hit(q, k)    = need(q) ⊆ top_k(q, k)          # multi-hop: ALL pages, strictly

recall@5     = mean of hit(q, 5) over answerable questions, reported per category
```

- Unanswerable questions (E, F) are **excluded from recall**. They are scored separately, against
  the `answerable` label, as gate precision/recall (17 §6) — that is the M1.5 refusal test.
- recall@30 (before reranking) and MRR are reported alongside; see 17 §4.1 for why.
- The aggregate goes in the changelog; the per-category table is what says what to fix next.

## 6. Authoring workflow — incremental

Budget: one to two days of one person's attention, splittable into sessions. After every session:

```bash
uv run python eval/check.py        # schema + progress vs the §3 targets
```

1. **Read the corpus first.** Real documents, open in a viewer. Questions come from pages you have
   in front of you — never from memory of what the corpus "probably says".
2. **A and C early**, while reading: easy lookups as you encounter plainly stated facts;
   exact-token questions as you spot ids, codes, acronyms, standard references.
3. **B as pairs emerge** — record *all* pages needed, across documents where possible.
4. **D last, after a break.** Paraphrase questions fail when you unconsciously reuse the source's
   vocabulary; distance helps.
5. **E and F from users.** Spend twenty minutes asking two or three of the ten users what they
   would ask this thing — their questions are differently shaped, and the unanswerable ones among
   them are exactly the plausible-adjacent kind that E needs.
6. **Verify every page number** against the physical page index (§4 above).
7. **At 50, freeze:**

   ```bash
   uv run python eval/check.py --frozen    # full targets met, all examples deleted
   ```

   Then snapshot per 17 §3.3–3.4: copy to `eval-set-v1.jsonl`, write the corpus manifest
   (sha256 + filename of every document present), and never edit v1 again. Growth is `v2`;
   results are always reported against a named version.

**Never edit a question because the system fails it.** That is the exact bias this whole exercise
exists to prevent. A genuinely wrong record (bad page reference, ambiguous wording) may be fixed —
note the fix and re-baseline everything.

## 7. Confidentiality — read before adding a real question

A filled eval set is **derived from the confidential corpus**: real document names, questions
about their contents, reference answers containing their facts. **N1** says no document text
leaves the network — and this repository pushes to a public GitHub remote, so committing real
questions here would put corpus-derived content on the public internet.

Therefore:

- **`eval/questions.jsonl` is gitignored** (alongside `eval-set-*.jsonl`, `corpus-manifest-*` and
  `eval/results/`), the same pattern as `deploy/fleet.local.yaml`: the repo carries the committed
  scaffold, the sensitive instance stays local. The synthetic examples it ships with are therefore
  working-tree-only; the schema they demonstrate is preserved in §4 and in `check.py`.
- **Version the real set on the network side** — it still needs the freeze-and-version discipline
  of 17 §3.3, just not in this repo. A bare git repo or dated read-only copies on `.87` beside the
  backups both work; what matters is that v1 is immutable and results name the version they ran
  against.
- Eval **results** (recall tables, gate-score CSVs) contain question text and document names, so
  they stay on-network too.

This diverges from 17 §3.4, which says the set and results live in this repo — written before the
public remote existed. Correct that section when `17` is next revised, per the convention that docs
describe what shipped.

## 8. Validating

```bash
uv run python eval/check.py                 # working draft: schema, ids, labels, progress
uv run python eval/check.py --frozen        # freeze gate: exact targets, no examples left
uv run python eval/check.py path/to/set.jsonl   # any other file, e.g. a frozen v1
```

Exit codes: `0` valid · `1` validation errors (readable, one per line, on stderr) · `2` file
missing or unreadable. Stdlib only — it runs anywhere Python 3.11+ exists, including the hosts.
