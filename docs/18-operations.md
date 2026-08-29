# 18 — Operations

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.

> The runbook. Backups, boot resilience, monitoring, the recurring verifications, rollback, and what
> to do at 9am when something is broken. This document carries the acceptance evidence for **N1**,
> **N6** and **N8** from [`00-goals-and-constraints.md`](./00-goals-and-constraints.md), and it is the
> mitigation for the project's largest non-technical risk (§10).

---

## Concept

## 1. Right-sizing operations for what this actually is

Ten seats. One operator. Three shared workstations that are not servers and never will be.
[`00-goals-and-constraints.md`](./00-goals-and-constraints.md) §4 already says it: **no guaranteed
uptime**, best-effort by design, and the UI says so.

That framing decides everything in this document. Two failure modes are available, and only one of
them is cheap to avoid:

- **Under-operating** — no backups, no boot resilience, no runbook. Recovery from any incident is a
  day of improvisation, and after the second such day people stop relying on the platform.
- **Over-operating** — Prometheus, Grafana, alerting rules, dashboards nobody opens. Weeks spent
  building observability for a system that one person can hold in their head, on a host whose real job
  is Postgres.

The line between them is drawn by one question: **does this help recover, or does it merely help
watch?** Backups, boot resilience, the rollback path and the runbook all help recover, and they all
get built. Metrics history mostly helps watch, so it waits until there is a question it would actually
answer (§4).

Five things are non-negotiable regardless of size, because each of them is unrecoverable if skipped:

| | Why it cannot wait |
|---|---|
| A **tested** restore | An untested backup is a rumour |
| Boot resilience (N8) | A platform that needs a human after every Windows Update is not a platform |
| The egress verification (N1) | It is the project's entire justification |
| The don't-disturb check (N6) | The platform's welcome on `.226` depends on it |
| Complete removability | It is what makes installing this on someone else's workstation politically possible |

---

## Build

## 2. Backups

### 2.1 First decide what is actually precious

Most of the bytes in this system are worthless. Knowing which ones is what keeps the backup small
enough to actually run every night.

| Data | Where | Precious? | Recovery path if lost |
|---|---|---|---|
| User accounts, chat history | Postgres (`.87`) | **Yes** | Backup only. Not reconstructible |
| Document registry, hashes, ingestion status | Postgres (`.87`) | **Yes** | Backup, or re-derive by re-hashing sources |
| **Chunks, embeddings, tsvectors** | Postgres (`.87`) | **No — derived** | Re-ingest from source (§2.4) |
| Source documents | `.87` NVMe #2 | **Yes**, unless they are copies | Original fileshare, if there is one. Confirm this — do not assume |
| Model weights (10–250 GB each) | `.226` 8 TB, `.149` 2 TB | No | Re-download. Slow, not lost. Revisions pinned in `.env` |
| Compose files, migrations, service code | Git | Yes | The repo, mirrored to at least one other machine |
| `.env` files, credentials | Host + password manager | **Yes** | Password manager. **Never** the repo (§9) |
| ComfyUI outputs, container logs | `.149`, `.226` | No | Regenerable / disposable |
| Eval set and results | Git (`eval/`) | Yes | The repo |

**The important line is the third one.** Chunks and embeddings are the bulk of the database and none
of it is original data. Backing them up is convenient; it is not the safety net. §2.4 is.

### 2.2 The nightly job

Nightly `pg_dump` from `.87` to `.226`'s 8 TB NVMe. Seven daily plus four weekly, as
[`delivery-plan.md`](./delivery-plan.md) §9 specifies.

**Why `.226` as the target:** it is a different physical machine with the largest disk in the fleet.
That covers the realistic failure — `.87`'s NVMe dies, or a bad migration eats the database. It does
**not** cover fire, theft, or anything that reaches both machines. Say that out loud rather than
letting anyone assume otherwise; if the corpus is ever business-critical, this needs a genuinely
offsite copy and that is a separate conversation.

```bash
#!/usr/bin/env bash
# scripts/backup-pg.sh  - runs on .87, inside WSL2, as the platform user
set -euo pipefail

STAMP=$(date +%F)
LOCAL=/var/backups/aiplatform
REMOTE_HOST=aiplatform@10.0.0.226
REMOTE_DIR=/mnt/nvme8tb/backups/aiplatform      # verify the actual mount point on .226
DUMP="$LOCAL/aiplatform-$STAMP.dump"

mkdir -p "$LOCAL"

# -Fc = custom format: compressed, and restorable selectively with pg_restore.
# Exclude derived data - it is the bulk of the database and is rebuildable (section 2.4).
docker exec aiplatform-postgres pg_dump \
    -U aiplatform -d aiplatform \
    --format=custom --compress=6 \
    --exclude-table-data='chunks' \
    --exclude-table-data='chunk_embeddings' \
  > "$DUMP"

# Fail loudly if the dump is implausibly small - a truncated dump that "succeeded"
# is the classic way to discover a backup problem only at restore time.
[ "$(stat -c%s "$DUMP")" -gt 1000000 ] || { echo "FAIL: dump under 1 MB"; exit 1; }

# Prove it is readable before shipping it anywhere.
docker exec -i aiplatform-postgres pg_restore --list < "$DUMP" > /dev/null

rsync -a --partial "$DUMP" "$REMOTE_HOST:$REMOTE_DIR/daily/"

# Weekly copy on Sundays
[ "$(date +%u)" = "7" ] && \
  rsync -a "$DUMP" "$REMOTE_HOST:$REMOTE_DIR/weekly/aiplatform-week-$(date +%G-W%V).dump"

# Retention: 7 daily, 4 weekly - enforced on the REMOTE side
ssh "$REMOTE_HOST" "cd $REMOTE_DIR && \
  ls -1t daily/*.dump  | tail -n +8 | xargs -r rm -- && \
  ls -1t weekly/*.dump | tail -n +5 | xargs -r rm --"

echo "backup ok: $DUMP -> $REMOTE_DIR"
```

**Excluding chunk data is a judgement call, not a rule.** It makes the dump small and fast — which is
what makes it survive as a nightly habit — at the cost of a longer recovery (a full re-ingest). If the
corpus is small enough that a full dump still runs in a few minutes, drop the `--exclude-table-data`
lines and take the simpler recovery. **Measure the dump size and duration once, then decide.**

Schedule it with a systemd timer inside `.87`'s WSL2 distro (systemd is already enabled there per
[`delivery-plan.md`](./delivery-plan.md) §5) — not with Windows Task Scheduler, which would need to
reach into WSL for every run:

