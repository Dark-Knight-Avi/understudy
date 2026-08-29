# 14 — MCP Tool Server

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> **Corrected during the build:** the `mcp` SDK renamed `FastMCP` to `MCPServer` in 2.1.1, and the old
> `mcp.server.fastmcp` import now raises with a migration pointer. Read every `FastMCP` reference below
> as `MCPServer`. The port is **8002** on the host (container 8080) -- publishing 8080 as written here
> collides with Open WebUI on `.87`. See [`ports.md`](./ports.md).

> One tool surface for every client. This is the piece that turns F10 from a three-way
> reimplementation into a single service. Milestone **M6**, hosted on `.87`.
> Read [`01-architecture.md`](./01-architecture.md) §1 D3 first — this document is that decision,
> built out.

---

## 1. Concept — why this is the lever

We committed to three clients: Open WebUI for chat, OpenCode in the terminal, Cline (or Roo Code) in
VS Code. Each has its own extension model. Written the obvious way, "the assistant can generate a
PDF" is three plugins in three ecosystems, with three sets of bugs and three places to change when
the Typst template moves.

All three speak the **Model Context Protocol**. So we write one server, and every capability appears
in all three surfaces at once:

```
                        +----------------------------+
   Open WebUI  ------>  |                            | --> RAG service (.87)    search_documents
   (chat, .87)          |     MCP tool server        |
                        |     Python + FastMCP       | --> SearXNG   (.87)      web_search
   OpenCode    ------>  |     .87:8080               |
   (terminal)           |                            | --> Typst     (.87)      generate_pdf
                        |     5 tools, one schema,   |
   Cline/Roo   ------>  |     one auth token,        | --> python-pptx (.87)    generate_pptx
   (VS Code)            |     one log                |
                        |                            | --> ComfyUI   (.149)     generate_image
                        +----------------------------+          different subnet
```

| | Without MCP | With one MCP server |
|---|---|---|
| Adding a capability | 3 implementations, 3 releases | 1 tool function |
| Fixing a retrieval bug | 3 places, or 3 different behaviours | 1 place |
| Auth and rate limiting | Per client, inconsistent | One token, one middleware |
| Audit log of tool use | Scattered or absent | One log — which is what makes [ADR-0004](./adr/0004-egress-policy.md) auditable |
| Cost of swapping the chat UI | Rewrite its plugins | Change one config line |

**`search_documents` is the sharpest example.** It does not reimplement retrieval; it calls the RAG
service built in M5 over HTTP. So there is exactly **one** retrieval implementation with **two entry
points**: the `team-docs` model in the gateway (for people who just want to chat with their
documents, [ADR-0005](./adr/0005-rag-as-a-model-endpoint.md)) and this tool (for an agent that wants
to look something up mid-task). Both hit the same code, so recall@5 measured in `17-evaluation.md`
describes both.

The protocol is young and the specs are still moving. That is the tradeoff we accepted in
[`tech-stack.md`](./tech-stack.md) §6, and the mitigation is that our tool logic lives in plain
functions with the MCP decorators as a thin skin — if the protocol shifts, the skin is what changes.

---

## 2. The five tools — and why there are only five

| Tool | Backing service | Host | Returns |
|---|---|---|---|
| `search_documents` | RAG service (M5) | `.87` | Passages with document + page citations |
| `web_search` | SearXNG | `.87` | Titles, URLs, snippets |
| `generate_pdf` | Typst | `.87` | A file id + path to a rendered PDF |
| `generate_pptx` | python-pptx | `.87` | A file id + path to an editable `.pptx` |
| `generate_image` | ComfyUI | `.149` | A file id + path to a PNG |

### The constraint that shapes every schema: tool count and description length

Every tool definition is injected into the model's context on **every** turn — name, description, and
the full JSON schema of its arguments. Frontier models absorb that cost invisibly. Ours cannot:

