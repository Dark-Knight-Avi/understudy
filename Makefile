# ai-platform -- see docs/delivery-plan.md section 4
#
# No CI. Three hosts and one operator do not justify one; add it later if it hurts.

REGISTRY ?= 10.0.0.87:5000
VERSION  ?= dev
SERVICE  ?=
HOST     ?=

.DEFAULT_GOAL := help

.PHONY: help
help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- development

# `uv sync` is EXACT by default: it uninstalls anything outside the requested
# groups. Switching between these two targets without --inexact throws away the
# ~2.5 GB CUDA PyTorch download every time, so both keep what the other installed.

.PHONY: setup
setup:  ## dev machine: services + tooling, no CUDA torch
	uv sync --all-packages --group dev --inexact

.PHONY: setup-spikes
setup-spikes:  ## hosts: dev tooling PLUS PyTorch w/ CUDA (~2.5 GB) for the M0 spikes
	uv sync --all-packages --group dev --group spikes --inexact

.PHONY: lint
lint:  ## ruff + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy services/

.PHONY: fix
fix:  ## autofix what ruff can
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: test
test:  ## pytest
	uv run pytest -q

# ---------------------------------------------------------------------- M0

.PHONY: spikes
spikes:  ## run the non-destructive M0 spikes on this host
	@mkdir -p results
	uv run --group spikes scripts/spike_01_vram.py --json results/spike01-$$(hostname).json

# ------------------------------------------------------------------- release

.PHONY: build
build:  ## build+push one service: make build SERVICE=mcp-tools VERSION=0.1.0
	@test -n "$(SERVICE)" || { echo "SERVICE= is required"; exit 1; }
	@test "$(VERSION)" != "dev" || { echo "VERSION= is required; never ship 'dev'"; exit 1; }
	docker build -t $(REGISTRY)/$(SERVICE):$(VERSION) services/$(SERVICE)
	docker push $(REGISTRY)/$(SERVICE):$(VERSION)

.PHONY: deploy
deploy:  ## deploy a host: make deploy HOST=87
	@test -n "$(HOST)" || { echo "HOST= is required (87|226|149)"; exit 1; }
	@echo "Pinned tags only -- never 'latest'. Rollback is editing the tag back."
	cd deploy/host-$(HOST) && docker compose pull && docker compose up -d

.PHONY: down
down:  ## stop a host's stack: make down HOST=87
	@test -n "$(HOST)" || { echo "HOST= is required"; exit 1; }
	cd deploy/host-$(HOST) && docker compose down

.PHONY: remove
remove:  ## full removal -- returns the host to a plain workstation
	@echo "See docs/18-operations.md; there is non-Docker residue"
	@echo "(scheduled task, .wslconfig, powercfg, firewall rules) to undo too."

# --------------------------------------------------------------------- checks

.PHONY: secrets-scan
secrets-scan:  ## fail if credentials look committed
	@# Scans only git-tracked files -- what would actually be committed. Scanning the
	@# worktree instead matches .venv and every vendored library that mentions a password.
	@# Patterns use character classes (c[i]stup) so this rule does not match itself.
	@! git ls-files -z | xargs -0 grep -niE 'c[i]stup@|passw[o]rd[[:space:]]*[:=][[:space:]]*[^ $$]' \
	  || { echo "POSSIBLE CREDENTIAL FOUND -- do not commit"; exit 1; }
	@echo "clean"
