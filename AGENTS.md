# Working on Understudy

You are picking up an in-progress project. **Nothing about its state is in this
file** — state lives in the repo, and this file tells you where.

### First — what this is

The delivery plan tells you where to resume; it does **not** tell you what you are
building. It is a delivery-mechanics document and says so in its own header. Read
the orientation first or the milestone table will not mean anything:

1. **[`README.md`](README.md)** — what the system is and the problem it solves.
2. **[`docs/00-goals-and-constraints.md`](docs/00-goals-and-constraints.md)** —
   the numbered requirements. Later docs argue by referring to `N1`, `N4`, `N6`;
   without this they read as arbitrary.
3. **[`docs/02-hardware-and-fleet.md`](docs/02-hardware-and-fleet.md)** — three
   workstations, what each is for, and why the 256 GB of system RAM matters more
   than the 4090.
4. **[`docs/03-gpu-sharing-policy.md`](docs/03-gpu-sharing-policy.md)** — **the
   load-bearing document.** The whole project exists to satisfy it. If you read
   only one, read this.
5. **[`docs/01-architecture.md`](docs/01-architecture.md)** and
   [`docs/adr/`](docs/adr/) — the shape, and why each major choice was made rather
   than its alternatives.

### Then — where the work stands

6. **[`docs/delivery-plan.md`](docs/delivery-plan.md) §6** — the milestone table.
   The status column is authoritative: what is done, what is amber and why, what
   has not started. Start from the first row that is not ✅.
7. **[`docs/delivery-plan.md`](docs/delivery-plan.md) §11** — the risk register.
   It says which failure is live right now, not which were feared at design time.
8. **The runbook for the milestone you are resuming**, if one exists:
   [`docs/m1-runbook.md`](docs/m1-runbook.md),
   [`docs/m2-runbook.md`](docs/m2-runbook.md). Each records what actually shipped,
   including where the original plan turned out to be wrong.

Then `git log --oneline -30`. Commit messages here carry the reasoning, not just
the change; they are the fastest way to learn why something is the way it is.

---

## How work happens here

**You cannot reach the hardware.** The three hosts sit inside a campus VPN with
no SSH. Every command runs by hand, in a terminal, by the person you are talking
to — so:

- **Label every command with the host and terminal.** `▸ .226 — Terminal 2`.
  Confusion about which machine a command belongs on has already wasted time more
  than once.
- **One verifiable step at a time.** Give a command, then say what its output
  should look like and what it means if it doesn't.
- **Ask for evidence, not confirmation.** "Send me the output" beats "did it
  work". Several defects were found only because raw output disagreed with what
  everyone assumed.

**The repo carries placeholder addresses.** `10.0.0.x` / `10.0.1.x` are not real.
Real addresses live in `deploy/fleet.local.yaml` and each host's `.env`, both
gitignored. `make check-env` fails the build if a placeholder survived. Never
commit a real address, key, or hostname.

---

## Two things that must not break

**N1 — no document text or source code leaves the network.** Search queries may
(ADR-0004). This is the constraint the whole architecture is shaped around; if a
change would send content outward, stop and raise it.

**The sharing guarantee — the person at the workstation outranks the platform.**
These are not servers. They are machines people use for transportation modelling
every day, and the platform is a guest on them. A change that could leave someone's
job short of VRAM is the one error this codebase must not make. See
[`docs/03-gpu-sharing-policy.md`](docs/03-gpu-sharing-policy.md).

---

## Traps that have already cost hours

Each is documented where it bit. Read the linked section before working in that
area, rather than rediscovering it.

| Trap | Where |
|---|---|
| `internal: true` **silently ignores** `ports:` — container healthy, port never published, `curl` returns `000` | [`m1-runbook.md`](docs/m1-runbook.md) troubleshooting |
| Compose prefers an **exported shell variable** over `--env-file`, so `set -a && . ./.env` makes later edits invisible | [`m1-runbook.md`](docs/m1-runbook.md) troubleshooting |
| A bare IP sends **no SNI**, so Caddy has no certificate to select and the handshake dies | [`m1-runbook.md`](docs/m1-runbook.md) troubleshooting |
| Under WSL2 a host **cannot reach its own LAN address** — hence `REGISTRY=localhost:5000` on the hub | [`deploy/host-87/.env.example`](deploy/host-87/.env.example) |
| Under WSL2 `nvidia-smi` reports **no process names and no per-process memory**, so GPU ownership cannot be decided by name or pid | [`deploy/fleet.yaml`](deploy/fleet.yaml) header |
| A rung's footprint is **chosen, not measured** — vLLM reserves `utilization × TOTAL` up front | [`deploy/fleet.yaml`](deploy/fleet.yaml) header |
| Chunked prefill turns itself **off** below `max_model_len` 32768, taking the KV cache with it | [`deploy/host-226/compose.yaml`](deploy/host-226/compose.yaml) |
| Bare `uv run` **uninstalls** anything not in `pyproject.toml`; use `--with` or the Makefile's `--inexact` | `Makefile` |
| Campus DNS blocks public resolvers, and WSL's stub resolver is unreachable from containers | [`docs/05-host-setup.md`](docs/05-host-setup.md) §5.3 |

---

## Conventions

- **Commits explain why.** A subject line naming the symptom, then a body giving
  the cause and the consequence of not fixing it. `git log` is the project's
  reasoning record.
- **Docs describe what shipped**, not what was planned. When reality diverges,
  correct the doc in the same change — a runbook that lies is worse than none.
- **Never `latest`.** Every image is pinned; rollback is editing the tag back.
- **`make test` before building an image.** 395 tests, seconds to run.
- Python 3.12 · FastAPI · `ruff` · `mypy --strict`. Match the surrounding style.
