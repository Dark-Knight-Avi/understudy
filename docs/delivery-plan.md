# Delivery Plan — Development, Implementation & Deployment

> How this actually gets built and shipped onto three shared workstations, in what order, by whom,
> with what acceptance at each step.
>
> Read after [`00`](./00-goals-and-constraints.md)–[`04`](./04-m0-spikes.md) and
> [`tech-stack.md`](./tech-stack.md).

---

## 1. How we work

**Roles.** You write all service code and run all commands on the hosts. I write `docs/**`,
configuration examples, schemas, and step-by-step build instructions with the reasoning behind them.
Each milestone produces working software *and* the doc describing what actually shipped — written
after, not before, so it records reality.

**Rhythm per milestone:**

```
  design note  ->  you build  ->  acceptance test  ->  I write the doc  ->  tag & deploy
      (me)          (you)          (both, from §6)         (me)              (you)
```

**Walking skeleton first.** M1 puts a thin end-to-end path into real use — browser to gateway to
model to browser — before anything is deep. Every later milestone thickens a slice of that path.
Nothing is built for months in isolation.

---

## 2. Repository layout

One repo, three services, per-host deploy configs:

```
ai-platform/
  docs/                      # this folder
  services/
    rag/                     # FastAPI, OpenAI-compatible; ingestion + retrieval
    mcp-tools/               # FastMCP; search, pdf, pptx, image
    fleet-controller/        # FastAPI + single-page dashboard
  deploy/
    common/
      compose.base.yaml
    host-226/                # vLLM, ik_llama.cpp, .wslconfig
      compose.yaml
      .env.example
    host-87/                 # postgres, litellm, open-webui, caddy, searxng, infinity, our 3 services
      compose.yaml
      .env.example
    host-149/                # comfyui
      compose.yaml
      .env.example
  migrations/                # Alembic
  scripts/
    gpu-run                  # preflight wrapper
    spike-*.sh               # M0 measurement scripts
  Makefile
  .env.example
  .gitignore
```

**Why per-host compose rather than one file with profiles:** the hosts genuinely run different
things, on different OS setups, on different subnets. Three small explicit files are easier to reason
about at 3 a.m. than one file with conditional profiles.

---

## 3. Environments — the shared-workstation problem

There is no staging hardware. Everything runs on machines people use. Three rules resolve this:

**Rule 1 — develop off-host.** The three services we build are thin and network-bound: they call the
gateway, Postgres and the embeddings server over HTTP. So develop them **on your own machine**,
pointed at the real backends:

```
DATABASE_URL=postgresql://...@10.0.0.87:5432/aiplatform_dev
GATEWAY_URL=http://10.0.0.87:4000
EMBEDDINGS_URL=http://10.0.0.87:7997
```

You get a fast edit-run loop with no containers, no deploys, and no risk to the running platform.
This is the single biggest productivity decision in the plan.

**Rule 2 — a `_dev` database, never a `_dev` GPU.** Run a second Postgres database (same server,
different DB name) for development. Do **not** stand up a second set of models — VRAM is the scarce
resource and duplicating it defeats the sharing policy.

**Rule 3 — prod is boring.** On the hosts, containers run with `restart: unless-stopped` and pinned
image tags. Nothing is edited in place on a host. If a fix is needed, it goes through the repo.

---

## 4. Build and release mechanics

No CI system. Three hosts and one operator do not justify one; add it later if it hurts.

**A local registry on `.87`.** Run `registry:2` on `.87` and push images there. This means each host
pulls a built image rather than rebuilding, and — importantly given the egress constraint — image
pulls stay inside the network.

```bash
# build and publish (from your machine or .87)
make build SERVICE=rag VERSION=0.3.0
# -> builds, tags 10.0.0.87:5000/rag:0.3.0, pushes

# deploy a host
make deploy HOST=87        # ssh, git pull, compose pull, compose up -d
```

**Versioning.** Semver tags on images, git tags on releases. Never deploy `latest` — a pinned tag is
what makes rollback a one-line change.

**Model weights are not in the pipeline.** They are 10–250 GB each. Download once per host onto local
NVMe, record the exact revision in the host's `.env`, and never pull them over the 1 GbE link during
a deploy.

---

## 5. Host bring-up order

Order matters, and it is not the order of the milestone list.

