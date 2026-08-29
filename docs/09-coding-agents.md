# 09 — Coding Agents (M3)

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> Agentic coding in the terminal and in VS Code, on local models only. The clients are off the shelf —
> we configure them, we do not build them. Read
> [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) first: the model under these clients can
> disappear mid-session, and every decision here is shaped by that.

---

## 1. Concept — what M3 actually delivers

M3 satisfies **F3** (agentic coding in the terminal) and **F4** (the same in VS Code) from
[`00-goals-and-constraints.md`](./00-goals-and-constraints.md). It adds no new services. Both clients
speak OpenAI-compatible HTTP, both point at the LiteLLM gateway on `.87`, and everything they can do
is already running after M1 and M2.

So the engineering in this milestone is not integration. It is **making a frontier-shaped client
behave sensibly against a non-frontier model with a small, contended context window.** That is §5, and
it is the section that decides whether M3 succeeds. The install steps either side of it are an
afternoon's work.

```
  VS Code                                     Terminal
  +---------------------------+               +---------------------------+
  | Cline  (or Roo Code)      |               | OpenCode                  |
  |   system prompt           |               |   agents: build / plan    |
  |   tool definitions        |               |   + subagents             |
  |   environment details     |               |   tool definitions        |
  |   file context            |               |   session history         |
  +-------------+-------------+               +-------------+-------------+
                |          OpenAI-compatible /v1/chat/completions        |
                +---------------------------+---------------------------+
                                            v
                        LiteLLM gateway   10.0.0.87:4000
                        catalog: coder | coder-14b | coder-8b | deep | chore
                                            |
                    +-----------------------+-----------------------+
                    v                                               v
        vLLM  .226:8000                                  vLLM  .87:8000
        Qwen3-Coder-30B-A3B Int4  ~17 GB                 Qwen3-4B Int4  ~3 GB
        ladder: 14B -> 8B -> 4B -> off                   always on, never on the ladder
                    ^
                    | rung selected from measured free VRAM
            Fleet controller (.87)  <---- "I'm using this GPU"

        MCP (arrives in M6, .87):
            search_documents | web_search | generate_pdf | generate_pptx | generate_image
            attached to both clients — expose the fewest that do the job (§8)
```

### The honest framing to give the team on day one

This will feel **clearly weaker than Claude Code.** Not marginally. It is good at localised work —
one file, a well-scoped function, a test, a refactor you can describe precisely — and it is poor at
the thing Claude Code is best at: holding a large unfamiliar codebase in its head and reasoning
across it for twenty minutes.

Say that up front. The value proposition is not "as good as"; it is *"good enough for a large share
of the work, on code that is not allowed to leave the building, at zero recurring cost."* People who
are told that use it happily. People who are sold parity uninstall it in a week. §10 has the
comparison in a table so nobody has to take it on faith.

---

## 2. Build — gateway prerequisites

Before touching a client, three things must be true on the gateway. Getting these wrong produces
failures that look like client bugs and waste a day.

### 2.1 Catalog names are stable and describe the rung

Coding clients pin a model name in a config file. That name must not silently change meaning. Give
every rung its own name, and give the always-on small model on `.87` a name too.

```yaml
# deploy/host-87/litellm/config.yaml  (excerpt — see 06-model-gateway.md for the whole file)
model_list:
  - model_name: coder                       # the only name a coding client should ask for
    litellm_params:
      model: openai/Qwen3-Coder-30B-A3B-Instruct-AWQ   # 'hosted_vllm/...' also works; verify
      api_base: http://10.0.0.226:8000/v1            # against your LiteLLM version
      api_key: os.environ/VLLM_226_KEY
    model_info:
      max_input_tokens: 65536               # keep in step with vLLM's --max-model-len (§5.4)

  - model_name: coder-14b                   # ladder rung 2 — named, not hidden behind a fallback
    litellm_params:
      model: openai/Qwen3-14B-AWQ
      api_base: http://10.0.0.226:8000/v1
      api_key: os.environ/VLLM_226_KEY

  - model_name: chore                       # .87, always on, never on the .226 ladder
    litellm_params:
      model: openai/Qwen3-4B-AWQ
      api_base: http://10.0.0.87:8000/v1
      api_key: os.environ/VLLM_87_KEY
```

**Do not configure an automatic fallback from `coder` to a lower rung.** For chat, silent fallback is
a kindness. For an agentic coding session it is a trap — see §6.2. Coding should fail loudly.

### 2.2 A per-developer virtual key

Issue one LiteLLM virtual key per person. It makes usage attributable, which is how §5's measurements
become possible, and it means revoking one person's access does not rotate everyone's.

