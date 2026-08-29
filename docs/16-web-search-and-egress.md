# 16 — Web Search & the Egress Boundary

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> Web search lands in **M6**; the egress proof is the headline test of **M8**. This document
> implements [ADR-0004](./adr/0004-egress-policy.md) and is where **N1** — *no document text or source
> code leaves the network* — stops being a claim and becomes a measurement.

---

## 1. Concept — the boundary is the product

Everything else in this project has an easier hosted equivalent. Chat, coding agents, retrieval,
image generation: all available, better, from vendors, today. The single thing we have that they do
not is that **our documents and our code never leave the building**.

That makes this document's subject the justification for the entire platform, and it means the
boundary has to be real in a specific way: enforced by the network, not by our own good behaviour, and
demonstrable by capture rather than by assertion. A policy that lives only in code review is one
careless `httpx.get` away from being false, and nobody would ever know.

The complication is F8. The team wants current answers, and no local model is current — its knowledge
is frozen at training time. Currency can only come from reaching outside. So the line is drawn at
**content, not connectivity**.

---

## 2. The policy

| May leave the network | Must never leave |
|---|---|
| A search query string | Document text, and any chunk or passage of it |
| The fact that a search happened, and when | Source code, file paths, repository names |
| SearXNG's requests to public search engines | Chat history, prompts, system prompts |
| | Embeddings (they are a lossy but real encoding of the text) |
| | Retrieved context assembled for a prompt |
| | Uploaded files of any kind |
| | Usernames, and anything identifying who asked |

Two clauses in that table are easy to miss.

**Embeddings count as content.** They are not human-readable, which makes them feel safe; they are
recoverable enough — inversion attacks on sentence embeddings are a published result — that treating
them as ciphertext would be wishful. They stay in Postgres on `.87`.

**Identity counts too.** SearXNG's value here is partly that it strips referrer and identifying
headers and fetches on our behalf, so an engine sees one machine asking, not ten named people.

---

## 3. SearXNG on `.87`

