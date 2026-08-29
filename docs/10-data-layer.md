# 10 — Data Layer

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> Postgres 17 + pgvector on `.87`: the schema, the one config constant that must never drift, the
> index choice, the hybrid search query, migrations, and backups. This is the foundation the rest of
> M5 stands on — get it wrong and every later doc inherits the mistake.
>
> Read [`01-architecture.md`](./01-architecture.md) §4 first. This document implements the storage
> half of both flows described there.

---

## Concept

### 1. What this layer owns, and what it deliberately does not

| Owns | Does not own |
|---|---|
| Documents, chunks, embeddings, the full-text index | *Which* chunks to return — that is the RAG service's strategy |
| Tenancy: workspaces and membership | Authentication — Open WebUI and LiteLLM own identity |
| The record of which embedding model produced which vector | Prompting, reranking, the relevance gate |
| Retrieval telemetry used to calibrate the gate | Chat history — Open WebUI keeps that |

The database answers "which 30 chunks are plausibly relevant, and where did each come from". Ranking
beyond that, and the decision about whether 30 plausible chunks are actually *good enough*, both live
in [`12-retrieval-and-rerank.md`](./12-retrieval-and-rerank.md).

### 2. Why one database and not a vector DB

Covered in [`tech-stack.md`](./tech-stack.md) §5; the short version is that a second datastore means
a second thing to keep in sync, back up, and secure, for vector features we do not need at this
corpus size. Two properties of the single-store choice matter concretely here:

- **Transactional re-ingestion.** Deleting a document's old chunks and inserting the new ones is one
  transaction. There is no window where a document is half-reindexed and retrieval returns a mixture
  of two chunking strategies.
- **Tenancy is a join, not a filter language.** `chunks -> documents -> workspaces -> members` is
  ordinary SQL with ordinary foreign keys, and a workspace deletion cascades correctly by
  construction.

The cost is real: index tuning is on us, and pgvector's filtered-search behaviour needs attention
(§9). Revisit if the corpus passes a few million chunks.

### 3. Host and storage placement

Postgres runs on `.87` — the hub, the least contended box, and the machine that also runs Infinity,
LiteLLM and our services, so retrieval never crosses the 1 GbE link mid-query.

