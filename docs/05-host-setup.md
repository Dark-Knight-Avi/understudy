# 05 — Host Setup

> **Pre-build draft.** Written ahead of the milestone; revise after it ships to describe what actually shipped.
>
> Milestone **M1**. How the three workstations become platform hosts without stopping being
> workstations. Read [`02-hardware-and-fleet.md`](./02-hardware-and-fleet.md) and
> [`03-gpu-sharing-policy.md`](./03-gpu-sharing-policy.md) first, and run
> [`04-m0-spikes.md`](./04-m0-spikes.md) spikes 1–4 before typing any of this.

---

## Concept

### 1. What "host setup" means here

Three properties, in priority order. Everything below serves them.

| | Property | Why it is non-negotiable |
|---|---|---|
| **H1** | **Capped.** The platform can never take more CPU or RAM than we allotted it | N6 — the modelling runs on `.226` must not be measurably slowed |
| **H2** | **Unattended.** Every service returns after a reboot with no human involved | N8 — nobody should have to log into a colleague's machine to restart chat |
| **H3** | **Removable.** One command per host puts the machine back to how we found it | It is what makes installing this on someone else's workstation politically possible |

H1 is why `.wslconfig` appears in this document at all. H2 is the part that is genuinely awkward on
Windows and gets its own section. H3 means containers and named volumes only, one directory tree per
host, and no change to the user's Windows environment beyond a firewall rule and a scheduled task.

### 2. WSL2 or native Ubuntu — decided per host, not by preference

| Host | OS decision | Reason |
|---|---|---|
| `.87` | **WSL2** on the existing Windows install | Someone uses this box as a Windows workstation. Ada (CC 8.9) is the well-trodden path under WSL2 |
| `.226` | **WSL2** on the existing Windows install | The long-running simulation toolchain lives on Windows and must not be disturbed. Repartitioning this machine is not on the table |
| `.149` | **Native Ubuntu** | No WSL is installed yet, and the reported WSL2 CUDA memory-overhead problem is specific to Blackwell / `sm_120`. Installing native Ubuntu sidesteps the whole class of problem — see [`02`](./02-hardware-and-fleet.md) §1 |

`.149` is the one host where we get to choose, and choosing native costs nothing. That choice is
blocked on approval to repartition the machine — [`delivery-plan.md`](./delivery-plan.md) §7 says
raise the request during M0, and it remains the longest lead time in the project.

### 3. Two WSL2 rules that are not negotiable

**Install only the CUDA toolkit inside WSL2. Never a Linux NVIDIA driver.** The driver comes through
from the Windows host via `/usr/lib/wsl/lib`. Installing `cuda-drivers` or a `.run` driver inside the
guest overwrites that passthrough and breaks CUDA in a way that looks like a hardware fault. Every
CUDA install line below is deliberately the `-toolkit-` package.

**Bind services to `0.0.0.0`, never `127.0.0.1`.** With `networkingMode=mirrored` the WSL2 instance
shares the Windows host's network interfaces, so a service bound to `0.0.0.0` inside WSL2 is reachable
at the host's LAN IP. Bound to loopback it is reachable only from that machine, which defeats the
point of having a fleet.

### 4. Port and path conventions

One table, referenced by every later doc. Ports are the container-published ports on the host's LAN
IP; anything user-facing is additionally fronted by Caddy on `.87`.

| Service | Host | Port | Exposure |
|---|---|---|---|
| Caddy (HTTP -> HTTPS) | `.87` | 80, 443 | LAN — the only front door |
| Open WebUI | `.87` | 8080 | Behind Caddy |
| LiteLLM gateway | `.87` | 4000 | LAN — clients and other hosts |
| Postgres 17 + pgvector | `.87` | 5432 | Internal subnets only |
| Infinity (embeddings + rerank) | `.87` | 7997 | Internal |
| Docker registry (`registry:2`) | `.87` | 5000 | Internal |
| Fleet controller + dashboard | `.87` | 8090 | Behind Caddy |
| RAG service | `.87` | 8001 | Internal |
| MCP tool server | `.87` | 8002 | Internal |
| SearXNG | `.87` | 8888 | Internal |
| vLLM (small / ladder) | `.87` | 8000 | Internal |
| vLLM (fast tier) | `.226` | 8000 | Internal |
| `ik_llama.cpp` (deep tier) | `.226` | 8081 | Internal |
| Fleet agent | `.226`, `.149` | 8099 | Internal, controller only |
| ComfyUI | `.149` | 8188 | Internal |
| vLLM (spare capacity) | `.149` | 8000 | Internal |

