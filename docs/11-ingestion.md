# 11 — Ingestion

> **Reference / fallback design.** [ADR-0007](./adr/0007-adopt-ragflow-for-retrieval.md) adopts
> RAGFlow instead of building this. Retained because it documents what we would build if the M1.5
> spike fails, and because the relevance gate in [`12`](./12-retrieval-and-rerank.md) is reusable
> as a wrapper in front of RAGFlow. Do not build from this without checking that ADR first.

> Parse -> chunk -> embed -> store. The offline half of M5, where correctness beats throughput and a
> single wrong page number does more damage than an hour of slow processing.
>
> Depends on [`10-data-layer.md`](./10-data-layer.md) for the schema and the embedding constant.

---

## Concept

### 1. The pipeline, and why it is offline

```
Document arrives  (upload via the API, or dropped into the watched folder /srv/corpus)
  |
  +-> hash the raw bytes -> SHA-256
  |     already present at this pipeline_version?  -> done, nothing to do
  |
  +-> INSERT documents row, status = 'pending'
  |
  +-> claim it: status = 'processing', attempts = attempts + 1
  |
  +-> PARSE      pypdfium2/pdftext -> per-page text, page numbers preserved
  |
  +-> NORMALISE  join pages into one string, remember each page's char span
  |
  +-> CHUNK      token-aware, with overlap; map each chunk's char span back to pages
  |
  +-> EMBED      Infinity on .87, PASSAGE mode, batched
  |
  +-> STORE      one transaction: DELETE old chunks, INSERT new, status = 'ready'
  |
  +-> on any exception: status = 'failed', last_error recorded, retryable
```

Nothing here is user-blocking. A person uploading a 400-page PDF gets an acknowledgement and a
document id immediately; the work happens in a background worker. That framing buys us the freedom
to be slow and careful, and this pipeline should spend that freedom.

### 2. What ingestion is optimising for

| Optimising for | Explicitly not optimising for |
|---|---|
| Correct page attribution on every chunk | Ingestion throughput |
| Idempotency — the corpus is rebuildable from source at any time | Incremental/partial document updates |
| Failing loudly and retryably | Parsing every PDF ever made |
| Chunks sized so a cross-encoder can judge them | Squeezing maximum text into minimum chunks |

The corpus for a 10-person team is thousands of documents, not millions. Ingesting a large PDF in two
minutes instead of twenty seconds costs nobody anything. Citing page 47 when the sentence is on page
52 costs us the entire product.

---

## Build

### 3. PDF parsing — a licence-driven choice

| Library | Licence | Speed | Layout / tables | Use it for |
|---|---|---|---|---|
| **pypdfium2** | Apache-2.0 / BSD-3 | Fast | Basic text order | **Default.** Bulk text extraction |
| **pdftext** | Apache-2.0 | Fast | Better line/block ordering; built on pypdfium2 | **Default** where reading order matters |
| **Docling** | MIT | Slow | Strong — tables, headings, reading order | Documents where tables carry the answer |
| **pdfplumber** | MIT | Slow | Precise word/table geometry | The awkward document you are debugging |
| ~~PyMuPDF / fitz~~ | **AGPL-3.0** | Fastest | Very good | **Do not use** |

**Why PyMuPDF is off the table.** It is AGPL-3.0, with a commercial licence sold separately by
Artifex. For a purely internal tool the AGPL's network clause is probably not triggered — but "probably"
is doing a lot of work in that sentence, and if this platform is ever exposed beyond the company the
obligation attaches to our source. Discovering an AGPL dependency during a retrospective security
review is a conversation nobody wants; avoiding it costs us a slightly slower parser. Take the
slower parser. (See [`tech-stack.md`](./tech-stack.md) §8.)

The parser returns pages, never a blob:

```python
# services/rag/rag/ingest/parse.py
from dataclasses import dataclass
import pypdfium2 as pdfium

@dataclass(frozen=True)
class Page:
    number: int      # 1-based, matching what the reader sees in a PDF viewer
    text: str

def parse_pdf(path: str) -> list[Page]:
    """Extract text per page. Page numbers are 1-based and never inferred."""
    pages: list[Page] = []
    pdf = pdfium.PdfDocument(path)
    try:
        for i in range(len(pdf)):
            textpage = pdf[i].get_textpage()
            try:
                pages.append(Page(number=i + 1, text=textpage.get_text_bounded() or ""))
            finally:
                textpage.close()
    finally:
        pdf.close()
    return pages
```

