# ADR-0004 — Search queries may leave; documents and code never do

**Status:** Accepted

## Context

The core justification for self-hosting is that company data must not leave the network. That
constraint alone rules out every hosted model, embedding and vector-database vendor, and it is the
reason this project exists at all.

But two requirements pull against it. F8 asks for web search, and the team wants answers that are
"always up to date" — and no local model is up to date, because its knowledge is frozen at training
time. Currency can only come from tools that reach outside.

A web search inherently transmits the query. If someone asks about an internal project by name, that
name leaves the network. There is no way to search the public web without telling the public web
something.

## Decision

Draw the line at **content, not connectivity**:

- **Search queries may leave** the network, via a self-hosted [SearXNG](https://searxng.org) instance
  that proxies to public engines and fetches result pages on our behalf.
- **Document text, file contents, code, chat history, embeddings and retrieved context never leave.**

Enforcement is at the **network layer, not by convention**: the platform's containers get no general
outbound route. Only SearXNG has an egress path, and only to search engines. Everything else is
default-deny.

Web search is a **switchable module, off by default** per workspace, so a sensitive corpus can run
fully air-gapped.

## Consequences

- **+** Answers can be current, and web results are citable like any other source.
- **+** SearXNG aggregates multiple engines without an API key or account, satisfying N2, and it
  strips referrer and identifying headers by default.
- **+** A single egress point is auditable — one place to log, one place to inspect, one thing to
  disable.
- **+** The invariant is testable rather than asserted: block everything except SearXNG, run a full
  ingest-and-ask cycle, capture traffic, and confirm no document text appears (N1).
- **−** Query text does leave. A question phrased around an internal codename discloses that codename
  to a search engine. This is a real, if narrow, disclosure and must be stated to users plainly.
- **−** The agent decides when to search, so it can leak more than intended through a poorly-formed
  query. Mitigate by logging every outbound query and making the log visible.
- **−** SearXNG's public-engine scraping is fragile; engines rate-limit and change markup, so it needs
  occasional maintenance.

## Alternatives considered

- **No web search at all (strict air-gap)** — the safest option, and still available per workspace.
  Rejected as the default because it forfeits currency entirely, which was an explicit requirement.
- **A commercial search API** (Brave, Tavily, Serper) — better reliability, but violates N2 and adds
  a vendor who sees every query with an account attached to it.
- **A local crawl and index of chosen sites** — genuinely zero-egress and attractive for a fixed set
  of reference sources. Rejected for v1 as significant extra work; worth revisiting if query
  disclosure ever becomes a real concern.
- **Route search through an approved logging proxy** — the governance-heavy version of this decision.
  Adopt it if a security review asks for provable egress control; the design already funnels through
  a single point, so the change is small.
