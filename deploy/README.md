# `deploy/` — per-host deployment configuration

> How three workstations become platform hosts, one directory each, and how they stop being platform
> hosts again in one command. Read [`../docs/05-host-setup.md`](../docs/05-host-setup.md) first — that
> document builds the hosts; these files are what runs on them afterwards.

---

## 1. Layout

```
deploy/
  README.md            this file
  fleet.yaml           the fleet controller's config -- ONE file for the whole fleet
  host-87/             the hub        10.0.0.87   RTX 4070 12 GB, 128 GB, WSL2
    compose.yaml
    .env.example
    Caddyfile
  host-226/            the models     10.0.0.226  RTX 4090 24 GB, 256 GB, WSL2
    compose.yaml
    .env.example
  host-210/            overflow       10.0.1.210  RTX 4070 12 GB, 96 GB, WSL2
    compose.yaml
    .env.example
```

**Why three files and not one with profiles.** The hosts genuinely run different things, on different
OS setups, on two subnets, with different resource caps and different owners. One conditional file
would need a mental interpreter every time you read it; three explicit files do not.
[`delivery-plan.md`](../docs/delivery-plan.md) §2 puts it as *"three small explicit files are easier
to reason about at 3 a.m."*, and 3 a.m. is the operating condition this tree is designed for.

`fleet.yaml` is the exception, and for the opposite reason: the ladder is a fleet-wide decision. The
controller runs on `.87` and has to see every host at once in order to route around a claimed one.

`.149` has **no directory here**. It is deferred, image generation moved to `.226` under admission
control, and adding it back later is a native-Ubuntu install plus one host entry in `fleet.yaml` plus
one new directory. See [`../docs/02-hardware-and-fleet.md`](../docs/02-hardware-and-fleet.md) §1.

### Files these compose files expect but that live elsewhere

Two bind-mounted configs are referenced and are **not** in this tree yet, because they belong to the
documents that specify them. Create them before the first `up` — Docker will silently create a
*directory* where a missing bind-mount source should be, and the service will start with no config
and fail in a confusing way:

| Path | Specified in | Notes |
|---|---|---|
| `host-87/litellm.config.yaml` | [`06-model-gateway.md`](../docs/06-model-gateway.md) §5 | The catalog. Verify every key against the pinned LiteLLM tag; a silently-ignored key is the default failure mode |
| `host-87/searxng/settings.yml` | [`16-web-search-and-egress.md`](../docs/16-web-search-and-egress.md) §3 | `autocomplete: ""` is not optional — autocomplete leaks keystroke-level query fragments |

`host-87/Caddyfile` **is** here, because it is short and because the TLS story is load-bearing.

---

## 2. Deploying

```bash
make deploy HOST=87        # then 226, then 210 -- the hub exists before anything registers with it
```

which is, expanded:

```bash
ssh <host>
cd /opt/ai-platform && git pull
cd deploy/host-87
docker compose pull
docker compose up -d
```

First time on a host, before any of that:

```bash
cp .env.example .env && chmod 600 .env && $EDITOR .env    # .env is gitignored, and stays that way
mkdir -p /srv/ai-platform/data/postgres /srv/ai-platform/data/registry /srv/ai-platform/models
```

The two data directories are required on `.87` because Postgres and the registry use **bind-backed
named volumes** — a named volume (so `down -v` removes it) pinned onto a specific NVMe (so Postgres
gets the IO isolation [`02`](../docs/02-hardware-and-fleet.md) §5 asks for). Docker will not create
those directories, and that refusal is deliberate: silently writing Postgres onto the distro's VHDX is
worse than failing to start.

**Bring-up order is `.87`, then `.226`, then `.210`.** Everything registers with the hub, so the hub
exists first.

### Validating a change before it touches a host

```bash
docker compose -f deploy/host-87/compose.yaml  --env-file deploy/host-87/.env.example  config
docker compose -f deploy/host-226/compose.yaml --env-file deploy/host-226/.env.example config
docker compose -f deploy/host-210/compose.yaml --env-file deploy/host-210/.env.example config
```

Using `.env.example` as the env file is the point: it parses **and** proves the committed example is
still complete. A variable added to a compose file and forgotten in `.env.example` fails here rather
than on the host.

### The deep tier does not start with the rest of `.226`

```bash
docker compose --profile deep up -d ik-llama
```

