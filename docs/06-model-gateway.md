# 06 — Model Gateway

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.
>
> Milestone **M1**. LiteLLM on `.87`: the catalog the team sees, how a model name becomes a request
> to one of three hosts, and what happens when the host that name points at has been claimed by the
> person sitting at it. Depends on [`05-host-setup.md`](./05-host-setup.md); paired with
> [`07-inference-servers.md`](./07-inference-servers.md), which is what it routes to.

---

## Concept

### 1. One URL, or the fleet leaks into every client

Without a gateway, every client has to know that the coder model is on `10.0.0.226:8000`, that
embeddings are on `10.0.0.87:7997`, and that when someone claims `.226` the right thing to do is
try `.87` instead. That knowledge would end up copy-pasted into Open WebUI settings, an OpenCode
config, a Cline config, and every script anyone writes. The fleet's topology would become
un-refactorable.

So: **one OpenAI-compatible endpoint at `http://10.0.0.87:4000`** (and `https://api.ai.lan` through
Caddy). Clients know one URL and a list of model names. Everything else — which host, which rung,
which fallback — is the gateway's problem.

That also delivers N9: swapping a model is a catalog edit, not a code change.

```
   Open WebUI     OpenCode      Cline        RAG service      MCP tools
        |             |           |               |               |
        +-------------+-----------+---------------+---------------+
                                  |
                       LiteLLM proxy on .87 :4000
                       catalog / routing / fallback / keys
                                  |
        +-------------------------+---------------------------+
        |                         |                           |
   vLLM .226:8000          vLLM .87:8000               vLLM .149:8000
   ik_llama .226:8081      Infinity .87:7997           (spare capacity)
        ^                         ^                           ^
        |                         |                           |
        +---- fleet controller (.87:8090) marks hosts up/down -+
```

### 2. The catalog is a product decision, not a config file

The names in the model picker are the platform's entire user interface for [ADR-0003](./adr/0003-model-tiers-and-ladder.md).
Three rules:

**Name by promise, not by parameter count.** `qwen3-coder-30b-a3b-instruct-awq` tells a user nothing
they can act on. `coder` tells them what to reach for. When we replace the model in six months, the
name still means the same thing and nobody's config breaks.

**One name per intent, plus pinned names for people who need reproducibility.** Evaluation (N7) and
bug reports need to name an exact model, so a small set of `pinned-*` entries exists alongside the
friendly ones. They are not hidden, just not the ones we advertise.

**Say the speed in the name where it is dramatic.** `deep-slow` is a better name than `deep`, because
a user who picks it and waits four minutes for a first token should have been warned by the label
rather than surprised by the clock.

### 3. The catalog

| Public name | Backing | Host | What it is for | Typical wait |
|---|---|---|---|---|
| `chat` | Largest fast-tier rung currently loaded | `.226`, falls back to `.87` | Default. General questions, drafting | TTFT < 2 s (N3) |
| `coder` | Qwen3-Coder-30B-A3B Int4 | `.226` | OpenCode, Cline, anything agentic | TTFT < 2 s warm |
| `chat-small` | Qwen3-4B Int4 | `.87` | Autocomplete, classification, cheap calls; the always-there floor | Fast, visibly weaker |
| `deep-slow` | Qwen3-235B-A22B Q4 via `ik_llama.cpp` | `.226` | Hard bugs, design review, long documents. **Gated** — see §7 | Tens of seconds to first token; ~10–20 tok/s (estimate, unmeasured) |
| `team-docs` | The RAG service, itself a model endpoint ([ADR-0005](./adr/0005-rag-as-a-model-endpoint.md)) | `.87` | Grounded answers with citations. **M5** | Retrieval overhead + generation |
| `embed` | Qwen3-Embedding-0.6B via Infinity | `.87` | Internal. Ingestion and query embedding | ms |
| `rerank` | bge-reranker-v2-m3 on CPU | `.87` | Internal. Retrieval reranking | 200–500 ms per query |
| `pinned-coder-30b`, `pinned-chat-14b`, … | Exact model, exact host | `.226` | Evaluation and bug reproduction | — |

