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
# `huggingface-cli` is deprecated; the command is now `hf`.
# Confirm the repo id first -- quantised repos get renamed and reorganised far
# more often than base models, and a wrong id fails with a 404:
#   uv run hf models ls --search "Qwen3-14B"
uv run hf download Qwen/Qwen3-14B-AWQ --local-dir /models/qwen3-14b-int4
```

**Why the 14B and not the 30B coder:** M1 proves the path end to end. A smaller
model downloads sooner, loads faster, and leaves room to be wrong about footprints.
The coder rung arrives once the ladder does.

Check the path matches `MODELS_DIR` in `deploy/host-226/.env`, and that it is on the
NVMe rather than `/mnt/c`.

---

## Step 1 — secrets (`.87`)

```bash
cd ~/understudy
uv run python scripts/gen-env.py deploy/host-87
```

Hand-editing this file goes wrong in one specific, quiet way: the Postgres
password appears three times — as `POSTGRES_PASSWORD`, and again inside both
`DATABASE_URL` and `LITELLM_DATABASE_URL`. Set one, miss the others, and Postgres
rejects the connection with an error that points at the credential rather than at
the URL still reading `CHANGE_ME`. The generator writes all three at once, and
gives `LITELLM_MASTER_KEY` the `sk-` prefix LiteLLM requires.

Then set the values it cannot know — this fleet's real addresses, and the vLLM
key, which **must be byte-identical to the one `.226` is serving with**:

```bash
cd ~/understudy/deploy/host-87
setenv() { grep -q "^$1=" .env && sed -i "s|^$1=.*|$1=$2|" .env || printf '%s=%s
' "$1" "$2" >> .env; }
setenv VLLM_226_BASE http://<226>:8000/v1
setenv VLLM_226_KEY  <the same key as deploy/host-226/.env>
```

`setenv` rather than plain `sed` because a `.env` generated before those keys
existed has no line to substitute, and `sed -i` would report success having
changed nothing.

`.env` is gitignored and mode 600. Copy it into a password manager now — losing
`WEBUI_SECRET_KEY` invalidates every session, and losing `POSTGRES_PASSWORD`
means restoring from backup.

> If it is ever lost while the stack is **running**, it is recoverable:
> `docker inspect <container> --format '{{range .Config.Env}}{{println .}}{{end}}'`
> prints what each container was started with. Recover before `docker compose down`.

**Verify:**
```bash
docker compose --env-file .env config -q && echo "compose OK"
```

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
docker compose --env-file .env up -d litellm
```

Nothing to edit: `litellm.config.yaml` reads `os.environ/VLLM_226_BASE`, which
Step 1 set. LiteLLM resolves `os.environ/` in **any** `litellm_params` field, not
only `api_key`, so the real address lives in `.env` and the config stays
committable. The gateway runs its own migrations against its own database on
first start, so Postgres must be healthy first.

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

**Then mint its gateway key.** `gen-env.py` puts a random string in
`OPEN_WEBUI_GATEWAY_KEY`, but LiteLLM only honours virtual keys minted against the
master key. Left as generated, the UI loads and simply shows an empty model list:

```bash
set -a && . ./.env && set +a
NEWKEY=$(curl -s -X POST http://localhost:4000/key/generate   -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'content-type: application/json'   -d '{"models":["chat"],"key_alias":"open-webui","metadata":{"surface":"chat-ui"}}'   | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
sed -i "s|^OPEN_WEBUI_GATEWAY_KEY=.*|OPEN_WEBUI_GATEWAY_KEY=$NEWKEY|" .env
docker compose --env-file .env up -d --force-recreate open-webui
```

`"models":["chat"]` is the point of the exercise: the key is scoped to one model,
so revoking it later logs out exactly the chat UI and nothing else. A surface
never gets the master key.

**Verify from another machine on the LAN:**
```
https://10.0.0.87
```

Also reachable at `https://<host-ip>` — the chat block is addressed by both its
name and `$PLATFORM_HOST` so nobody is blocked on a DNS ticket to try it.

The certificate warning is expected until each client trusts Caddy's local CA
root. Do that once, before the team arrives, rather than teaching people to click
through warnings:

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
# Windows: import to "Trusted Root Certification Authorities" (Local Machine)
# Linux:   /usr/local/share/ca-certificates/ then update-ca-certificates
```

That root lives in the `caddy_data` volume — `docker compose down -v` destroys it
and every client has to trust a new one. Copy it off first.

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
| A deploy keeps running the OLD image however often you edit the tag and recreate | **The shell environment beats `--env-file`.** `set -a && . ./.env && set +a` -- run to get `$LITELLM_MASTER_KEY` for a curl -- exports every variable in the file, and Compose then prefers the exported value over the file it is told to read. Editing `.env` afterwards changes nothing, `docker compose ps` shows the stale tag, and the bug you just fixed appears unfixed. `unset` the variable, or read single values with `grep '^KEY=' .env \| cut -d= -f2-` instead of sourcing the whole file |
| `curl` returns **`000`** — even on the host itself | The service is on a `internal: true` network. Docker **silently ignores `ports:`** there: no error, no published mapping, and `docker compose ps` shows an empty PORTS column while the container is healthy and answering its own healthcheck. It needs a second, non-internal network. Cost half of M1's bring-up on two separate hosts |
| Reachable locally, not from another host | Hyper-V firewall layer ([`05`](./05-host-setup.md) §7) |
| UI loads but the model dropdown is empty | `OPEN_WEBUI_GATEWAY_KEY` was never minted — Step 6 |
| **`ERR_SSL_PROTOCOL_ERROR`** in the browser, or curl's `tlsv1 alert internal error`, when visiting the host by **IP** | No SNI. TLS server names are DNS names, so browsers and curl omit the extension entirely for IP literals, and Caddy selects certificates by SNI — having a certificate for the IP is not enough, there must be a name to look it up by. Fixed by `default_sni` in the Caddyfile global block. Reproduces on the host's own loopback, which is how you tell it apart from the firewall faults below |
| Caddy answers on loopback but `000` from the host to its **own** LAN IP | WSL2 mirrored-networking hairpin, not a firewall. Test from a *different* host before chasing it — `.226 → .87` returning 200 while `.87 → .87` returns 000 is the expected shape |
| Replies contain literal `<think>` tags | `--reasoning-parser=qwen3` missing from the vLLM args |
| Gateway returns 404 for a model | Name mismatch between `litellm/config.yaml` and vLLM's `--served-model-name` |
| vLLM OOMs on load | `--gpu-memory-utilization` too high, or another process holds VRAM |
| Everything gone after a reboot | WSL2 did not start — the boot task ([`05`](./05-host-setup.md) §8) |
| Inexplicably slow generation | System-memory spill. `nvidia-smi` during a request |

---

## Then

Record the fast rung's footprint in `fleet.local.yaml` — M1 measured **16.2 GB**
resident at `FAST_GPU_UTIL=0.62`, against the 9.0 GB the file guessed. Note *why*
they differ: vLLM reserves `utilization x TOTAL` up front, so the footprint is set
by that flag rather than by the model, and the two must be edited together
([`deploy/fleet.yaml`](../deploy/fleet.yaml) header). Then go to **M2** — the
fleet controller, the toggle, and the ladder. That is what makes the platform a
guest rather than a squatter, and it should not wait.