That is a *Compose profile* for one service that physically cannot run under the default memory
profile — not the "one file with conditional profiles" rejected above, which was an argument about
hosts. See §5.

---

## 3. Rollback

Everything here is pinned, and pinning is what makes rollback one line.

| Scenario | Action | Time |
|---|---|---|
| Bad release of **our** service | Edit the `*_TAG` line in that host's `.env`, `docker compose up -d <service>` | ~30 s |
| Bad third-party version bump | Revert the `image:` line in `compose.yaml`, `docker compose up -d <service>` | ~30 s |
| Bad migration | `alembic downgrade` **first**, then the previous image tag — so the schema is never newer than the code running against it | minutes |
| Bad model choice | Change the catalog entry, restart the server. No code change (N9) | minutes |
| Platform disturbing the person at the machine | `docker compose down` on that host; the gateway routes elsewhere | seconds |
| Everything wrong | `docker compose down` on all three; they are plain workstations again | seconds |

Two conventions make that table work, and they are deliberate:

- **Tags for our own services live in `.env`**, because rolling one back is an *operational* action
  taken under pressure — one line, no diff, no review.
- **Tags for third-party images are pinned inline in `compose.yaml`**, because bumping LiteLLM or vLLM
  or Postgres is a *deliberate, reviewed* change with a changelog to read first. Putting them in
  `.env` would hide them from review.

**Never `latest`.** Not on any image, anywhere in this tree. `latest` turns "redeploy the previous
tag" into "hope the registry still has what you had."

---

## 4. Boot resilience — the trap, and it is a real one

Requirement **N8**: *every service returns after a reboot with no human involved.* Nobody should have
to log into a colleague's machine to restart chat.

Every service in this tree carries `restart: unless-stopped` and a healthcheck. **On the two Windows
hosts that is not enough, and believing it is will cost you a morning.**

> **WSL2 does not start at boot.** It starts when a user runs `wsl.exe`. Until then there is no
> distro, no systemd, no Docker daemon, and therefore no containers — whatever their restart policy
> says. `restart: unless-stopped` restarts a container when *Docker* comes back; nothing here brings
> *Docker* back, because nothing brings WSL2 back.

Three things must all be true:

1. **`systemd=true` in `/etc/wsl.conf`** inside the distro. That is what makes `systemctl enable
   docker` mean anything, and it is the entire basis of the restart chain.
2. **`sudo systemctl enable --now docker`** in the distro.
3. **A Windows Task Scheduler task per host** that boots the distro at startup. Booting it starts
   systemd, which starts Docker, which starts the containers.

```powershell
# Elevated PowerShell, on .87, .226 and .210. `--exec /bin/true` is enough to
# trigger boot; the task does not need to keep running.
$action  = New-ScheduledTaskAction -Execute 'C:\Windows\System32\wsl.exe' `
             -Argument '-d Ubuntu-24.04 --exec /bin/true'
$trigger = New-ScheduledTaskTrigger -AtStartup
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 3 `
             -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName 'AIPlatform-WSL-Boot' -Action $action `
  -Trigger $trigger -Settings $set -User '<host-user>' -Password '<password>' `
  -RunLevel Highest
```

Three honest caveats, because this is the least clean part of the whole setup:

- **WSL2 instances are per Windows user.** A task running as `SYSTEM` boots a distro in a session the
  interactive user does not share, which produces confusing double-instance behaviour. Run it as the
  machine's normal user with *"run whether user is logged on or not"* — which means storing that
  account's password in Task Scheduler. It goes in the password manager and **nowhere else**; it does
  not go in `.env`, and it does not go in this repo.
- **If `.226` uses the raw-disk mount for its weights** ([`05`](../docs/05-host-setup.md) §6.3), that
  mount does not survive a reboot. Add a second action (`wsl.exe --mount \\.\PHYSICALDRIVE<n>
  --bare`) that runs *before* the boot action, and test it by actually rebooting.
- Add a 30–60 second startup delay if the task proves flaky. The network stack and the NVIDIA driver
  both need to be up before containers that claim GPUs start.

**The acceptance test is a cold reboot, not an argument.** Reboot each host, touch nothing, and
confirm within ~5 minutes that `https://chat.ai.lan` answers, the gateway lists its models, and
`nvidia-smi` on `.226` shows the fast-tier model loaded. Budget an afternoon for this section alone;
it is the part that must be tested by rebooting rather than by reasoning.