Paths, identical on all three hosts so the compose files and runbooks do not fork:

```
/opt/ai-platform/           repo checkout (deploy/, services/)
/opt/ai-platform/.env       host secrets, gitignored, mode 0600
/srv/ai-platform/models/    model weights   (.226: physically on the 8 TB NVMe)
/srv/ai-platform/data/      container volumes, Postgres, logs
```

**Credentials never enter the repo.** `.env.example` is committed and lists every key with an empty
value; `.env` is gitignored and written by hand on each host. If a real key ever reaches a commit,
rotate it rather than rewriting history and hoping.

---

## Build

Order is `.87`, then `.226`, then `.149` — the hub exists before anything registers with it. This
matches [`delivery-plan.md`](./delivery-plan.md) §5.

---

## 5. `.87` — the hub (10.0.0.87, RTX 4070 12 GB, i9-14900K 24c/32t, 128 GB)

The least contended box, so it carries everything that must stay up: Postgres, the gateway, the
registry, Caddy, the reranker, and the three services we build.

### 5.1 Windows side, before WSL

Elevated PowerShell.

```powershell
# 1. The Windows NVIDIA driver is what WSL2 uses. Confirm it exists and is recent.
nvidia-smi

# 2. Install/refresh WSL itself (the store version, not the old optional feature).
wsl --install --no-distribution
wsl --update
wsl --version          # need WSL >= 2.0.0 for mirrored networking

# 3. Install Ubuntu 24.04.
wsl --install -d Ubuntu-24.04
wsl --set-default-version 2
```

**Verify:** `wsl --version` reports a WSL version of 2.0.0 or higher plus a kernel version. Mirrored
networking needs Windows 11 22H2 or later *and* that WSL version. This fleet is on Windows 11, but
check rather than assume — the fallback (NAT mode plus a `netsh interface portproxy` entry for every
port in §4) is materially worse to operate and easy to get subtly wrong.

### 5.2 `.wslconfig`

Lives at `C:\Users\<you>\.wslconfig` — per Windows user, not per distro. Changes take effect only
after `wsl --shutdown`.

```ini
# C:\Users\<you>\.wslconfig      --- host .87 (128 GB RAM, 24c/32t)
[wsl2]
memory=64GB
processors=12
swap=16GB

# Share the Windows host's interfaces, so 10.0.0.87:4000 reaches a service
# bound to 0.0.0.0:4000 inside WSL2. Requires Windows 11 + WSL >= 2.0.0.
networkingMode=mirrored

# Return freed guest memory to Windows instead of holding the high-water mark.
autoMemoryReclaim=gradual

# DNS through the Windows resolver; avoids the classic broken /etc/resolv.conf.
dnsTunneling=true
firewall=true
autoProxy=true

[experimental]
hostAddressLoopback=true
```

`.87` gets a generous cap because nothing else on this machine contends for it. `.226` will not —
see §6.2.

**Verify:**

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04 -- nproc         # expect 12
wsl -d Ubuntu-24.04 -- free -g       # total near 64
```

### 5.3 `/etc/wsl.conf`

Lives **inside** the distro. Enables systemd, which is what makes `systemctl enable docker` mean
anything — and that is the entire basis of H2.

```ini
# /etc/wsl.conf
[boot]
systemd=true

[automount]
enabled=true
options="metadata,umask=22,fmask=11"

[interop]
enabled=true
appendWindowsPath=false

[user]
default=aiplat