```ini
# /etc/systemd/system/aiplatform-backup.service
[Unit]
Description=Nightly Postgres backup for the AI platform
[Service]
Type=oneshot
User=aiplatform
ExecStart=/opt/ai-platform/scripts/backup-pg.sh

# /etc/systemd/system/aiplatform-backup.timer
[Unit]
Description=Nightly at 02:30
[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true          # catches up if the host was asleep at 02:30
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now aiplatform-backup.timer
systemctl list-timers aiplatform-backup.timer     # confirm the next run time
journalctl -u aiplatform-backup.service -n 50     # confirm the last run
```

**Two timing traps.** `Persistent=true` matters because these are workstations that get shut down.
And 02:30 writes to `.226`'s disk — the modelling box. Confirm that window is quiet, and see §6:
a backup running during a modelling run is a resource-footprint change and therefore in the N6 blast
radius.

**A failed backup must be noisy.** The worst outcome is a job that has been failing silently for three
weeks. Have the script's non-zero exit surface somewhere you will see it — the fleet dashboard is
already built and already on your screen, so post a last-success timestamp there and show it red past
36 hours.

### 2.3 Test the restore — the part that is usually skipped

**An untested backup is a rumour.** Run this at M8, then quarterly.

```bash
# 1. Pull the most recent dump back from .226 to .87
scp aiplatform@10.0.0.226:/mnt/nvme8tb/backups/aiplatform/daily/$(
    ssh aiplatform@10.0.0.226 'ls -1t /mnt/nvme8tb/backups/aiplatform/daily | head -1'
  ) /tmp/restore-test.dump

# 2. Restore into a THROWAWAY database. Never into aiplatform.
docker exec aiplatform-postgres createdb -U aiplatform aiplatform_restore_test
docker exec -i aiplatform-postgres pg_restore -U aiplatform \
  -d aiplatform_restore_test --no-owner < /tmp/restore-test.dump

# 3. Does it contain what it should?
docker exec aiplatform-postgres psql -U aiplatform -d aiplatform_restore_test -c "
  SELECT 'users' t, count(*) FROM users
  UNION ALL SELECT 'documents', count(*) FROM documents
  UNION ALL SELECT 'chat_messages', count(*) FROM chat_messages;"

# 4. Does the newest data survive? (catches a dump that is silently days stale)
docker exec aiplatform-postgres psql -U aiplatform -d aiplatform_restore_test -c "
  SELECT max(created_at) FROM chat_messages;"

# 5. Clean up
docker exec aiplatform-postgres dropdb -U aiplatform aiplatform_restore_test
```

**Pass:** the restore completes with no errors, row counts are within a day of production, and the
newest message timestamp is from the night of the dump.
**Fail:** anything. Fix it the same day — a broken backup is a silent problem that only announces
itself on the worst possible morning.

**Then run the second half of the drill**, which is the one people skip: point the RAG service at the
restored database and ask **three questions from the eval set**
([`17-evaluation.md`](./17-evaluation.md)). If chunk data was excluded from the dump, this is where
you find out how long the re-ingest actually takes. **Record that duration** — it is your real
recovery time objective, and until you have measured it you do not know it.

### 2.4 The real safety net: idempotent ingestion

[`delivery-plan.md`](./delivery-plan.md) §9 makes ingestion idempotent by document hash. That property
is worth more than the backup, because it means the expensive, bulky part of the database is
**reconstructible on demand**:

```
  source documents (.87 NVMe #2)  --- hash --> already ingested? --> skip
                                                       |
                                                       no
                                                       v
                                          parse -> chunk -> embed -> insert
```

Consequences worth stating explicitly:

- **Re-ingesting the whole corpus is safe and repeatable.** It is not a recovery of last resort, it is
  a normal operation. Run it after any embedding-model change, any chunker change, and any restore
  that excluded chunk data.
- **Source documents are therefore more precious than the database.** Confirm where their originals
  live. If `.87`'s copy *is* the original, back it up too; if it is a mirror of a fileshare, note that
  and skip it.
- **Record the embedding model and dimension in the schema** ([`delivery-plan.md`](./delivery-plan.md)
  §9) so a mismatch after a restore fails loudly at query time rather than silently returning
  nonsense.
- **Measure the full re-ingest time once** and put the number in §11. "We can rebuild it" means very
  different things at 20 minutes and at 9 hours.

---

## 3. Boot resilience (N8)

**N8: every service returns after a host reboot with no manual intervention.** These are workstations.
They get shut down at night, restarted by Windows Update, and power-cycled when someone's job hangs.
A platform that needs a human after each of those events is not a platform — and it fails invisibly,
because nobody reports a service they did not know was supposed to be running.

### 3.1 The chain, per host

```
  .226 / .87   (Windows + WSL2)                 .149  (native Ubuntu)
  ------------------------------------          --------------------------------
  power on                                      power on
    |                                             |
  BIOS: restore on AC power loss                BIOS: restore on AC power loss
    |                                             |
  Windows boots (NO login required)             systemd
    |                                             |
  Task Scheduler: "At startup"  <-- THE TRAP      |
    |                                           docker.service (enabled)
  wsl.exe starts the distro                       |
    |                                           containers: restart unless-stopped
  /etc/wsl.conf systemd=true -> systemd           |
    |                                           v
  docker.service (systemd-enabled)              serving
    |
  containers: restart unless-stopped
    |
    v
  serving
```

Three links on `.226` and `.87`, one of which does not exist by default. Every link must be verified
independently, because a break in any of them looks identical from outside: nothing responds.

### 3.2 Docker restart policy

Every service in every host's compose file:

```yaml
services:
  rag:
    image: 10.0.0.87:5000/rag:0.3.0     # never :latest - see delivery-plan section 4
    restart: unless-stopped
```

**Know what `unless-stopped` actually means.** It restarts a container after a crash and after a
daemon restart — **except** one the operator explicitly stopped, and that exception persists across
reboots. That is the behaviour you want (a service you deliberately stopped during an incident should
not silently come back when the machine reboots), but it is also a way to be surprised. If a service
is missing after a reboot, `docker inspect -f '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}'`
is the first thing to check.

```bash
# Fix an existing container without recreating it
docker update --restart unless-stopped <container>

# Audit every container on a host at once
docker ps -a --format '{{.Names}}' | while read -r c; do
  printf '%-28s %s\n' "$c" "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")"
done
```

**Prefer the Docker engine installed inside the WSL2 distro over Docker Desktop.** Docker Desktop adds
a Windows-side service and, in some configurations, a per-user startup dependency — one more link in
the chain, and one that expects a logged-in session. The engine inside the distro is started by
systemd, which the distro already runs. Verify the behaviour of your Docker Desktop version before
relying on it either way.

### 3.3 The WSL2 trap — the specific thing that breaks N8

**WSL2 does not start at boot.** It starts when something asks for it: a terminal, an `explorer.exe`
click, a `wsl.exe` invocation. Nothing inside the distro runs until then — not systemd, not Docker,
not a single container, however diligently their restart policies are set.