---

## 5. `.226`: the caps, and the two `.wslconfig` profiles

The most consequential lines affecting `.226` are not in its `compose.yaml` at all — they are in
`C:\Users\<you>\.wslconfig`, and they exist because this box runs 8–13 hour long-running simulation
jobs that must not be measurably slowed (**N6**).

```ini
# FAST-TIER PROFILE -- the default
[wsl2]
processors=8          # ~8 of 32 physical cores. The modelling runs keep the rest
memory=48GB
swap=0                # a model server that starts swapping is already broken;
                      # fail loudly instead of degrading silently
```

The `deploy.resources.limits` blocks in `host-226/compose.yaml` carve up **that 48 GB**. They do not
add to it. The guest cap is the outer bound; the container limits stop any one service inside it from
taking the lot.

**The tension, stated plainly.** The deep tier (Qwen3-235B-A22B Q4) needs roughly **130 GB of guest
RAM** for its experts. The fast profile caps the guest at **48 GB**. Both statements are correct and
they cannot both hold, so there are two profiles:

```
  fast-tier profile   processors=8    memory=48GB    default; modelling runs unaffected
  deep-tier profile   processors=24   memory=180GB   only while NO modelling job is running
```

**Switching requires `wsl --shutdown`, which restarts every container on `.226`.** That is the real
cost, and it is exactly why `ik-llama` sits behind `profiles: ["deep"]` and why the deep tier is a
*gated session* rather than a permanent catalog entry.

```powershell
# 1. Route `chat` and `coder` to .87 at the gateway FIRST -- .226 is about to go away
Copy-Item C:\Users\<you>\wslconfig-deep.ini C:\Users\<you>\.wslconfig -Force
wsl --shutdown
wsl -d Ubuntu-24.04 --exec /bin/true
wsl -d Ubuntu-24.04 -- nproc            # expect 24
wsl -d Ubuntu-24.04 -- free -g          # expect ~180 BEFORE loading 130 GB of weights
```
```bash
docker compose --profile deep up -d ik-llama
```

and reverse it afterwards. Verify after *every* switch, and confirm on the Windows side that
`nvidia-smi` still shows the modelling job's memory untouched.

If M0 spike 7 shows the deep tier starving the modelling runs even in a dedicated window, the honest
outcome is that this profile never gets used, `deep-slow` comes out of the catalog, and the `ik-llama`
service is deleted. Nothing else depends on it.

---

## 6. The egress boundary is structural — do not undo it by accident

`host-87/compose.yaml` builds the thing that justifies this whole platform: **our documents and our
code never leave the network** (N1). It is enforced by Docker, not by our own good behaviour, because
a policy that lives only in code review is one careless `httpx.get` away from being false and nobody
would ever know.

```
  platform  (internal: true)  no route out, no NAT, no public DNS -- everything
  egress    (bridge)          SearXNG, and only SearXNG
  lan       (bridge, 172.28.9.0/24)  litellm, mcp-tools, fleet-controller
```

**The `lan` network is the deliberate hole**, and it needs saying out loud because it looks like a
mistake:

| Flow | Why it must cross the host boundary |
|---|---|
| `litellm` → `10.0.0.226:8000`, `:8081` | The fast and deep tiers live on another machine |
| `litellm` → `10.0.1.210:8000` | Overflow capacity, other subnet |
| `mcp-tools` → `10.0.0.226:8188` | ComfyUI; image gen shares the 4090 under admission control |
| `fleet-controller` → `.226:8099`, `.210:8099` | The per-host agents it polls every ~2 s |

Docker has no "LAN-only" network primitive: a container either has a default route or it does not.
[`16`](../docs/16-web-search-and-egress.md) §4.1 suggests "an explicit route to those hosts", which is
not achievable on an `internal: true` network without granting `NET_ADMIN` — worse than the problem.
So the construction is a bridge network with a **pinned subnet**, attached to exactly three
containers, clamped by a host firewall rule:

