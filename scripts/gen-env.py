#!/usr/bin/env python3
"""Fill a host's `.env` from its `.env.example`, generating every secret.

Hand-editing these files goes wrong in a specific, quiet way: the Postgres
password appears *twice*, once as `POSTGRES_PASSWORD` and again embedded in
`DATABASE_URL` and `LITELLM_DATABASE_URL`. Set one and miss the others and
Postgres rejects the connection with an authentication error that points at
credentials rather than at the URL that still says CHANGE_ME.

So this generates once and substitutes everywhere.

    python3 scripts/gen-env.py deploy/host-87

Values are never printed. If you lose them, `WEBUI_SECRET_KEY` invalidates every
session and `POSTGRES_PASSWORD` means restoring from backup -- copy the finished
file into a password manager.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

# Empty values for these get a fresh random secret.
SECRET_KEYS = {
    "POSTGRES_PASSWORD",
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "LITELLM_UI_PASSWORD",
    "VLLM_226_KEY",
    "VLLM_87_KEY",
    "VLLM_210_KEY",
    "INFINITY_KEY",
    "RAG_SERVICE_KEY",
    "OPEN_WEBUI_GATEWAY_KEY",
    "RAGFLOW_GATEWAY_KEY",
    "WEBUI_SECRET_KEY",
    "SEARXNG_SECRET",
    "MCP_TOKEN",
    "FLEET_TOKEN",
    "AGENT_TOKEN",
}

# LiteLLM rejects a master key without this prefix, and the error it gives
# points at authentication rather than at the key's shape.
PREFIXED = {"LITELLM_MASTER_KEY": "sk-"}

# Sensible non-secret defaults for keys that would otherwise be blank.
DEFAULTS = {"LITELLM_UI_USERNAME": "admin"}

PLACEHOLDER = "CHANGE_ME"
LINE = re.compile(r"^(?P<key>[A-Z0-9_]+)=(?P<val>.*?)(?P<comment>\s+#.*)?$")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host_dir", type=Path, help="e.g. deploy/host-87")
    ap.add_argument("--force", action="store_true", help="overwrite an existing .env")
    args = ap.parse_args()

    example = args.host_dir / ".env.example"
    target = args.host_dir / ".env"

    if not example.is_file():
        print(f"no {example}", file=sys.stderr)
        return 1
    if target.exists() and not args.force:
        # Regenerating over a live .env would rotate every secret and orphan the
        # data encrypted under the old ones. Refuse rather than ask.
        print(
            f"{target} already exists. Use --force only if you mean to rotate "
            f"EVERY secret -- existing sessions and the Postgres role will break.",
            file=sys.stderr,
        )
        return 1

    generated: list[str] = []
    defaulted: list[str] = []
    blank: list[str] = []
    postgres_password = ""
    out: list[str] = []

    for raw in example.read_text(encoding="utf-8").splitlines():
        m = LINE.match(raw)
        if not m or raw.lstrip().startswith("#"):
            out.append(raw)
            continue

        key, val, comment = m["key"], m["val"].strip(), m["comment"] or ""

        if not val and key in SECRET_KEYS:
            val = PREFIXED.get(key, "") + secrets.token_hex(24)
            generated.append(key)
            if key == "POSTGRES_PASSWORD":
                postgres_password = val
        elif not val and key in DEFAULTS:
            val = DEFAULTS[key]
            defaulted.append(key)
        elif not val:
            blank.append(key)

        out.append(f"{key}={val}{comment}")

    text = "\n".join(out) + "\n"

    # The whole reason this script exists.
    if postgres_password:
        text = text.replace(PLACEHOLDER, postgres_password)

    target.write_text(text, encoding="utf-8")
    target.chmod(0o600)

    print(f"wrote {target} (mode 600)")
    print(f"  generated {len(generated)} secrets: {', '.join(sorted(generated))}")
    if defaulted:
        print(f"  defaulted: {', '.join(sorted(defaulted))}")
    if postgres_password:
        print(f"  substituted the Postgres password into every {PLACEHOLDER} URL")
    if blank:
        print(f"\n  STILL BLANK -- set these by hand: {', '.join(sorted(blank))}")
    print("\n  Values are not shown. Copy the file into a password manager now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