Keys live in the developer's environment or the editor's secret storage — **never in a file that goes
in a repo.** `opencode.json` is committed; the key goes in `$LITELLM_KEY` and is referenced by
substitution (§3.2).

### 2.3 Tool calling is actually enabled on the server

A coding agent that cannot emit a structured tool call is useless. vLLM needs to be started with
automatic tool-choice and a parser matching the model family:

```bash
# on .226 — flags are version-sensitive; verify against your vLLM version
vllm serve /models/Qwen3-Coder-30B-A3B-Instruct-AWQ \
  --served-model-name Qwen3-Coder-30B-A3B-Instruct-AWQ \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \      # recent vLLM ships a Qwen3-Coder parser; 'hermes' is the
  --max-model-len 65536 \               # older general fallback — check which yours has
  --kv-cache-dtype fp8
```

Verify with a bare `curl` carrying one tool definition **before** installing any client. If the model
returns a `tool_calls` array, the client's problems are the client's. If it returns a tool call as
prose inside `content`, the parser is wrong and no client configuration will fix it.

---

## 3. Build — OpenCode in the terminal

[OpenCode](https://opencode.ai) is the closest open equivalent to Claude Code: a TUI agent with a
plan/build split, subagents, permissions, and MCP support. MIT licensed, no account required, and it
will talk to any OpenAI-compatible endpoint.

### 3.1 Install, and pin the version

```bash
curl -fsSL https://opencode.ai/install | bash     # or: npm i -g opencode-ai@<version>
opencode --version
```

**Pin it.** OpenCode ships releases extremely fast — often several a week. That is good for the
project and bad for a team standard: config keys move, defaults change, and a colleague who installed
yesterday can have different behaviour from one who installed last month. Record the exact version in
the repo, install that version explicitly, and disable auto-update:

```json
{ "autoupdate": false }
```

Upgrade deliberately, one person first, and re-run the §9 acceptance task before telling everyone
else to move. Treat an OpenCode upgrade like a model change, not like a `brew upgrade`.

### 3.2 Global config — the provider

Global config lives at `~/.config/opencode/opencode.json` (verify the path against your version; it
has moved historically). Point it at the gateway:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "provider": {
    "fleet": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local fleet (LiteLLM on .87)",
      "options": {
        "baseURL": "http://10.0.0.87:4000/v1",
        "apiKey": "{env:LITELLM_KEY}"
      },
      "models": {
        "coder":     { "name": "Qwen3-Coder-30B  (rung 1 - full capability)" },
        "coder-14b": { "name": "Qwen3-14B        (rung 2 - degraded)" },
        "coder-8b":  { "name": "Qwen3-8B         (rung 3 - poor at coding)" },
        "deep":      { "name": "Qwen3-235B       (deep tier - minutes per turn)" },
        "chore":     { "name": "Qwen3-4B on .87  (always on)" }
      }
    }
  },
  "model": "fleet/coder",
  "small_model": "fleet/chore"
}
```

Three things worth understanding rather than copying:

- **`{env:LITELLM_KEY}`** keeps the credential out of the file. Verify the substitution syntax against
  your version; OpenCode has supported `{env:...}` and `{file:...}` forms.
- **The display names carry the rung.** The model picker is the cheapest place to make degradation
  visible (§6.2), and it costs nothing.
- **`small_model` points at `.87`, not `.226`.** OpenCode uses a cheap model for chores — session
  titles, summarisation. Sending those to the 4090 wastes the scarce resource and adds latency to
  every session. `.87`'s 4B is always on and never on the ladder, which makes it exactly right for
  this.

### 3.3 Per-project config

A committed `opencode.json` at the repo root overrides the global one. Keep it small and about *this
project*, not about credentials:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "fleet/coder",
  "instructions": ["AGENTS.md", "docs/tech-stack.md"],
  "permission": {
    "edit": "ask",
    "bash": {
      "*": "ask",
      "git status": "allow",
      "git diff *": "allow",
      "uv run pytest*": "allow"
    }
  }
}
```

Project instructions go in **`AGENTS.md`** at the repo root — the same file several other agents read,
so it is not OpenCode-specific work. Keep it short. Every token in it is spent on every single turn
(§5), so a 300-line style guide is an expensive habit; link to docs instead of inlining them.

### 3.4 Subagents, and how they map onto our catalog

OpenCode has primary agents (`build`, `plan`) and subagents it can delegate to. Subagents matter more
here than they would against a frontier model, for a reason that is specific to us: **a subagent runs
in its own context window.** Delegating a bounded question means the main session does not absorb the
20k tokens of file content needed to answer it. On a constrained window that is not a nicety, it is
the main lever.

