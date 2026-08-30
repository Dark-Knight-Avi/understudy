# M1 Runbook — chat online

> **Goal:** a colleague opens a browser, logs in, and talks to a local model.
> Nothing else. Not RAG, not coding agents, not the fleet controller.
>
> **Prerequisites:** M0 gates cleared (spikes 1, 2, 4 pass), and
> [`05-host-setup.md`](./05-host-setup.md) §0 complete on `.87` and `.226`.
>
> **Time:** roughly a day, most of it waiting on a model download.
>
> **Addresses below are the repo's placeholders.** Substitute your own from
> `deploy/fleet.local.yaml` -- `10.0.0.87` is the hub, `10.0.0.226` the model host.

---

## Why this order

The temptation is to bring everything up at once and debug the pile. Don't.

Each step here is independently verifiable, and every one has a `curl` that either
answers or doesn't. When something breaks — and something will — you want to be
debugging one new thing, not seven interacting ones. Do not proceed past a failed
verification.

The dependency chain is real: Open WebUI needs the gateway, the gateway needs a
backend, and the backend needs weights on disk. So the model download starts first
even though it's used last.

---

## Step 0 — start the download (`.226`)

~17 GB. Do this first and let it run while you do everything else.

**On `.226` only.** vLLM runs there; `.87` serves the gateway and UI and never
loads a model. Downloading to `.87` puts 17 GB on the wrong machine, and on a
smaller disk than the one meant to hold it.

```bash
cd ~/understudy && git pull
uv pip install huggingface-hub

# `uv run` -- the CLI installs into the venv, not onto PATH.
#
# This is the CHAT model: the thing that generates replies. Hugging Face is a
# model registry, not an embeddings service -- it hosts every kind of model.
# Embeddings arrive at M5 and come from the same place.
uv run huggingface-cli download Qwen/Qwen3-14B-Instruct-AWQ \
  --local-dir /models/qwen3-14b-int4
```

**Why the 14B and not the 30B coder:** M1 proves the path end to end. A smaller
model downloads sooner, loads faster, and leaves room to be wrong about footprints.
The coder rung arrives once the ladder does.

Check the path matches `MODELS_DIR` in `deploy/host-226/.env`, and that it is on the
NVMe rather than `/mnt/c`.

---

## Step 1 — secrets (`.87`)

```bash
cd ~/understudy/deploy/host-87
cp .env.example .env

# Generate every secret. Never reuse a value across two of them.
for k in POSTGRES_PASSWORD LITELLM_MASTER_KEY LITELLM_SALT_KEY LITELLM_UI_PASSWORD \
         INFINITY_KEY RAG_SERVICE_KEY OPEN_WEBUI_GATEWAY_KEY RAGFLOW_GATEWAY_KEY \
         WEBUI_SECRET_KEY SEARXNG_SECRET MCP_TOKEN; do
  printf '%s=%s\n' "$k" "$(openssl rand -hex 24)"
done >> .env

$EDITOR .env      # remove the now-duplicated placeholder lines, set the paths
```

`.env` is gitignored. Keep a copy in your password manager — losing
`WEBUI_SECRET_KEY` invalidates every session, and losing `POSTGRES_PASSWORD`
means restoring from backup.

**Verify:**
```bash
docker compose --env-file .env config -q && echo "compose OK"
```

---

## Step 2 — Postgres (`.87`)

```bash
docker compose --env-file .env up -d postgres
docker compose logs -f postgres      # wait for "database system is ready"
```