So `.226` and `.87` reboot, Windows comes up, everything looks normal, and the platform is simply
absent. There is no error anywhere. This is the single most likely way N8 fails, and it fails silently.

**The fix is a Windows Task Scheduler task per host.** With `systemd=true` in `/etc/wsl.conf`, merely
starting the distro is enough — systemd then brings up Docker, and Docker brings up the containers.

First confirm the prerequisites inside the distro:

```bash
cat /etc/wsl.conf        # expect: [boot] \n systemd=true
systemctl is-enabled docker    # expect: enabled
```

Then, in an **elevated** Windows PowerShell on `.226` and again on `.87`:

```powershell
# Keep the distro alive for the life of the session; systemd inside it does the rest.
$action  = New-ScheduledTaskAction -Execute 'C:\Windows\System32\wsl.exe' `
                                   -Argument '-d Ubuntu -u root -e sleep infinity'
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT60S'          # let networking settle before the distro comes up
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount `
                                        -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName 'AIPlatform-WSL-Boot' -Action $action `
  -Trigger $trigger -Principal $principal -Settings $settings -Force

Start-ScheduledTask -TaskName 'AIPlatform-WSL-Boot'          # test it now
Get-ScheduledTaskInfo -TaskName 'AIPlatform-WSL-Boot'        # LastTaskResult should be 0
```

**Four things that will bite you here, in the order they usually do:**

1. **Running as `SYSTEM` starts the distro in session 0.** Verify this works on your Windows and WSL
   versions before trusting it — WSL's behaviour under a non-interactive service account has changed
   across releases. The alternative is to run the task as the platform user with *"Run whether user is
   logged on or not"* and a stored password (which then lives in the password manager, §9). Test the
   one you choose by rebooting, **not** by running the task from an interactive session.
2. **Fast Startup.** Windows Fast Startup makes a shutdown a hybrid hibernate, so "At startup"
   triggers may not fire the way you expect. Disable it: `powercfg /h off` (this also disables
   hibernation — fine on these machines, but confirm nobody relies on it).
3. **Sleep.** A workstation that suspends takes the platform with it. Set the power plan so the
   machine never sleeps, though the display may. `powercfg /change standby-timeout-ac 0`.
4. **`networkingMode=mirrored`** ([`delivery-plan.md`](./delivery-plan.md) §5) is what makes the host
   IP serve LAN clients. Verify after every reboot that the services are reachable *from another
   machine*, not just from localhost. A service that binds correctly but is unreachable across the
   network passes every local check and is still down for the users.

Also set **"restore on AC power loss"** in the BIOS of all three hosts. It costs one reboot to
configure and converts a power blip from an outage into a two-minute gap.

### 3.4 `.149` — native Ubuntu, the easy one

This is one of the quieter benefits of the native-Ubuntu decision in
[`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) §1: no WSL layer, so no trap.

```bash
sudo systemctl enable docker           # not enabled by default on every distro - check
sudo systemctl is-enabled docker
# containers carry restart: unless-stopped, so nothing else is needed
```

Confirm the machine is not set to suspend, and that it is actually left powered on — an M0 open item
that is still worth re-confirming, since a box someone switches off at night is a box that fails N8
without anything being wrong with it.

### 3.5 The verification — reboot every host

Not a thought experiment. Actually reboot them, at M8 and after any change to the boot chain.

```bash
#!/usr/bin/env bash
# scripts/healthcheck.sh - one command that answers "is the platform up?"
set -u
fail=0
check() {  # name url expected-substring
  if curl -fsS --max-time 10 "$2" 2>/dev/null | grep -q "$3"; then
    printf 'OK    %s\n' "$1"
  else
    printf 'FAIL  %s  (%s)\n' "$1" "$2"; fail=1
  fi
}

check "gateway (.87)"        http://10.0.0.87:4000/health          ""
check "gateway catalog"      http://10.0.0.87:4000/v1/models       "qwen"
check "open webui (.87)"     http://10.0.0.87:8080/health          ""
check "embeddings (.87)"     http://10.0.0.87:7997/health          ""
check "rag service (.87)"    http://10.0.0.87:8100/health          "ok"
check "mcp tools (.87)"      http://10.0.0.87:8200/health          "ok"
check "fleet ctrl (.87)"     http://10.0.0.87:8300/api/fleet       "226"
check "searxng (.87)"        http://10.0.0.87:8888/healthz         ""
check "vllm (.226)"          http://10.0.0.226:8000/health         ""
check "comfyui (.226)"       http://10.0.0.226:8188/system_stats   ""

# Postgres needs a real query, not a port check
docker exec aiplatform-postgres pg_isready -U aiplatform >/dev/null 2>&1 \
  && echo "OK    postgres" || { echo "FAIL  postgres"; fail=1; }

# An end-to-end assertion beats ten component checks
curl -fsS --max-time 30 -X POST http://10.0.0.87:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-14b","messages":[{"role":"user","content":"say OK"}],"max_tokens":5}' \
  >/dev/null && echo "OK    end-to-end generation" \
             || { echo "FAIL  end-to-end generation"; fail=1; }

exit $fail
```

**The reboot test.** Run it once per host, then once with all three down together:

| Step | Action | Pass criterion |
|---|---|---|
| 1 | Baseline: `scripts/healthcheck.sh` from a machine that is not in the fleet | All OK |
| 2 | Reboot `.87`. **Touch nothing.** Do not open a terminal on it | All OK within 5 min |
| 3 | Reboot `.226`. Touch nothing | All OK within 5 min (allow longer for a cold model load) |
| 4 | Reboot `.149`. Touch nothing | All OK within 5 min |
| 5 | Power all three off, then on together | All OK within 10 min |
| 6 | Simulate a power cut: hard power-off `.87`, restore power | Comes back; Postgres recovers cleanly, no corruption in `docker logs` |

**Pass (N8):** every step reaches all-OK with **zero keystrokes beyond the power button**. Opening a
terminal on a WSL host counts as intervention — it is the exact thing that masks the §3.3 trap, and
it is how this test gets accidentally passed.

**Record the time-to-green** for each host in §11. It is the number you will quote when someone asks
how long an unplanned reboot costs, and it is a regression signal in its own right.

---

## 4. Monitoring — deliberately minimal, and why

### 4.1 What ships at M8

| Tool | Answers | Already exists? |
|---|---|---|
| **Fleet dashboard** ([`03`](./03-gpu-sharing-policy.md) §4.1) | Which host is claimed, which rung is loaded, live VRAM | Yes — built at M2 |
| `scripts/healthcheck.sh` | Is everything up, right now | §3.5 |
| `docker compose ps` / `docker logs` | What broke and what it said | Built in |
| Backup freshness on the dashboard | Did last night's backup run | §2.2 |
| **Free disk on all three hosts** | The failure that takes everything down at once | Add to healthcheck |
| Structured app logs (§4.3) | The four things you will actually need | Build into the services |

