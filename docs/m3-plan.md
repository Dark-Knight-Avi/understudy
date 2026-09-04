# M3 Plan — coding agents (pre-implementation)

> **This is a plan, not a record.** The convention here is that numbered docs
> describe what shipped — so [`09-coding-agents.md`](./09-coding-agents.md), the
> pre-build *design* draft for this milestone, gets rewritten as a
> what-actually-shipped document when M3 lands, and an `m3-runbook.md` records
> the bring-up as it really went. This file is the delivery mechanics in
> between: exact steps, exact commands, decided defaults. Where it disagrees
> with reality on the day, reality wins and this file gets corrected.
>
> **Goal:** a developer completes a real task end-to-end in OpenCode (terminal)
> and in Cline (VS Code), on local models, with source code never leaving the
> network ([F3, F4, N1](./00-goals-and-constraints.md)).
>
> **Prerequisites:** M1 and M2's gate complete (they are). Read
> [`m2-runbook.md`](./m2-runbook.md) first — the catalog mechanism decided
> there shapes every routing decision below.
>
> **Addresses below are the repo's placeholders** (`10.0.0.x`). Substitute the
> real ones from `deploy/fleet.local.yaml` and each host's `.env`. Never commit
> a real address or key.

---

## 1. What M3 delivers, and its acceptance test

M3 adds **no services**. Both clients speak OpenAI-compatible HTTP at the
LiteLLM gateway on `.87:4000`, which has existed since M1. The work is:

1. Gateway-side: the `coder` model group goes live (weights, tool-call
   parsing, per-person keys).
2. Client-side: OpenCode and Cline configured on colleagues' own machines,
   pointed **only** at the gateway, telemetry off.
3. Context tuning: the part [`09`](./09-coding-agents.md) §5 calls "the
   section that decides M3" — measured, not guessed.

**Acceptance (from [`delivery-plan.md`](./delivery-plan.md) §6):** a real task
completed end-to-end in both clients. Concretely, per `09` §10.1: a genuine
small change to this repo — touches 2–3 files, requires reading existing code,
has an objective pass (`make test` green), small enough that failure is
diagnosable. Candidate: *add a per-host `last_sample_age_s` field to the fleet
controller's `/fleet/status` response, with a test.* Run it in both clients,
fill in the §10.2 table from `09`, and repeat one run with `.226`'s toggle
flipped mid-task to confirm §7's behaviour below.

The bar is not parity with Claude Code. It is: *a developer chooses to use it
for a real task, twice, without being asked to* (`09` §10.3).

---

## 2. The model alias: a dedicated `coder` group, not `chat`

**Decision: coding clients use the public name `coder`. They must not be
pointed at `chat`.** Reasoning from the M2 catalog design:

- **`chat` is engineered to degrade silently, and that is exactly wrong for an
  agent.** M2 made `chat` one model group with two deployments —
  `chat-226` (order 1, the big rung) and `chat-small-87` (order 2, the Qwen3-4B
  standby). When `.226` is claimed the controller deletes `chat-226` and the
  group keeps answering from the 4B. For chat that is the right promise: an
  answer, always. For an agentic client it is a trap — a 4B silently
  substituted mid-session loses the tool-call format, describes edits instead
  of making them, and produces plausible diffs with wrong logic, and the client
  has no way to notice ([`09`](./09-coding-agents.md) §6.2). Coding must fail
  loudly; chat must not. Two promises, two names.

- **The M2 mechanism gives fail-loud for free.** The catalog is
  controller-owned (`model_list: []` in
  [`deploy/host-87/litellm.config.yaml`](../deploy/host-87/litellm.config.yaml);
  the controller registers deployments via `/model/new` and `/model/delete`).
  `Rung.public_name`/`Rung.order` in `fleet_controller.models` already support
  this: the `.226` `coder` rung advertises deployment `coder-226` under public
  name `coder`, and **no other host carries that public name**. When `.226`
  yields, the controller deletes `coder-226`, the group is empty, and LiteLLM
  answers HTTP 400 "model not found" — measured at M2, before any fallback
  could run. That 400 *is* the designed behaviour: the session stops instead
  of silently getting worse. No new controller capability is needed.