**Verify the extraction API against your pypdfium2 version** — the text-getter method names have
moved between releases. Pin the version in `pyproject.toml` and let `uv` lock it.

**Routing to Docling.** Docling is materially slower but understands tables and reading order. Rather
than choosing globally, route per document:

```python
def choose_parser(path: str, sample: list[Page]) -> str:
    """Cheap heuristic: if the fast parse looks degenerate, pay for Docling."""
    chars = sum(len(p.text) for p in sample)
    if chars / max(len(sample), 1) < 200:        # near-empty pages: scanned, or table-heavy
        return "docling"
    return "pypdfium2"
```

Record which parser produced a document (a column on `documents`, or fold it into
`pipeline_version`) so a later quality problem can be traced to its cause.

### 4. Page provenance — the theme of this document

**A citation pointing at the wrong page destroys trust in every other citation.** This is not
hyperbole about tidiness; it is the mechanism by which the whole platform loses its users. Someone
follows a citation to page 47, finds nothing there, and from that moment reads every citation the
system produces as decoration rather than evidence. The failure is silent, permanent, and does not
show up in any latency graph.

So provenance is not a field we populate at the end. It is the invariant the pipeline is built
around, and it survives three transformations that each want to destroy it.

**Transformation 1 — joining pages loses page boundaries.** Fix it by recording the spans as you join:

```python
# services/rag/rag/ingest/normalise.py
from dataclasses import dataclass

@dataclass(frozen=True)
class PageSpan:
    number: int
    start: int      # inclusive char offset into the joined document text
    end: int        # exclusive

PAGE_SEP = "\n\n"

def join_pages(pages: list[Page]) -> tuple[str, list[PageSpan]]:
    """Join pages into one string while remembering exactly where each page lives."""
    parts: list[str] = []
    spans: list[PageSpan] = []
    cursor = 0
    for p in pages:
        text = normalise_whitespace(p.text)
        spans.append(PageSpan(number=p.number, start=cursor, end=cursor + len(text)))
        parts.append(text)
        cursor += len(text) + len(PAGE_SEP)
    return PAGE_SEP.join(parts), spans
```

Note the ordering: the span is recorded from the **normalised** text, after whitespace collapsing, so
the offsets refer to the same string the chunker will actually see. Normalising after computing spans
is the classic off-by-a-lot bug here.

**Transformation 2 — chunking cuts across pages.** A chunk can legitimately begin on page 12 and end
on page 13. Do not pick one and hope:

```python
def pages_for_span(spans: list[PageSpan], start: int, end: int) -> tuple[int, int]:
    """Every page this char range touches. Both ends are recorded; nothing is guessed."""
    touched = [s.number for s in spans if s.start < end and s.end > start]
    if not touched:
        raise ValueError(f"chunk span [{start},{end}) maps to no page -- offsets are corrupt")
    return min(touched), max(touched)
```

The `raise` matters. If the offsets ever go wrong, ingestion must stop rather than store a plausible
guess. A document that failed to ingest is a visible problem; a document with subtly wrong page
numbers is an invisible one.

`page_start` and `page_end` both go into the schema, and the citation renderer says
`p. 12` when they are equal and `pp. 12-13` when they are not — which is honest, and is what a
reader chasing the reference actually needs.