**Verify:**
```bash
docker compose exec postgres psql -U aiplatform -c "SELECT version();"
docker compose exec postgres psql -U aiplatform -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

The `vector` extension must succeed here. If it fails, the image is plain Postgres
rather than pgvector, and every later step would build on a store that cannot hold
an embedding.

---

## Step 3 — local registry (`.87`)

```bash
docker compose --env-file .env up -d registry
curl -sf http://localhost:5000/v2/_catalog && echo " registry OK"
```

Needs `{"insecure-registries": ["10.0.0.87:5000"]}` in `/etc/docker/daemon.json`
on **all three hosts**, then `sudo systemctl restart docker`. Unauthenticated and
unencrypted, which is tolerable only because it is firewalled to the internal
subnets — see [`05`](./05-host-setup.md) §7.

---

## Step 4 — vLLM (`.226`)

The first real GPU work.

```bash
cd ~/understudy/deploy/host-226
cp .env.example .env && $EDITOR .env        # MODELS_DIR, model name, gateway key
docker compose --env-file .env up -d vllm-fast
docker compose logs -f vllm-fast            # first load takes minutes
```

**Verify — from `.226` first, then from `.87`:**
```bash
curl -s http://localhost:8000/v1/models | head
curl -s http://10.0.0.226:8000/v1/models | head     # run this one ON .87
```

If it answers locally but not from `.87`, that is the **Hyper-V firewall layer**,
not vLLM. See [`05`](./05-host-setup.md) §7 — layer 1 alone is not enough, and this
is the step where that bites.

**Watch VRAM while it loads:**
```bash
watch -n2 nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

Record the resident figure. That is the **real footprint** — weights plus KV cache
plus context — and the first honest number to replace the 9.0 GB estimate in
`fleet.local.yaml`. If it lands far above, the ladder rungs move.

---

## Step 5 — LiteLLM gateway (`.87`)

```bash
cd ~/understudy/deploy/host-87
$EDITOR litellm/config.yaml      # point a model at http://10.0.0.226:8000
docker compose --env-file .env up -d litellm
```

**Verify:**
```bash
curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  http://localhost:4000/v1/models | jq .

curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"chat","messages":[{"role":"user","content":"reply with just: ok"}]}' \
  http://localhost:4000/v1/chat/completions | jq -r '.choices[0].message.content'
```

**This is the milestone's real test.** A token generated on `.226` and returned
through the gateway on `.87` means the whole spine works. Everything after is
presentation.

---

## Step 6 — Open WebUI + Caddy (`.87`)

```bash
docker compose --env-file .env up -d open-webui caddy
```

Open WebUI is deliberately **not published** — only Caddy is, so there is one front
door with TLS.

**Verify from another machine on the LAN:**
```
https://10.0.0.87
```

First account created becomes the admin. Create yours before telling anyone the URL.

---

## Step 7 — the acceptance test

Not `curl`. A colleague, their own laptop, their own browser:

1. Opens `https://10.0.0.87`
2. Creates an account
3. Sends a message
4. Gets a streamed reply

**Measure time to first token.** Target is < 2 s warm ([N3](./00-goals-and-constraints.md)).
If it is much worse, check whether vLLM is spilling to system RAM — both hosts do
that silently rather than raising OOM, and an oversized model crawls over PCIe
instead of failing fast.

---

## What M1 deliberately does not include

| Not yet | Why |
|---|---|
| The fleet controller | M2. Until then `.226`'s GPU is held permanently — **tell whoever uses it** |
| RAG, embeddings, RAGFlow | M5 — and `.87` needs its driver update first |
| Coding agents | M3 |
| The model ladder | M2. One model, one rung |
| Egress lockdown | M8 — `.87` currently has open internet |

**The first of those matters socially.** Between M1 and M2 the platform holds VRAM
on `.226` with no way to yield it. If someone needs that GPU, they stop the stack:

```bash
cd ~/understudy/deploy/host-226 && docker compose down
```

Say so explicitly before anyone starts using the platform, and make M2 the next
milestone rather than a later one.

---

## If it breaks

| Symptom | First thing to check |
|---|---|
| Reachable locally, not from another host | Hyper-V firewall layer ([`05`](./05-host-setup.md) §7) |
| Gateway returns 404 for a model | Name mismatch between `litellm/config.yaml` and vLLM's `--served-model-name` |
| vLLM OOMs on load | `--gpu-memory-utilization` too high, or another process holds VRAM |
| Everything gone after a reboot | WSL2 did not start — the boot task ([`05`](./05-host-setup.md) §8) |
| Inexplicably slow generation | System-memory spill. `nvidia-smi` during a request |

---

## Then

Record the measured vLLM footprint in `fleet.local.yaml`, and go to **M2** — the
fleet controller, the toggle, and the ladder. That is what makes the platform a
guest rather than a squatter, and it should not wait.