```bash
# DOCKER-USER is evaluated before Docker's own FORWARD rules. Verify by test,
# never by reading -- Docker manipulates iptables itself and OUTPUT rules do not
# see forwarded container traffic the way you expect.
iptables -I DOCKER-USER -s 172.28.9.0/24 -d 10.0.0.0/24 -j RETURN
iptables -I DOCKER-USER -s 172.28.9.0/24 -d 10.0.1.0/24 -j RETURN
iptables -A DOCKER-USER -s 172.28.9.0/24 -j DROP
```

**What the M0/M8 packet capture must prove** ([`16`](../docs/16-web-search-and-egress.md) §7):

1. `172.28.9.0/24` appears **only** with `10.0.x.x` destinations. One packet from that subnet to a
   public address is a fail.
2. Exactly **one** internal source address appears in the egress-leg capture: SearXNG's. Pin that
   address before writing the firewall rule, or the rule is about whatever container restarted last.
3. The deliberately-sent search query **is** present in the capture. Verify this positive control
   first — a capture with no expected content in it is a broken capture, not a clean result.
4. No canary, no corpus filename, no embedding-shaped payload, anywhere in either capture.

**Review rule:** a diff that adds `egress` to any service other than `searxng`, or adds `lan` to a
fourth service, invalidates the egress proof until it has been re-run and re-recorded in
[`16`](../docs/16-web-search-and-egress.md). Treat it accordingly.

---

## 7. GPU access under WSL2

Every GPU service uses:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
```

That is the only supported way to claim a GPU from Compose, and it works on `.87`, `.226` and `.210`
because the **Windows** driver is passed through to the guest at `/usr/lib/wsl/lib` and
`nvidia-container-toolkit` is installed *inside* the distro.

> **Never install a Linux NVIDIA driver in a WSL2 guest.** Not `cuda-drivers`, not a `.run` installer.
> It overwrites the passthrough and breaks CUDA in a way that looks like a hardware fault. Install the
> CUDA **toolkit** only — `cuda-toolkit-12-6` on `.87`, `cuda-toolkit-13-1` on `.226` and `.210`. If
> `nvidia-smi` works but `torch.cuda.is_available()` is `False`, this is why, and reinstalling the
> distro is faster than unpicking it.

Note also what `reservations` does **not** do: it does not partition the card. Every GPU container on
a host sees the whole GPU. Who actually gets VRAM is decided at runtime by the fleet controller
(sleep/wake) and, for images, by admission control. Docker has no mechanism for the policy in
[`03`](../docs/03-gpu-sharing-policy.md) — which is precisely why the fleet controller exists.

---

## 8. Removability — the property that makes this politically possible

Two of these three machines belong, in practice, to other people's work. The reason it is acceptable
to install a platform on someone's workstation is that it comes off completely, on demand, with no
argument and no residue. **If that stops being true, the social contract in
[`03`](../docs/03-gpu-sharing-policy.md) is broken regardless of how well the toggle works.**

Everything in these compose files is a container or a **named volume**. There are exactly three bind
mounts, and all three point into declared data directories:

| Bind | Where | Why not a volume |
|---|---|---|
| `${MODELS_DIR}:/models:ro` | `/srv/ai-platform/models` | 100–250 GB of weights that took days to download. `down -v` must not delete them |
| `./Caddyfile`, `./litellm.config.yaml`, `./searxng` | the repo checkout | config, read-only, removed with the checkout |
| `/proc:/host/proc:ro` | the fleet agent | read-only. The controller has no authority to kill anybody's process |

### The three levels

```bash
# Level 1 -- stop. Reversible in seconds. This is the one people will ask for.
docker compose -f deploy/host-226/compose.yaml down

# Level 2 -- remove containers and images, keep data
docker compose -f deploy/host-226/compose.yaml down --rmi all