Run `healthcheck.sh` from cron every five minutes, write the result where the fleet dashboard can
render it, and show a red banner when anything has been failing for more than two consecutive runs.
That is the whole monitoring stack, and for ten users and one operator it is close to sufficient.

### 4.2 The argument against Prometheus and Grafana on day one

Standing up node_exporter, DCGM exporter, Prometheus and Grafana is maybe half a day and it is
genuinely tempting, because it looks like operational maturity. Four reasons to wait:

1. **Metrics help you watch; they rarely help you recover.** When RAG is down, the fix comes from
   `docker logs` and the runbook in §8, not from a graph. Everything in §1's non-negotiable list is
   recovery. Metrics are not.
2. **It costs the hub.** Four more containers on `.87`, whose actual job is Postgres, the gateway, the
   reranker and three of our services. Prometheus with default retention is also a steady disk
   consumer on the box whose disk exhaustion is a top-eight failure (§8.4).
3. **Alerting needs a recipient.** With one operator there is no rotation, so alert rules mostly page
   the person who is already looking. At ten seats the real alerting channel is a colleague saying
   "hey, is the chat thing down?" — which is fast, reliable and free.
4. **Unwatched dashboards are theatre.** A Grafana that nobody opens for six weeks provides the
   *feeling* of observability and none of the substance, and it will be stale exactly when it matters.

### 4.3 Log these four things from day one regardless

Cheap, tiny, and each one is something you will otherwise wish you had:

| Log | Why |
|---|---|
| **Every outbound SearXNG query**, with user and timestamp | Required by [ADR-0004](./adr/0004-egress-policy.md): "log every outbound query and make the log visible." It is the audit trail for the one thing that does leave |
| **Every ingestion failure**, with document hash and the exception | Silent ingestion failures are how a corpus quietly develops holes, and the eval set will not catch a document that was never there |
| **Every relevance-gate refusal**, with the question and top score | The recalibration input for [`17-evaluation.md`](./17-evaluation.md) §6, and the source of new eval questions |
| **Every rung change**, with measured free VRAM and trigger | The only way to debug a flapping ladder or an unexplained quality drop |

### 4.4 When to actually add the metrics stack

Add it when you have a **specific question that history would answer** and you have wanted the answer
more than once:

- "Was it slow yesterday afternoon, or is that person misremembering?"
- "Is the 4090 thermally throttling during long deep-tier runs?"
- "Is Postgres growing linearly with the corpus, or worse?"
- "How much of the day is `.226` actually claimed?" — which would tell you whether
  [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md)'s "30–60 minute blocks, a few hours a day"
  assumption survived contact with reality.

That last one is the most likely trigger, and it is a good one — it feeds directly back into the
sharing policy. When you get there: node_exporter and NVIDIA DCGM exporter on all three hosts,
Prometheus and Grafana on `.87`, **retention capped at 15–30 days**, and a Postgres exporter only if
database questions are what drove you here. Nothing else. Adding one exporter to answer one question
is operations; adding a whole observability platform because it is best practice is a second project.

---

## 5. Egress verification (N1) — a recurring check, not a one-off

**N1 is the project's entire justification.** It is also the requirement most likely to be verified
once at M8, ticked off, and quietly broken three months later — because it does not degrade, it
*breaks*, and it breaks from ordinary changes:

- A new container added to a compose file, inheriting a default bridge network with a route out.
- A dependency that phones home for telemetry or update checks on start.
- A model download path left enabled after the weights were fetched.
- A firewall rule loosened during an unrelated debugging session and never restored.
- A WSL or Docker upgrade that resets networking defaults.

So: **verify at M8, after any change that adds or upgrades a container, and quarterly regardless.**

### 5.1 The enforcement being verified

Per [ADR-0004](./adr/0004-egress-policy.md): default-deny outbound for platform containers; only
SearXNG has an egress path. Enforced at the network layer, not by convention.

```bash
# Put every platform service on an internal network with no gateway to the outside.
# SearXNG gets its own network that does have one.
docker network create --internal aiplatform-internal
docker network create aiplatform-egress
```

```yaml
# deploy/host-87/compose.yaml  (shape)
networks:
  internal: { external: true, name: aiplatform-internal }
  egress:   { external: true, name: aiplatform-egress }

services:
  rag:      { networks: [internal] }      # no route out, by construction
  mcp:      { networks: [internal] }
  litellm:  { networks: [internal] }
  searxng:  { networks: [internal, egress] }   # the ONLY container on both
```

`--internal` is doing the real work here: Docker gives that network no external route at all, so it is
structural rather than a rule someone can forget to reapply. Back it with an explicit host firewall
default-deny as well, since containers are not the only thing on these machines.

### 5.2 The verification run

```bash
# --- 1. Plant a canary in a document nobody would ever search for by accident
CANARY="ZZQX-CANARY-$(date +%s)-DO-NOT-INDEX-ELSEWHERE"
echo "$CANARY" >> /srv/corpus/canary-test.txt      # then ingest it normally

# --- 2. Start capturing on the host's real external interface, for the whole run
sudo tcpdump -i eth0 -s 0 -w /tmp/egress-$(date +%F).pcap \
     'not net 10.0.0.0/24 and not net 10.0.1.0/24 and not port 22'

# --- 3. In another shell: exercise EVERY path that touches data
#   a. ingest the canary document
#   b. ask the RAG service a question that retrieves it
#   c. ask a question that triggers a web search (the one permitted egress)
#   d. generate a PDF and a PPTX from document content
#   e. generate an image
#   f. run a real OpenCode session over a source file
#   g. let a nightly ingestion batch run

# --- 4. Stop the capture. Then interrogate it.
# 4a. Does the canary appear anywhere in plaintext?
grep -a "$CANARY" /tmp/egress-*.pcap && echo "*** N1 FAILED ***" || echo "canary not in plaintext"

# 4b. STRONGER TEST: what external hosts were contacted at all?
tshark -r /tmp/egress-*.pcap -T fields -e ip.dst \
  | sort -u | while read -r ip; do printf '%-16s %s\n' "$ip" "$(dig +short -x "$ip")"; done
```

### 5.3 The caveat that makes 4b the real test

**Grepping a capture for plaintext proves very little on its own.** Anything exfiltrating over TLS
looks like noise, and every meaningful destination speaks TLS. The plaintext grep catches
misconfiguration — a service posting to an HTTP endpoint, a debug hook, an unencrypted telemetry
beacon — and that is worth having, but it is not proof.