- **Do not add LiteLLM `fallbacks` for `coder`.** M2 proved fallbacks act only
  within a group that still exists, so they cannot fire here anyway — and we
  would not want them to. The empty-group 400 is the feature.

- **Per-surface budgets and attribution.** A dedicated alias means keys scoped
  to `coder`, so Cline's context appetite (the live M3 risk in
  [`delivery-plan.md`](./delivery-plan.md) §11) is measurable per client and
  per person from the gateway's spend table, without mixing in chat traffic.

One consequence to state plainly: **`coder` exists in the catalog only while
the coder rung is actually loaded on `.226`.** With the ladder unbuilt (M2's
honest gap), the host serves one model; while it serves the 14B under the
`chat` rung, `coder` is absent and clients get the 400. The catalog tells the
truth about the fleet — that is the design, not a bug.

*Open item, deferred:* `09` §3.2 wants a `chore`/`small_model` alias (the
always-on 4B on `.87`) so OpenCode's housekeeping calls never touch the 4090.
A rung advertises exactly one public name, and `.87`'s 4B already advertises
`chat` (as the standby). For M3, set OpenCode's `small_model` to `chat` —
usually the 14B, occasionally the 4B, both fine for titles and summaries. A
dedicated always-on alias needs a second registration mechanism; decide at M4
if the 4090 shows measurable housekeeping load.

---

## 3. Gateway-side prep

### Step 0 — start the 30B download (`.226`) — do this first

~16–17 GB of AWQ weights. This is the parallel-track item from
[`delivery-plan.md`](./delivery-plan.md) §7; it runs while everything else
proceeds.

`▸ .226 — Terminal 1`

```bash
cd ~/understudy && git pull
# Confirm the repo id first -- quantised repos get renamed (m1-runbook step 0):
uv run hf download Qwen/Qwen3-Coder-30B-A3B-Instruct-AWQ \
  --local-dir /srv/ai-platform/models/Qwen3-Coder-30B-A3B-Instruct-AWQ
```

**Expected:** a progress bar and, at the end, the directory populated with
`*.safetensors` plus `config.json` on the 8 TB NVMe — never `/mnt/c`.
**Send me:** `ls -la` of the directory and `df -h` of the mount, so we know it
landed on the right disk. Record the exact revision in
`deploy/host-226/.env` (`CODER_MODEL_REVISION=`).

### Step 1 — the vLLM version question (decide before touching compose)

The pinned `vllm/vllm-openai:v0.9.1` predates Qwen3-Coder's release. The model
*architecture* (Qwen3 MoE) is served fine, but Qwen3-Coder emits tool calls in
its own XML-ish format, and the matching `--tool-call-parser qwen3_coder`
arrived in later vLLM releases; the older `hermes` parser generally does not
parse it. A coding agent without working tool calls is useless (`09` §2.3), so
expect this milestone to need a **deliberate vLLM bump**:

- Pick the newest tag whose release notes list the `qwen3_coder` tool parser,
  and read the changelog for the two behaviours the sharing policy rides on:
  the `/sleep` & `/wake_up` endpoints (names and payloads have moved) and
  `--gpu-memory-utilization` semantics (fraction of *total* vs *free*).
- A bump re-opens M0 spike 6's measurements. Re-verify sleep/wake with one
  claim cycle before calling this step done.
- If the bump proves disruptive, the fallback is serving the coder without a
  parser and testing whether OpenCode/Cline can work in "text tool call" mode
  — they mostly cannot, so treat that as a blocker, not a workaround.

**Send me:** the tag you chose and the changelog lines for sleep/wake and
utilization, before deploying it.

### Step 2 — repoint `vllm-fast` at the coder and enable tool calling (`.226`)

Edits to `deploy/host-226/.env` (real file, gitignored):