`chat` and `coder` are deliberately *not* pinned to a rung. When `.226` is claimed and the ladder
drops to Qwen3-8B, `chat` still answers — from the 8B. The name is a promise about intent, not about
weights. The dashboard and Open WebUI show which rung is actually live, per
[`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §5, so the quality drop is explained rather
than mysterious.

**Image generation is not in this table.** It goes through the MCP tool server to ComfyUI on `.149`,
not through an OpenAI chat completion. See [`15-generation-tools.md`](./15-generation-tools.md).

---

## Build

### 4. Run it on `.87`

```yaml
# deploy/host-87/compose.yaml  (fragment)
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-v1.XX.Y     # PIN. Never :latest — see delivery-plan §4
    restart: unless-stopped
    ports:
      - "0.0.0.0:4000:4000"
    volumes:
      - ./litellm.config.yaml:/app/config.yaml:ro
    environment:
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}
      LITELLM_SALT_KEY: ${LITELLM_SALT_KEY}
      DATABASE_URL: ${LITELLM_DATABASE_URL}          # postgres on .87, its own database
      VLLM_226_KEY: ${VLLM_226_KEY}
      VLLM_87_KEY: ${VLLM_87_KEY}
      VLLM_149_KEY: ${VLLM_149_KEY}
      UI_USERNAME: ${LITELLM_UI_USERNAME}
      UI_PASSWORD: ${LITELLM_UI_PASSWORD}
    command: ["--config", "/app/config.yaml", "--port", "4000", "--host", "0.0.0.0"]
    depends_on:
      - postgres
```

`--host 0.0.0.0` matters — see [`05`](./05-host-setup.md) §3. Every value on the right of a `:` comes
from `/opt/ai-platform/.env`, which is gitignored; `deploy/host-87/.env.example` lists the same keys
with empty values and is committed.

Give LiteLLM **its own database** on the existing Postgres instance (`litellm`), not the platform's
application database. It manages its own schema with its own migrations, and mixing them makes both
harder to restore.

### 5. Example configuration

This is a starting point to type and then adjust, not a file to trust unread. LiteLLM's config schema
moves between releases: **verify every key below against the version tag you pinned**, and treat a
silently-ignored key as the default failure mode.

```yaml
# deploy/host-87/litellm.config.yaml

model_list:

  # ---- fast tier on .226 -------------------------------------------------
  # Two deployments share the public name `chat`: the .226 rung and the .87
  # floor. The router prefers .226 while it is healthy.
  - model_name: chat
    litellm_params:
      model: openai/fast-tier                 # matches vLLM --served-model-name
      api_base: http://10.0.0.226:8000/v1
      api_key: os.environ/VLLM_226_KEY
      rpm: 240
      timeout: 120
      stream_timeout: 120
    model_info:
      mode: chat
      id: chat-226
      description: "General chat. Runs on the largest model the 4090 can hold right now."

  - model_name: chat
    litellm_params:
      model: openai/small-tier
      api_base: http://10.0.0.87:8000/v1
      api_key: os.environ/VLLM_87_KEY
      rpm: 120
      timeout: 120
    model_info:
      mode: chat
      id: chat-87
      description: "Fallback chat on the hub's 4070. Weaker, always there."

  - model_name: coder
    litellm_params:
      model: openai/coder
      api_base: http://10.0.0.226:8000/v1
      api_key: os.environ/VLLM_226_KEY
      timeout: 600                            # agentic turns are long
      stream_timeout: 600
    model_info:
      mode: chat
      id: coder-226
      description: "Agentic coding. Qwen3-Coder-30B-A3B Int4."

  - model_name: chat-small
    litellm_params:
      model: openai/small-tier
      api_base: http://10.0.0.87:8000/v1
      api_key: os.environ/VLLM_87_KEY
    model_info:
      mode: chat
      id: small-87

  # ---- deep tier on .226 (gated, see section 7) --------------------------
  - model_name: deep-slow
    litellm_params:
      model: openai/deep
      api_base: http://10.0.0.226:8081/v1   # ik_llama.cpp llama-server
      api_key: os.environ/VLLM_226_KEY
      timeout: 1800                           # minutes, not seconds. On purpose
      stream_timeout: 1800
    model_info:
      mode: chat
      id: deep-226
      description: "Near-frontier, slow, RAM-resident. Availability depends on host load."

  # ---- retrieval and internal models -------------------------------------
  - model_name: team-docs                     # M5; the RAG service is a model
    litellm_params:
      model: openai/team-docs
      api_base: http://rag:8001/v1
      api_key: os.environ/RAG_SERVICE_KEY
      timeout: 300
    model_info:
      mode: chat

  - model_name: embed
    litellm_params:
      model: openai/Qwen3-Embedding-0.6B
      api_base: http://infinity:7997
      api_key: os.environ/INFINITY_KEY
    model_info:
      mode: embedding                         # health checks must not send chat here

  # ---- pinned names for evaluation and bug reports -----------------------
  - model_name: pinned-coder-30b
    litellm_params:
      model: openai/coder
      api_base: http://10.0.0.226:8000/v1
      api_key: os.environ/VLLM_226_KEY
    model_info:
      mode: chat
      description: "Exact deployment. Use for evals; do not use for daily work."