```json
{
  "agent": {
    "build": {
      "model": "fleet/coder",
      "tools": { "webfetch": false }
    },
    "plan": {
      "model": "fleet/coder",
      "tools": { "write": false, "edit": false, "patch": false }
    },
    "locate": {
      "description": "Finds where something lives. Returns file paths and a two-line summary. Never edits.",
      "mode": "subagent",
      "model": "fleet/chore",
      "tools": { "write": false, "edit": false, "patch": false, "bash": false }
    },
    "review": {
      "description": "Reads a diff and reports problems. Never edits.",
      "mode": "subagent",
      "model": "fleet/coder",
      "tools": { "write": false, "edit": false, "patch": false }
    }
  }
}
```

Mapping guidance:

| Agent | Model | Why |
|---|---|---|
| `build` (main loop) | `coder` | The only rung that is genuinely good at multi-step edits |
| `plan` | `coder`, or `deep` for a hard design question | `deep` is 10–20 tok/s — acceptable for one planning turn you will read carefully, not for a loop |
| `locate` / search subagents | `chore` (4B on `.87`) | Grep-and-summarise does not need 30B, and it keeps `.226` free |
| `review` | `coder` | Judgement task; the small rungs miss real defects |
| Anything with `write`/`edit` | `coder` only | Never let a 4B or 8B rung hold an edit tool. §6.2 |

**Do not build a deep subagent tree.** Every level of delegation is another full system prompt,
another set of tool definitions, and another chance for a local model to mis-format a hand-off. Two
or three flat, single-purpose subagents earn their keep. A hierarchy does not.

---

## 4. Build — Cline in VS Code (and Roo Code as the alternative)

### 4.1 Install and point at the gateway

Install **Cline** from the VS Code marketplace, open its settings, and configure:

| Setting | Value | Note |
|---|---|---|
| API Provider | **OpenAI Compatible** | Not "Cline" / "OpenRouter" — those route off-network |
| Base URL | `http://10.0.0.87:4000/v1` | Include `/v1`; verify whether your version wants it |
| API Key | the developer's LiteLLM virtual key | Stored in VS Code secret storage, not in the repo |
| Model ID | `coder` | Exactly the gateway catalog name |
| Context Window | **set explicitly, below the server's `--max-model-len`** | See §5.4 — this is the setting people skip and then spend a day debugging |
| Max Output Tokens | modest, e.g. 4096 | Output competes with input for the same KV cache |
| Telemetry | **off** | §7 |

Cline exposes a **Plan / Act** split. Use it. Plan mode reads and proposes without editing; Act mode
executes. Against a local model the split is worth more than against a frontier one, because it gives
you a cheap checkpoint to abandon a bad trajectory before it writes files.

**Approval settings:** start with auto-approve **off** for everything except read-only file access.
Then, once you trust a given repo, enable auto-approve for reads and for a short allowlist of
commands. Never auto-approve terminal execution broadly on a local model — the failure mode is not
malice, it is a mis-formatted command issued confidently, and you want to be in the loop for that.

### 4.2 Roo Code

Roo Code is a fork of Cline with more configuration surface. Install and connect it identically —
same provider type, same base URL, same model name. What it adds is the set of knobs in §5.5, which
may make it the better default here despite Cline being the more popular extension.

Run both for a week during M3 and let the §9 acceptance task decide. They can coexist in one VS Code
install; just do not run both on the same repo at once, or you will not know which one produced a
result.

---

## 5. Context economics — the section that decides M3

This is the central problem of the milestone. Everything above is installation; this is engineering.

### 5.1 The mismatch, stated plainly

Cline's design assumes a model with a large, cheap context window and strong recovery from clutter.
Its system prompt is long and detailed, it defines many tools, and it volunteers "environment
details" — workspace file listing, open tabs, terminal state — on turns. This is a *good* design
against a frontier model: it front-loads knowledge so the model rarely has to ask.

Against Qwen3-Coder-30B on a 4090 sharing 24 GB with its weights, the same design can consume most of
the usable window before the agent has read a single line of the code you asked about. And a local
model degrades more sharply than a frontier one as its window fills — attention to the middle of a
long prompt gets worse, tool-call formatting gets sloppier, and it starts re-reading files it already
has.

**Do not take any specific token number on faith — not from this doc, not from a blog post, not from
the client's own display.** Client system prompts change with every release. The first task of M3 is
to measure yours.

### 5.2 Measuring what the client actually sends

Three methods, in increasing order of trustworthiness.