Per [`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) §5, **the data directory gets its own
NVMe**. `.87` has two 2 TB drives; NVMe #1 is Postgres and nothing else, NVMe #2 carries the ingested
source documents and the other container volumes. IO isolation from Docker image pulls, from log
churn, and from a colleague copying a 40 GB dataset is worth more than the capacity.

```
  .87  (i9-14900K 24c/32t, 128 GB, RTX 4070 12 GB)
  |
  +-- NVMe #1  -> /var/lib/postgresql/data     Postgres ONLY
  +-- NVMe #2  -> /srv/corpus                  source PDFs (the re-ingest safety net)
                  /var/lib/docker              images, other volumes
```

**Remember WSL2's cap.** Per [`delivery-plan.md`](./delivery-plan.md) §5, `.87`'s WSL2 instance is
capped at roughly 8 processors and 48 GB. Postgres does not see 128 GB — it sees the cap. Size
`shared_buffers` and friends against the cap, not the sticker (§11).

---

## Build

### 4. The single config constant

**This is the most important section in the document.** The embedding dimension `D` exists once, in
one Python module, and both the schema and the embedding wrapper read it from there. Nothing else may
hard-code `1024`.

```python
# services/rag/rag/config.py  --  the single source of truth
from dataclasses import dataclass

@dataclass(frozen=True)
class EmbeddingConfig:
    name: str        = "Qwen/Qwen3-Embedding-0.6B"
    revision: str    = "<pin-the-commit-sha-here>"   # unpinned model == unreproducible corpus
    dim: int         = 1024                          # D
    normalized: bool = True
    # Qwen3-Embedding is asymmetric: queries carry an instruction, passages do not.
    # Verify these strings against the model card for YOUR revision.
    query_prefix: str   = ("Instruct: Given a search query, retrieve relevant "
                           "passages that answer the query\nQuery: ")
    passage_prefix: str = ""

EMBEDDING = EmbeddingConfig()
```

Why `D` is not merely a number:

- **Different embedding models produce incompatible vector spaces.** A vector from model A scored
  against a query embedded by model B is not "slightly worse" — it is noise wearing the costume of a
  valid similarity score. Nothing throws, nothing looks obviously wrong, and retrieval quietly
  becomes random. Nobody notices for a week.
- **Changing the model invalidates every vector in the corpus** and forces a full re-embed. That is
  survivable — chunks and embeddings are derived data, per
  [`delivery-plan.md`](./delivery-plan.md) §9 — but it must be a deliberate act, not the accident of
  editing an env var.

So the schema **records the model and dimension** alongside the vectors, and every query filters on
it. A mismatch becomes a startup abort or a query-time zero-row result — loud, immediate,
diagnosable — instead of silent nonsense.

Qwen3-Embedding-0.6B supports Matryoshka dimensions from 32 to 1024. We take the full 1024.
Truncating to 512 would halve vector storage and speed up scans at some recall cost; that is a knob
to revisit once [`17-evaluation.md`](./17-evaluation.md) gives a recall baseline, not before. Note
that pgvector's indexable `vector` type has a dimension ceiling (2000 at time of writing), so 1024
leaves headroom — **verify against your pgvector version.**

### 5. Extensions and schema namespace

```sql
-- Run once per database, as a superuser.
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector; use the pgvector/pgvector:pg17 image
CREATE SCHEMA IF NOT EXISTS rag;

-- gen_random_uuid() is built in from Postgres 13 onward; pgcrypto is not needed on 17.
```

Everything lives in a `rag` schema rather than `public`. Open WebUI and LiteLLM also want databases
on this server; namespacing our objects means `\dt rag.*` shows exactly our surface and nothing else,
and a `DROP SCHEMA rag CASCADE` during development destroys only our data.

### 6. Tenancy

Identity belongs to Open WebUI. We do not duplicate users; we store an **opaque subject string** —
whatever stable identifier the gateway forwards — and join on it.

```sql
CREATE TABLE rag.workspaces (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text NOT NULL UNIQUE,      -- 'team-docs', 'transport-modelling'
    name        text NOT NULL,
    is_default  boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- At most one default workspace, enforced by the database rather than by convention.
CREATE UNIQUE INDEX workspaces_one_default
    ON rag.workspaces ((is_default)) WHERE is_default;

CREATE TABLE rag.workspace_members (
    workspace_id uuid NOT NULL REFERENCES rag.workspaces(id) ON DELETE CASCADE,
    subject      text NOT NULL,            -- opaque identity from the gateway; we never parse it
    role         text NOT NULL DEFAULT 'reader'
                 CHECK (role IN ('reader', 'author', 'admin')),
    added_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, subject)
);

CREATE INDEX workspace_members_subject_idx ON rag.workspace_members (subject);
```

At 10 seats one workspace may well be enough on day one. Model it anyway: retrofitting tenancy onto a
populated corpus means backfilling a `NOT NULL` column on every chunk row and rewriting every query.
A `workspace_id` column is nearly free to carry and expensive to add later.

The RAG service resolves subject to workspace on every request; there is no ambient "current
workspace" anywhere in the system.

**Row-level security is deliberately not used in v1.** Our service is the only reader and the only
writer, it holds one connection pool as one role, and RLS would push the tenancy decision into a
policy that is harder to test than an explicit `WHERE workspace_id = $n`. Revisit the moment a second
application gets direct database access.

### 7. The core schema

```sql
-- ---------------------------------------------------------------------------
-- Which embedding model produced which vector. This table is the reason a
-- model change is detected rather than silently corrupting retrieval.
-- ---------------------------------------------------------------------------
CREATE TABLE rag.embedding_models (
    id             smallint PRIMARY KEY,
    name           text NOT NULL,
    revision       text NOT NULL,          -- pinned commit / tag, never 'main'
    dim            integer NOT NULL CHECK (dim BETWEEN 1 AND 2000),
    normalized     boolean NOT NULL DEFAULT true,
    query_prefix   text NOT NULL DEFAULT '',
    passage_prefix text NOT NULL DEFAULT '',
    is_active      boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, revision)
);