[network]
generateHosts=true
generateResolvConf=false
```

`appendWindowsPath=false` keeps the Windows `PATH` out of the Linux shell. Small thing; prevents a
whole category of confusing failure where a Linux script silently invokes a Windows executable of the
same name.

Create the unprivileged service account referenced above — per
[`01-architecture.md`](./01-architecture.md) §5 the platform never runs as root:

```bash
sudo adduser --disabled-password --gecos "" aiplat
sudo usermod -aG sudo aiplat          # add to the docker group after §5.5
```

Then `wsl --shutdown` from PowerShell and reopen.

**Verify:** `systemctl is-system-running` returns `running` or `degraded`. Degraded is common and
acceptable under WSL — read `systemctl --failed` and confirm nothing we depend on is in the list.

### 5.4 CUDA toolkit inside WSL2

Toolkit only. `.87` reports CUDA 12.6, so pin a matching toolkit rather than taking whatever is
newest: vLLM wheels are built against specific CUDA minor versions and mismatches show up as
missing-symbol errors at import time, long after you have forgotten this step.

```bash
# NVIDIA's WSL-specific repo. Note the distro string: wsl-ubuntu, not ubuntu2404.
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update

# TOOLKIT ONLY. Never cuda-drivers, never a .run driver installer.
sudo apt-get install -y cuda-toolkit-12-6

echo 'export PATH=/usr/local/cuda/bin:$PATH' | sudo tee /etc/profile.d/cuda.sh
```

**Verify:**

```bash
nvidia-smi                       # driver comes from Windows; must show the 4070
nvcc --version                   # toolkit version; may differ from the driver's CUDA version
ls /usr/lib/wsl/lib/libcuda.so*  # the passthrough stub. Missing => CUDA will not work
python3 -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

If `nvidia-smi` works but `torch.cuda.is_available()` is `False`, the usual cause is a Linux driver
installed over the passthrough. Reinstalling the distro is faster than unpicking it.

### 5.5 Docker Engine inside WSL2 — not Docker Desktop

Docker Desktop works, but it is a licensed product above a company-size threshold (N2), it manages
its own hidden WSL distros, and it adds a Windows-side service to the boot path we are trying to keep
simple. Install the engine inside the distro instead.

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker aiplat
sudo systemctl enable --now docker

# GPU access for containers
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Move Docker's data root onto the larger NVMe so image layers and volumes do not fill the distro's
virtual disk:

```json
// /etc/docker/daemon.json
{
  "data-root": "/srv/ai-platform/data/docker",
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
```

The log rotation is not cosmetic. An unrotated vLLM container log will quietly eat tens of GB.

**Verify:**

```bash
docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi
```

### 5.6 The local registry

`registry:2` on `.87`, so each host pulls a built image instead of rebuilding it, and so image
traffic never leaves the network ([ADR-0004](./adr/0004-egress-policy.md)).

```yaml
# deploy/host-87/compose.yaml  (fragment)
services:
  registry:
    image: registry:2
    restart: unless-stopped
    ports:
      - "0.0.0.0:5000:5000"
    environment:
      REGISTRY_STORAGE_DELETE_ENABLED: "true"
    volumes:
      - /srv/ai-platform/data/registry:/var/lib/registry
```

Docker refuses plain-HTTP registries by default. Two ways out:

| Option | What you do | Tradeoff |
|---|---|---|
| **Insecure registry** | Add `"insecure-registries": ["10.0.0.87:5000"]` to `/etc/docker/daemon.json` on all three hosts | One line, no certificates. Unauthenticated and unencrypted — tolerable only because it is firewalled to internal subnets |
| **Behind Caddy** (preferred) | Publish the registry on loopback only, front it as `registry.ai.lan` in Caddy, trust Caddy's internal CA root on each host | TLS, one hostname, no per-host Docker config drift. Costs distributing one root certificate |

Start insecure if that is what unblocks M1, but write the Caddy route the same week. Per-host
`daemon.json` drift is exactly what makes the fourth host painful.

**Verify:** `docker pull alpine && docker tag alpine 10.0.0.87:5000/alpine:test && docker push
10.0.0.87:5000/alpine:test`, then pull that tag from `.226`.

### 5.7 Caddy — TLS on the LAN

Everything user-facing goes through one front door with a certificate that validates, so we do not
train the team to click through browser warnings.

Caddy's `tls internal` issues certificates from a CA it generates locally: no public DNS, no ACME, no
egress. The cost is that each client machine trusts that CA root once.