```
CODER_MODEL_DIR=Qwen3-Coder-30B-A3B-Instruct-AWQ
CODER_MODEL_REVISION=<the revision from step 0>
FAST_MAX_MODEL_LEN=32768        # see §5 -- the context budget section
FAST_GPU_UTIL=<set from the arithmetic in §5, WITH the fleet.local.yaml footprint>
```

And `deploy/host-226/compose.yaml` gains, on `vllm-fast` (via the repo, not
edited in place on the host — delivery-plan §3 rule 3):

```yaml
      - --enable-auto-tool-choice
      - --tool-call-parser=qwen3_coder   # verify the name against the tag from step 1
```

Two flags to re-check while there:

- `--reasoning-parser=qwen3` was added for the 14B, a hybrid *thinking* model.
  Qwen3-Coder is non-thinking; verify against the step-1 tag whether the
  parser is harmless or must come out when this model is loaded.
- `--enable-chunked-prefill` stays **explicit**. At 32768 it sits exactly on
  the threshold below which vLLM silently turns it off, taking the KV cache
  concurrency with it (the trap in the compose file's own comments).

`▸ .226 — Terminal 1`

```bash
cd ~/understudy/deploy/host-226
docker compose --env-file .env up -d --force-recreate vllm-fast
docker compose logs -f vllm-fast
```

**Expected:** the model loads in minutes; the log prints the KV cache size it
profiled. **Send me:** the log lines around "GPU KV cache size" / "Maximum
concurrency", and this, once it settles:

`▸ .226 — Terminal 2`

```bash
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

That resident number is the coder rung's **real footprint**. Write it into
`fleet.local.yaml` (`coder` rung `footprint_gb`) in the same edit as
`FAST_GPU_UTIL` — the fleet.yaml header explains why they must move together:
a footprint smaller than the real reservation silently eats the headroom
protecting somebody's job.

### Step 3 — prove tool calling with bare curl, before any client

The rule from `09` §2.3: if a bare request with one tool definition returns a
`tool_calls` array, every later problem is the client's. If the call comes
back as prose in `content`, the parser is wrong and no client config will fix
it.

`▸ .87 — Terminal 1`

```bash
KEY=$(grep '^LITELLM_MASTER_KEY=' ~/understudy/deploy/host-87/.env | cut -d= -f2-)
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{
    "model": "coder",
    "messages": [{"role":"user","content":"What is the weather in Bengaluru? Use the tool."}],
    "tools": [{"type":"function","function":{"name":"get_weather",
      "description":"Get current weather for a city",
      "parameters":{"type":"object","properties":{"city":{"type":"string"}},
      "required":["city"]}}}]
  }' | python3 -m json.tool
```

(Read the key with `grep`, not `set -a && . ./.env` — the exported-variable
trap from the M1 runbook.)

**Expected:** `"finish_reason": "tool_calls"` and a
`message.tool_calls[0].function` with name `get_weather` and JSON arguments
naming a city. **It means the parser works end to end through the gateway.**
If `model not found`: the controller has not registered `coder-226` — step 4.
If the tool call appears as text inside `content`: wrong parser, back to
step 1. **Send me:** the full JSON either way.

### Step 4 — confirm the controller advertises `coder-226`

Nothing should need doing here — the `coder` rung already exists in
`fleet.local.yaml`, and the controller reconciles the catalog every tick. This
step verifies rather than configures. Two things can silently disagree:

- The rung's `served_model` must be **byte-identical** to one of vLLM's
  `--served-model-name` values (`coder` in compose). The controller registers
  `openai/<served_model>`; a mismatch 404s on every request while the gateway
  reports the deployment configured. M2's defect list is full of this shape.
- The rung is only advertised while it is the *selected* rung. If the card
  shows the 14B loaded, `coder` is rightly absent.

`▸ .87 — Terminal 1`

```bash
curl -s http://localhost:8090/fleet/status | python3 -m json.tool
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/v1/models \
  | python3 -c 'import json,sys; print([m["id"] for m in json.load(sys.stdin)["data"]])'
```

**Expected:** `.226` reporting `current_rung: coder`, and the model list
containing `coder` and `chat`. **Send me:** both outputs.

### Step 5 — per-person keys

M1 chose per-*surface* keys; M3 moves coding to per-person-per-surface, because
[`09`](./09-coding-agents.md) §2.2's argument holds: usage becomes
attributable (which is how §5's context measurements work per developer), and
revoking one person's key touches nobody else.

`▸ .87 — Terminal 1`, once per developer:

```bash
curl -s -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{
        "key_alias": "coding-<name>",
        "models": ["coder", "chat"],
        "metadata": {"surface": "coding", "owner": "<name>"}
      }' | python3 -m json.tool
```

**Expected:** a JSON body containing `"key": "sk-..."`. Hand it to the
developer over a channel that is not this repo and not a group chat; they put
it in an environment variable (§4). **Verify the scope** — a coding key asked
for a model outside its list must get 401/403, not an answer:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer <the new key>" -H 'content-type: application/json' \
  -d '{"model":"team-docs","messages":[{"role":"user","content":"x"}]}'
```

**Expected:** `401` or `403`. **Send me:** the status code.

---

## 4. Client-side setup — on each colleague's own machine

The clients run on laptops and desktops we do not manage. Everything below is
done by the developer at their own machine, one verifiable step at a time.
Templates live in [`deploy/clients/`](../deploy/clients/) — placeholders in,
real values from the operator, never committed anywhere.

### 4.1 OpenCode (terminal)

Config format verified 2026-09-04 against https://opencode.ai/docs (config +
providers pages): global config at `~/.config/opencode/opencode.json`,
project `opencode.json` overrides it; custom providers use the
`@ai-sdk/openai-compatible` package with `options.baseURL` / `options.apiKey`;
`{env:VAR}` and `{file:path}` substitution are supported; `autoupdate` and
`share` are real keys. OpenCode ships releases near-daily — **re-verify the
schema against the version you pin on the day.**