CREATE UNIQUE INDEX embedding_models_one_active
    ON rag.embedding_models ((is_active)) WHERE is_active;

INSERT INTO rag.embedding_models
    (id, name, revision, dim, normalized, query_prefix, passage_prefix, is_active)
VALUES
    (1, 'Qwen/Qwen3-Embedding-0.6B', '<pin-the-commit-sha-here>', 1024, true,
     E'Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ',
     '', true);

-- ---------------------------------------------------------------------------
-- Documents
-- ---------------------------------------------------------------------------
CREATE TABLE rag.documents (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL REFERENCES rag.workspaces(id) ON DELETE CASCADE,
    title            text NOT NULL,
    source_uri       text NOT NULL,        -- file:///srv/corpus/... -- where to re-read it from
    media_type       text NOT NULL DEFAULT 'application/pdf',
    byte_size        bigint NOT NULL CHECK (byte_size > 0),
    page_count       integer CHECK (page_count IS NULL OR page_count > 0),

    -- Idempotency key: SHA-256 of the raw file bytes.
    content_sha256   bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    -- Bumped when the parser or chunker changes, so identical bytes are re-derived on purpose.
    pipeline_version integer NOT NULL DEFAULT 1,

    embedding_model_id smallint REFERENCES rag.embedding_models(id),

    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    attempts         integer NOT NULL DEFAULT 0,
    last_error       text,

    uploaded_by      text,                 -- same opaque subject as workspace_members
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    ingested_at      timestamptz,

    CHECK (status <> 'ready' OR (embedding_model_id IS NOT NULL AND ingested_at IS NOT NULL))
);

-- The same bytes may exist in two workspaces; within one workspace they are one document.
CREATE UNIQUE INDEX documents_workspace_hash_uniq
    ON rag.documents (workspace_id, content_sha256);

-- Partial index: the ingestion worker only ever asks for what is unfinished.
CREATE INDEX documents_pending_idx
    ON rag.documents (status, created_at) WHERE status <> 'ready';