[SearXNG](https://searxng.org) is a self-hosted metasearch engine: it forwards a query to several
public engines, aggregates the results, and returns them without an account, an API key, or a
per-query fee. That satisfies N2, and — more importantly for us — it collapses all outbound traffic to
**one container**, which is what makes §4 and §6 possible at all.

The alternatives and why not: a commercial search API (Brave, Tavily, Serper) is more reliable and
better documented, but it costs money (N2) and hands a vendor every query with an account attached to
it. That is the full argument, and it is recorded in ADR-0004.

### Configuration

Key settings — **verify names against the version you deploy**, SearXNG's config schema does move:

```yaml
# deploy/host-87/searxng/settings.yml  (fragment)
use_default_settings: true

server:
  secret_key: "${SEARXNG_SECRET}"       # from .env, gitignored - see delivery-plan.md
  limiter: true
  image_proxy: true                     # images fetched by SearXNG, not by the client
  method: "POST"

search:
  safe_search: 0
  autocomplete: ""                      # OFF: autocomplete leaks keystroke-level query fragments
  formats:
    - html
    - json                              # required - our MCP tool calls the JSON API

outgoing:
  request_timeout: 5.0
  max_request_timeout: 10.0
  pool_connections: 10

engines:
  - name: google
    disabled: false
  - name: duckduckgo
    disabled: false
  - name: wikipedia
    disabled: false
  # Disable engines that require an account or an API key: they reintroduce
  # an identified relationship with a vendor, which is what we are avoiding.
```

Turning **autocomplete off** matters more than it looks. Autocomplete sends partial queries as the
user types — more outbound strings, at higher volume, that nobody consciously chose to send, and that
never appear in any log a person would think to read.

```yaml
# deploy/host-87/compose.yaml (fragment)
  searxng:
    image: searxng/searxng:<pinned-tag>       # pin it - see delivery-plan.md
    restart: unless-stopped
    volumes:
      - ./searxng:/etc/searxng:ro
    environment:
      SEARXNG_BASE_URL: http://searxng:8080/
    networks: [platform, egress]              # THE ONLY container on both
```

The client side is the `web_search` tool from [`14`](./14-mcp-tool-server.md) §4.3, calling
`http://searxng:8080/search?q=...&format=json` over the internal network.

---

## 4. Enforcement — at the network layer

### 4.1 Two Docker networks

```
   +------------------------------------------------------------+
   |  network: platform   (internal: true - NO route out)        |
   |                                                             |
   |  open-webui   litellm   rag   mcp-tools   postgres          |
   |  infinity     fleet-controller   caddy                      |
   |                                        \                    |
   |                                         \  http://searxng:8080
   |                                          \                  |
   |                                       +--------+            |
   +---------------------------------------| searx  |------------+
                                           |  ng    |
                                           +--------+
                                                |
   +--------------------------------------------|---------------+
   |  network: egress   (bridge, NAT to the LAN and out)         |
   +------------------------------------------------------------+
```

```yaml
networks:
  platform:
    internal: true        # <- the whole enforcement, in one line
  egress:
    driver: bridge
```

`internal: true` means Docker installs no default route and no NAT for that network. A container on it
cannot reach anything off-host regardless of what its code attempts — no library update, no
misconfigured base image, no dependency phoning home. **Every** platform service is on `platform`
only. SearXNG alone is on both.

Two cross-host exceptions have to be reasoned about explicitly, because they are traffic leaving the
*host* even though they never leave the *network*: `mcp-tools` -> `.149:8188` (ComfyUI) and
`litellm` -> `.226` (vLLM). Both are LAN destinations. Handle them with an explicit route to those
hosts, or by placing those flows on a separate internal-to-the-LAN network — and then make sure §6's
capture proves that is all they reach.

### 4.2 The DNS trap

An internal network cannot reach an external DNS resolver either. Containers on `platform` resolve
each other through Docker's embedded DNS, which is fine — but if any service is configured with an
external resolver, or with a hostname that must resolve publicly, it will fail in a confusing way.
Two rules:

- Platform containers address each other **by service name** only.
- **SearXNG is the only container that needs public DNS**, and it is on `egress`, so it has it.

A DNS query is itself an egress event, and one that survives TLS. Keeping resolution off the platform
network removes a whole channel — and a `.87` DNS log of nothing but search-engine names is a second,
independent piece of evidence for §6.

### 4.3 Host firewall

Docker's `internal: true` is the primary control; the host firewall is the belt to that pair of
braces, and it covers the non-container case (a service accidentally run outside Compose).

On the WSL2 side of `.87`, default-deny outbound, with narrow exceptions:

```bash
# Verify against your distro's firewall tooling and your WSL2 networking mode.
# With networkingMode=mirrored the Windows firewall also applies - check both.
sudo iptables -P OUTPUT DROP
sudo iptables -A OUTPUT -o lo -j ACCEPT
sudo iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# LAN peers we legitimately talk to
sudo iptables -A OUTPUT -d 10.0.0.0/24 -j ACCEPT      # local subnet (.226, clients)
sudo iptables -A OUTPUT -d 10.0.0.226/32 -j ACCEPT    # ComfyUI (image gen shares the 4090)

# The single egress exception: the SearXNG container's address
sudo iptables -A OUTPUT -s <searxng-container-ip>/32 -p tcp --dport 443 -j ACCEPT
sudo iptables -A OUTPUT -s <searxng-container-ip>/32 -p udp --dport 53  -j ACCEPT
```

Pin SearXNG's address (a static IP on the `egress` network, or match its interface) — a container IP
that changes on restart turns this rule into a rule about some other container.

Two honest caveats. Docker manipulates iptables itself, and rules in `OUTPUT` do not see forwarded
container traffic the way you might expect — verify your rules with an actual test, never by reading
them. And under WSL2 the Windows Defender Firewall and Hyper-V firewall rules also apply; with
`networkingMode=mirrored` the interaction is version-specific. **Test the block, do not assume it.**
§6 exists precisely because this configuration is too subtle to trust by inspection.

### 4.4 Why not a filtering proxy?

An HTTP proxy with a domain allowlist would be stricter still — SearXNG could only reach approved
engines. ADR-0004 lists it as the governance-heavy variant, adopted if a security review asks for
provable egress control. It is not in v1 because the design already funnels through one point, so
adding it later is a small change rather than a redesign. If you do add it, put it between `searxng`
and the internet, not between the tools and SearXNG.

---

## 5. The switch — off by default

Web search is a **module, off by default per workspace** (ADR-0004). A team working on a sensitive
corpus runs fully air-gapped without a special deployment, and enabling search is a decision someone
makes and can be seen to have made.

- Default `WEB_SEARCH_ENABLED=false` ([`14`](./14-mcp-tool-server.md) §4.2).
- Per workspace, stored with the workspace's config, not in the client. A user cannot enable it by
  editing their own Cline settings.
- When disabled, `web_search` returns a `refused` envelope and **makes no outbound call at all** —
  the check comes before the HTTP client is touched (M6 acceptance test 7 verifies this with a
  capture, not by reading the code).
- **Show the state in the UI.** A small persistent indicator on any workspace where search is on. The
  question "does this thing send my stuff to Google?" should be answerable at a glance, forever.

### The query log

Every outbound query is logged — timestamp, workspace, user, the exact query string, the engines hit,
the number of results. ADR-0004 promises the log is **visible**, which means more than "exists":

- A page in the fleet dashboard listing recent outbound queries, readable by anyone on the platform,
  not just an admin.
- Retained long enough to answer "did anything about project X leave last quarter?" — a quarter or two.
- Reviewed as a habit. The first month of that log is also the best available data on how well the
  model phrases queries, which feeds directly back into the tool description
  ([`14`](./14-mcp-tool-server.md) §4.5).

---

## 6. Residual risk — stated plainly

The boundary is strong on content and weak on inference. Be honest about all of it, in the user-facing
documentation as well as here.

| Risk | Reality | Mitigation |
|---|---|---|
| **Query text discloses names** | Asking about an internal codename sends that codename to Google. Unavoidable — this is the decision, not a bug | Tell users plainly; the switch; the visible log |
| **The agent decides when to search** | A model may search unprompted, and phrase the query badly. The user never chose those words | Log everything; cap query length; review the log early and often |
| **Retrieved text leaking into a query** | The sharpest one. An agent that just called `search_documents` has document text in its context and may paste a chunk of it into `web_search` — technically permitted by the tool, entirely against the policy | §6.1 |
| **Traffic analysis** | Engines see one IP, timing, and volume. Query timing correlates with working hours and with events | Accepted. SearXNG's header stripping is the only realistic control |
| **A user pastes document text into chat, then asks to search it** | The system did what it was asked; the policy was defeated by a person | Training and the length cap. Do not pretend a technical control solves this |
| **SearXNG fetches result pages** | Outbound requests to third-party sites, not just engines — normal, but it means the egress destination set is wider than "search engines" | Expected in the capture; the allowlist proxy in §4.4 if it ever matters |
| **Dependency phone-home** | Telemetry in some library, an update check, a crash reporter | `internal: true` makes this structurally impossible for platform containers — the main reason to enforce at the network layer |

### 6.1 The chunk-in-query problem

This is the leak most likely to actually happen, because nothing about it looks like a mistake. The
agent retrieves a passage, decides it wants context, and searches for a sentence lifted from it. The
tool call is well-formed, the policy is broken, and the audit log records it after the fact.

Defend in three cheap layers, all in the `web_search` tool:

1. **Cap query length** — around 200 characters. Real search queries are short; a 600-character query
   is a paste, and refusing it costs nothing.
2. **Reject queries that overlap retrieved context.** The MCP server knows what `search_documents`
   returned in this session. If a candidate query shares a long substring — say a matching 12-word
   shingle — with any passage returned in the last few turns, refuse it and tell the model to
   rephrase in its own words. Approximate, cheap, and it catches the copy-paste case that matters.
3. **Flag long queries in the log** so review is targeted rather than a wall of text.

None of this is airtight. It converts the common accidental case into a refusal, which is the
realistic goal.

---

## 7. The verification procedure — N1

**This is the single most important test in the project.** Run it at M8, then after any change to the
network topology, the Compose files, or the tool server — and record the result and the date in this
document each time.

### 7.1 Seed the corpus with canaries

Do this first; the rest of the procedure depends on it. Grepping a capture for "document text" is
hopeless, because you do not know what to look for. Grepping for a string that exists nowhere else in
the universe is trivial.

```bash
python3 - <<'PY'
import secrets
for i in range(5):
    print(f"CANARY-{i}-{secrets.token_hex(8).upper()}")
PY
```

Put one canary in each of five places, and record which is where:

| Canary | Placed in |
|---|---|
| 1 | Body text of an ingested PDF, mid-paragraph |
| 2 | An ingested document's **filename** |
| 3 | A source file ingested from a code repository |
| 4 | A chat message typed into Open WebUI |
| 5 | A document that is ingested but **never retrieved** during the test |

Canary 5 is the control: if it appears anywhere, the leak is in ingestion, not in answering.

### 7.2 Lock down and capture

```bash
# 1. Confirm the intended state
docker network inspect platform | grep -i internal        # expect: true
docker compose ps                                          # everything up

# 2. Prove a platform container genuinely cannot get out
docker compose exec rag        sh -c 'curl -m 5 https://example.com; echo "exit=$?"'   # expect failure
docker compose exec mcp-tools  sh -c 'curl -m 5 https://example.com; echo "exit=$?"'   # expect failure
docker compose exec mcp-tools  sh -c 'getent hosts example.com; echo "exit=$?"'        # expect failure
docker compose exec searxng    sh -c 'curl -m 5 -o /dev/null -w "%{http_code}\n" https://duckduckgo.com'  # expect 200

# 3. Start TWO captures, in two shells.
#    (a) the host's real egress interface - what actually leaves the machine
sudo tcpdump -i <host-egress-if> -s 0 -w ~/egress-$(date +%F).pcap \
     'not net 10.0.0.0/16 and not port 22'

#    (b) the docker bridge for the platform network - what containers try to send,
#        BEFORE TLS, which is the only place payload bytes are readable
sudo tcpdump -i <br-of-platform-net> -s 0 -w ~/platform-$(date +%F).pcap
```

Capture (b) is the one people skip, and it is the one that can actually see document text. On the
egress interface everything is inside TLS, so a clean capture there proves *who* talked to *whom*, not
*what they said*. You need both legs: (b) proves platform containers emit no content anywhere, (a)
proves nothing but SearXNG reaches the outside at all.

### 7.3 Run a full cycle

With captures running, exercise the whole system for 10–15 minutes:

1. Ingest the canary corpus from §7.1 (fresh, so ingestion traffic is included).
2. Ask a question whose answer is in canary document 1 — confirm citations.
3. Ask a question about the ingested code, hitting canary 3.
4. Ask a question the corpus cannot answer, so the relevance gate fires (F7).
5. Enable web search on the test workspace and ask a current-events question, so SearXNG is genuinely
   used. Note the exact query.
6. Ask a question that mixes both: something in the documents *and* something current.
7. Generate a PDF, a PPTX and an image, so `.149` traffic is in the capture too.
8. Attempt the chunk-in-query case from §6.1 deliberately: ask the assistant to "search the web for
   this exact sentence" and give it a sentence from the corpus. Record what it does.

Stop the captures.

### 7.4 Analyse

```bash
# Every external endpoint the host talked to, with byte counts
tshark -r ~/egress-2026-xx-xx.pcap -q -z conv,tcp

# Every TLS SNI - the destination names, readable despite encryption
tshark -r ~/egress-2026-xx-xx.pcap -Y 'tls.handshake.extensions_server_name' \
       -T fields -e ip.dst -e tls.handshake.extensions_server_name | sort -u

# Every DNS name looked up
tshark -r ~/egress-2026-xx-xx.pcap -Y 'dns.flags.response == 0' \
       -T fields -e dns.qry.name | sort -u

# Confirm the ONLY internal source that left the box is SearXNG
tshark -r ~/egress-2026-xx-xx.pcap -T fields -e ip.src | sort -u

# Canaries and content, in the pre-TLS capture (and the egress one, for completeness)
for f in ~/platform-*.pcap ~/egress-*.pcap; do
  echo "== $f"
  strings -a "$f" | grep -F -f ~/canaries.txt || echo "  no canaries"
done
```

Also grep both captures for:

| What | Why |
|---|---|
| The five canary strings | The primary test |
| Ingested document filenames | Filenames leak topic even when text does not |
| Distinctive phrases from the corpus, ~8 words | Catches paraphrase-free copying the canaries missed |
| Repository and internal host names | Code-adjacent identifiers |
| `[0.0` / long comma-separated float runs | The shape of a serialised embedding vector |
| The recorded web-search query | It **must** be present — proving the capture actually works. A capture with no expected content in it is a broken capture, not a clean result |

That last row is the discipline that makes the whole procedure meaningful. **Verify the positive
control first.** If the query you deliberately sent is not in the capture, you are capturing on the
wrong interface and everything else you conclude is worthless.

### 7.5 Pass criteria

| # | Criterion | Pass |
|---|---|---|
| 1 | Platform containers cannot reach the internet | `curl` and DNS both fail from `rag` and `mcp-tools` |
| 2 | Only SearXNG appears as an internal source in the egress capture | One address |
| 3 | The known search query is present in the capture | Positive control confirmed |
| 4 | No canary appears in either capture | Zero hits, all five |
| 5 | No document filename or corpus phrase appears | Zero hits |
| 6 | No embedding-shaped payload appears | Zero hits |
| 7 | Destinations are search engines and result pages only | SNI/DNS list reviewed by a human |
| 8 | With search disabled, a repeat of step 5 produces **no** outbound packet | Empty capture on the egress leg |
| 9 | The chunk-in-query attempt (§7.3 step 8) was refused | Refusal in the tool log, nothing in the capture |

Anything less than all nine is a fail, and a fail blocks M8. Record the date, the pcap hashes and the
verdict in this document — a test whose result was not written down did not happen.

### 7.6 When to re-run

- After any change to Compose networks, firewall rules, or the MCP server.
- After adding any dependency to a platform service.
- After a major version bump of Open WebUI, LiteLLM or SearXNG.
- Quarterly regardless, as a habit. It takes an afternoon; the claim it defends is the product.

---

## 8. Citing web results next to document citations

Web results are citable like any other source, with one difference the user must be able to see: a web
citation is *outside* the trust boundary and was not part of the corpus anyone curated.

Return one citation shape for both kinds, with an explicit source type:

```json
{"citations": [
  {"type": "document", "n": 1, "title": "Network Design Standard v4.pdf", "page": 12,
   "workspace": "engineering"},
  {"type": "web", "n": 2, "title": "IEEE 802.1Q-2022 overview", "url": "https://...",
   "engine": "duckduckgo", "retrieved_at": "2026-08-29T10:14:00Z"}
]}
```

Conventions:

- **Distinct markers.** `[D1]` for documents, `[W1]` for the web. Do not blend them into one numbered
  list where the reader cannot tell which is which at a glance.
- **Documents first** in the rendered source list. Our corpus is the trusted half, and ordering is a
  quiet way of saying so.
- **`retrieved_at` on every web citation.** A web page's content is not stable; a document's version
  in our corpus is.
- **The grounding label from F7 still governs.** If neither the corpus nor the web supported the
  answer, the answer is marked ungrounded. Web results must not become a back door for laundering an
  unsupported answer into a cited-looking one.
- **A mixed answer says so** — "based on 2 internal documents and 1 web source" above the citation
  list. Somebody reviewing an answer needs to know whether the outside world contributed to it.

---

## Reflect

The technical content of this document is small: one container on two networks, one line of Compose
(`internal: true`), a handful of firewall rules, a switch, and a log. The rest is procedure —
canaries, two capture points, a positive control, nine criteria. That ratio is correct. **The
enforcement is easy; knowing it works is the hard part**, and every serious failure of a boundary like
this comes from a configuration that looked right and was never tested end to end.

The part we are least comfortable with is §6.1. Everything else is enforced by the network, which is
to say enforced whether or not our code is correct. The chunk-in-query case is enforced by *our tool
logic*, against an agent we do not fully control, using heuristics that are approximate by
construction. It is the one place where the guarantee degrades from "structurally impossible" to
"caught most of the time" — and it is the one to revisit first if the platform ever handles a corpus
where a single leaked sentence would be serious. The strict answer already exists and is already
supported: turn web search off for that workspace, and the whole class of risk disappears.

**Next:** `17-evaluation.md` and `18-operations.md`.
