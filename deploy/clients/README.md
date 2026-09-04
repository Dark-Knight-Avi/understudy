# Client config templates — coding agents (M3)

Draft configs for pointing OpenCode and Cline at the LiteLLM gateway. Plan and
bring-up order: [`docs/m3-plan.md`](../../docs/m3-plan.md). Design rationale:
[`docs/09-coding-agents.md`](../../docs/09-coding-agents.md).

**Everything here carries placeholders.** `10.0.0.87` is the repo's stand-in
for the hub; the real address lives in `deploy/fleet.local.yaml` and each
host's `.env`, which never leave the hosts. Keys are referenced as
`{env:UNDERSTUDY_KEY}` / `${UNDERSTUDY_KEY}` and are issued per person by the
operator (`docs/m3-plan.md` §3 step 5). **Never write a real address or key
into any of these files** — they are committed.

| File | What it is | Where it goes |
|---|---|---|
| `opencode.json` | Working OpenCode global config (JSON forbids comments; notes below) | `~/.config/opencode/opencode.json` on the developer's machine |
| `cline.settings.jsonc` | **Transcription sheet** — Cline keeps provider settings in VS Code internal/secret storage, so there is no file to drop; type these into the Cline settings panel | Read it, enter values in the ⚙ panel |
| `clineignore.example` | Ignore file: context budget + keeps secrets out of prompts | `.clineignore` at the root of each repo you work on |

## Filling in `opencode.json`

1. Replace `10.0.0.87` in `baseURL` with the real hub address (ask the
   operator). Keep the `/v1` suffix.
2. Put your personal key in your shell profile, not in the file:
   `export UNDERSTUDY_KEY=sk-...` — the `{env:UNDERSTUDY_KEY}` reference picks
   it up (OpenCode also supports `{file:...}` if you prefer a mode-600 file
   outside any repo).
3. Leave these alone unless you know why you're changing them:
   - `"share": "disabled"` — OpenCode's share feature uploads conversation
     content (our source code) to opencode.ai. N1 says no. Do not re-enable.
   - `"autoupdate": false` — the team pins one OpenCode version
     (`docs/m3-plan.md` §4.1); upgrades happen deliberately, one person first.
   - The `limit` blocks — they keep the client compacting *below* the
     server's window so sessions fail soft instead of dying mid-task, and the
     window is shared by everyone using `.226`. Raising them costs your
     colleagues concurrency before it helps you.
4. `coder` disappearing from `/models` (or answering "model not found") means
   the `.226` GPU is claimed by the person sitting at it. That is the platform
   working as designed: check the fleet dashboard, wait, or use Aider for a
   narrow change. Details: `docs/m3-plan.md` §6.