**The control is the deny rule. The capture verifies the deny rule is intact.** So the assertion that
matters is 4b: *every external destination contacted during the run must be on the allowlist.* If the
only external IPs in the capture resolve to search engines that SearXNG legitimately queries, the
boundary held — regardless of what was inside those packets. One unexplained destination is a finding
even if the payload is unreadable, and it is exactly the finding you are looking for.

Complement the capture with a live view during the run:

```bash
# Established outbound connections from the host, excluding the local subnets
ss -tanp state established | grep -Ev '10\.72\.(32|19)\.' 

# And from inside a container that must NEVER reach out - this should FAIL
docker exec aiplatform-rag curl -sS --max-time 5 https://example.com \
  && echo "*** N1 FAILED: rag container has egress ***" \
  || echo "rag container correctly has no egress"
```

That last one is the cheapest possible regression test, it takes two seconds, and it should be run
after **every** deploy — not quarterly. Add it to `healthcheck.sh` as an inverted check.

**Pass (N1):** the canary appears in no capture; every external destination is on the allowlist and
attributable to SearXNG; the direct container egress probe fails as designed.
**Fail:** any unexplained destination. Treat it as an incident: identify the container, cut its
network, and do not resume until it is explained.

**A limitation to state plainly:** this verifies the *platform's* egress. It does not stop a user
copying a document into a browser tab. N1 is a technical control on the system, not on people, and it
should be described to users that way.

---

## 6. The don't-disturb check (N6) — recurring

[`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) §7 test 8 defines it: *a full modelling run
alongside the platform under load; per-iteration time stays within noise of its ~48 min baseline.*

**This is a recurring check, not an M2 acceptance test**, because the thing it measures is the
platform's resource footprint — and that footprint changes constantly as milestones land. It is also
the check on which the platform's welcome depends: [`00`](./00-goals-and-constraints.md) §3 says to
treat N5 and N6 as harder requirements than any latency target.

### 6.1 Establish the baseline's own variance first

You cannot say "within noise" until you know what the noise is. **Measure the baseline before
measuring the effect**, with the platform fully stopped:

```bash
# On .226, with the platform down
docker compose -f deploy/host-226/compose.yaml down
# Run the modelling job and record per-iteration wall time for at least 3 iterations
```

| Run | Iteration | Wall time | Notes |
|---|---|---|---|
| baseline | 1 | | platform stopped |
| baseline | 2 | | |
| baseline | 3 | | |
| | **mean / spread** | | **this spread is "noise"** |

Three iterations at ~48 minutes is about two and a half hours. That is the price of having a defensible
number instead of an impression, and it only has to be paid once per significant hardware or job
change.

### 6.2 Then measure under load

Bring the platform back up and generate a **realistic worst case** — not an idle platform, which
proves nothing:

```bash
docker compose -f deploy/host-226/compose.yaml up -d

# 4 concurrent fast-tier streams for the duration of a full iteration
python eval/bench_latency.py --model qwen3-coder-30b-a3b --concurrency 4 --duration 3600 &

# an ingestion batch (CPU + embeddings + Postgres, mostly on .87 but it shares the 1 GbE)
python -m rag.ingest --path /srv/corpus/backlog &

# a RAG query loop
python eval/run_retrieval_eval.py --loop &
```

| Run | Iteration | Wall time | Delta vs baseline mean | Within baseline spread? |
|---|---|---|---|---|
| under load | 1 | | | |
| under load | 2 | | | |
| under load | 3 | | | |

**Pass (N6):** per-iteration time under load stays within the baseline's own run-to-run spread.
**Fail:** a consistent shift beyond that spread. Respond in this order — tighten `.wslconfig` CPU and
memory caps on `.226`; move the reranker off `.226` entirely (it should already be on `.87`); gate the
deep tier to off-hours ([`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) §4); and if none of
that is enough, reduce what `.226` runs. **Never respond by asking the modelling job's owner to
accommodate the platform.** The platform is the guest.

**Do not invent a percentage tolerance.** "Within 5%" sounds rigorous and is meaningless if the job's
own variance is 8%. The baseline spread is the tolerance.

### 6.3 Re-run triggers

Every one of these changes the platform's resource footprint on `.226`:

| Trigger | Why it matters |
|---|---|
| Deep tier enabled or its `--n-cpu-moe` split changed | Memory-bandwidth contention, which the ladder cannot solve ([`03`](./03-gpu-sharing-policy.md) §6) |
| `.wslconfig` CPU or memory caps changed | The direct control on CPU starvation |
| vLLM version or `--gpu-memory-utilization` changed | Its semantics have moved between versions ([`02`](./02-hardware-and-fleet.md) §2) |
| A new service placed on `.226` | Any new resident consumer |
| The nightly backup's target or schedule changed | It writes to `.226`'s disk (§2.2) — IO contention in a window you assumed was quiet |
| A new modelling job type, or a change to the existing one | The baseline itself has moved; re-measure §6.1 |
| Ladder rungs changed | Different resident model, different footprint |

Put the last-run date in §11 and treat anything older than a quarter as unverified.

---

## 7. Rollback and removability

### 7.1 The rollback table

Expanded from [`delivery-plan.md`](./delivery-plan.md) §10 with the actual commands.

| Scenario | Action | Time |
|---|---|---|
| Bad service release | Previous image tag | ~30 s |
| Bad migration | Alembic downgrade, then previous tag | ~2 min |
| Bad model choice | Change the catalog entry, restart. **No code change (N9)** | ~1 min + load |
| Platform disturbing a user | Stop that host's stack; the gateway routes elsewhere | ~10 s |
| Everything wrong | `docker compose down` on all three | ~1 min |

```bash
# Bad release - this is why delivery-plan section 4 forbids :latest
$EDITOR deploy/host-87/.env          # RAG_IMAGE_TAG=0.3.0 -> 0.2.9
docker compose -f deploy/host-87/compose.yaml up -d rag
docker compose -f deploy/host-87/compose.yaml logs -f rag

# Bad migration - downgrade FIRST, then the image, so the schema is never
# newer than the code that is running against it
docker compose -f deploy/host-87/compose.yaml run --rm rag alembic downgrade -1
$EDITOR deploy/host-87/.env
docker compose -f deploy/host-87/compose.yaml up -d rag
```

**On migrations.** [`delivery-plan.md`](./delivery-plan.md) §9 applies them as an explicit deploy step,
never on service start, precisely so a rollback cannot silently migrate forward. Two additions:

- **Write and test the `downgrade()` when you write the `upgrade()`**, not when you need it.
- **Some migrations are genuinely irreversible** — a dropped column is dropped. Say so in the
  migration's docstring, in capitals, and take a `pg_dump` immediately before applying one. An
  irreversible migration is a one-way door and it should be documented as one.

