# Delivery Plan — Development, Implementation & Deployment

> How this actually gets built and shipped onto three shared workstations, in what order, by whom,
> with what acceptance at each step.
>
> Read after [`00`](./00-goals-and-constraints.md)–[`04`](./04-m0-spikes.md) and
> [`tech-stack.md`](./tech-stack.md).

---

## 1. How we work

**Roles.** I write the code and the docs; you run everything that touches the hosts — installs,
GPU work, deploys, and the M0 measurements. That split follows the hardware: I cannot reach the
machines, and the measurements are the part no one can guess.

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

One repo, two services, per-host deploy configs:

```
ai-platform/
  docs/                      # this folder
  services/
    mcp-tools/               # FastMCP; search, pdf, pptx, image
    fleet-controller/        # FastAPI + single-page dashboard
    #  retrieval is RAGFlow, adopted -- see adr/0007
  deploy/
    common/
      compose.base.yaml
    host-226/                # vLLM, ik_llama.cpp, .wslconfig
      compose.yaml
      .env.example
    host-87/                 # postgres, litellm, open-webui, caddy, searxng, ragflow, our 2 services
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

**Rule 1 — develop off-host.** Both services we build are thin and network-bound: they call the
gateway, RAGFlow and Postgres over HTTP. So develop them **on a normal machine**, pointed at the
real backends:

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

### `.210` third — overflow and embeddings failover

Same WSL2 setup and caps. This is somebody's daily workstation, so it holds nothing critical: a small
chat model and a GPU replica of the embeddings service.

### `.149` — not in scope

Optional and deferred. Image generation runs on `.226` under admission control instead. Adding
`.149` later is a native-Ubuntu install plus a config change; nothing in the design needs revisiting.

---

## 6. Milestone plan

Effort assumes focused days. Part-time, multiply by two to three.

**Status as of 2026-08-30.** M0, M1 and M2's gate are complete and running on the
real fleet. Per-milestone detail: [`04-m0-spikes.md`](./04-m0-spikes.md),
[`m1-runbook.md`](./m1-runbook.md), [`m2-runbook.md`](./m2-runbook.md).

| # | Milestone | Status | Hosts | You build | I document | Acceptance | Effort |
|---|---|---|---|---|---|---|---|
| **M0** | Spikes | ✅ **done** — both gating risks closed. WSL2 VRAM overhead 1.24–1.49 GiB, not the ~16 feared; 2 h CUDA soak passed on the Threadripper. **Spike 5 (workload profile) still outstanding** | all 3 | Run [`04-m0-spikes.md`](./04-m0-spikes.md) | Results recorded, design adjusted | Spikes 1–4 pass or have a recorded workaround | 1–2 d |
| **M1** | Chat online | ✅ **done** — Qwen3-14B on `.226` behind LiteLLM on `.87`, Open WebUI + Caddy TLS. 4.12× concurrency at 16k context | `.87`, `.226` | Host setup, Postgres, LiteLLM, vLLM, Open WebUI, Caddy | `05-host-setup`, `06-model-gateway`, `07-inference-servers` | A colleague logs in from their own machine and chats; TTFT < 2 s | 2–3 d |
| **M1.5** | **RAGFlow spike** | ⬜ not started | `.87` | Install RAGFlow, point at the gateway, run the eval set | ADR-0007 verdict | Refusal, per-user isolation, MCP reachability — all three pass | **1 d** |
| **M2** | Coexistence | 🟡 **gate passed, 4 of 8 tests unrun.** Claim → card free in ~12 s and held; chat uninterrupted from a 4B standby on `.87`; reclaim ~5 min after release. **The per-host ladder is not built** and only the cooperative path is proven — see [`m2-runbook.md`](./m2-runbook.md) | `.87`, `.226` | **Fleet controller** + dashboard toggle, `gpu-run` | `08-fleet-controller` | All 8 tests in [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §7 | 4–6 d |
| **M3** | Coding | ⬜ not started | `.226` | OpenCode + Cline config; context tuning | `09-coding-agents` | A real task completed end-to-end in both clients | 2–3 d |
| **M4** | Deep tier | ⬜ not started | `.226` | `ik_llama.cpp`, 235B weights, gating on modelling state | `07-inference-servers` §deep | Deep model in the catalog; modelling runs unaffected | 3–5 d |
| **M5** | RAG | ⬜ not started | `.87` | Integrate RAGFlow; relevance-gate wrapper only if the spike needs it | ADR-0007, `12` if wrapping | Cited answers; recall@5 on the eval set | **2–4 d** (12 d if the spike fails) |
| **M6** | Tools | ⬜ not started — note: `searxng`'s pinned tag does not exist upstream and must be corrected first | `.87`, `.226` | **MCP server**: search, PDF, PPTX; SearXNG; ComfyUI on `.226` under admission control | `14`, `15`, `16` | One tool invoked from all three clients; an image generated without disturbing a coding session | 5–7 d |
| **M7** | Hardening | ⬜ not started | all 3 | Backups, monitoring, egress lockdown, boot resilience | `17`, `18` | Egress proof; reboot test; eval set green | 4–6 d |

**Total: roughly 4–6 focused weeks** with RAGFlow adopted; 6–9 if the M1.5 spike fails and M5 is
built. That single day of spiking is the highest-leverage hour in the plan.

**On the estimates, now that three milestones have actually run.** M0 and M1 landed
close to plan. M2 did not: bringing the fleet controller up on real hardware
surfaced ten defects in a service that had 395 passing tests, and every one was
invisible until it ran against real GPUs — the controller could not recognise its
own model after a restart, mistook its own parked engine for a user's job and
re-took a reserved card, could not authenticate a deployment it had registered
itself, and discarded every log line below WARNING, which is why finding the rest
took a day rather than an hour.

None of that was avoidable by more careful design. It is what "deploy to real
hardware" costs, and the later milestones should be read with that in mind rather
than as though the estimates were wrong.

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
| ~~WSL2 eats VRAM on `.226`~~ | **Closed at M0.** Overhead is 1.49 GiB, not the ~16 feared | — |
| ~~CUDA hangs on AMD + WSL2~~ | **Closed at M0.** 2 h soak, 1,082,616 iterations, 150.4 it/s flat | — |
| **A colleague's job OOMs against a card the platform said was free** | Any report of an unexplained CUDA OOM on `.226` | **The live one.** Auto-preemption is untested, so tell people the toggle is *required* rather than optional until it is. M2 already produced this bug once — the platform re-took a reserved card after 60 s — and it was caught only by watching `nvidia-smi` during a claim |
| Platform slows the modelling runs it lives alongside | Per-iteration time vs the ~48 min baseline | **Untested.** The don't-disturb check must run before anyone else uses the platform: it is the question that decides whether this stays installed |
| `.210` approval refused | M0 | Drop to two hosts; image gen time-shares `.226` |
| Deep tier starves modelling runs | M0 spike 7 | Off-hours only, or no deep tier |
| Driver drift across hosts | `nvidia-smi` version per host | `.87` is on 615, `.226` still a generation back. It works; close it before M4 |
| Secrets exposed in transcripts or chat | Any key pasted outside `.env` | One rotation pass covering the host passwords, both vLLM keys, the Postgres password and the LiteLLM master/salt keys. Outstanding since M1 |
| Cline burns the context window | M3 | Roo Code, or Aider's diff-based flow |
| Retrieval quality disappoints | M5 recall@5 | Chunking strategy first, then rerank depth, then model |
| Nobody adopts it | Usage after M3 | Talk to people. Usually the reason is speed or a missing tool, both fixable |
| Operator bus factor (one person) | Ongoing | The docs *are* the mitigation. Keep them current |

The last two are the ones that actually kill projects like this. The technical risks all have
workarounds; adoption and bus factor do not.

**One risk was missing and belongs at the top now that the code has met real
hardware: a service that is confidently wrong about the GPU.** Every M2 defect
took that shape — the controller could not recognise its own model, mistook its
own parked engine for a user's job, and re-took a card it had just promised away.
None was caught by 395 passing tests, and none would have been caught by more of
them. They were caught by watching `nvidia-smi` while using the thing. Budget for
that on every milestone that touches the cards.

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