| Method | How | Trust |
|---|---|---|
| The client's own token display | Cline and Roo show a per-task token count in the UI | Indicative. It is the client's own accounting and may exclude tool definitions or count differently from the server |
| Gateway logs | LiteLLM records `prompt_tokens` per request; read it from the proxy's log output, its spend/usage endpoints, or its UI on `:4000` | Good. This is what was actually billed against the model |
| Server counters | vLLM's Prometheus endpoint, sampled either side of one client turn | **Best.** This is the tokenizer that actually ran |

```bash
# The server-side measurement. Run it, do one client turn, run it again, subtract.
curl -s http://10.0.0.226:8000/metrics | grep -E 'vllm:(prompt|generation)_tokens_total'
# metric names have shifted across vLLM versions — verify yours before trusting the delta
```

**The measurement that matters is the first one.** Send a trivial request — "what files are in
`services/rag`?" — and record the prompt token count. That number is the client's fixed overhead: the
system prompt, the tool definitions, the MCP tool definitions, and the environment blob, before any
of *your* code is in the window. Everything in §5.3 is about shrinking that number.

Record it in a table like this during M3, per client, per configuration:

| Client | Config | Fixed overhead (prompt tokens on a trivial turn) | After 5 turns | Notes |
|---|---|---|---|---|
| Cline | default, no MCP | | | |
| Cline | ignore file + tools trimmed | | | |
| Roo Code | default | | | |
| Roo Code | trimmed + condensing on | | | |
| OpenCode | default | | | |
| Aider | `--map-tokens 512` | | | |

An empty table in the repo is the deliverable of the first morning of M3. Fill it before changing
anything else.

### 5.3 Reducing it

In rough order of return on effort:

| Lever | How | Effect | Cost |
|---|---|---|---|
| **Open a narrower workspace** | Open `services/rag/`, not the monorepo root | Large. Shrinks the file listing in environment details and every subsequent search | You lose cross-service navigation |
| **Ignore file** | `.clineignore` / `.rooignore` at the repo root | Large. Excludes `node_modules`, `.venv`, `migrations/`, model weights, lockfiles, build output, fixtures | Files you excluded genuinely cannot be read |
| **Disable unused tools** | Turn off browser/computer-use, web fetch, and any tool group the task does not need | Moderate and free — each tool is a schema plus prose in every request | The agent cannot do that thing |
| **Expose fewer MCP tools** | §8. Attach `search_documents` and `web_search` only | Moderate. MCP definitions are sent every turn, forever | PDF/PPTX/image live in the chat UI instead |
| **Shorten `AGENTS.md` / rules files** | Link to docs; do not inline them | Small but permanent | Slightly more asking |
| **Cap max output tokens** | 4096 rather than 32768 | Frees KV cache for input | Long generations get truncated |
| **Start a new task more often** | One task = one bounded objective | Large in practice | Manual discipline |

That last row is the one people resist and the one that helps most. A local model's quality declines
across a long session faster than a frontier model's. Finishing a task and starting a fresh one with
a two-sentence handover is almost always better than continuing at 80% window occupancy.

**A note on automatic context condensing.** Both Cline and Roo can summarise the conversation when it
approaches the limit. Be sceptical of it here. The summarisation call itself reads the entire window
— the single most expensive request in the session — and a 30B model's summary of its own messy
session is lossy in ways you cannot see. It is a reasonable safety net against a hard failure. It is
not a substitute for shorter tasks.

### 5.4 The `--max-model-len` trap

Three numbers must agree, and when they disagree the failure looks like a client bug:

1. vLLM's `--max-model-len` on `.226`.
2. `model_info.max_input_tokens` in the LiteLLM catalog.
3. The **Context Window** setting in Cline / Roo.

Set (3) meaningfully *below* (1) — leave room for the output — so the client compacts or warns before
the server rejects the request. If the client believes it has 128k and the server was started with
65k, the session dies mid-task with a context-length error and the work in flight is lost.

And (1) is not a free parameter. It is bought out of the same 24 GB as the weights:

```
KV bytes/token = 2 x n_layers x n_kv_heads x head_dim x bytes_per_element
```

Read the real values from the model's `config.json` rather than trusting anything written here:

```bash
python3 - <<'PY'
import json
c = json.load(open("/models/Qwen3-Coder-30B-A3B-Instruct-AWQ/config.json"))
L  = c["num_hidden_layers"]
kv = c["num_key_value_heads"]
hd = c.get("head_dim", c["hidden_size"] // c["num_attention_heads"])
for name, b in (("fp16", 2), ("fp8", 1)):
    per_tok = 2 * L * kv * hd * b
    print(f"{name}: {per_tok/2**20:.3f} MB/token  ->  1 GB of KV = {2**30//per_tok:,} tokens")
PY
```