**Transformation 3 — PDF page numbers are not document page numbers.** A report with roman-numbered
front matter has a printed "page 3" at PDF index 9. We store the **PDF page index** (1-based, what a
viewer's page box shows) because that is what a reader can actually navigate to, and because it is
the only number we can derive reliably. Say so in the citation format so nobody is confused:
`[2] Traffic Study 2025.pdf, p. 47` means the 47th page of the file.

Extracting printed page labels is a v2 idea. It is genuinely useful and genuinely fiddly, and doing
it badly is worse than not doing it.

**Test it.** This gets a unit test with a hand-built fixture, and the test is not optional:

```python
def test_chunk_pages_are_exact():
    pages = [Page(1, "alpha " * 200), Page(2, "beta " * 200), Page(3, "gamma " * 200)]
    text, spans = join_pages(pages)
    for ch in chunk_text(text, spans):
        window = text[ch.char_start:ch.char_end]
        for page_no in range(ch.page_start, ch.page_end + 1):
            span = next(s for s in spans if s.number == page_no)
            # every claimed page must actually overlap the chunk's text
            assert span.start < ch.char_end and span.end > ch.char_start
        # and no unclaimed page may overlap it
        for s in spans:
            if not (ch.page_start <= s.number <= ch.page_end):
                assert not (s.start < ch.char_end and s.end > ch.char_start)
```

Add one real PDF from the corpus as a golden fixture with hand-verified page numbers for three
chunks. When someone later "improves" the chunker, that fixture is what stops them shipping a
provenance regression.

### 5. Token-aware chunking with overlap

Chunk on **tokens from the embedding model's own tokeniser**, not on characters and not on words.
Character counts drift from token counts by a factor that varies with the content — tables, code and
identifier-heavy text tokenise far worse than prose — so a character-based chunker silently produces
chunks that overflow the embedding model's window on exactly the documents where retrieval matters
most.

```python
# services/rag/rag/ingest/chunk.py
from dataclasses import dataclass
from transformers import AutoTokenizer
from rag.config import EMBEDDING

_tok = AutoTokenizer.from_pretrained(EMBEDDING.name, revision=EMBEDDING.revision)

CHUNK_TOKENS   = 512      # target size
OVERLAP_TOKENS = 64       # ~12% overlap
MIN_TOKENS     = 32       # below this, merge into the neighbour rather than store a fragment

@dataclass(frozen=True)
class Chunk:
    ordinal: int
    content: str
    token_count: int
    char_start: int
    char_end: int
    page_start: int
    page_end: int

def chunk_text(text: str, spans: list[PageSpan]) -> list[Chunk]:
    enc = _tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offsets = enc["input_ids"], enc["offset_mapping"]

    step = CHUNK_TOKENS - OVERLAP_TOKENS
    chunks: list[Chunk] = []
    i, ordinal = 0, 0
    while i < len(ids):
        j = min(i + CHUNK_TOKENS, len(ids))
        if len(ids) - j < MIN_TOKENS:        # absorb a runt tail rather than emit it
            j = len(ids)

        char_start = offsets[i][0]
        char_end   = offsets[j - 1][1]
        char_start, char_end = snap_to_boundary(text, char_start, char_end)

        page_start, page_end = pages_for_span(spans, char_start, char_end)
        content = text[char_start:char_end]
        chunks.append(Chunk(
            ordinal=ordinal,
            content=content,
            token_count=len(_tok(content, add_special_tokens=False)["input_ids"]),
            char_start=char_start, char_end=char_end,
            page_start=page_start, page_end=page_end,
        ))
        ordinal += 1
        if j >= len(ids):
            break
        i += step
    return chunks
```

`return_offsets_mapping=True` is the load-bearing detail: it is what lets a token index become a
character offset, which is what lets a character offset become a page number. A tokeniser without
offset mapping cannot preserve provenance, so it is not a candidate. (It requires a *fast* tokeniser;
confirm `_tok.is_fast` at import and fail hard if it is not.)

`snap_to_boundary` nudges the cut to the nearest sentence end within a small window, so chunks do not
begin mid-word. Keep the window small — a chunk that slides 300 characters to find a full stop has
stopped being 512 tokens.

**The parameters, and the reasoning behind each:**

| Parameter | Value | Why |
|---|---|---|
| Chunk size | 512 tokens | Large enough to hold a complete argument; small enough that a cross-encoder can judge it as one unit, and that five of them fit a local model's context with room for the answer |
| Overlap | 64 tokens | An answer that straddles a boundary appears whole in at least one chunk. Costs ~12% more rows and more duplicate hits — which the reranker then collapses |
| Minimum | 32 tokens | A 6-token fragment retrieves noisily and cites uselessly. Merge it into its neighbour |

Two failure modes worth naming. **Too large** and the reranker's judgement blurs — a 2,000-token
chunk containing one relevant sentence and 1,900 tokens of unrelated text scores like a mediocre
chunk, and it eats the generation model's context. **Too small** and chunks lose the context that
made them meaningful ("this value" with the table three chunks away). 512 with overlap is the
starting hypothesis; [`17-evaluation.md`](./17-evaluation.md) is what settles it, and chunking is the
*first* thing to tune when recall disappoints — before rerank depth, before the model.

Structure-aware splitting — preferring to break at headings and never mid-table — is a real
improvement available when Docling supplies the structure. Land it after the naive version is
measured, so the improvement is measurable rather than assumed.

### 6. Embedding via Infinity, and the passage/query asymmetry

Infinity runs on `.87` serving Qwen3-Embedding-0.6B (~1.2 GB VRAM) behind an OpenAI-compatible API.
It is one of the small always-on models on that box, because nothing in RAG works without it.

**Qwen3-Embedding is asymmetric.** Queries are embedded with an instruction prefix; passages are
embedded bare. Get it backwards and there is no error, no warning, and no obviously broken output —
recall simply degrades, and you will spend a day blaming the chunker. The asymmetry is exactly the
kind of thing that gets got wrong once during ingestion and stays wrong for a year.

**So the wrapper does not expose a way to get it wrong.** There is no `embed(texts, mode=...)`. There
are two functions with two names, and the mode is not a parameter anyone can pass:

```python
# services/rag/rag/embed.py
import httpx
from rag.config import EMBEDDING

_INFINITY_URL = "http://10.0.0.87:7997/embeddings"
_BATCH = 32
_TIMEOUT = httpx.Timeout(connect=2.0, read=30.0, write=10.0, pool=2.0)

async def _post(texts: list[str]) -> list[list[float]]:
    """Private. Callers use embed_passages() or embed_query(); there is no third option."""
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for i in range(0, len(texts), _BATCH):
            batch = texts[i:i + _BATCH]
            r = await client.post(_INFINITY_URL,
                                  json={"model": EMBEDDING.name, "input": batch})
            r.raise_for_status()
            vecs = [d["embedding"] for d in sorted(r.json()["data"],
                                                   key=lambda d: d["index"])]
            for v in vecs:
                if len(v) != EMBEDDING.dim:      # the D guard, at the other end of the wire
                    raise RuntimeError(
                        f"embedding dim {len(v)} != configured {EMBEDDING.dim}; "
                        f"the served model is not {EMBEDDING.name}@{EMBEDDING.revision}")
            out.extend(vecs)
    return out

async def embed_passages(texts: list[str]) -> list[list[float]]:
    """INGEST ONLY. Passages get no instruction prefix."""
    return await _post([EMBEDDING.passage_prefix + t for t in texts])

async def embed_query(text: str) -> list[float]:
    """SEARCH ONLY. Queries get the instruction prefix."""
    return (await _post([EMBEDDING.query_prefix + text]))[0]
```

Three things that wrapper is doing beyond calling HTTP:

- **Sorting by `index` before unpacking.** The OpenAI embeddings schema does not promise response
  order matches request order. Trusting the order works right up until a batched server reorders,
  and then every chunk in that batch has somebody else's vector — the single nastiest bug available
  in this pipeline, because everything still looks normal.
- **Checking the dimension on every response.** This is the same guard as
  [`10-data-layer.md`](./10-data-layer.md) §8, applied at the other end of the wire. If Infinity is
  serving a different model than we think, we find out on the first batch.
- **Never exposing the raw call.** `_post` is private for the same reason the mode is not a
  parameter.

**Batching.** 32 texts per request is a starting point; Infinity does its own dynamic batching
server-side, so this mostly controls request overhead and memory spikes. Measure with a real
document and tune. **Do not** parallelise across many concurrent connections to squeeze throughput —
the GPU on `.87` is shared with its user, and ingestion is the one part of this system that has no
deadline. Slow and polite beats fast and evicted.

**Retries.** Wrap `_post` in exponential backoff for connection errors and 5xx, with a cap. A single
failed batch should not fail a 400-page document; a persistently unreachable Infinity should fail the
document quickly, into `failed`, so the retry loop in §8 owns it rather than a stuck worker.

### 7. Idempotency by document hash

**Ingestion is idempotent by SHA-256 of the raw bytes.** Feed the same file twice and the second run
is a no-op. Feed the whole corpus back in after a disaster and you get the same corpus.

```python
# services/rag/rag/ingest/pipeline.py
import hashlib
from rag.config import PIPELINE_VERSION      # bump when parser or chunker changes

def file_sha256(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.digest()                        # bytea, matching the schema

async def ingest_file(conn, workspace_id, path, title, uploaded_by=None) -> tuple[str, str]:
    digest = file_sha256(path)

    existing = await conn.fetchrow("""
        SELECT id, status, pipeline_version FROM rag.documents
        WHERE workspace_id = $1 AND content_sha256 = $2
    """, workspace_id, digest)

    if existing and existing["status"] == "ready" \
       and existing["pipeline_version"] == PIPELINE_VERSION:
        return existing["id"], "unchanged"        # the common case: nothing to do

    doc_id = existing["id"] if existing else await conn.fetchval("""
        INSERT INTO rag.documents
            (workspace_id, title, source_uri, byte_size, content_sha256,
             pipeline_version, uploaded_by, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
        RETURNING id
    """, workspace_id, title, f"file://{path}", os.path.getsize(path),
         digest, PIPELINE_VERSION, uploaded_by)

    if existing:                                   # same bytes, newer pipeline: re-derive
        await conn.execute("""
            UPDATE rag.documents
               SET status='pending', pipeline_version=$2, attempts=0, last_error=NULL
             WHERE id = $1
        """, doc_id, PIPELINE_VERSION)

    return doc_id, "queued"
```

**`pipeline_version` is what makes the hash honest.** A content hash alone says "these bytes are
already ingested", which becomes wrong the moment you improve the chunker: the bytes are unchanged
but the derived data should not be. Bumping `PIPELINE_VERSION` re-derives everything without needing
anyone to remember to purge the table.

**The store step is one transaction**, and it is a replace, not an append:

```python
async def store_chunks(conn, doc_id, model_id, chunks, vectors, page_count):
    async with conn.transaction():
        await conn.execute("DELETE FROM rag.chunks WHERE document_id = $1", doc_id)
        await conn.copy_records_to_table(
            "chunks", schema_name="rag",
            columns=["document_id", "workspace_id", "ordinal", "content", "token_count",
                     "page_start", "page_end", "char_start", "char_end",
                     "embedding_model_id", "embedding"],
            records=[...],                       # zip(chunks, vectors)
        )
        await conn.execute("""
            UPDATE rag.documents
               SET status='ready', page_count=$2, embedding_model_id=$3,
                   ingested_at=now(), updated_at=now(), last_error=NULL
             WHERE id = $1
        """, doc_id, page_count, model_id)
```

Because it is one transaction, there is no moment at which a document is half-reindexed. Retrieval
sees the old chunks, then the new chunks, and never a mixture of two chunking strategies. Combined
with `d.status = 'ready'` in the search query, a document being re-ingested is simply invisible for
the duration.

**Why this is the real safety net.** Chunks, vectors and the full-text index are all *derived data*.
The only irreplaceable artefact is the source file in `/srv/corpus`. That means: a bad chunking
change is recoverable by bumping the pipeline version and re-running; a corrupted index is
recoverable by re-ingesting; a restored backup that turns out to be stale is recoverable by
re-ingesting anything newer. Every one of these is a `make reingest` away rather than an incident.
Guard the source files accordingly — they are the thing worth backing up carefully, and the database
is, in the end, a cache.

### 8. Status lifecycle and retry

```
                +--------------------------- retry (attempts < MAX) ---------+
                |                                                            |
                v                                                            |
  [pending] --claim--> [processing] --success--> [ready]                     |
                            |                                                |
                            +--exception--> [failed] --------- requeue ------+
                                              (last_error recorded)
```

| Status | Meaning | Visible to search? |
|---|---|---|
| `pending` | Queued, not started | No |
| `processing` | A worker holds it | No |
| `ready` | Chunks and vectors are current | **Yes** |
| `failed` | Terminal until requeued; `last_error` says why | No |

The claim must be atomic, so two workers cannot take the same document:

```sql
-- Claim one document. SKIP LOCKED makes concurrent workers safe with no external queue.
UPDATE rag.documents SET status = 'processing', attempts = attempts + 1, updated_at = now()
WHERE id = (
    SELECT id FROM rag.documents
    WHERE status = 'pending' AND attempts < 3
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, workspace_id, source_uri, title;
```

Postgres is the queue. At our volume adding Redis or Celery buys nothing but another service to
operate and another thing to be down; `FOR UPDATE SKIP LOCKED` is the whole mechanism, and the queue
is backed up whenever the database is.

**Stuck `processing` rows** are the failure mode this design has to handle explicitly: a worker
killed mid-document leaves a row claimed forever. A sweeper handles it —

```sql
-- Anything claimed for over an hour lost its worker. Return it to the queue.
UPDATE rag.documents
   SET status = 'pending', last_error = 'reclaimed after worker timeout'
 WHERE status = 'processing' AND updated_at < now() - interval '1 hour';
```

**Retry policy.** Up to 3 attempts with backoff, then `failed` and it stays there. Distinguish the two
kinds of failure when you write the handler, because they want different responses:

| Failure | Example | Response |
|---|---|---|
| Transient | Infinity restarting, database connection reset | Retry with backoff; usually self-heals |
| Permanent | Encrypted PDF, zero extractable text, corrupt file | Fail fast, do not burn attempts. Surface it to the uploader |

Detect the second class early — if `parse_pdf` returns pages totalling under a few hundred characters
for a multi-megabyte file, that is a scanned document, not a transient error, and no number of
retries will fix it. Set `last_error` to something a human can act on ("no extractable text — this
looks like a scanned PDF; OCR is not supported in v1") rather than a stack trace.

Operationally, one query is the whole dashboard:

```sql
SELECT status, count(*), max(updated_at) AS latest
FROM rag.documents GROUP BY status ORDER BY status;
```

### 9. Getting documents in

Two entry points, one pipeline:

- **Upload API** — `POST /admin/documents` on the RAG service ([`13-rag-service-api.md`](./13-rag-service-api.md)):
  multipart file, workspace, title. Writes the file into `/srv/corpus/<workspace>/`, then calls
  `ingest_file`. Returns `{document_id, status}` immediately.
- **Watched folder** — `/srv/corpus/<workspace>/` on `.87`'s NVMe #2, swept on a timer. Drop 200 PDFs
  onto a share and walk away. This is how the initial corpus load happens, and it is dramatically
  less painful than 200 uploads through a browser.

Both paths hash first, so dropping a file that was already uploaded costs one SHA-256 and nothing
else. The folder is the canonical source of truth for §7's rebuild story, so it lives on the NVMe
that is *not* Postgres and it is backed up separately from the database dump.

### 10. Non-goal: no OCR in v1

**Scanned and image-only PDFs are not supported.** They ingest to `failed` with an explanatory
`last_error`, and the uploader is told why.

This is a deliberate non-goal from [`00-goals-and-constraints.md`](./00-goals-and-constraints.md) §4,
and the reasoning is worth keeping visible: OCR is not one feature, it is a second pipeline. It needs
its own models, its own GPU budget on an already-shared 12 GB card, its own quality bar, and — most
importantly — it produces text whose page provenance is *approximate* and whose accuracy is
unmeasured. Feeding that into a citation system whose entire value rests on citations being exactly
right is precisely the wrong first extension.

If scanned documents turn out to matter, the right sequence is: measure how much of the corpus is
actually image-only, then add OCR as an explicit, separately-flagged document class whose citations
are labelled as OCR-derived. Not silently mixed into the same corpus.

---

## Reflect

**What we traded away.** Throughput, mostly, and deliberately. A faster parser exists (and is AGPL),
larger embedding batches exist (and would compete with `.87`'s user for the GPU), and skipping the
offset-mapping work would make chunking simpler (and would make provenance unverifiable). Each of
those trades bought correctness or licence safety with time that nobody is waiting on.

**The thing most likely to bite.** Chunk size. 512 tokens with 64 overlap is a defensible default and
nothing more — it is not derived from our corpus, because our corpus does not exist yet. If recall@5
disappoints in [`17-evaluation.md`](./17-evaluation.md), this is the first parameter to move, and
`pipeline_version` exists specifically so that moving it is a one-line change plus a re-ingest rather
than a migration and an argument. Budget for at least one full re-ingest during M5; treat it as
planned work, not as a setback.

**What we would revisit first.** Structure-aware chunking. Breaking at headings and never mid-table
is very likely worth more than any amount of index tuning, because it directly improves the *unit*
the reranker judges rather than the search over units. It is deferred only because it needs Docling
in the loop and because we want the naive baseline measured first — otherwise we will never know
whether it helped.

**Next:** [`12-retrieval-and-rerank.md`](./12-retrieval-and-rerank.md).