```caddyfile
# deploy/host-87/Caddyfile
{
    email ops@internal.invalid
    admin off
}

# Chat UI — what the team actually visits.
chat.ai.lan {
    tls internal
    encode zstd gzip
    reverse_proxy open-webui:8080
}

# Fleet dashboard — the toggle from 03-gpu-sharing-policy.md.
fleet.ai.lan {
    tls internal
    reverse_proxy fleet-controller:8090
}

# Gateway. Clients (OpenCode, Cline) may also hit :4000 directly on the LAN.
api.ai.lan {
    tls internal
    reverse_proxy litellm:4000
}

# Local image registry.
registry.ai.lan {
    tls internal
    reverse_proxy registry:5000
}
```

Name resolution: either add A records for `*.ai.lan` on the internal DNS server, or — if you do not
control DNS — four `hosts` file lines on each client machine. DNS is worth asking for; hosts files
rot.

Export the CA root for clients:

```bash
docker compose exec caddy \
  cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
# Windows clients: import into "Trusted Root Certification Authorities" (Local Machine)
# Linux clients:   copy to /usr/local/share/ca-certificates/, run update-ca-certificates
```

**Verify:** from a colleague's machine, `https://chat.ai.lan` loads with no certificate warning after
the root is installed.

### 5.8 Postgres, Infinity, and the rest of the hub