### `.87` first — the hub

Everything else registers with it, so it exists before anything else is useful.

1. WSL2 configured and capped (`.wslconfig`: ~8 processors, ~48 GB, `autoMemoryReclaim=gradual`)
2. `networkingMode=mirrored` so the host IP serves LAN clients; services bind `0.0.0.0`
3. `systemd=true` in `/etc/wsl.conf`; Windows Task Scheduler task to start WSL at boot
4. Docker, local registry, Postgres + pgvector, Caddy
5. LiteLLM gateway, Open WebUI

### `.226` second — the models

1. Same WSL2 setup and caps (this is the box with the modelling runs — caps are not optional)
2. CUDA toolkit **only** inside WSL2; never a Linux NVIDIA driver
3. vLLM, model weights on the 8 TB NVMe
4. Register with the gateway on `.87`

### `.149` last — and start the paperwork now

Blocked on approval to install native Ubuntu, so **raise that request during M0**, not at M7. Its
lead time is the longest thing in this plan and it is pure waiting.

1. Native Ubuntu (skips the Blackwell/WSL2 memory-overhead problem entirely)
2. Verify `sm_120` kernels in your PyTorch/ComfyUI builds
3. ComfyUI + FLUX.1-schnell
4. Register with the gateway

---

## 6. Milestone plan

Effort assumes focused days. Part-time, multiply by two to three.