**On model swaps.** Verified by the N9 test in [`17-evaluation.md`](./17-evaluation.md) §8.4. Rolling
back a model is editing `.env` back and restarting; there is no code path involved, which is the whole
point of the requirement.

### 7.2 Removability — the property that makes this politically possible

From [`delivery-plan.md`](./delivery-plan.md) §10: *the platform must be fully removable in one command
per host, and nothing it does may leave the machines worse than it found them.*

This is not a nice-to-have. Two of these three machines belong, in practice, to other people's work.
The reason it is acceptable to install a platform on someone's workstation is that it can be removed
completely, on demand, with no argument and no residue. **If that ever stops being true, the social
contract in [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) is broken regardless of how well
the toggle works.**

Because "remove it" must be executable by someone who is annoyed and in a hurry, write it down:

```bash
# --- Level 1: stop. Reversible in seconds. This is the one people will ask for.
docker compose -f deploy/host-226/compose.yaml down

# --- Level 2: remove containers and images, keep data
docker compose -f deploy/host-226/compose.yaml down --rmi all

# --- Level 3: full uninstall of the platform's footprint on this host
docker compose -f deploy/host-226/compose.yaml down -v --rmi all   # -v drops named volumes
docker network rm aiplatform-internal aiplatform-egress 2>/dev/null
rm -rf /opt/ai-platform /var/backups/aiplatform
rm -rf /mnt/nvme8tb/models/aiplatform          # model weights - hundreds of GB
```

And the parts that are **not** inside Docker, which is exactly what gets forgotten:

| Residue | Remove with | Host |
|---|---|---|
| `AIPlatform-WSL-Boot` scheduled task | `Unregister-ScheduledTask -TaskName 'AIPlatform-WSL-Boot'` | `.226`, `.87` |
| `.wslconfig` CPU/memory caps | Restore the pre-platform file — **keep a copy of the original** | `.226`, `.87` |
| `powercfg /h off`, sleep settings | Restore the original power plan | `.226`, `.87` |
| Host firewall rules for egress lockdown | Remove the rules; restore the previous default policy | all |
| `gpu-run` shell alias / wrapper | Remove from the shell profile and `/usr/local/bin` | all |
| Backup files on `.226`'s 8 TB | `rm -rf /mnt/nvme8tb/backups/aiplatform` | `.226` |
| Model weights | The largest thing by far — check both NVMes | `.226`, `.149` |
| BIOS "restore on AC power loss" | Only if it was off before. Note the original setting | all |
| Docker itself | Leave it if it predates the platform; note whether the platform installed it | all |

**Keep a `PRE-PLATFORM/` directory on each host** containing the original `.wslconfig`, power plan
export and firewall rules, captured *before* the platform was installed. Restoring a machine is then
copying files back rather than remembering what it used to look like. Do this at M1 host setup; it
takes five minutes and it is unrecoverable later.

**Verify removability once, at M8.** On the least contended host, run the level-3 uninstall, confirm
the machine is a plain workstation again, and reinstall from the repo. That drill proves two things at
once: the uninstall is complete, and the install is reproducible from the docs.

---

## 8. Common failures runbook

For each: symptom, first check, fix, prevention. Add to this list every time something breaks — a
runbook that stops growing is a runbook that is going stale.

### 8.1 A host is unreachable

**Symptom:** `healthcheck.sh` fails for everything on one host; the fleet dashboard shows it offline.

```bash
ping -c 3 10.0.0.226
ssh aiplatform@10.0.0.226 'uptime'          # is it the network or the host?
```

| Finding | Cause | Fix |
|---|---|---|
| Ping fails, host is on | Network, or WSL mirrored networking lost after a reboot | §8.7 |
| Ping fails, host is off | Powered down, asleep, or a power cut | Power on; check sleep settings (§3.3) and BIOS AC restore |
| Ping OK, SSH refused | WSL not started | §8.7 |
| `.149` only | **Different subnet** — routing or firewall | Re-run M0 spike 4. `.149` dropping out is a known degraded mode, not an outage |
| `uptime` shows a recent boot | Windows Update rebooted it | The boot chain (§3) should have handled this. If it did not, §8.7 |

**The platform is designed to survive this.** The gateway reroutes; `.149` offline costs image
generation, `.226` offline drops chat to `.87`'s small model. Confirm the degradation is graceful,
then fix the host — do not treat one host down as an outage.

### 8.2 A GPU is claimed and not released

**Symptom:** the dashboard shows a host held for hours; the platform sits on a low rung or nothing.

```bash
ssh aiplatform@10.0.0.226 'nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv'
curl -s http://10.0.0.87:8300/api/fleet | jq '.hosts[] | select(.id=="226")'
```

| Finding | Cause | Fix |
|---|---|---|
| A real CUDA process is running | Working as designed | Nothing. Someone is using their machine |
| No CUDA process, toggle held | Someone forgot to flip it back | The ~30 min auto-release ([`03`](./03-gpu-sharing-policy.md) §4.2) should handle it. If it did not, the auto-release is broken — fix that, then release manually |
| No CUDA process, no toggle, platform still low | Controller stuck, or stale state | Restart the fleet controller. Check hysteresis (§4.6 of `03`) is not holding it down |
| Zombie process holding VRAM | Crashed job that never released | Confirm with the machine's owner **before** killing anything. Never kill a foreign process unasked |

**Prevention:** the auto-release exists precisely because forgetting is the expected failure. If it
fires often, that is the system working. If it never fires, verify it still works.

### 8.3 A model fails to load

**Symptom:** the gateway reports the model unavailable; vLLM logs an error at startup.

```bash
docker compose -f deploy/host-226/compose.yaml logs --tail=100 vllm
nvidia-smi --query-gpu=memory.used,memory.free --format=csv
df -h /mnt/nvme8tb
```

| Log says | Cause | Fix |
|---|---|---|
| CUDA out of memory | Rung too high for the free VRAM, or `--gpu-memory-utilization` semantics changed between vLLM versions ([`02`](./02-hardware-and-fleet.md) §2) | Lower the rung; re-pin the flag's meaning for your version |
| Unsupported quantisation / unknown format | GGUF given to vLLM ([`02`](./02-hardware-and-fleet.md) §2, "two traps") | Use an AWQ/GPTQ/FP8 build, or serve it via `ik_llama.cpp` instead |
| No such file / revision mismatch | Weights not on this host, or the pinned revision changed | Re-download to local NVMe. **Never** pull weights over the 1 GbE link during a deploy ([`delivery-plan`](./delivery-plan.md) §4) |
| No kernel image for device | `sm_120` missing on `.149` (M0 spike 3) | Upgrade the PyTorch/vLLM build. Do not attempt `TORCH_CUDA_ARCH_LIST` workarounds |
| Disk full | Weights partially written | §8.4 |