# Level 3 -- full uninstall of the platform's Docker footprint on this host
docker compose -f deploy/host-226/compose.yaml down -v --rmi all
```

**Before a level-3 removal on `.87`, copy Caddy's CA root off the box.** `down -v` destroys the
`caddy_data` volume, and every client machine then has to trust a newly generated root.

### And the residue that is NOT inside Docker

This is the part that gets forgotten, and forgetting it is what makes "removable" untrue:

| Residue | Remove with | Host |
|---|---|---|
| `AIPlatform-WSL-Boot` scheduled task | `Unregister-ScheduledTask -TaskName 'AIPlatform-WSL-Boot'` | `.87`, `.226`, `.210` |
| `.wslconfig` CPU/memory caps (and `wslconfig-deep.ini`) | Restore the pre-platform file | `.87`, `.226`, `.210` |
| `/etc/wsl.conf` changes (`systemd=true`, default user) | Restore, or leave if the distro was ours | all WSL hosts |
| `powercfg` sleep/hibernate and power-plan changes | Restore the original power plan | `.226` (and any host changed) |
| Windows firewall rules (`AI Platform - *`) | `Remove-NetFirewallRule -DisplayName 'AI Platform - *'` | all |
| Hyper-V firewall rules for the WSL VM | Remove; they are separate from the Windows rules | all WSL hosts |
| `DOCKER-USER` iptables rules for `172.28.9.0/24`, and the egress default-deny | Remove; restore the previous default policy | `.87` |
| `"insecure-registries"` and `"data-root"` in `/etc/docker/daemon.json` | Restore the original file | all |
| Caddy CA root trusted on client machines | Remove from the trust store on **every client**, not just the hosts | all clients |
| `gpu-run` wrapper and shell alias | Remove from `/usr/local/bin` and the shell profile | all |
| The `aiplat` service account | `deluser` if the platform created it | all |
| `/opt/ai-platform`, `/srv/ai-platform` | `rm -rf` — check both NVMes | all |
| Model weights | The largest thing by far, hundreds of GB | `.226`, `.210`, `.87` |
| Backups on `.226`'s 8 TB NVMe | `rm -rf` the backup directory | `.226` |
| Docker itself | Leave it if it predates the platform. **Record which** at install time | all |
| BIOS "restore on AC power loss" | Only if it was off before. Note the original setting | all |

**Keep a `PRE-PLATFORM/` directory on each host** holding the original `.wslconfig`, the power-plan
export, the original `daemon.json` and a firewall-rule export, captured *before* the platform was
installed. Restoring a machine then means copying files back rather than remembering what it used to
look like. Do it at M1 host setup; it takes five minutes and is unrecoverable afterwards.

**Verify removability once, at M8**, on the least contended host: run the level-3 uninstall, confirm
the machine is a plain workstation again, and reinstall from the repo. That drill proves the uninstall
is complete *and* that the install is reproducible from the docs.

---

## 9. Secrets

- `.env.example` is committed and lists every key with an empty or placeholder value.
- `.env` is gitignored, written by hand on each host, mode `0600`, and never leaves it.
- **Host passwords live in a password manager.** Not in `.env`, not in this repo, not in a comment.
  That includes the Windows account password Task Scheduler needs (§4).
- Generate every secret per environment; never reuse a dev value.
- Model revisions are pinned in `.env` — an unpinned model is an unreproducible deploy.
- If a real key ever reaches a commit, **rotate it**. Do not rewrite history and hope.
  `make secrets-scan` is the cheap check before pushing.

---

## 10. Known gaps in this tree

Recorded here rather than discovered later:

1. **RAGFlow's backing stack is not vendored.** Upstream ships MySQL, Redis, MinIO and a document
   engine in its own compose file. Which of those we run is exactly what the one-day M1.5 spike
   decides (ADR-0007). Until then, run RAGFlow's bundled stack alongside ours with
   `aiplatform-internal` joined as an external network, and fold the result back into
   `host-87/compose.yaml`.
2. **`litellm.config.yaml` and `searxng/settings.yml` are referenced but not committed** — see §1.
3. **Port-table drift.** `host-226` adds `:8001` for the second vLLM (the floor rung), and the MCP
   server is published on `:8002` rather than the `:8080` shown in
   [`14`](../docs/14-mcp-tool-server.md) because `:8080` collides with Open WebUI on `.87`.
   [`05`](../docs/05-host-setup.md) §4's table and the firewall rules in §9 need both.
4. **SearXNG's container address is not pinned yet.** [`16`](../docs/16-web-search-and-egress.md) §4.3
   requires it before the single-permitted-source firewall rule is meaningful. Pin the `ipam` subnets
   on `platform` and `egress`, assign the address, and record it in `16`.
5. **The fleet agent may need to run on Windows, not in Docker.** From inside the WSL2 guest,
   `nvidia-smi` sees the card but the process list is incomplete — the modelling job's PID is on the
   Windows side. If M0 spike 5 shows foreign-VRAM attribution is unreliable from the guest, move the
   agent out of these compose files and onto Windows. The controller does not care which side answers.