| # | Milestone | Hosts | You build | I document | Acceptance | Effort |
|---|---|---|---|---|---|---|
| **M0** | Spikes | all 3 | Run [`04-m0-spikes.md`](./04-m0-spikes.md) | Results recorded, design adjusted | Spikes 1–4 pass or have a recorded workaround | 1–2 d |
| **M1** | Chat online | `.87`, `.226` | Host setup, Postgres, LiteLLM, vLLM, Open WebUI, Caddy | `05-host-setup`, `06-model-gateway`, `07-inference-servers` | A colleague logs in from their own machine and chats; TTFT < 2 s | 2–3 d |
| **M2** | Coexistence | `.87`, `.226` | **Fleet controller** + dashboard toggle, `gpu-run` | `08-fleet-controller` | All 8 tests in [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §7 | 4–6 d |
| **M3** | Coding | `.226` | OpenCode + Cline config; context tuning | `09-coding-agents` | A real task completed end-to-end in both clients | 2–3 d |
| **M4** | Deep tier | `.226` | `ik_llama.cpp`, 235B weights, gating on modelling state | `07-inference-servers` §deep | Deep model in the catalog; modelling runs unaffected | 3–5 d |
| **M5** | RAG | `.87` | Schema, ingestion, hybrid search, rerank, **RAG service** | `10`–`13` | Cited answers; recall@5 measured before/after rerank | 8–12 d |
| **M6** | Tools | `.87` | **MCP server**: search, PDF, PPTX; SearXNG | `14`, `15`, `16` | One tool invoked from all three clients | 4–6 d |
| **M7** | Image | `.149` | Ubuntu, ComfyUI, FLUX.1-schnell, MCP tool | `15` §image | Image generated from chat and terminal | 2–3 d |
| **M8** | Hardening | all 3 | Backups, monitoring, egress lockdown, boot resilience | `17`, `18` | Egress proof; reboot test; eval set green | 4–6 d |

**Total: roughly 6–9 focused weeks.** M5 is a third of it and is where the differentiated value is —
resist the urge to rush it after the quick wins of M1–M3.

### Why this order

- **M2 before M3.** Coexistence lands before anyone depends on the platform daily. Annoying people in
  week two is recoverable; annoying them in week eight is not.
- **M3 before M5.** Coding agents need only the gateway, so they deliver value while RAG is still
  being built. It also gets the platform used daily, which surfaces gateway bugs early.
- **M4 before M5.** Knowing the deep tier's real speed shapes retrieval design — how much context is
  affordable depends on how fast the model reads it.
- **M7 late.** It is the most isolated and the most externally blocked.

---

## 7. Parallel tracks

Three things can proceed independently of the critical path:

| Track | Start | Runs alongside | Why it can be parallel |
|---|---|---|---|
| `.149` Ubuntu approval + install | M0 | M1–M6 | Pure waiting on other people |
| Deep-tier weight downloads (100–250 GB) | M0 | M1–M3 | Bandwidth, not attention |
| Eval-set authoring (~50 Q&A pairs) | M1 | M2–M4 | Needs the corpus, not the code. **The most commonly skipped and most valuable prep** |

Building the eval set early is what makes M5 measurable rather than vibes. Write the questions before
you write the retriever, so you cannot unconsciously tune the questions to the implementation.

---

## 8. Configuration and secrets

- `.env.example` per host is committed; `.env` never is.
- Host passwords live in a password manager, not in this repo. (The three currently in circulation
  should be rotated — they were pasted in plaintext.)
- Generate `LITELLM_MASTER_KEY`, `BETTER_AUTH_SECRET` etc. per environment; never reuse dev values.
- Model revisions are pinned in `.env` — an unpinned model is an unreproducible deploy.

---

## 9. Data, migrations, backups

- **Migrations:** Alembic, versioned in `migrations/`, applied by an explicit step in the deploy — not
  automatically on service start, so a rollback never silently migrates forward.
- **Backups:** nightly `pg_dump` to `.226`'s 8 TB NVMe, seven daily plus four weekly. **Test a restore
  during M8** — an untested backup is a rumour.
- **Re-ingestion:** ingestion must be idempotent by document hash, so a corpus can be rebuilt from
  source at any time. This is your real safety net: chunks and embeddings are derived data.
- **The embedding-model trap:** changing the embedding model invalidates every vector. Record the
  model and dimension in the schema so a mismatch is detected at query time rather than silently
  returning nonsense.

---

## 10. Rollback

| Scenario | Action |
|---|---|
| Bad service release | Redeploy the previous image tag; ~30 s |
| Bad migration | Alembic downgrade, then previous tag. Test the downgrade path when you write it |
| Bad model choice | Change the catalog entry, restart the server. No code change ([N9](./00-goals-and-constraints.md)) |
| Platform disturbing users | Stop the compose stack on that host; the gateway routes elsewhere |
| Everything wrong | `docker compose down` on all three; the hosts return to being plain workstations |

That last row matters. The platform must be fully removable in one command per host, and nothing it
does may leave the machines worse than it found them. That property is what makes it politically
possible to install this on someone else's workstation.

---

## 11. Risk register

| Risk | Trigger to watch | Response |
|---|---|---|
| WSL2 eats VRAM on `.226` | M0 spike 1 < 20 GiB | Ladder rungs shift down; consider native Linux |
| CUDA hangs on AMD + WSL2 | M0 spike 2 | Ollama fallback, or move serving to `.149` |
| `.149` approval refused | M0 | Drop to two hosts; image gen time-shares `.226` |
| Deep tier starves modelling runs | M0 spike 7 | Off-hours only, or no deep tier |
| Cline burns the context window | M3 | Roo Code, or Aider's diff-based flow |
| Retrieval quality disappoints | M5 recall@5 | Chunking strategy first, then rerank depth, then model |
| Nobody adopts it | Usage after M3 | Talk to people. Usually the reason is speed or a missing tool, both fixable |
| Operator bus factor (one person) | Ongoing | The docs *are* the mitigation. Keep them current |

The last two are the ones that actually kill projects like this. The technical risks all have
workarounds; adoption and bus factor do not.

---

## 12. Definition of done

A milestone is done when **all four** hold:

1. It passes its acceptance test from §6, run by someone other than the person who built it where possible.
2. Its doc is written and describes what shipped, not what was planned.
3. It survives a reboot of every host it touches.
4. It can be rolled back with the table in §10.

---

## Reflect

The plan's shape is deliberate: **value early, coexistence before dependence, the hard part in the
middle.** M1 gets people using it in week one, M2 makes it safe to leave running, and M5 — the RAG
work that justifies the whole exercise — lands once the platform is stable enough to build on.

The sequencing risk worth naming: after the fast wins of M1–M3 there is a real temptation to declare
victory. But a chat UI over a local model is something anyone can install in an afternoon; the thing
that makes this platform *ours* is retrieval over our documents, and that is entirely in M5. Budget
for it, and start the eval set in week one so its quality is measurable rather than argued about.

**Next:** run [`04-m0-spikes.md`](./04-m0-spikes.md), and raise the `.149` Ubuntu request the same day.