router_settings:
  routing_strategy: usage-based-routing-v2     # prefers the least-loaded healthy deployment
  num_retries: 2
  retry_after: 5
  allowed_fails: 2                             # failures before a deployment is cooled down
  cooldown_time: 60                            # seconds a failed deployment is skipped
  enable_pre_call_checks: true                 # skips deployments whose context window is too small

  fallbacks:
    - coder: ["chat", "chat-small"]
    - chat: ["chat-small"]
    - deep-slow: []                            # no silent downgrade. See section 7

  context_window_fallbacks:
    - coder: ["deep-slow"]                     # only if deep-slow is currently available

litellm_settings:
  drop_params: true              # clients send params our servers do not implement; drop, do not 400
  request_timeout: 600
  telemetry: false               # no phone-home. N1
  turn_off_message_logging: true # spend logs record tokens and cost, never prompt or response text
  json_logs: true

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  store_model_in_db: true        # required if the fleet controller edits the catalog at runtime
  background_health_checks: true
  health_check_interval: 60
  alerting: ["webhook"]          # points at the fleet controller; no external services
```

### 6. Routing and fallback — what should happen when a host is claimed

This is the gateway's half of the sharing policy. The fleet controller decides *what* is available;
the gateway decides *where a request goes* given that.

| Situation | Desired behaviour | Mechanism |
|---|---|---|
| `.226` free, everything healthy | `chat` and `coder` served from `.226` | Normal routing; `.226` deployments are healthy and preferred |
| `.226` claimed, ladder drops to 8B | `chat` and `coder` still answer, from the 8B rung | Nothing changes at the gateway — vLLM re-serves under the same `--served-model-name`. The *rung label* changes in the dashboard |
| `.226` fully asleep (< 4 GB free) | `chat` -> `.87`; `coder` -> `chat` -> `chat-small` | `fallbacks`, plus the controller marking `.226` deployments unhealthy |
| `.226` unreachable (reboot, WSL down) | Same as above, automatically | `allowed_fails` + `cooldown_time` |
| Deep tier gated off | `deep-slow` returns a clear, human error | Empty fallback list; see §7 |
| Request exceeds the loaded rung's context | Route to a deployment with room, or fail cleanly | `enable_pre_call_checks`, `context_window_fallbacks` |

**Two deliberate non-behaviours.**

*No silent downgrade for `deep-slow`.* A user who chose the slow, careful model and receives a fast
4B answer with no notice has been lied to. Failing with "the deep tier is unavailable because `.226`
is running a modelling job — try again after 18:00" is better product design than a quiet fallback.

*Fallbacks apply to failures, not to slowness.* Do not add aggressive client-side timeouts hoping to
race hosts. A `coder` request that is merely slow because the ladder dropped a rung should finish,
not get retried against a second host and double the GPU cost.

### 7. Health checks, and how the fleet controller updates the catalog

LiteLLM ships three surfaces (**verify the exact paths against your pinned version**):

| Endpoint | Purpose | Use |
|---|---|---|
| `/health/liveliness` | Is the proxy process up? | Container healthcheck, Caddy |
| `/health/readiness` | Is it up and connected to its DB? | Deploy gate |
| `/health` | Actively calls each deployment | Fleet dashboard; **admin-authenticated**, and it costs real inference |

`background_health_checks: true` with `health_check_interval: 60` makes `/health` serve cached
results rather than hammering the backends. Set `mode:` correctly per model in `model_info` — a chat
health probe sent to the embeddings server produces a permanently "unhealthy" model that is perfectly
fine.

**The open question this doc cannot answer from a desk:** a vLLM instance in sleep mode still has an
HTTP server listening. Whether its `/health` reports healthy while the weights are parked in RAM
decides how much work the fleet controller has to do. **Measure this during M0 spike 6 and record the
answer here.** If a sleeping vLLM looks healthy, passive health checks are not enough and the
controller must tell the gateway explicitly.

Three ways for the controller to do that, in increasing order of coupling:

| Approach | How | Verdict |
|---|---|---|
| **Passive only** | Controller does nothing; the gateway discovers failures and cools deployments down | Simplest, and enough if a sleeping vLLM actually fails health checks. Costs one failed user request per transition |
| **Admin API** (preferred) | Controller calls LiteLLM's model-management endpoints (`/model/new`, `/model/delete`, or the update route — **names have changed between versions, verify**) with the master key, adding and removing deployments as rungs change | No restarts, no failed requests, and the catalog reflects reality. Requires `store_model_in_db: true` |
| **Config rewrite** | Controller writes `litellm.config.yaml` and restarts the container | Avoid. A restart drops in-flight streams, and a config bug takes the whole gateway down |

Whichever is chosen, the controller stays the **only** writer. Nobody edits the catalog through the
LiteLLM UI on a live system, because the next deploy would silently revert it.

### 8. Keys and auth

Two layers, and they are for different things.

**Layer 1 — gateway to backends.** vLLM and Infinity each start with `--api-key`, so a stray process
on the LAN cannot drive the GPUs. These keys live in each host's `.env` and are passed to LiteLLM as
`VLLM_226_KEY` and friends. Low-stakes but not optional: without them, the firewall is the only thing
standing between the 4090 and anyone on the subnet.

**Layer 2 — clients to gateway.** The master key (`LITELLM_MASTER_KEY`, `sk-...`) is an admin
credential. It is never given to a client. Instead, generate one **virtual key per surface**:

```bash
curl -s http://10.0.0.87:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "key_alias": "open-webui",
        "models": ["chat", "coder", "chat-small", "deep-slow", "team-docs"],
        "rpm_limit": 240,
        "metadata": {"surface": "open-webui", "owner": "platform"}
      }'