-- ---------------------------------------------------------------------------
-- Chunks -- the retrieval unit.
-- NOTE: vector(1024) is RENDERED from EmbeddingConfig.dim by the migration.
--       Do not type the number by hand. See section 4 and section 12.
-- ---------------------------------------------------------------------------
CREATE TABLE rag.chunks (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id        uuid NOT NULL REFERENCES rag.documents(id) ON DELETE CASCADE,
    workspace_id       uuid NOT NULL REFERENCES rag.workspaces(id) ON DELETE CASCADE,
    ordinal            integer NOT NULL CHECK (ordinal >= 0),

    content            text NOT NULL,
    token_count        integer NOT NULL CHECK (token_count > 0),

    -- Provenance. A chunk may straddle a page break; record both ends and cite honestly.
    page_start         integer NOT NULL CHECK (page_start >= 1),
    page_end           integer NOT NULL CHECK (page_end >= 1),
    char_start         integer NOT NULL,   -- offsets into the document's normalised text, so a
    char_end           integer NOT NULL,   -- chunk can always be located in the source again
    section_path       text,               -- 'Chapter 3 > 3.2 Signal timings', when known

    embedding_model_id smallint NOT NULL REFERENCES rag.embedding_models(id),
    embedding          vector(1024) NOT NULL,

    -- Generated, so the lexical index can never drift from the text it indexes.
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    created_at         timestamptz NOT NULL DEFAULT now(),

    UNIQUE (document_id, ordinal),
    CHECK (page_end >= page_start),
    CHECK (char_end > char_start)
);
```

Four choices in there worth defending:

| Choice | Why | What it costs |
|---|---|---|
| `workspace_id` **denormalised** onto `chunks` | The hot retrieval path filters on it; joining to `documents` before the candidate scan is avoidable work | One redundant column, kept honest by the FK and by ingestion writing both from the same row |
| `tsv` as a **generated column** | The lexical index cannot go stale relative to `content`. A trigger can be forgotten; a generated column cannot | `to_tsvector` must be immutable here, which means naming the config explicitly (`'english'`) rather than relying on the `default_text_search_config` GUC |
| `status` as **text + CHECK**, not a native enum | Adding a value later is one `ALTER TABLE ... DROP/ADD CONSTRAINT` inside an ordinary transaction; native enums need `ALTER TYPE ... ADD VALUE`, which has historically carried transaction restrictions | Slightly wider rows, no ordering semantics. Neither matters here |
| `content_sha256` as **bytea**, not hex text | 32 bytes instead of 64, and it is exactly what `hashlib.sha256().digest()` returns | You must remember `encode(content_sha256, 'hex')` when reading it by eye |

Finally, the telemetry table that makes the relevance gate calibratable rather than guessed:

```sql
CREATE TABLE rag.retrieval_events (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id       uuid NOT NULL,
    workspace_id     uuid NOT NULL REFERENCES rag.workspaces(id) ON DELETE CASCADE,
    query_sha256     bytea NOT NULL,
    query_text       text,                 -- only when RAG_LOG_QUERY_TEXT=1; see below
    candidate_count  integer NOT NULL,
    top_rerank_score real,
    threshold        real NOT NULL,
    grounded         boolean NOT NULL,
    cited_chunk_ids  bigint[] NOT NULL DEFAULT '{}',
    timings_ms       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX retrieval_events_created_idx ON rag.retrieval_events (created_at DESC);
CREATE INDEX retrieval_events_ungrounded_idx
    ON rag.retrieval_events (created_at DESC) WHERE NOT grounded;
```

`query_text` is nullable and off by default. Queries are user content; storing them is a social
decision, not a technical one. Default to the hash — enough to spot a question failing repeatedly —
and turn text logging on deliberately, with the team's knowledge, while calibrating the threshold.

### 8. Detecting a model mismatch at query time

Two guards: one at startup, one in every query.

```sql
-- Startup guard: the service asserts the database agrees with its own config, or refuses to serve.
CREATE OR REPLACE FUNCTION rag.assert_embedding_model(
    p_name text, p_revision text, p_dim integer
) RETURNS smallint
LANGUAGE plpgsql STABLE AS $$
DECLARE m rag.embedding_models%ROWTYPE;
BEGIN
    SELECT * INTO m FROM rag.embedding_models WHERE is_active;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no active embedding model registered';
    END IF;
    IF m.name <> p_name OR m.revision <> p_revision OR m.dim <> p_dim THEN
        RAISE EXCEPTION
            'embedding model mismatch: db has %@% (dim %), service has %@% (dim %). '
            'The corpus must be re-embedded, or the service config reverted.',
            m.name, m.revision, m.dim, p_name, p_revision, p_dim;
    END IF;
    RETURN m.id;
END;
$$;
```

```python
# services/rag/rag/db.py  --  called once during FastAPI lifespan startup
async def resolve_embedding_model(conn) -> int:
    """Returns the active embedding_model_id, or raises and stops the service."""
    return await conn.fetchval(
        "SELECT rag.assert_embedding_model($1, $2, $3)",
        EMBEDDING.name, EMBEDDING.revision, EMBEDDING.dim,
    )
```

The second guard is `AND c.embedding_model_id = $model_id` in the retrieval query (§10). Together
they mean: change the model without re-embedding and the service **fails to start**; get halfway
through a re-embed and only the chunks carrying the new model are searchable — degraded, but never
wrong.

The re-embed procedure itself is deliberately boring:

```
  1. INSERT the new model row with is_active = false
  2. Re-embed every chunk into it (the corpus stays searchable on the old model throughout)
  3. In ONE transaction: flip is_active from old to new
  4. Delete the old chunks' vectors, then VACUUM
```

### 9. Indexes — HNSW over IVFFlat

| | HNSW | IVFFlat |
|---|---|---|
| Structure | Multi-layer navigable small-world graph | Inverted lists over k-means centroids |
| Recall at equal latency | Higher, consistently | Lower; sensitive to `lists` and to data drift |
| Build time | Slow — the graph is built edge by edge | Fast |
| Build memory | High; wants the graph to fit `maintenance_work_mem` | Modest |
| Needs existing data? | **No.** Builds on an empty table and stays correct as rows arrive | **Yes.** Centroids are trained on what is present, so it degrades as the corpus grows |
| Index size | Larger (the graph edges) | Smaller |
| Verdict | **Chosen** | Rejected |

The decider is not recall, it is the *"needs existing data"* row. Our corpus grows continuously as
people upload documents. An IVFFlat index trained on the first 5,000 chunks quietly gets worse as the
corpus reaches 500,000, and nothing tells you — you would simply have to remember to rebuild it
periodically, forever. HNSW has no training step and no drift. Paying once at build time for a
structure that never needs babysitting is the right trade at our size.

```sql
-- Give the build room and cores. Both are session settings; they do not persist.
SET maintenance_work_mem = '4GB';           -- respect the WSL2 ~48 GB cap on .87
SET max_parallel_maintenance_workers = 4;

CREATE INDEX chunks_embedding_hnsw
    ON rag.chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX chunks_tsv_gin        ON rag.chunks USING gin (tsv);
CREATE INDEX chunks_workspace_idx  ON rag.chunks (workspace_id);
CREATE INDEX chunks_document_idx   ON rag.chunks (document_id);
```

**The three parameters, in plain terms:**

- **`m` (default 16)** — how many bidirectional edges each node keeps on the upper graph layers. More
  edges means more paths into a neighbourhood, so better recall on hard queries, at the cost of index
  size and build time. 16 is the standard starting point. 32 is the knob to reach for only if
  measured recall@5 says the *index* is the bottleneck — which it usually is not; chunking usually
  is.
- **`ef_construction` (default 64)** — how wide the candidate list is *while building*. Higher means
  better-chosen edges, so recall improves permanently, but build time rises roughly linearly. It must
  be at least `2 * m`. 64 for the first build; 128 is a reasonable second attempt if the eval set is
  unhappy and you can afford the rebuild.
- **`hnsw.ef_search` (default 40)** — how wide the candidate list is *at query time*. This is the only
  runtime knob, and the only one changeable without a rebuild. Raise it to trade latency for recall:

```sql
SET LOCAL hnsw.ef_search = 100;   -- must be >= the dense arm's LIMIT
```

Set it in the same transaction as the search, never globally — the right value differs per call site.
Our dense arm asks for 60 candidates, so `ef_search` must be at least 60; 100 gives the graph room to
find them properly. Treat 100 as a starting hypothesis for
[`17-evaluation.md`](./17-evaluation.md) to settle.

**The build-time and memory tradeoff, honestly.** HNSW build is the slowest operation in this system.
If the graph does not fit in `maintenance_work_mem`, pgvector builds it in two passes using temporary
files and it gets dramatically slower — this is the single failure mode to watch during a full
re-index. So:

- Size `maintenance_work_mem` to hold the graph, within the WSL2 cap.
- **Bulk-load first, index after.** During a full corpus rebuild, drop the HNSW index, insert every
  chunk, create the index once. Building it incrementally row by row is far slower in total.
- Rough sizing, and **mark it an estimate until measured**: a 1024-dim float32 vector is
  `4 x 1024 + 8 = 4,104` bytes, so vectors alone are roughly 4 GB per million chunks, with the graph
  adding the same order again. Get the truth with
  `SELECT pg_size_pretty(pg_relation_size('rag.chunks_embedding_hnsw'));` after the first real load,
  and replace this paragraph with the number.

**The filtered-search caveat.** Every one of our queries carries `WHERE workspace_id = ... AND
embedding_model_id = ...`. Historically pgvector applied such filters *after* the index scan, so a
narrow filter could return fewer rows than `LIMIT` asked for. pgvector 0.8 added iterative index
scans, which keep scanning until enough rows survive the filter:

```sql
SET LOCAL hnsw.iterative_scan  = 'relaxed_order';   -- verify against your pgvector version
SET LOCAL hnsw.max_scan_tuples = 20000;
```

With one or two workspaces this barely matters. With twenty it matters a great deal. Check what your
installed version supports; if iterative scan is unavailable, the fallback is over-fetching (ask for
200, filter, keep 60) — inelegant but correct.

### 10. The hybrid search query

This is what the RAG service runs on every question: one round trip, two arms scored independently,
fused with Reciprocal Rank Fusion, top 30 returned for reranking.

```sql
-- Parameters:
--   $1 query embedding (vector)   $4 active embedding_model_id (smallint)
--   $2 query text                 $5 per-arm candidates, e.g. 60
--   $3 workspace_id (uuid)        $6 rows returned, e.g. 30
WITH params AS (
    SELECT $1::vector    AS q_vec,
           $2::text      AS q_text,
           $3::uuid      AS ws,
           $4::smallint  AS model_id,
           $5::int       AS per_arm,
           $6::int       AS out_k,
           60.0::real    AS rrf_k
),
dense AS (
    SELECT c.id,
           row_number() OVER (ORDER BY c.embedding <=> p.q_vec) AS rnk,
           1 - (c.embedding <=> p.q_vec)                        AS cosine_sim
    FROM rag.chunks c
    CROSS JOIN params p
    JOIN rag.documents d ON d.id = c.document_id
    WHERE c.workspace_id       = p.ws
      AND c.embedding_model_id = p.model_id
      AND d.status             = 'ready'
    ORDER BY c.embedding <=> p.q_vec
    LIMIT (SELECT per_arm FROM params)
),
lexical AS (
    SELECT c.id,
           row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.query) DESC) AS rnk,
           ts_rank_cd(c.tsv, q.query)                                   AS ts_score
    FROM rag.chunks c
    CROSS JOIN params p
    CROSS JOIN LATERAL websearch_to_tsquery('english', p.q_text) AS q(query)
    JOIN rag.documents d ON d.id = c.document_id
    WHERE c.workspace_id       = p.ws
      AND c.embedding_model_id = p.model_id
      AND d.status             = 'ready'
      AND c.tsv @@ q.query
    ORDER BY ts_rank_cd(c.tsv, q.query) DESC
    LIMIT (SELECT per_arm FROM params)
),
fused AS (
    SELECT COALESCE(dn.id, lx.id)                  AS chunk_id,
           COALESCE(1.0 / (p.rrf_k + dn.rnk), 0.0)
         + COALESCE(1.0 / (p.rrf_k + lx.rnk), 0.0) AS rrf_score,
           dn.rnk        AS dense_rank,
           lx.rnk        AS lexical_rank,
           dn.cosine_sim AS cosine_sim,
           lx.ts_score   AS ts_score
    FROM dense dn
    FULL OUTER JOIN lexical lx ON lx.id = dn.id
    CROSS JOIN params p
)
SELECT f.chunk_id,
       c.content,
       c.page_start,
       c.page_end,
       c.token_count,
       c.section_path,
       d.id    AS document_id,
       d.title AS document_title,
       d.source_uri,
       f.rrf_score,
       f.dense_rank,
       f.lexical_rank,
       f.cosine_sim,
       f.ts_score