Those belong to [`10-data-layer.md`](./10-data-layer.md) and
[`12-retrieval-and-rerank.md`](./12-retrieval-and-rerank.md). Two facts belong here: the Postgres data
directory goes on its **own** NVMe (`/srv/ai-platform/data/postgres` backed by NVMe #1) per
[`02`](./02-hardware-and-fleet.md) §5, and Infinity gets the GPU while the reranker stays pinned to
CPU per [`03`](./03-gpu-sharing-policy.md) §5.

---

## 6. `.226` — the model host (10.0.0.226, RTX 4090 24 GB, TR PRO 9975WX 32c/64t, 256 GB)

Same shape as `.87` with three differences that matter: the caps are tight, the weights need a fast
filesystem, and this box also runs 8–13 hour long-running simulation jobs that must not be slowed.

### 6.1 Before anything: two M0 spikes

Do not build this host until [`04-m0-spikes.md`](./04-m0-spikes.md) spike 1 (usable VRAM under WSL2,
target >= 21 GiB) and spike 2 (two-hour CUDA soak — this is an **AMD** host, and NVIDIA documents a
cache-coherency fault under WSL2 on Ryzen) have recorded verdicts. If spike 2 fails, this section
becomes deep-tier-only and fast-tier serving moves to `.149`.

### 6.2 `.wslconfig` — the caps are the point

```ini
# C:\Users\<you>\.wslconfig     --- host .226 (256 GB, 32c/64t)  [FAST-TIER PROFILE]
[wsl2]
# ~8 of 32 physical cores. The modelling runs keep the rest.
processors=8
memory=48GB
swap=0

networkingMode=mirrored
autoMemoryReclaim=gradual
dnsTunneling=true
firewall=true

[experimental]
hostAddressLoopback=true
```

`swap=0` is deliberate: WSL2 swap is a VHDX on the Windows filesystem, and a model server that starts
swapping is already broken. Fail loudly instead of degrading silently.

**This profile cannot run the deep tier.** Qwen3-235B-A22B Q4 needs roughly 130 GB of system RAM for
its experts ([`02`](./02-hardware-and-fleet.md) §4), and this file caps the guest at 48 GB. The
tension is real, and it is resolved in [`07-inference-servers.md`](./07-inference-servers.md)
§Deep tier with a **second profile**, swapped in only when the modelling jobs are idle:

```
  fast-tier profile   processors=8    memory=48GB     default; modelling runs unaffected
  deep-tier profile   processors=24   memory=180GB    only while no modelling job runs
                                                      switching requires `wsl --shutdown`
```

Switching profiles restarts WSL2 and therefore every container on the host. That is a real cost, and
it is why the deep tier is a scheduled, gated capability rather than something permanently in the
catalog.

**Verify after each switch:** `wsl -d Ubuntu-24.04 -- nproc` and `free -g` report the profile you
intended, and `nvidia-smi` on the Windows side still shows the modelling job's memory untouched.

### 6.3 Where the weights live — pick one, deliberately

Deep-tier GGUFs are 100–250 GB each and are read continuously during inference. Reading them across
the WSL2/Windows filesystem boundary (`/mnt/d/...`, 9p/drvfs) is slow enough to change the tokens/sec
number, so do not.

| Option | How | Tradeoff |
|---|---|---|
| **Move the distro's VHDX to the 8 TB NVMe** (recommended) | `wsl --export`, then `wsl --import Ubuntu-24.04 D:\wsl\ubuntu-226 <tarball>` where `D:` is the 8 TB drive | Simplest. Native ext4 speed inside the VHDX. One sparse file growing to hundreds of GB — expected, not a fault |
| **Mount a raw disk into WSL2** | `wsl --mount \\.\PHYSICALDRIVE<n> --bare`, then partition and format ext4 inside the guest | Fastest, cleanest separation. Fiddly: the mount does **not** survive a reboot, so the startup task in §8 must re-issue it, and the disk becomes invisible to Windows |
| **Leave them on NTFS at `/mnt/d`** | Nothing | Do not. 9p throughput turns a cold load into minutes and may bottleneck mmap'd inference |

Whichever you pick, expose it at the same path as the other hosts — `/srv/ai-platform/models/` — via
a bind mount or symlink, so the compose files stay identical.

### 6.4 CUDA, Docker, and the rest

Identical to §5.3–5.5 with one change: `.226` reports CUDA 13.1, so install `cuda-toolkit-13-1`
rather than `12-6`, and confirm your vLLM image is built for that CUDA line before pulling it. Two
hosts on two CUDA versions is a small tax we accept; the alternative is downgrading a driver on
someone else's workstation.

Docker's data root goes on 4 TB NVMe #2 (`/srv/ai-platform/data/docker`), keeping container churn off
the drive holding the weights.

### 6.5 Do-not-disturb settings on the Windows side

The modelling runs are the incumbent tenant. Three Windows-side settings protect them:

```powershell
# 1. Never sleep or hibernate. An 11-hour modelling run must not be interrupted,
#    and WSL2 does not reliably survive S3/S4 anyway.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off

# 2. A performance power plan, so cores do not park mid-run.
powercfg /list          # pick the High performance / Ultimate GUID on this machine
powercfg /setactive <GUID>

# 3. Confirm the GPU is in WDDM mode and in DEFAULT compute mode.
#    Do NOT switch to exclusive-process: the sharing policy needs the platform
#    and the user's job to coexist on one card.
nvidia-smi --query-gpu=compute_mode,driver_model.current --format=csv
```

**Verify:** run a real modelling job with the platform stack up but idle, and confirm per-iteration
time stays within noise of its ~48 minute baseline (N6). On this host that is the acceptance test
that matters most.

---

## 7. `.149` — native Ubuntu (10.0.1.149, RTX 5080 16 GB, i9-14900K, 32 GB)

Different subnet, different GPU generation, different OS. Do this host last, and only after
[`04-m0-spikes.md`](./04-m0-spikes.md) spike 4 confirms `.32.x` can actually reach `.149`.

### 7.1 Install

1. Back up anything on the 2 TB NVMe. This is a destructive install with no undo.
2. Ubuntu 24.04 LTS, minimal install, **OpenSSH server enabled**, whole 2 TB NVMe.
3. Static IP `10.0.1.149` with the correct netmask and gateway **for that subnet** — confirm both
   with whoever runs the network rather than inferring them from the `.32` subnet.

### 7.2 Driver and CUDA — the opposite rule from WSL2

Here you **do** install a Linux driver, because there is no Windows host passing one through.

```bash
sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common
ubuntu-drivers devices                  # note the recommended branch

# Blackwell (CC 12.0) needs a recent driver branch AND the open kernel modules.
sudo apt-get install -y nvidia-driver-<branch>-open
sudo reboot
```

```bash
# CUDA 13.1 toolkit from the NATIVE repo (not the wsl-ubuntu one used on .87/.226)
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update && sudo apt-get install -y cuda-toolkit-13-1
```

**Verify — this is spike 3, and it is the one that fails quietly:**

```bash
nvidia-smi
python3 -c "import torch; print(torch.cuda.get_device_capability())"  # expect (12, 0)
python3 -c "import torch; print(torch.cuda.get_arch_list())"          # sm_120 MUST be present
```

A PyTorch/vLLM/ComfyUI build without `sm_120` kernels either refuses to start or falls back to
something pathologically slow. Upgrade the build; do not paper over it with `TORCH_CUDA_ARCH_LIST`.

### 7.3 Docker and boot

Same Docker Engine and NVIDIA container toolkit steps as §5.5. Boot resilience here is trivial, and
that is the compensation for the install effort:

```bash
sudo systemctl enable docker
# containers carry restart: unless-stopped — that is the whole mechanism
```

**Verify:** `sudo reboot`, wait, then from `.87`: `curl -sf http://10.0.1.149:8188/` returns.

### 7.4 32 GB of RAM is the real constraint here

Not the GPU. This host cannot run Postgres, the gateway, or anything that caches. It runs ComfyUI,
optionally a small vLLM for spare fast-tier capacity, and the fleet agent. Resist putting anything
stateful on it — [`02`](./02-hardware-and-fleet.md) §1 made that call for good reasons and 32 GB has
not changed since.

---

## 8. Boot and restart resilience

N8 says every service returns after a reboot with no manual intervention. On `.149` that is systemd
plus `restart: unless-stopped`. On the two Windows hosts there is one genuine gap:

**WSL2 does not start at boot.** It starts when a user runs `wsl.exe`. Until then there is no distro,
no systemd, no Docker daemon and no containers — whatever their restart policy says.

The fix is a Windows Task Scheduler task per host:

```powershell
# Elevated PowerShell. Booting the distro starts systemd, which starts Docker,
# which starts containers marked restart: unless-stopped. `--exec /bin/true`
# is enough to trigger boot; the task does not need to keep running.
$action  = New-ScheduledTaskAction -Execute 'C:\Windows\System32\wsl.exe' `
             -Argument '-d Ubuntu-24.04 --exec /bin/true'
$trigger = New-ScheduledTaskTrigger -AtStartup
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 3 `
             -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName 'WSL2-AI-Platform' -Action $action `
  -Trigger $trigger -Settings $set -User '<host-user>' -Password '<password>' `
  -RunLevel Highest
```

Two honest caveats, because this is the least clean part of the whole setup:

- **WSL2 instances are per Windows user.** A task running as `SYSTEM` boots a distro in a session the
  interactive user does not share, which produces confusing double-instance behaviour. Running the
  task as the machine's normal user with "run whether user is logged on or not" is the arrangement
  most likely to behave — and it means storing that account's password in Task Scheduler. Put it in
  the password manager and nowhere else; it does not go in the repo.
- **If you chose the raw-disk mount in §6.3**, the mount must be re-issued after every boot. Add a
  second action (`wsl.exe --mount \\.\PHYSICALDRIVE<n> --bare`) that runs *before* the boot action,
  and test it by actually rebooting rather than by reasoning about it.

Add a 30–60 second startup delay if the task proves flaky — the network stack and the NVIDIA driver
both need to be up before containers that claim GPUs start.

**Verify (this is the M1 acceptance test for this doc):** cold-reboot each host, touch nothing, and
confirm within ~5 minutes that `https://chat.ai.lan` answers, the gateway lists its models, and
`nvidia-smi` on `.226` shows the fast-tier model loaded.

---

## 9. Firewall and exposure

Nothing is published to the internet ([`00`](./00-goals-and-constraints.md) §4). The rule is
default-deny inbound, with the ports from §4 opened only to the two internal subnets.

### Windows hosts (`.87`, `.226`)

```powershell
# One rule per service group, scoped to the internal subnets only.
New-NetFirewallRule -DisplayName 'AI Platform - gateway/UI' -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 80,443,4000 `
  -RemoteAddress 10.0.0.0/24,10.0.1.0/24

New-NetFirewallRule -DisplayName 'AI Platform - internal services' -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 5000,5432,7997,8000,8081,8090,8099 `
  -RemoteAddress 10.0.0.0/24,10.0.1.0/24
```

**Mirrored networking adds a second firewall.** With `networkingMode=mirrored` and `firewall=true`,
inbound traffic to the WSL2 instance is also filtered by Hyper-V firewall policy, and its default
inbound action can block LAN clients even when the Windows rules above allow them. If a service is
reachable from its own host but not from another machine, check that first —
`Get-NetFirewallHyperVVMSetting` and `Get-NetFirewallHyperVRule` are the cmdlets to look at, and the
WSL VM is identified by a fixed GUID. **Verify the cmdlet names, parameters and that GUID against
your Windows build**; this surface is newer than the rest of this document and the syntax has moved
between releases.

### `.149`

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 10.0.0.0/24 to any port 22,8000,8099,8188 proto tcp
sudo ufw allow from 10.0.1.0/24 to any port 22,8000,8099,8188 proto tcp
sudo ufw enable
```

Egress lockdown — the part that enforces N1, where only SearXNG may reach the internet — is a
separate job and lives in [`16-web-search-and-egress.md`](./16-web-search-and-egress.md). Do not
consider the egress boundary done because the inbound firewall is.

**Verify:** from a machine on neither subnet, confirm the ports do not answer. From `.87`, confirm
they do.

---

## 10. Verification checklist

Run top to bottom on each host. Every row is a command whose output can be pasted into the shipped
version of this doc.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | WSL version | `wsl --version` | >= 2.0.0 |
| 2 | Caps applied | `wsl -d Ubuntu-24.04 -- nproc; free -g` | Matches `.wslconfig` |
| 3 | systemd up | `systemctl is-system-running` | `running` / `degraded` (inspect failures) |
| 4 | GPU visible | `nvidia-smi` | Correct card, expected driver |
| 5 | No Linux driver | `ls /usr/lib/wsl/lib/libcuda.so*` | Present (WSL hosts) |
| 6 | Torch sees CUDA | `python3 -c "import torch;print(torch.cuda.is_available())"` | `True` |
| 7 | Arch list (`.149`) | `python3 -c "import torch;print(torch.cuda.get_arch_list())"` | `sm_120` present |
| 8 | GPU in containers | `docker run --rm --gpus all nvidia/cuda:*-base nvidia-smi` | Same card |
| 9 | LAN binding | From another host: `curl -sf http://<ip>:4000/health/liveliness` | Answers |
| 10 | Registry round-trip | Push from `.87`, pull from `.226` | Succeeds |
| 11 | TLS | Browse `https://chat.ai.lan` | No warning once the root is installed |
| 12 | Cross-subnet | `ping` / `iperf3` between `.87` and `.149` | Spike 4 thresholds |
| 13 | Reboot | Cold boot, wait 5 min, touch nothing | Everything answers |
| 14 | Don't-disturb | Modelling run plus idle platform on `.226` | Iteration time within noise of ~48 min |
| 15 | Removable | `docker compose down` on each host | Machine is a plain workstation again |

---

## Reflect

**The hard parts of this document are not the CUDA installs.** They are the two places where Windows
does not want to be a server: WSL2 not starting at boot, and mirrored networking's second firewall.
Both have workarounds, both are fiddly, and both must be tested by actually rebooting rather than by
reading. Budget an afternoon for §8 alone.

**The caps on `.226` are the most consequential lines in the file**, and they are in direct tension
with the deep tier: 48 GB is right for coexisting with the modelling runs and nowhere near enough for
a 235B MoE's experts. Rather than split the difference — which would serve neither — we keep two
profiles and accept that switching restarts the host's containers. If M0 spike 7 shows the deep tier
starving the modelling runs anyway, the deep profile never gets used and the question is moot.

What we would revisit first: native Linux on `.226`. Nearly every awkward thing above is a
consequence of WSL2, and a spare NVMe with a dual-boot Ubuntu would remove all of it — at the cost of
the modelling toolchain, which lives on Windows. That is why we did not.

**Next:** [`06-model-gateway.md`](./06-model-gateway.md).