Two consequences fall out of whatever that prints:

- **The window is shared across concurrent sequences.** A single Cline session at 60k tokens can
  occupy the entire KV budget, and the second developer's request queues. With 2–4 concurrent coding
  sessions (N4), per-session context is a *fleet* budget, not a personal one.
- **`--kv-cache-dtype fp8` roughly doubles it**, and Ada (CC 8.9) supports it natively on `.226`.
  Take that before considering a smaller model.

### 5.5 Why Roo Code's extra knobs may win here

Cline is the more popular extension; Roo is the more configurable one, and configurability is exactly
what this problem needs. The knobs that matter — verify each against your version, since Roo moves
quickly and some of these have been experimental:

| Roo capability | Why it matters against a local model |
|---|---|
| **Per-mode system prompt override** (a `.roo/system-prompt-<mode>` file) | The only clean way to *replace* a frontier-shaped system prompt rather than work around it. This is the single biggest available reduction |
| **Custom modes with restricted tool groups** | Define a "surgeon" mode with read + edit and nothing else. Fewer tools = fewer tokens *and* better tool-call reliability (§7) |
| **Per-mode API profile** | Point plan-ish modes at `deep` and edit modes at `coder`, without changing settings by hand |
| **Max concurrent file reads** | Caps how much file content arrives in one turn |
| **Explicit context-condensing controls** | Choose the threshold and the model that does the condensing — send it to `chore`, not `coder` |
| **Granular auto-approve** | Lets you loosen approvals for reads while keeping writes gated |

If your §5.2 table shows Cline's fixed overhead eating an unacceptable share of the window and its
settings cannot bring it down, move to Roo and spend the time on a replacement system prompt. That is
the intended response to the risk already logged in
[`delivery-plan.md`](./delivery-plan.md) §11.

### 5.6 When to prefer Aider instead

Aider takes the opposite approach: **you** choose the files with `/add`, it sends a compact repo map
for everything else, and it asks the model for a *diff* rather than a rewrite. That is dramatically
cheaper per turn, and diffs are an easier output format for a local model to get right than whole-file
rewrites.

```bash
export OPENAI_API_BASE=http://10.0.0.87:4000/v1
export OPENAI_API_KEY="$LITELLM_KEY"
aider --model openai/coder --edit-format diff --map-tokens 512 --no-auto-commits
```

| Prefer Aider when | Prefer Cline/OpenCode when |
|---|---|
| You already know which 1–4 files change | You need the agent to find things first |
| The change is mechanical across many files | The task needs running tests and iterating on failures |
| The window measurement in §5.2 says the agentic clients cannot fit the task | Overhead measured acceptable |
| The ladder has dropped and only a small rung is available (§6.2) | `coder` is on rung 1 |
| You want every change as a reviewable git commit | You want a conversational loop |

Aider is not a consolation prize. On this hardware it will often be the *better* tool, and it is worth
installing for everyone during M3 even though it is not the headline client. Its weakness is real
though: it does not autonomously explore, so it is poor when the honest answer to "which files?" is
"I don't know."

---

## 6. Which model for which client

### 6.1 Defaults

| Client / role | Catalog name | Rationale |
|---|---|---|
| OpenCode `build`, Cline Act, Roo Code mode, Aider | `coder` | Qwen3-Coder-30B-A3B Int4 (~17 GB, `.226`). MoE, ~3B active — fits 24 GB *and* decodes fast |
| OpenCode `plan`, Cline Plan, Roo Architect | `coder`, occasionally `deep` | One slow planning turn can be worth it; a slow loop never is |
| OpenCode `small_model`, Roo's condensing model, titles/summaries | `chore` (4B, `.87`) | Never spend `.226` on housekeeping |
| Autocomplete (Continue.dev, if used) | `chore` | Latency dominates; quality barely matters |

### 6.2 What happens when `.226` is claimed — and how to make it visible