`▸ colleague's machine — Terminal 1`

```bash
npm install -g opencode-ai@<the version we pin>   # pin; do not track latest
opencode --version
```

**Expected:** the exact pinned version echoed back. Record it in the M3
runbook when it is written; everyone installs that version.

```bash
mkdir -p ~/.config/opencode
cp <repo>/deploy/clients/opencode.json ~/.config/opencode/opencode.json
$EDITOR ~/.config/opencode/opencode.json    # real gateway address in baseURL
export UNDERSTUDY_KEY=sk-...                # the key from step 5 -- put it in
                                            # your shell profile, not the file
```

Then, from a small test directory (not a real repo yet):

```bash
opencode
# in the TUI: /models
```

**Expected:** the `understudy` provider listed with `coder` and `chat`; pick
`coder`, ask it to run `ls` and summarise. A reply that used the tool means
the whole spine works from this machine. **Send me:** a screenshot or paste of
the `/models` list and the first reply.

**If the model list is empty:** the key or the address. `curl -s
http://10.0.0.87:4000/v1/models -H "Authorization: Bearer $UNDERSTUDY_KEY"`
from the same machine distinguishes them — a JSON list means OpenCode config,
an auth error means the key, a timeout means routing/firewall.

### 4.2 Cline (VS Code)

Settings format verified 2026-09-04 against https://docs.cline.bot
(openai-compatible provider + telemetry pages). Cline stores provider settings
in VS Code's internal storage and secret store — **there is no committable
settings file for the provider**, so
[`deploy/clients/cline.settings.jsonc`](../deploy/clients/cline.settings.jsonc)
is a transcription sheet: open Cline's settings panel (⚙) and enter the values
by hand.

`▸ colleague's machine — VS Code`

1. Install the Cline extension from the marketplace. Pin: note the installed
   version and disable auto-update for the extension
   (right-click the extension → Auto Update off) so the team stays on one
   behaviour.