**While it is broken:** the gateway's fallback should already have routed elsewhere. If it did not,
that is a second bug and it is more urgent than the first.

### 8.4 Postgres full, or a disk full

**Symptom:** writes fail; ingestion errors; in the worst case Postgres refuses to start.

```bash
df -h                                              # on .87 - check BOTH NVMes
docker exec aiplatform-postgres psql -U aiplatform -d aiplatform -c "
  SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
  FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"
docker system df                                   # images and volumes are usually the surprise
```

| Cause | Fix |
|---|---|
| Docker images and build cache | `docker image prune -a`, `docker builder prune`. Usually reclaims the most, fastest |
| Container logs unbounded | Set `logging: { driver: json-file, options: { max-size: 50m, max-file: 3 } }` on **every** service. Do this before it happens |
| `chunk_embeddings` growth | Expected as the corpus grows. Check the HNSW index size too — it is not small |
| Old backups | Retention should cap this (§2.2). If it did not, the retention step is failing |
| WAL growth | Check for a stuck replication slot or a long-running transaction |

**Prevention:** free disk is the highest-value single check in the whole system, because disk
exhaustion takes down every service at once and can corrupt the thing you most want to recover. Put
`df` thresholds in `healthcheck.sh` on day one, at 80% warn and 90% red.

### 8.5 The embeddings server is down — RAG stops entirely

**Symptom:** every RAG query fails; ingestion stalls. Chat still works, which makes it easy to
misdiagnose as "RAG is broken" rather than "one dependency is down".

```bash
curl -s http://10.0.0.87:7997/health
docker compose -f deploy/host-87/compose.yaml logs --tail=50 infinity
nvidia-smi   # on .87 - is there room for the ~1.2 GB?
```

**This is the single most load-bearing dependency in the RAG path.** Both ingestion and every query go
through it, and there is no cache that makes a query work without it.

| Cause | Fix |
|---|---|
| `.87` GPU claimed, embeddings evicted | **The CPU fallback should have engaged** ([`03`](./03-gpu-sharing-policy.md) §3). If it did not, that is the bug — the fallback exists so this outcome is impossible |
| Container crashed | `restart: unless-stopped` should have restarted it. Check the exit reason before assuming it is transient |
| OOM on `.87` | Something else grew. Check what else is resident |

**Verify the CPU fallback works, deliberately, at M8.** It is the mitigation for the highest-impact
single failure in the system, and an untested fallback is in the same category as an untested backup.

### 8.6 The gateway is out of sync with the fleet controller

**Symptom:** the gateway routes to a model that is no longer loaded; requests fail or hang; the
dashboard and the catalog disagree.

```bash
curl -s http://10.0.0.87:4000/v1/models | jq -r '.data[].id'      # what the gateway thinks
curl -s http://10.0.0.87:8300/api/fleet | jq '.hosts[].loaded'    # what is actually loaded
```

| Cause | Fix |
|---|---|
| Controller updated the ladder but did not notify the gateway | Restart the controller; check the notify path and its error handling |
| Gateway restarted and lost dynamic state | The controller must re-push state on gateway reconnect, not only on change. If it does not, that is a design bug worth fixing properly |
| A rung change raced an in-flight request | Expected occasionally. The gateway should retry to a fallback rather than fail the user |

**This is the seam most likely to rot**, because it is the only place where two services we built must
agree about the world. Make the controller's push idempotent and re-pushed on a timer as well as on
change, so drift self-heals within a minute instead of requiring a restart.

### 8.7 WSL2 did not start after a reboot

**Symptom:** a Windows host is up and pingable, but nothing on it responds. No error anywhere. This is
the §3.3 trap, and it is the most likely N8 failure.

From Windows PowerShell on the affected host:

```powershell
wsl.exe --list --running                                  # is the distro even running?
Get-ScheduledTaskInfo -TaskName 'AIPlatform-WSL-Boot'     # LastRunTime, LastTaskResult (0 = ok)
Get-ScheduledTask -TaskName 'AIPlatform-WSL-Boot' | Select-Object State
```

| Finding | Cause | Fix |
|---|---|---|
| Distro not running, task never ran | Task disabled, or Fast Startup suppressed the startup trigger | Re-enable the task; `powercfg /h off`; re-run the §3.5 reboot test |
| Task ran, `LastTaskResult` non-zero | Session-0 / service-account problem (§3.3 trap 1) | Switch to "run whether user is logged on or not" with stored credentials, or investigate the specific error |
| Distro running, Docker not | `docker.service` not enabled, or `systemd=true` missing from `/etc/wsl.conf` | `systemctl enable --now docker`; fix `wsl.conf`; `wsl --shutdown` and retest |
| Docker running, containers not | Restart policy is `no`, or they were manually stopped (§3.2) | `docker update --restart unless-stopped`; fix the compose file so it survives a recreate |
| Everything running, unreachable from the LAN | `networkingMode=mirrored` not applied, or services bound to `127.0.0.1` | Check `.wslconfig`; bind `0.0.0.0`. **Always test from another machine** |

**Immediate workaround:** `wsl.exe -d Ubuntu -e true` starts the distro and systemd brings the rest up.
Then fix the cause — do not leave a host that needs a manual poke after every reboot, because you will
not be there for the next one.

### 8.8 SearXNG stopped returning results

**Symptom:** web search returns nothing or errors; everything else is fine.

```bash
docker compose -f deploy/host-87/compose.yaml logs --tail=50 searxng
curl -s 'http://10.0.0.87:8888/search?q=test&format=json' | head
```

Expected maintenance, not a failure: [`tech-stack.md`](./tech-stack.md) §6 flags that public-engine
scraping is fragile and engines rate-limit and change markup. Update the SearXNG image, disable the
engines that are failing, and move on. **Web search is a switchable module**
([ADR-0004](./adr/0004-egress-policy.md)) — turning it off is a legitimate response, and the MCP tool
should degrade to a clear "search unavailable" rather than an opaque error.

---

## 9. Secrets hygiene

**The rule, from [`README.md`](./README.md):** credentials never go in this repo. If you find one
committed, rotate it.

| Secret | Lives in | Never in |
|---|---|---|
| Host / workstation passwords | Password manager | The repo, chat, a doc, a comment |
| `LITELLM_MASTER_KEY` | `.env` on `.87`, generated per environment | Compose files, image layers, this folder |
| Postgres password | `.env` on `.87`, mirrored to the password manager | Anything committed |
| Open WebUI / auth secrets | `.env` on `.87` | Anything committed |
| Per-user API keys | Issued by LiteLLM, stored by the user | A shared file |
| Scheduled-task account password (§3.3) | Password manager | The task's XML export, which then must not be committed |

**Mechanics:**