Someone flips *"I'm using this GPU"* or launches a modelling run. Within seconds the 30B coder is
asleep and the ladder drops to 14B, 8B, 4B or nothing
([`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §3). Coding quality does not degrade
gracefully across those rungs — it falls off a cliff. A 14B produces plausible-looking edits with
subtly wrong logic. An 8B loses the tool-call format and starts describing edits instead of making
them.

**The failure mode to design against is not the outage. It is the silent swap.** A developer whose
agent quietly got worse will conclude the platform is unreliable, not that the GPU is busy — and an
unexplained quality drop erodes trust far faster than an explained one.

Four mechanisms, in order of how much they help:

1. **Fail loudly, do not fall back.** Configure no automatic gateway fallback for `coder`. When rung 1
   is unavailable, the request returns an error. Make the error message say the real thing:
   *"Qwen3-Coder-30B is unloaded — `.226`'s GPU is in use by <who>, ~22 min. Use `coder-14b` (weaker)
   or wait."* An error a person can read beats a worse answer they cannot detect. This is the
   difference between coding and chat: for chat, degrade silently; for coding, stop.
2. **Name the rung in the model list.** The display names in §3.2 mean the OpenCode picker and the
   Cline model field both say which rung is in play. Free, and it works.
3. **Inject a one-line notice.** A LiteLLM pre-call hook can prepend a system line to requests served
   by a lower rung: *"You are Qwen3-8B, ladder rung 3, because `.226` is in use. Prefer small,
   verifiable edits; say so if a task is beyond you."* This both tells the user (the model will
   mention it) and measurably improves the small rungs' behaviour, since they stop attempting work
   they cannot finish.
4. **Put the dashboard where people already are.** The fleet page from
   [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §4.1, bookmarked, plus a terminal pane:

   ```bash
   watch -n 5 'curl -s http://10.0.0.87:8080/api/status | jq -r ".hosts[] | \"\(.host)  \(.rung)  \(.free_gb)GB\""'
   ```

**And a policy, not just a mechanism:** when the ladder drops below `coder`, the right move is
usually to *stop the agentic session*, not to continue on a weaker model. Switch to Aider on a rung-2
model for a narrow change, or do it by hand, or wait. A 30–60 minute wait is cheaper than reviewing
and unpicking an 8B model's confident mess. Write that sentence into `AGENTS.md` so it is the team's
default reflex.

---

## 7. Tool-calling reliability

Local models are measurably worse than frontier models at emitting well-formed structured tool calls,
and an agentic client is *entirely* built on that ability. Expect a failure rate that is low but not
negligible, and expect it to rise as the window fills and as the ladder drops.

Symptoms, roughly in order of frequency:

- The call is emitted as prose or as a fenced code block instead of a tool call. Usually a **server
  parser** problem, not a model problem — recheck §2.3.
- Arguments are malformed: a path with the wrong root, JSON with a trailing comma, a required field
  omitted.
- The model narrates the edit instead of calling the edit tool.
- It calls the same tool repeatedly with the same arguments — a loop, almost always a sign the window
  is too full to notice it already has the answer.

Mitigations that actually work:

| Mitigation | Detail |
|---|---|
| **Expose fewer tools at once** | The strongest lever, and it helps §5 simultaneously. A restricted Roo mode or an OpenCode agent with `tools` trimmed picks correctly far more often than one facing thirty options |
| **Prefer explicit modes over autonomous chains** | Plan mode → review → Act mode beats "go do it" on this hardware. Each hand-off is a checkpoint where a wrong turn costs one turn instead of ten |
| **Keep tool descriptions short and concrete** | Applies to our MCP server (§8), which we control. One sentence of purpose, explicit required arguments, an example. Prose about when *not* to use a tool is mostly wasted tokens |
| **Let the client retry, but bound it** | One or two retries recover most transient malformations. More than that and it is burning window on a request that will not succeed — cap it and surface the failure |
| **Disable thinking on the ladder rungs** | See below. This one bites specifically at M3 |
| **Constrain output where the server supports it** | vLLM's guided/structured decoding can force valid JSON. Verify support in your version and note it costs some throughput |

**The thinking-mode trap.** Qwen3-Coder is a non-thinking model, but the ladder rungs beneath it —
Qwen3-14B/8B/4B — are hybrid reasoning models. When the ladder drops, the client can suddenly start
receiving `<think>` blocks it does not expect: they consume window, they slow every turn, and some
clients' parsers mishandle them. Disable thinking for the rungs used by coding clients, in the
gateway's per-model parameters (`chat_template_kwargs: {"enable_thinking": false}` or the equivalent
`--reasoning-parser` handling — verify against your vLLM and LiteLLM versions), and confirm with a
`curl` that no `<think>` reaches the client.

---

## 8. MCP wiring

Our MCP tool server arrives in **M6** with five tools: `search_documents`, `web_search`,
`generate_pdf`, `generate_pptx`, `generate_image`. It is the mechanism behind F10 — one tool surface
for the chat UI, the terminal and the editor
([`01-architecture.md`](./01-architecture.md) §1). Attaching it here is a config change per client.

### 8.1 OpenCode

```json
{
  "mcp": {
    "team-tools": {
      "type": "remote",
      "url": "http://10.0.0.87:8081/mcp",
      "enabled": true
    }
  }
}
```

### 8.2 Cline / Roo Code

Both keep an MCP settings JSON reachable from the extension's MCP panel (`cline_mcp_settings.json`
for Cline; the Roo equivalent under its config directory — verify paths for your versions). The shape
is the familiar `mcpServers` map with a remote/SSE entry pointing at the same URL. Both extensions can
enable and disable *individual tools* within a server from their UI — use that; it is exactly the
control §8.3 needs.

### 8.3 Keep the exposed tool count small — this is a context decision, not a tidiness one

**Every MCP tool's name, description and JSON schema is sent on every request, for the whole session.**
Five tools is not free, and it stacks on top of the client's own dozen-plus built-ins. It also
worsens tool selection (§7).

The recommendation:

| Tool | Expose in coding clients? | Why |
|---|---|---|
| `search_documents` | **Yes** | Grounding a code change in our own specs and docs is the platform's differentiated value |
| `web_search` | **Yes** | Library and API questions are most of what a coding agent needs the internet for |
| `generate_pdf` | No | Nobody generates a PDF from inside an editing loop. It lives in the chat UI |
| `generate_pptx` | No | Same |
| `generate_image` | No | Same, and it is the tool most likely to be unavailable (`.149` claimed) |

Add tools when someone asks for them and can say why, not by default. If M6 later grows the tool
count, consider a second MCP endpoint — a "coding" profile exposing two tools and a "full" profile for
the chat UI — rather than expecting each client's per-tool toggles to be configured correctly on ten
machines.

One more thing to verify at M6: what a **tool error** looks like to each client when `.149` is claimed
and `generate_image` returns "unavailable, host in use". A clear error string is part of the tool's
contract, and a local model handles a plain-English failure far better than a stack trace.

---

## 9. Alternatives

### 9.1 Terminal

| | Licence | Strengths | Weaknesses | Verdict |
|---|---|---|---|---|
| **OpenCode** | MIT | Closest to the Claude Code experience: TUI, plan/build, subagents, permissions, MCP. Provider-agnostic by design | Ships extremely fast — config keys and defaults move. Newer than Aider, so fewer sharp edges are sanded off | **Chosen.** Pin the version |
| **Aider** | Apache-2.0 | Mature, git-native, diff-based edits, explicit file control, `--map-tokens`. By far the most context-economical | Not autonomous — you drive file selection. No real subagents. Weaker MCP story | **Install alongside.** Often the better tool here (§5.6) |
| **OpenHands** | MIT | Properly sandboxed execution; agent can run a whole environment; strong on long autonomous tasks | Heavy — Docker runtime per session; the most token-hungry of the four; needs a strong model to be worth it | Reserve. Revisit when the deep tier (M4) exists |
| **Goose** | Apache-2.0 | Clean extension model built on MCP; CLI and desktop; good tool ergonomics | Smaller community; less refined at multi-file code editing specifically | Reserve. Worth a look if MCP ergonomics become the pain point |
| **Qwen Code** | Apache-2.0 | Tuned by the Qwen team for exactly the model we run — prompts and parsers match Qwen3-Coder | Narrow: essentially single-model. Fewer features than OpenCode | **Worth trying in M3.** If OpenCode's tool-call reliability disappoints, this is the first thing to test, because the mismatch it removes is the one we have |

### 9.2 Editor

| | Licence | Strengths | Weaknesses | Verdict |
|---|---|---|---|---|
| **Cline** | Apache-2.0 | Most popular; Plan/Act; strong MCP; large community, so problems are already answered | Frontier-shaped prompts and context strategy — the whole of §5 | **Default.** Measure it first |
| **Roo Code** | Apache-2.0 | A Cline fork with the knobs: system-prompt override per mode, custom modes with restricted tool groups, per-mode model profiles, condensing controls | More configuration to get wrong; forks can drift from upstream fixes | **The likely winner here** (§5.5) |
| **Continue.dev** | Apache-2.0 | Best-in-class inline autocomplete; light context footprint; simple `config.yaml` with `apiBase` | Weaker as an autonomous multi-step agent than Cline/Roo | **Complementary, not competing.** Consider it for autocomplete against `chore` on `.87` while Cline/Roo does agentic work |

---

## 10. Acceptance test

M3 is done when **a real task completes end-to-end in both clients** — the acceptance criterion from
[`delivery-plan.md`](./delivery-plan.md) §6.

### 10.1 The task

Use a genuine, small, verifiable change on our own repo. A good candidate:

> Add a `GET /healthz` endpoint to the fleet controller that returns each host's current ladder rung
> and free VRAM. Add a test for it. Run the test suite. Show the diff.

It is real work, it touches 2–3 files, it requires reading existing code to match conventions, it has
an objective pass/fail (the test runs green), and it is small enough that a failure is diagnosable.

### 10.2 What to record

Run it in OpenCode and in Cline (or Roo), on rung 1, and fill this in:

| | OpenCode | Cline / Roo |
|---|---|---|
| Fixed overhead, trivial turn (§5.2) | | |
| Prompt tokens, total across the task | | |
| Wall clock, start to green test | | |
| Turns needed | | |
| Malformed tool calls | | |
| Times it re-read a file it already had | | |
| Manual repairs before the test passed | | |
| Did it finish without hitting the context limit? | | |

Then repeat one run with `.226`'s toggle flipped mid-task, and confirm the §6.2 behaviour: the client
gets a legible error, the person understands why, and nothing is silently worse.

### 10.3 "Good enough" — honestly, versus Claude Code

| Dimension | Local (Qwen3-Coder-30B) | Claude Code | Honest read |
|---|---|---|---|
| Single-file, well-scoped edit | Good | Excellent | **Genuinely usable.** This is the bulk of daily work |
| Writing tests for existing code | Good | Excellent | Usable |
| Multi-file refactor with a clear spec | Fair | Excellent | Works with supervision; check every file |
| Exploring an unfamiliar codebase | Poor–Fair | Excellent | The clearest gap. Use grep and your own eyes first, then hand it a narrow task |
| Long autonomous sessions (20+ turns) | Poor | Good | Do not attempt. Short bounded tasks only |
| Tool-call reliability | Fair | Excellent | Occasional malformed calls; §7 mitigates, does not eliminate |
| Speed (fast tier, warm) | Good | Good | MoE keeps this competitive — one of the few even rows |
| Availability | **Best-effort** | High | `.226` can be claimed at any moment. By design |
| Privacy | **Total — nothing leaves** | Third-party API | The reason the project exists |
| Cost | **Zero** | Per-seat / metered | N2 |

**The bar for M3 to pass is not parity.** It is: *a developer chooses to use it for a real task, twice,
without being asked to.* If after two weeks people have quietly gone back to doing it by hand, the
context tuning in §5 is where to look first, then §7, then the model.

---

## 11. Egress, credentials, and telemetry

Coding clients are the highest-risk surface in the platform for N1, because their whole job is to read
source code and send it somewhere. Three checks:

- **No provider may be configured except our gateway.** Cline, Roo and Continue all ship with
  built-in routing to hosted providers and, in some versions, their own accounts. Configure *only* the
  OpenAI-compatible provider pointed at `.87`, and verify with a packet capture during M8 that a
  coding session produces no traffic leaving the network.
- **Turn off client telemetry** in Cline and Roo. It is anonymised usage data, not code, but the
  simplest defensible position is that these extensions phone home not at all.
- **Keys never enter the repo.** `opencode.json` is committed and uses `{env:LITELLM_KEY}`; the VS
  Code extensions use secret storage; `.aider.conf.yml` holding a key is gitignored. One LiteLLM
  virtual key per person, revocable individually
  ([`delivery-plan.md`](./delivery-plan.md) §8).
- **Watch what an ignore file is for.** `.clineignore` is a context-economy tool, but it is also the
  place to exclude `.env` files and secrets directories from ever being read into a prompt. Do both.

Extension updates come from the VS Code marketplace, which is an ordinary outbound HTTPS fetch and
sits outside the model path. Note it in the M8 egress review so it is a recorded exception rather than
a surprise finding.

---

## Reflect

The install is an afternoon. The milestone is §5.

What makes this hard is a mismatch, not a defect: the best open coding clients were designed against
models with abundant context, and we are running a good model on a constrained, *shared* window. Every
lever that helps — fewer tools, narrower workspace, aggressive ignore files, shorter tasks, subagents
with their own windows, Aider's diff-based flow — is the same lever pointed at the same problem, which
is that **context is the scarce resource here, not intelligence.** Budget it the way
[`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) budgets VRAM: measure first, then spend.

The second thing to defend is honesty about degradation. `.226` is a shared workstation running
8–13 hour modelling jobs, and the coding model will vanish mid-session with no warning that the client
itself can give. Refusing loudly rather than falling back quietly feels like the worse user experience
for about a day, and is the better one from the second day onward — because a developer who
understands why the agent stopped stays a user, and one who watched it get mysteriously stupid does
not.

If one thing gets cut for time, cut Cline. A working OpenCode plus Aider covers F3 and most real work;
the editor extension is the one with the worst context economics and the most tuning ahead of it.

**Next:** [`07-inference-servers.md`](./07-inference-servers.md) §deep for M4 — the deep tier that
makes `plan` mode worth waiting for.