FROM fused f
JOIN rag.chunks    c ON c.id = f.chunk_id
JOIN rag.documents d ON d.id = c.document_id
ORDER BY f.rrf_score DESC, f.chunk_id
LIMIT (SELECT out_k FROM params);
```

Notes on the shape of it:

- **`FULL OUTER JOIN`, not `UNION`.** A chunk found by both arms must contribute *both* reciprocal
  ranks. A `UNION` deduplicates it and throws one away — and agreement between the two arms is
  precisely the signal RRF exists to capture.
- **`COALESCE(..., 0.0)`** is what "not found by this arm" means: rank infinity, contribution zero.
- **`rrf_k = 60`** is the constant from the original RRF paper. It flattens the difference between
  ranks 1 and 2 relative to the difference between 1 and 20 — i.e. "being in the top few matters, the
  exact order within the top few does not", which is exactly right when a cross-encoder is about to
  reorder them anyway.
- **`websearch_to_tsquery`** over `plainto_tsquery` because it understands quoted phrases and `-`
  exclusion, which is what people actually type — and unlike `to_tsquery` it never raises a syntax
  error on user input.
- **`ts_rank_cd`** (cover density) over `ts_rank` because it rewards query terms appearing close
  together, the right bias for chunk-sized text.
- **`d.status = 'ready'`** keeps half-ingested documents out of results. A document being re-ingested
  is invisible rather than partially visible.
- **Tie-break on `chunk_id`** makes the query deterministic. Two chunks with identical RRF scores must
  come back in the same order every time, or [`13-rag-service-api.md`](./13-rag-service-api.md)'s
  idempotency promise is a lie.

Run `EXPLAIN (ANALYZE, BUFFERS)` before believing anything about its cost, and confirm the dense arm
actually uses `chunks_embedding_hnsw` rather than falling back to a sequential scan. An operator or
opclass mismatch — `<=>` against a `vector_l2_ops` index, say — silently produces a correct but slow
plan, and "correct but slow" is the hardest kind of bug to notice.

### 11. Postgres configuration

Sized for `.87`'s WSL2 cap (~8 vCPU, ~48 GB), not for the 128 GB on the sticker. **Verify against
your version and re-tune after M0.**

| Setting | Value | Why |
|---|---|---|
| `shared_buffers` | `8GB` | ~1/6 of the WSL2 allocation; leaves room for the page cache and for Infinity |
| `effective_cache_size` | `24GB` | A planner hint, not an allocation. Tells it the OS cache is large |
| `work_mem` | `64MB` | Per sort/hash node, and the hybrid query has several. Multiply by concurrency before raising it |
| `maintenance_work_mem` | `4GB` | HNSW builds want this. Raise temporarily for a full re-index |
| `max_parallel_maintenance_workers` | `4` | Parallel HNSW build |
| `random_page_cost` | `1.1` | It is NVMe, not a spinning disk |
| `effective_io_concurrency` | `200` | NVMe |
| `max_connections` | `100` | Our services use a small pool; do not let this creep upward |
| `hnsw.ef_search` | per query | Never global — the right value differs per call site |

### 12. Migrations with Alembic

Per [`delivery-plan.md`](./delivery-plan.md) §9, migrations are **an explicit deploy step, never
automatic on service start.** The reason is narrow and important: if the service migrates on boot,
then rolling back to the previous image tag starts a container that immediately migrates the database
*forward* again. Your rollback silently undoes itself, and the symptom looks like the old version
being broken too. Separating them keeps rollback a one-line, predictable action.

```
migrations/
  env.py
  versions/
    0001_initial_schema.py
    0002_add_section_path.py