- Local models have far less context to spend, and on `.226` the KV cache is a budgeted resource
  shared with everything else ([`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) §2).
- Local models select tools less reliably. Two tools whose descriptions overlap will be confused with
  each other, and the failure is silent — you get the wrong tool, not an error.

So treat the tool surface as a **context budget**, and spend it deliberately:

1. **Five tools, and argue hard before adding a sixth.** If a capability is a variant of an existing
   tool, make it an argument, not a tool. `search_documents(workspace=...)` beats one tool per
   workspace.
2. **One line of description, imperative, naming the distinguishing noun.** "Search the team's
   indexed internal documents" and "Search the public web" are unambiguous. "Search documents" and
   "Search for information" are not.
3. **Few arguments, all with defaults.** Every required argument is another thing the model can get
   wrong. `query` is required; everything else has a default.
4. **No free-form `options` dict.** A model handed an open-ended object will invent keys.
5. **Never expose an enum with more than a handful of values.** Long enums are where the token budget
   goes to die; validate server-side instead and return an error listing what is valid.
6. **Docstrings are the wire format.** FastMCP derives the tool description from the function
   docstring. A three-paragraph docstring is three paragraphs in every prompt, forever. Keep the long
   explanation in a `#` comment above the function, where the model never sees it.

Measure this rather than guessing: count the tokens of the serialised tool list once the server is
up, using the same tokeniser the fast tier uses, and record the number here when the milestone ships.

### Schemas

Sketches to type — argument names are the contract, so fix them now and keep them stable.

| Tool | Arguments | Notes |
|---|---|---|
| `search_documents` | `query: str`, `top_k: int = 5`, `workspace: str = "default"` | Delegates to the RAG service's retrieval endpoint, not its chat endpoint |
| `web_search` | `query: str`, `max_results: int = 5` | Refused with a clear message when the workspace has search disabled ([`16`](./16-web-search-and-egress.md)) |
| `generate_pdf` | `title: str`, `body_markdown: str`, `template: str = "report"` | Model supplies content; the template owns layout ([`15`](./15-generation-tools.md)) |
| `generate_pptx` | `title: str`, `slides: list[Slide]`, `template: str = "default"` | `Slide` is a small typed model: `title`, `bullets`, `notes` |
| `generate_image` | `prompt: str`, `aspect: str = "16:9"`, `quality: str = "fast"` | `quality` maps to whichever ladder rung `.149` can hold, not to a model name |

`generate_image` deliberately takes `quality`, not `model`. Which model is loaded on `.149` is
decided by free VRAM ([`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §3), not by the
caller, and exposing a model name would make the tool lie every time the ladder moved.

---

## 3. Transport — stdio or HTTP?

MCP servers can run as a subprocess speaking stdio, or as an HTTP service (SSE in older revisions,
streamable HTTP in newer ones — **verify which your SDK and each client support in your versions**;
this is the fastest-moving part of the protocol).

| | stdio | HTTP / streamable HTTP |
|---|---|---|
| Where it runs | One subprocess per client, on the client's machine | One service on `.87` |
| Reaching `.149` and Postgres | Every developer laptop needs routes and credentials | Only `.87` does |
| Upgrades | Every laptop, individually | `make deploy HOST=87` |
| Auth | Inherits whoever ran it | One bearer token, one place to rotate |
| Audit log | Per laptop, uncollected | One log — required by ADR-0004 |
| Open WebUI (a server, not a desktop app) | Awkward: it would have to spawn processes in its own container | Natural |
| Works offline on a laptop | Yes | No — needs the LAN or VPN |

**Decision: HTTP on `.87`, one shared instance.** The clients are three different kinds of program —
a containerised web app, a CLI, a VS Code extension — and the backing services sit on two subnets.
Only a network service fits all three. stdio's advantages (no auth, no network) are advantages for a
single-user desktop tool, not for a shared platform whose central requirement is an auditable egress
boundary.

Consequences to plan for:

- **Auth.** A bearer token in the `Authorization` header, read from the environment, generated per
  environment, gitignored ([`delivery-plan.md`](./delivery-plan.md) §8). This is a LAN service, so
  the token is about attribution and accident-prevention, not about defeating an attacker on the wire.
- **Bind and firewall.** Bind `0.0.0.0` (WSL2 with `networkingMode=mirrored`, see
  `05-host-setup.md`) and restrict the port to the internal subnets. Nothing here is published to the
  internet — a stated non-goal in [`00`](./00-goals-and-constraints.md) §4.
- **A stdio shim is the fallback, not the plan.** If a client turns out not to speak our HTTP
  revision, ship a ~20-line stdio process that proxies to the HTTP server. The tools stay in one
  place; only the doorway changes.
- **Statelessness.** Tools take arguments and return a result. No per-session state, so restarting
  the server mid-conversation costs nothing and a second replica stays possible later.

---

## 4. Build

### 4.1 Skeleton

```
services/mcp-tools/
  pyproject.toml
  app/
    server.py          # tool definitions only - thin
    config.py          # env -> settings
    errors.py          # the one error envelope
    clients/
      rag.py           # -> RAG service      (.87)
      searx.py         # -> SearXNG          (.87)
      comfy.py         # -> ComfyUI          (.149)
      fleet.py         # -> fleet controller (.87)
    render/
      pdf.py           # Typst       - see 15-generation-tools.md
      pptx.py          # python-pptx - see 15-generation-tools.md
    templates/
```

`server.py` holds no logic. Each tool validates, calls a client, and shapes a result. That is what
keeps MCP's protocol churn contained to one file.

```toml
# pyproject.toml  (uv; pin everything - see tech-stack.md, section 7)
[project]
name = "mcp-tools"
requires-python = ">=3.12"
dependencies = [
  "mcp[cli]>=1.2",        # official Python SDK, includes FastMCP - verify current version
  "httpx>=0.27",
  "pydantic>=2.7",
  "pydantic-settings>=2.3",
  "python-pptx>=0.6.23",
]
```

### 4.2 Config — no credentials in the repo

```python
# app/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    rag_url: str = "http://rag:8001"
    searxng_url: str = "http://searxng:8080"
    comfyui_url: str = "http://10.0.0.226:8188"     # shares the 4090; admission-controlled
    fleet_url: str = "http://fleet-controller:8090"
    mcp_token: str                                     # required; from .env, never committed
    artifact_dir: str = "/data/artifacts"
    web_search_enabled: bool = False                   # OFF BY DEFAULT - ADR-0004
    timeout_fast_s: float = 10.0                       # RAG, SearXNG
    timeout_render_s: float = 30.0                     # Typst, python-pptx
    timeout_image_s: float = 180.0                     # ComfyUI queue + sampling

    class Config:
        env_file = ".env"

settings = Settings()
```

Those timeouts are starting values, not measurements. Replace them with numbers taken from real runs
once M6 and M7 are up, and record what you observed.

### 4.3 The server and the first two tools

```python
# app/server.py
from mcp.server.fastmcp import FastMCP     # verify the import path for your SDK version
from .clients import rag, searx
from .config import settings
from .errors import unavailable, refused

mcp = FastMCP("ai-platform-tools")

@mcp.tool()
async def search_documents(query: str, top_k: int = 5, workspace: str = "default") -> dict:
    """Search the team's indexed internal documents. Returns passages with document and page citations."""
    # Delegates to the RAG service built in M5. One retrieval implementation, two entry points:
    # the `team-docs` gateway model, and this tool. Do not reimplement retrieval here.
    try:
        hits = await rag.retrieve(query, top_k=min(top_k, 10), workspace=workspace)
    except rag.Unavailable as e:
        return unavailable("rag", str(e))
    if not hits:
        return {"ok": True, "results": [], "note": "No indexed document matched this query."}
    return {"ok": True, "results": hits}      # each: {text, document, page, score}

@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> dict:
    """Search the public web for current information. The query text leaves the network; document text never does."""
    if not settings.web_search_enabled:
        return refused("web_search", "Web search is disabled for this workspace.")
    try:
        return {"ok": True, "results": await searx.search(query, max_results=min(max_results, 10))}
    except searx.Unavailable as e:
        return unavailable("searxng", str(e))
```

Two details worth copying. `min(top_k, 10)` caps the damage from a model that asks for 100 results
and then drowns in them. And the second sentence of `web_search`'s docstring is deliberate: it is
read by the model *and* shown to the user in the client's tool-approval prompt, which is exactly
where the egress contract should be visible.

### 4.4 Degradation — the part that decides whether people trust this

A tool that hangs is worse than a tool that fails. The agent waits, the client's own timeout
eventually fires, and the user sees nothing useful. On this platform unavailability is **normal**:
`.149` gets claimed by the person sitting at it, `.87` restarts during a deploy, SearXNG's upstream
engines rate-limit.

Rules:

1. **Every outbound call has an explicit timeout.** No default-infinite HTTP client, anywhere.
2. **Every tool returns the same envelope**, success or failure. Never raise into the protocol —
   client behaviour on a protocol-level error varies, and a plain result the model can read is more
   useful than a stack trace it cannot.
3. **Check before you queue.** For `.149`, ask the fleet controller *first*. It already knows whether
   the host is claimed, and answering "unavailable" in milliseconds beats submitting a job and
   waiting minutes for it to fail.
4. **Say what to do next.** "Host in use" tells the model to stop retrying; "temporarily busy" tells
   it to try again later. Which one you send changes agent behaviour, so choose deliberately.
5. **A circuit breaker on repeated failure.** After ~3 consecutive failures, fail fast for ~60 s
   rather than making every subsequent turn wait out the full timeout.

```python
# app/errors.py
def unavailable(service: str, detail: str) -> dict:
    return {"ok": False, "error": "unavailable", "service": service, "detail": detail}

def refused(tool: str, detail: str) -> dict:
    return {"ok": False, "error": "refused", "tool": tool, "detail": detail}
```

```python
# app/server.py (continued)
@mcp.tool()
async def generate_image(prompt: str, aspect: str = "16:9", quality: str = "fast") -> dict:
    """Generate an image from a text description. Returns a file path."""
    state = await fleet.host_state("149")             # cheap, local, sub-second
    if state.claimed or state.free_vram_gb < 7:
        return unavailable(
            "comfyui",
            "Image generation is unavailable: host .149 is in use by its owner. Try again later.",
        )
    try:
        return {"ok": True, **await comfy.render(prompt, aspect, quality, state.rung)}
    except comfy.Busy:
        return unavailable("comfyui", "Image generation is temporarily busy; try again in a minute.")
    except comfy.Unavailable as e:
        return unavailable("comfyui", f"Image generation is offline: {e}")
```

The wording of that first message is the whole point of the section. The model reads it, tells the
user "image generation is unavailable because that workstation is in use", and nobody files a bug.
That sentence is the sharing policy ([`03`](./03-gpu-sharing-policy.md) §3) becoming visible to an
end user — the same principle as surfacing the current ladder rung in the chat UI.

### 4.5 Logging

One structured line per invocation: timestamp, caller identity if the client passes one, tool name,
arguments, outcome, duration. Two reasons, and the second is the important one:

- Tool-selection failures are otherwise invisible. If the model keeps calling `web_search` for
  questions about internal documents, the description needs rewriting, and only the log shows it.
- **`web_search` queries are the entire outbound surface of this platform.** ADR-0004 promises that
  every outbound query is logged and the log is visible. This is where that promise is kept — see
  [`16-web-search-and-egress.md`](./16-web-search-and-egress.md) §5.

Log `web_search` arguments in full. For `search_documents`, log the query but **not** the retrieved
passages — those are document text, and duplicating them into a log file guarded less carefully than
the database is a quiet way to undo the whole point of the project.

### 4.6 Running it

```python
# app/main.py
from .server import mcp

if __name__ == "__main__":
    # Transport names have changed across SDK revisions ("sse" -> "streamable-http").
    # Verify against your version before pinning this.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
```

```yaml
# deploy/host-87/compose.yaml (fragment)
  mcp-tools:
    image: 10.0.0.87:5000/mcp-tools:0.1.0     # never :latest - see delivery-plan.md
    restart: unless-stopped
    environment:
      RAG_URL: http://rag:8001
      SEARXNG_URL: http://searxng:8080
      COMFYUI_URL: http://10.0.0.226:8188
      FLEET_URL: http://fleet-controller:8090
      MCP_TOKEN: ${MCP_TOKEN}                   # from .env, gitignored
      WEB_SEARCH_ENABLED: "false"               # ADR-0004 default
    volumes:
      - artifacts:/data/artifacts
    ports:
      - "8080:8080"
    networks: [platform]                        # internal only - no egress route, see doc 16
```

Note the network. This container gets the internal network only. It reaches SearXNG as a *peer*, and
SearXNG is the one container with an egress path. That is the enforcement point for N1, and it is why
the MCP server must never be handed a general outbound route "just for convenience".

---

## 5. Attaching the clients

Every client's MCP configuration has moved between releases. Treat the shapes below as the idea, and
**verify the exact keys against the version you install** — then record the working config here when
the milestone ships.

### Open WebUI (`.87`)

Recent versions connect to MCP servers over streamable HTTP directly; older ones expect an
OpenAPI-shaped tool server and need the `mcpo` adapter in front. Check which your pinned version does
before configuring anything. Either way it is one entry under *Settings -> Tools*: the URL
`http://10.0.0.87:8080/mcp` and the bearer token.

### OpenCode (terminal)

```jsonc
// opencode.jsonc
{
  "mcp": {
    "ai-platform": {
      "type": "remote",
      "url": "http://10.0.0.87:8080/mcp",
      "headers": { "Authorization": "Bearer ${MCP_TOKEN}" },
      "enabled": true
    }
  }
}
```

### Cline / Roo Code (VS Code)

```jsonc
// cline_mcp_settings.json
{
  "mcpServers": {
    "ai-platform": {
      "type": "streamableHttp",
      "url": "http://10.0.0.87:8080/mcp",
      "headers": { "Authorization": "Bearer <token from the password manager>" },
      "disabled": false
    }
  }
}
```

Remember the Cline caveat from [`tech-stack.md`](./tech-stack.md) §4: its prompts are already
context-hungry against a local model. Five compact tools is a deliberate accommodation of that, and
if context pressure shows up in M3/M6, disabling individual tools per client is the first lever —
Cline rarely needs `generate_pptx`.

---

## 6. Acceptance — the same tool from all three clients

This is M6's acceptance test in [`delivery-plan.md`](./delivery-plan.md) §6, and the only proof that
F10 holds. Run it in one sitting, in this order.

| # | Test | Pass |
|---|---|---|
| 1 | Server up: list tools over HTTP with `curl` and the bearer token | Exactly 5 tools, descriptions as written |
| 2 | `search_documents` from **Open WebUI** — ask about a document ingested in M5 | Answer cites document + page; citations match the `team-docs` model's for the same question |
| 3 | Same question, same tool, from **OpenCode** | Same passages retrieved |
| 4 | Same question, same tool, from **Cline** | Same passages retrieved |
| 5 | `generate_pptx` from all three with the same outline | Three `.pptx` files that open in PowerPoint and differ only in timestamp metadata |
| 6 | `generate_image` from chat while `.149` is toggled *in use* | Clear "unavailable, host in use" almost immediately — no hang, no stack trace |
| 7 | `web_search` with the workspace switch off | Explicit refusal, and **no** outbound packet (verify with the capture in [`16`](./16-web-search-and-egress.md) §6) |
| 8 | Stop the RAG service, call `search_documents` from any client | `unavailable` envelope within the fast timeout; the client stays responsive |
| 9 | Reboot `.87` | Every tool works again with no manual step (N8) |

Test 5 is the one that actually proves the thesis. Identical output from three unrelated clients
means there is genuinely one implementation, not three that happen to agree today.

---

## Reflect

The engineering here is small — five functions, some HTTP clients, one error envelope. What makes it
load-bearing is arithmetic: three clients times five capabilities is fifteen integrations built the
obvious way, and five built here. That ratio is why this document sits at the centre of the
architecture rather than in an appendix.

**Two things we expect to get wrong first.**

*Tool descriptions.* We are writing them for a model class measurably worse at tool selection than
the one most MCP examples were written against. Expect to rewrite all five after watching a week of
logs, and treat the descriptions as tuned parameters rather than documentation.

*Where the boundary sits between tool and model.* `generate_pptx` takes a structured outline because
we do not trust a local model to emit raw python-pptx calls. That line — how much structure the tool
imposes versus how much freedom the model gets — is the recurring design question in
[`15-generation-tools.md`](./15-generation-tools.md), and we will probably move it at least once.

The residual risk worth naming: **MCP is young and all three clients implement it slightly
differently.** A revision could break one client and leave the others fine. The mitigation is
architectural rather than defensive — logic in plain functions, protocol in one thin file — so a
breaking change costs a day, not a milestone.

**Next:** [`15-generation-tools.md`](./15-generation-tools.md) and
[`16-web-search-and-egress.md`](./16-web-search-and-egress.md).
