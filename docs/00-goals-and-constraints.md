# 00 — Goals & Constraints

> The contract. Everything downstream is measured against this document. Written before any code,
> deliberately, because it's cheaper to argue about a requirement than to refactor around one.

---

## 1. What we're building

A self-hosted AI platform that gives the team the capabilities they'd otherwise get from Claude and
Claude Code, running entirely on hardware we own.

**Capabilities in scope:**

- Chat — general question answering, drafting, analysis
- Agentic coding — in the terminal, and as a VS Code extension
- Multi-agent workflows — an agent that can plan, call tools, and delegate
- Document RAG — grounded answers with citations over our own PDFs and documents
- Web search — so answers aren't limited by a model's training cutoff
- PDF generation
- PPT generation
- Image generation

**Model policy:** open-weight models only, and the user picks which model handles which task.

---

## 2. Functional requirements

| | Requirement |
|---|---|
| **F1** | A user can chat with the assistant through a web UI, with history and their own account |
| **F2** | A user can select which model answers, from a catalog spanning fast → deep |
| **F3** | A developer can run an agentic coding session in the terminal against a local model |
| **F4** | A developer can do the same inside VS Code |
| **F5** | A user can upload documents; the system parses, chunks, embeds and indexes them |
| **F6** | A question about indexed documents returns an answer **with citations** to source document and page |
| **F7** | When the corpus doesn't contain the answer, the response is explicitly marked as **not grounded** rather than silently guessed |
| **F8** | The assistant can search the web and cite what it found |
| **F9** | The assistant can produce a PDF, a PPTX, and an image on request |
| **F10** | Every one of those tools is available from the chat UI, the terminal agent **and** the VS Code extension — the same tool, not three implementations |
| **F11** | A person using a workstation directly can claim its GPU and have the platform get out of the way |

---

## 3. Non-functional requirements

These are the ones with numbers, because "fast" and "secure" can't fail a test.

| | Requirement | How it's verified |
|---|---|---|
| **N1** | **No document text or source code leaves the network.** Search queries may | Packet capture with egress blocked except SearXNG (`16-web-search-and-egress.md`) |
| **N2** | **Zero recurring cost.** No paid API, no per-seat licence, no metered service | Inspect the dependency and service list |
| **N3** | Time to first token **< 2 s** on the fast tier, warm | Benchmarked at 1, 2 and 4 concurrent streams |
| **N4** | Supports **10 seats, 2–4 concurrent generations** without queueing on the fast tier | Load test |
| **N5** | **A direct user of a workstation is never blocked by the platform.** They take as much GPU as they need; the platform runs on the remainder | The toggle tests in `03-gpu-sharing-policy.md` |
| **N6** | The existing long-running simulation runs are **not measurably slowed** | Per-iteration time stays within noise of its ~48 min baseline |
| **N7** | Retrieval quality is **measured, not felt** — recall@5 tracked on a fixed eval set | `17-evaluation.md` |
| **N8** | Every service returns after a host reboot with **no manual intervention** | Reboot test |
| **N9** | Model choice is **swappable** — changing a model is config, not code | Change the catalog entry, restart, no source edits |

### On N5 and N6 — the constraint that shapes everything

These are shared workstations that people already use for real work. The platform is a guest.
Failing N5 or N6 doesn't degrade the platform, it makes the platform unwelcome — and an unwelcome
tool gets switched off. Treat these as harder requirements than any latency target.

---

## 4. Explicit non-goals

Saying what we won't do is as important as what we will:

- **No frontier-parity claim.** The best open-weight models trail the closed frontier. We'll get
  close on the deep tier and be honest about the gap. See `02-hardware-and-fleet.md` §4.
- **No training or fine-tuning.** RAG and tools supply the knowledge.
- **No custom chat UI.** Open WebUI is good and free; our effort goes where it's differentiated.
- **No distributed inference across hosts.** Over 1 GbE it doesn't work — see [ADR-0001](./adr/0001-partition-by-service.md).
- **No OCR of scanned/image PDFs in v1.** Text-based documents only.
- **No public internet exposure.** LAN and VPN only.
- **No guaranteed uptime.** These are workstations, not servers. The platform is best-effort by
  design, and the UI says so.

---

## 5. Constraints we didn't choose

| Constraint | Consequence |
|---|---|
| No data egress | Rules out every hosted model, embedding and database vendor |
| No budget | Rules out new hardware, paid licences, cloud burst capacity |
| Three heterogeneous workstations, not servers | No clustering; partition by service instead |
| `.226` and `.87` run Windows | WSL2, with its documented CUDA caveats |
| `.149` is on a different subnet | Routing and firewall work; may be unreachable |
| Machines are shared and in daily use | The whole of `03-gpu-sharing-policy.md` |
| 24 GB is the largest single GPU | Frontier models only via CPU offload, at reduced speed |

---

## Reflect

The tension in this project is between **ambition** (everything Claude Code does) and **hardware**
(three shared workstations, one of them borrowed). The design resolves it in two ways: assemble
rather than build wherever mature open-source exists, so effort goes only where it's differentiated;
and tier the models so the user chooses speed or quality per task instead of accepting one mediocre
compromise.

The risk that most deserves watching isn't technical — it's adoption. A tool that is slower and
weaker than what people already use gets abandoned unless it's clearly better at something. Here that
something is *our documents, our code, our network*. Keep that the headline.

**Next:** [`01-architecture.md`](./01-architecture.md).