```

The migration imports the config constant, so schema and wrapper cannot disagree:

```python
# migrations/versions/0001_initial_schema.py
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from rag.config import EMBEDDING          # the same constant the embed wrapper uses

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS rag")
    # ... workspaces, workspace_members, embedding_models, documents ...
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # ... the rest of the columns ...
        sa.Column("embedding", Vector(EMBEDDING.dim), nullable=False),   # D, not a literal
        schema="rag",
    )
    op.execute("""
        ALTER TABLE rag.chunks
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
    """)
    op.execute("""
        CREATE INDEX chunks_embedding_hnsw ON rag.chunks
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)

def downgrade() -> None:
    op.drop_table("chunks", schema="rag")
    # ... and the rest, in reverse dependency order
```

```bash
# Deploy sequence, from the Makefile. Migrate, THEN start the new image.
make db-migrate HOST=87              # alembic upgrade head, in a one-shot container
make deploy     HOST=87 VERSION=0.5.0
```

Three rules that keep this survivable:

1. **Write and test the `downgrade()`.** An untested downgrade is not a rollback plan. Run
   `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` against the `_dev` database
   as part of writing every migration.
2. **Never rewrite a migration that has been applied anywhere.** Add a new one.
3. **Long index builds do not belong in a deploy-blocking migration.** Creating an HNSW index on a
   populated table takes minutes to hours. Use `CREATE INDEX CONCURRENTLY` from a standalone
   maintenance script — it cannot run inside a transaction, so it cannot live in an ordinary Alembic
   step anyway.

### 13. Backups

Nightly `pg_dump` to `.226`'s 8 TB NVMe. Seven daily plus four weekly, per
[`delivery-plan.md`](./delivery-plan.md) §9.

```bash
#!/usr/bin/env bash
# scripts/backup-postgres.sh -- runs on .87 from a systemd timer inside WSL2
set -euo pipefail

STAMP=$(date +%Y%m%d-%H%M)
LOCAL=/var/backups/postgres
REMOTE=user@10.0.0.226:/mnt/d/backups/postgres     # .226's 8 TB NVMe

mkdir -p "$LOCAL"
umask 077                                            # dumps contain full document text

# Custom format: compressed, restorable selectively and in parallel.
pg_dump --dbname="$DATABASE_URL" \
        --format=custom --compress=9 --no-owner --no-privileges \
        --file="$LOCAL/aiplatform-$STAMP.dump"

# Roles and other cluster-wide objects are NOT in a per-database dump.
pg_dumpall --dbname="$DATABASE_URL" --globals-only \
        --file="$LOCAL/globals-$STAMP.sql"

rsync -a --remove-source-files "$LOCAL"/ "$REMOTE"/

# Retention is applied on .226, where the files actually live.
ssh user@10.0.0.226 \
  'find /mnt/d/backups/postgres -name "aiplatform-*.dump" -mtime +7 -delete'
```

**Things that are easy to get wrong here:**

- **A dump *is* the corpus.** It contains every word of every document, so it inherits N1 exactly: it
  may move between `.87` and `.226` because both are inside the network, and it may go nowhere else —
  not a laptop, not a USB stick, not a cloud drive. Hence `umask 077`, and a backup directory that is
  not inside any shared folder.
- **`--globals-only` is not optional.** Restore a database dump into a fresh cluster without the roles
  and every `GRANT` fails.
- **Credentials.** `DATABASE_URL` comes from the host's gitignored `.env` and `~/.pgpass`. The script
  never contains a password, and neither does this repo.
- **A dump stores index *definitions*, not index contents.** Restore rebuilds the HNSW index from
  scratch, and that is the long pole in any recovery — quite possibly longer than restoring the data.
  Measure it during the restore test so the recovery-time estimate is real rather than hoped for.

**Test the restore.** An untested backup is a rumour.

```bash
# Restore into a scratch database on .87 and prove the corpus is intact.
createdb aiplatform_restoretest
time pg_restore --dbname=aiplatform_restoretest --jobs=4 --no-owner \
     /mnt/d/backups/postgres/aiplatform-20260901-0300.dump

psql -d aiplatform_restoretest -c "
  SELECT (SELECT count(*) FROM rag.documents WHERE status='ready') AS ready_docs,
         (SELECT count(*) FROM rag.chunks)                         AS chunks,
         (SELECT count(*) FROM rag.chunks WHERE embedding IS NULL) AS null_vectors;"

# The real test is not row counts: run one eval-set question against the restored database
# and confirm it returns the same citations as production.
dropdb aiplatform_restoretest
```

Do this in M8 and record the wall-clock restore time in [`18-operations.md`](./18-operations.md).

**What we are deliberately not doing.** No WAL archiving, no pgBackRest, no point-in-time recovery.
The recovery objective is "the corpus comes back", and the corpus is rebuildable from `/srv/corpus`
in any case — chunks and embeddings are derived data. Losing up to 24 hours of ingestion means
re-running ingestion, not losing information. **If the platform ever stores something that is neither
derived nor re-creatable, revisit this the same day.**

---

## Reflect

**What we traded away.** Purpose-built vector databases offer quantisation, richer metadata
filtering, and snapshotting that we do not get. We accepted worse vector tooling in exchange for one
datastore, transactional re-ingestion, and tenancy as an ordinary join. At a few hundred thousand
chunks that is clearly the right side of the trade; at a few million it deserves re-examination, and
the trigger to watch is HNSW rebuild time during a full re-index, not query latency.

**The thing most likely to bite.** Not performance — the embedding-model constant. Every guard in §4
and §8 exists because a model mismatch is the one failure in this layer that produces confident,
plausible, entirely wrong answers with no error anywhere in the logs. Everything else here fails
loudly. If a future reader is tempted to simplify away the `embedding_models` table because "we only
ever use one model", this paragraph is the argument against it.

**What we would revisit first.** The `english` text-search configuration is baked into a generated
column, which makes it awkward to change and useless for documents in other languages. If the corpus
turns out to be multilingual, that column needs a rethink — probably a `language` column on
`documents` plus per-language partial indexes — and it is far cheaper to discover that during M5 than
after 100,000 chunks exist.

**Next:** [`11-ingestion.md`](./11-ingestion.md).