```

| Key alias | Given to | Models | Why separate |
|---|---|---|---|
| `open-webui` | Open WebUI on `.87` | All user-facing | Revoking it logs out the chat UI only |
| `opencode` | Terminal agent config on each dev machine | `coder`, `chat`, `deep-slow` | Per-surface usage is visible; can be revoked without touching chat |
| `cline` | VS Code extension | `coder`, `chat` | Cline's context appetite (see [`tech-stack.md`](./tech-stack.md) §4) makes its own budget useful |
| `rag-service` | Our RAG service | `chat`, `deep-slow`, `embed` | Internal service, no user models |
| `mcp-tools` | Our MCP server | `chat-small` | Least privilege for the tool layer |
| `eval` | Eval harness | `pinned-*` | Keeps benchmark traffic out of everyday spend numbers |

Per-person keys are possible and we are not doing them in M1: Open WebUI already owns per-person
accounts and history, so per-person gateway keys would duplicate that with a second thing to
provision. Revisit if per-person quotas ever become necessary.

**Two privacy settings that are policy, not preference.** `telemetry: false` stops the proxy phoning
home, and `turn_off_message_logging: true` keeps prompt and response text out of the spend logs. The
spend table then holds token counts and model names, which is what we want for capacity planning and
is the only thing we want persisted about a colleague's conversation with a work tool. Verify both
behave as documented for your version — this is exactly the kind of default that changes.

### 9. Pointing the clients at it

```
Open WebUI   OPENAI_API_BASE_URL=http://litellm:4000/v1     (container network)
             OPENAI_API_KEY=<open-webui virtual key>

OpenCode     base URL https://api.ai.lan/v1  or  http://10.0.0.87:4000/v1
Cline        OpenAI Compatible provider, same base URL, its own key
curl         curl -s https://api.ai.lan/v1/models -H "Authorization: Bearer <key>"
```

**Verify (the M1 acceptance test):**

```bash
# 1. Catalog visible with a virtual key
curl -s http://10.0.0.87:4000/v1/models -H "Authorization: Bearer $OPENCODE_KEY" | jq '.data[].id'

# 2. Streaming works end to end, and first token is under 2 s (N3)
time curl -s http://10.0.0.87:4000/v1/chat/completions \
  -H "Authorization: Bearer $OPENCODE_KEY" -H "Content-Type: application/json" \
  -d '{"model":"chat","stream":true,"messages":[{"role":"user","content":"one sentence on hashing"}]}'

# 3. Fallback: stop vLLM on .226, repeat (2), confirm it is answered by .87 and not an error
# 4. A key restricted to `chat-small` is refused `coder` with 401/403, not silently served
```

---

## Reflect

**The catalog is the platform's UI.** Almost everything else in this document is mechanical; the
naming is the part that decides whether people pick the right model for the job or default to
whatever is at the top of the list. `chat` / `coder` / `deep-slow` / `team-docs` is a small enough set
to hold in your head, and each name is a promise about waiting time rather than a spec sheet.

**The uncomfortable coupling is between the controller and the gateway.** Ideally the gateway would
discover backend state passively and the controller would only manage GPUs. In practice a sleeping
vLLM may still look healthy, so the controller probably has to tell the gateway what is real. That is
one API call and one master key of coupling — acceptable, but it is the seam to keep thin, and if it
starts carrying more than "this deployment is up / down", something has gone wrong in the design.

**What we would revisit:** LiteLLM is another moving part with a fast release cadence and a
config schema that shifts. If it turns out to be flaky, the alternative is not "a different gateway"
but "no gateway" — clients configured with several base URLs and no fallback — which costs the whole
of §6. Pin the version, upgrade deliberately, and read the changelog for catalog and health-check
changes before every bump.

**Next:** [`07-inference-servers.md`](./07-inference-servers.md).