- `.env` is gitignored; `.env.example` is committed with placeholder values and every key present, so
  a new host setup cannot silently omit one.
- **Never bake a secret into an image.** It persists in the layer even if a later layer deletes it, and
  the local registry on `.87` keeps every layer.
- **Add a pre-commit guard.** A `gitleaks` hook, or at minimum a grep for the obvious shapes. It costs
  nothing and catches the paste-into-the-wrong-file mistake, which is how this almost always happens.
- **Postgres exposure is real.** [`delivery-plan.md`](./delivery-plan.md) §3 has you developing from
  your own machine against `10.0.0.87:5432`. Restrict `pg_hba.conf` and the firewall to the specific
  developer address and the LAN subnets — not `0.0.0.0/0` — and use a separate, lower-privileged role
  for the `_dev` database.

**Rotation:**

| Trigger | Action |
|---|---|
| **Now** | [`delivery-plan.md`](./delivery-plan.md) §8 records three host passwords pasted in plaintext. Rotate them. This is outstanding work, not a hypothetical |
| A secret reaches the repo | Rotate immediately. **Deleting the commit is not enough** — assume it is compromised the moment it is pushed |
| Someone leaves, or the operator changes | Rotate everything, and re-share via the password manager |
| Annually | Rotate `LITELLM_MASTER_KEY`, the Postgres password, and the auth secrets. Schedule it as a §11 item |

---

## 10. Bus factor — one operator

**Say it plainly: this platform has exactly one person who knows how to run it.** Ten people will come
to depend on it, on three machines that belong to other people's daily work, and there is no second
operator.

[`delivery-plan.md`](./delivery-plan.md) §11 lists it as a risk and gives the mitigation in one line:
**the docs are the mitigation. Keep them current.** That is not a slogan. It is the only mitigation
available, and it is the reason `docs/` is treated as a deliverable of every milestone rather than
something written afterwards if there is time.

**What "keep them current" means concretely:**

1. **Every milestone's doc is revised after the milestone ships**, describing what actually shipped —
   including this document and [`17-evaluation.md`](./17-evaluation.md), both of which are pre-build
   drafts today and both of which will be wrong in specifics.
2. **Every manual fix that is not already in §8 becomes a §8 entry the same day.** If you had to think
   about it, the next person will have to think about it for longer. The runbook grows by exactly one
   entry each time something new breaks.
3. **Every undocumented setting is a bug.** BIOS changes, power settings, `.wslconfig` values,
   firewall rules, the scheduled task — anything a fresh install would need and would not discover.
4. **The recovery test.** Could a competent colleague, given the password manager and this folder,
   bring the platform back from all three hosts powered off and a wiped `.87`? Walk through it on
   paper once. Every place the honest answer is "they would have to ask me" is a documentation gap,
   and those gaps are where the bus factor actually lives.

**What is currently in one head and needs writing down** — start here, because none of it is in these
docs today:

- Who approves changes to `.149`, and the state of the native-Ubuntu request.
- The BIOS settings changed on each host, and what they were before.
- When the long-running simulation runs are normally scheduled, and whose they are.
- Which of the source documents are originals and which are copies of something else (§2.1).
- The measured re-ingest duration (§2.4) and time-to-green after reboot (§3.5).

Add a short handover section to this document as those answers arrive.

**The honest limit:** documentation reduces the recovery time from days to hours. It does not make the
platform survivable without an operator. If the platform becomes genuinely load-bearing for the team,
the correct response is a second person who has actually run the M8 drills — not more documentation.
Say that to whoever owns the decision, before it matters rather than after.

---

## 11. Operational cadence and record

| Cadence | Task | Section | Last done |
|---|---|---|---|
| Continuous | `healthcheck.sh` every 5 min -> dashboard | §3.5, §4.1 | |
| Nightly | `pg_dump` to `.226`, retention enforced | §2.2 | |
| Every deploy | Container egress probe (2 s, inverted check) | §5.3 | |
| Every deploy | Retrieval eval + citation assertions | [`17`](./17-evaluation.md) §10 | |
| Weekly | Glance at backup freshness, disk free on all three hosts | §2.2, §8.4 | |
| Monthly | Read the SearXNG query log (ADR-0004 audit trail) | §4.3 | |
| Quarterly | **Restore test**, including 3 eval questions against the restored DB | §2.3 | |
| Quarterly | **Full egress verification** with packet capture | §5.2 | |
| Quarterly | **Don't-disturb check (N6)** | §6 | |
| Quarterly | Eval-set quarterly pass and query-log harvest | [`17`](./17-evaluation.md) §10 | |
| Annually | Secret rotation | §9 | |
| On any boot-chain change | **Reboot test, all three hosts** | §3.5 | |
| On any `.226` footprint change | Don't-disturb check | §6.3 | |
| Once, at M8 | Removability drill: uninstall and reinstall a host | §7.2 | |

**Measured facts to record here as they are established** (leave blank until measured — do not fill in
estimates):

| Fact | Value | Measured on |
|---|---|---|
| `pg_dump` size and duration | | |
| Full corpus re-ingest duration (the real RTO) | | |
| Time-to-green after reboot: `.87` | | |
| Time-to-green after reboot: `.226` | | |
| Time-to-green after reboot: `.149` | | |
| Modelling baseline per-iteration mean and spread | | |

---

## Reflect

The operations work in this document divides cleanly into two kinds, and only one of them is
interesting. Backups, rollback and the runbook are ordinary craft — do them properly, keep them
boring. The other kind is the recurring verifications: **egress (N1), don't-disturb (N6) and boot
resilience (N8)**. Those are not maintenance. They are the requirements that justify the platform's
existence and its presence on other people's machines, and each of them degrades silently between
checks. That is why §11 puts them on a calendar rather than in a checklist that was ticked once at M8.

**The most underrated risk here is boot resilience, specifically the WSL2 trap (§3.3.)** It has an
unusually bad combination of properties: it is triggered by something entirely outside our control
(Windows Update rebooting a workstation overnight), it produces no error anywhere, and the host looks
completely healthy from Windows. The platform is simply not there. Worse, the way it is discovered is
almost always a user finding the chat UI dead, which means the platform's reliability is judged by its
worst-behaved link. Two or three of those in a month and people go back to whatever they used before —
and by then the fix is no longer technical.

[`00-goals-and-constraints.md`](./00-goals-and-constraints.md) closes by observing that the risk most
worth watching is adoption, not engineering. Everything in this document is downstream of that. A
platform that loses data, disturbs a modelling run, leaks a document, or is silently absent after a
reboot does not get debugged. It gets switched off.

**Next:** revise `00`–`16` to describe what actually shipped, and fix `00`'s N7 cross-reference to
point at [`17-evaluation.md`](./17-evaluation.md).