2. Cline settings (⚙):
   - **API Provider:** `OpenAI Compatible` — *not* Cline accounts, not
     OpenRouter, not any hosted entry. Those route off-network (N1).
   - **Base URL:** `http://10.0.0.87:4000/v1` (real address; include `/v1`)
   - **API Key:** the personal key from step 5 (lands in VS Code secret
     storage — fine)
   - **Model:** `coder`
   - Model Configuration → **Context Window:** `24576`, **Max Output:** `4096`
     (see §5), image/computer-use support **off**
   - **Cline Telemetry:** off (§8)
3. VS Code `settings.json`: `"telemetry.telemetryLevel": "off"` — Cline's
   docs state VS Code-level telemetry off disables Cline's as well; set both
   anyway.
4. Copy `deploy/clients/clineignore.example` to the target repo root as
   `.clineignore`.

**Verify — the same trivial task as OpenCode:** open a small repo, Plan mode,
ask "what files are in this project and what does the Makefile do?".
**Expected:** an answer that used file reads, and — this is the real check —
the gateway saw it. `▸ .87 — Terminal 1`:

```bash
docker compose logs --since 5m litellm | grep -o '"model": *"[^"]*"' | sort | uniq -c
```

**Send me:** that output plus Cline's own token count for the task. The
`prompt_tokens` the gateway logged for the first trivial turn is Cline's
**fixed overhead** — write it into the §5 measurement table before doing
anything else. (Known Cline issue, verified against their tracker: the custom
context-window setting for OpenAI-Compatible providers has failed to
propagate in some versions — issues #2073/#6494. The gateway's
`prompt_tokens` is the ground truth for whether the cap is honoured; if it is
not, that alone is a reason to move to Roo Code, §6.)

---

## 5. The context budget — numbers from this repo, then caps

### What we actually have

M1 measured, on `.226` at `FAST_GPU_UTIL=0.62` (14.88 GiB reserved), 14B,
fp8 KV, chunked prefill explicit: **KV cache 5.15 GiB → 4.12× concurrency at
16,384 tokens** (≈67.5k KV tokens total; fleet.yaml header, delivery-plan §6).

That 16k window is the M1 *chat* setting, and it is disqualifying for agentic
clients as-is: Cline's fixed overhead — system prompt, tool schemas,
environment details — is commonly reported north of 10k tokens, before one
line of *your* code enters the window. On 16k, Cline could burn the entire
window doing nothing, which is precisely the risk register entry.

### The coder rung's budget (hypothesis — measure at step 2)

Qwen3-Coder-30B-A3B, fp8 KV, from its published shape (48 layers, 4 KV heads,
head_dim 128 — **read the real values from the model's `config.json` at
step 0**, per `09` §5.4):

```
KV/token = 2 × 48 × 4 × 128 × 1 B  =  48 KiB/token   →   1 GiB ≈ 21,800 tokens
```

The catch: the rung's budgeted 17.0 GB footprint was a **weights-only
estimate**, and the AWQ weights alone are ~16 GiB. Serving 32k contexts with
any concurrency needs roughly:

```
weights ~16  +  KV 3 (≈65k tokens ≈ 2×32k sessions)  +  CUDA context 1.49
   →  a real footprint nearer 20 GB, i.e. FAST_GPU_UTIL ≈ 0.78
```

Consequences to accept, not hide:

- At a ~20 GB footprint the coder rung fits the **Free** state (headroom
  1 GB) and effectively never fits **Sharing** (20 + 3 > 24). The coder is an
  empty-card model; when anyone is on `.226`, coding drops out rather than
  down. That matches docs/03's band table (`>= 20 GB free → coder`) and §7
  below.
- **The window is a fleet budget, not a personal one** (N4 wants 2–4
  concurrent streams). One Cline session at 60k would occupy the whole KV
  pool by itself. Hence server 32k and client caps below it.
- Whatever step 2's log prints for "GPU KV cache size" replaces this
  arithmetic. Update `fleet.local.yaml` and this plan's numbers from it.

### Client-side caps (the concrete settings)

| Setting | Value | Where |
|---|---|---|
| Server `--max-model-len` | 32768 | `FAST_MAX_MODEL_LEN`, `.226` `.env` |
| OpenCode `limit.context` / `limit.output` for `coder` | 24576 / 4096 | `deploy/clients/opencode.json` |
| OpenCode `limit.context` / `limit.output` for `chat` | 12288 / 4096 | same (chat's server window is 16k) |
| Cline Context Window / Max Output | 24576 / 4096 | Cline settings panel |

24576 + 4096 sits well under 32768, so the client compacts or warns *before*
the server rejects the request — the failure `09` §5.4 warns about, where the
client believes in a bigger window than the server has and the session dies
mid-task with the work in flight lost.

### The Cline-burns-the-window risk, worked

Mitigations, in the order to apply them (each is a lever from `09` §5.3):

1. **Measure first.** The fixed-overhead number from §4.2 goes in the table
   below. An empty table is the deliverable of M3's first morning.
2. **`.clineignore`** from the template — `node_modules`, `.venv`, weights,
   lockfiles, build output, **and `.env`/secrets** (it is a privacy control
   too).
3. **Open the narrowest workspace** — `services/fleet-controller/`, not the
   repo root.
4. **Disable unused capabilities** — browser/computer-use off in settings;
   no MCP servers at all until M6 (every tool schema rides on every request).
5. **Cap output at 4096** — output competes with input for the same KV.
6. **Short tasks, fresh starts** — one task = one bounded objective; a
   two-sentence handover into a new task beats continuing at 80% occupancy.
7. Treat automatic context condensing as a safety net, not a plan — the
   condensing call itself reads the whole window.

| Client | Config | Fixed overhead (trivial turn) | After 5 turns | Notes |
|---|---|---|---|---|
| Cline | default, no MCP | | | |
| Cline | ignore + tools trimmed | | | |
| OpenCode | default | | | |
| Aider | `--map-tokens 512` | | | |

### When to fall back — the decision rules

- **→ Roo Code** when Cline's *fixed overhead exceeds ~⅓ of the 24k client
  window after trimming*, or the context-window cap is not honoured (§4.2's
  known issue), or condensing thrashes. Roo is a Cline fork wired identically
  (same provider type, same base URL, same `coder`, same key) whose per-mode
  system-prompt override is the only clean way to *replace* a frontier-shaped
  prompt rather than work around it (`09` §5.5).
- **→ Aider** when the files to change are already known, the change is
  mechanical, or **only a small rung is available** — its diff-based flow and
  `--map-tokens 512` repo map make it dramatically cheaper per turn, and
  diffs are the output format small local models get right most often
  (`09` §5.6). Install it alongside from day one; it is not a consolation
  prize:

  ```bash
  export OPENAI_API_BASE=http://10.0.0.87:4000/v1
  export OPENAI_API_KEY="$UNDERSTUDY_KEY"
  aider --model openai/coder --edit-format diff --map-tokens 512 --no-auto-commits
  ```

---

## 6. How M3 interacts with the sharing policy

The scenario: a developer is mid-session in Cline, and the person at `.226`
flips *"I'm using this GPU"* (or a modelling run appears).

What mechanically happens, in M2's measured order: the controller pulls
`coder-226` out of the catalog **first**, then sleeps the engine (~12 s to a
free card). An in-flight generation is lost at sleep time — the client sees
one aborted/failed turn. Every subsequent `coder` request gets HTTP 400
"model not found", because the group is empty (§2).

**The session pauses. It does not fall back — and this is a decision, not a
gap.** The task brief asks what fallback to the 4B standby on `.87` would look
like for a coding agent, so, concretely: the 4B is a hybrid *thinking* model
that would start emitting `<think>` blocks mid-session, its tool-call
formatting degrades to prose ("I would now edit the file to…"), and its edits
are plausible-looking with subtly wrong logic — inside an agentic loop that
executes what the model emits, unreviewed. The developer's signal that
anything changed would be *the agent quietly getting stupider*, which
[`09`](./09-coding-agents.md) §6.2 identifies as the trust-destroying failure
mode. A paused session with a legible reason beats a continuing session that
cannot be trusted. That is why coding gets a dedicated single-deployment group
while `chat` keeps its standby: chat degrades, coding stops.

What the developer experiences and should do:

- Cline / OpenCode surfaces an API error on the failed turn. Conversation
  state is local to the client; **nothing already done is lost.** Wait, or
  switch that narrow change to Aider by hand, or do it by hand. The rung and
  the claim holder are on the fleet dashboard (`.87:8090`); check it before
  starting long sessions.
- After release, reclaim takes ~5 minutes (the deliberate clear window) plus
  a ~1.4 s wake. Retry the same turn; the session resumes where it stopped.
  vLLM's prefix cache is warm again after the first turn.
- The raw 400 body is functional but unfriendly. An M3 nice-to-have (not a
  gate): a LiteLLM pre-call hook or error rewrite so the message says the real
  thing — *"coder is unloaded: .226's GPU is claimed by <who>, ~<n> min.
  Wait, or use Aider against chat for narrow edits."* If it costs more than an
  hour, defer it and rely on the dashboard.

And the etiquette in the other direction, which is the whole project: a
paused coding agent costs a developer minutes; the platform squeezing a
modelling run costs a colleague a day. The trade is correct
([`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §5).

---

## 7. N1 — what each client wants to send out, and how it's stopped

Coding clients are the highest-risk N1 surface in the platform: their job is
to read source code and POST it somewhere. The config templates enforce, and
the M8 egress capture must later verify:

| Client | Default behaviour to neutralise | Setting |
|---|---|---|
| OpenCode | **Session sharing** uploads conversation content — i.e. our code — to opencode.ai's share service when used | `"share": "disabled"` in `opencode.json` (verified against current docs: `manual`/`auto`/`disabled`) |
| OpenCode | Auto-update fetches releases; also pins drift | `"autoupdate": false`; install a pinned version |
| OpenCode | May fetch its provider/model catalog (models.dev) at startup — metadata, not code | Verify on the pinned version with a capture; record as an exception or block it |
| Cline | **Telemetry on by default** (PostHog: feature usage, task metrics, error events — their docs say never code/paths/conversations, but the defensible position is zero) | Cline settings → Cline Telemetry **off**, plus VS Code `"telemetry.telemetryLevel": "off"` |
| Cline | Built-in routing to hosted providers and Cline accounts | Provider = OpenAI Compatible **only**, base URL = the gateway; never sign into a Cline account |
| Both | Reading `.env`/secrets into a prompt (the prompt then lives in client-side history) | `.clineignore` / narrow workspaces exclude secrets paths |
| Both | Marketplace / npm update traffic from the developer's machine | Outside the model path; **record it in the M8 egress review as a known exception** rather than a surprise finding |

The structural backstop: the only credential a client holds is a LiteLLM
virtual key, useless outside the LAN, and the only base URL in any config is
`.87`. A client cannot leak through the platform; these settings are about the
client's *own* side channels.

---

## 8. Definition of done for M3

1. Tool-call curl (step 3) returns a structured `tool_calls` array through
   the gateway.
2. The acceptance task (§1) completed end-to-end in OpenCode **and** Cline,
   by a developer other than the operator, with the `09` §10.2 table filled
   in.
3. One run repeated with `.226` claimed mid-task: the client shows a legible
   failure, nothing silently degrades, and the session resumes after release.
4. The §5 measurement table has real numbers, and `fleet.local.yaml`'s coder
   footprint matches the measured resident figure and `FAST_GPU_UTIL`.
5. Both clients' telemetry/sharing switched off per §7, checked on each
   machine.
6. Then: rewrite [`09-coding-agents.md`](./09-coding-agents.md) to describe
   what shipped, write `m3-runbook.md`, and flip the delivery-plan §6 row.
