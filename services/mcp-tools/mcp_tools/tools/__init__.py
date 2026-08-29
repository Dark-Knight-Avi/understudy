"""Machinery every tool shares: one envelope, one log line, one artefact store.

Five tools in five modules would otherwise grow five slightly different ways of
saying "no". docs/14 section 4.4 is blunt about why that matters: a tool that
hangs is worse than a tool that fails, and the *wording* of a failure changes
what the agent does next -- "host in use" stops it retrying, "temporarily busy"
tells it to come back. So the envelope lives here and nowhere else.

This module holds no I/O of its own beyond writing an artefact to disk.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

_AUDIT = logging.getLogger("mcp_tools.audit")

ToolResult = dict[str, Any]
"""What every tool returns, success or failure. Never raise into the protocol.

Client behaviour on a protocol-level error varies between Open WebUI, OpenCode
and Cline; a plain result the model can read is useful in all three.
"""


# --------------------------------------------------------------------- envelope


def ok(**fields: Any) -> ToolResult:
    """A successful result."""
    return {"ok": True, **fields}


def unavailable(service: str, detail: str) -> ToolResult:
    """A backing service could not serve us. `detail` is read aloud to the user.

    Write it as a sentence a person should see, because the model will repeat it:
    "host .226 is in use by its owner" turns the sharing policy (docs/03) into an
    explanation instead of a bug report.
    """
    return {"ok": False, "error": "unavailable", "service": service, "detail": detail}


def refused(tool: str, detail: str) -> ToolResult:
    """We said no on purpose -- policy, or an argument we will not accept.

    Distinct from `unavailable` because retrying will not help, and the model
    needs to be able to tell those apart.
    """
    return {"ok": False, "error": "refused", "tool": tool, "detail": detail}


# ------------------------------------------------------------------- audit log


def log_tool_call(tool: str, *, outcome: str, duration_ms: float, **fields: Any) -> None:
    """One structured line per invocation (docs/14 section 4.5).

    Two reasons, and the second is the load-bearing one. Tool-*selection* failures
    are otherwise invisible -- if the model keeps reaching for `web_search` to
    answer questions about internal documents, only this log shows it. And
    `web_search` arguments are the entire outbound surface of the platform, so
    this is where ADR-0004's promise that every outbound query is logged is kept.

    Callers log the query for `search_documents` but never the retrieved passages:
    copying document text into a log file guarded less carefully than the database
    quietly undoes the point of the project.
    """
    _AUDIT.info(
        json.dumps(
            {"tool": tool, "outcome": outcome, "duration_ms": round(duration_ms, 1), **fields},
            ensure_ascii=False,
            default=str,
        )
    )


class Timer:
    """Elapsed milliseconds, for the duration field of the audit line."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0


# --------------------------------------------------------------- artefact store


def store_artifact(
    data: bytes, *, suffix: str, filename: str, directory: Path, base_url: str
) -> ToolResult:
    """Write bytes under a random id and describe them (docs/15 section 5).

    The id is ours, never a model-supplied name: a name that reaches the
    filesystem is a path-traversal argument waiting to be discovered. The
    human-readable name survives as metadata, to be set in Content-Disposition
    when Caddy serves the file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    path = directory / f"{file_id}{suffix}"
    path.write_bytes(data)
    return {
        "file_id": file_id,
        "filename": safe_filename(filename, suffix),
        "url": f"{base_url.rstrip('/')}/{file_id}{suffix}",
        "bytes": len(data),
    }


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, suffix: str) -> str:
    """A display name, stripped of anything that could act as a path.

    This never touches the filesystem -- `store_artifact` writes under a uuid --
    but the string is handed to a browser in a download header, so it is cleaned
    at the same place it is created rather than at the place someone forgets.
    """
    # Tokens that are nothing but dots are dropped rather than trimmed: ".." is
    # the interesting half of a traversal string, and a name that still reads as
    # one invites the next person to trust it somewhere it matters.
    words = [word for word in _UNSAFE_NAME.sub(" ", name).split() if word.strip(".")]
    cleaned = " ".join(words)[:80].strip(". ") or "artifact"
    return cleaned if cleaned.lower().endswith(suffix.lower()) else f"{cleaned}{suffix}"


# ------------------------------------------------------------------ degradation


class CircuitBreaker:
    """Fail fast after repeated failures instead of waiting out every timeout.

    docs/14 section 4.4 rule 5. Without this, a backend that is simply down costs
    every subsequent turn the full timeout -- ten seconds of an agent doing
    nothing, repeatedly, which reads to the user as the whole platform being slow.

    Deliberately not thread-safe: the server is single-process asyncio, and a lock
    here would buy nothing but a chance to deadlock.
    """

    def __init__(self, *, failures: int = 3, cooldown_s: float = 60.0) -> None:
        self._threshold = failures
        self._cooldown_s = cooldown_s
        self._consecutive = 0
        self._opened_at: float | None = None

    def allow(self, *, now: float | None = None) -> bool:
        """False while the breaker is open. Check before making the call."""
        if self._opened_at is None:
            return True
        clock = time.monotonic() if now is None else now
        if clock - self._opened_at >= self._cooldown_s:
            self._opened_at = None
            self._consecutive = 0
            return True
        return False

    def record_success(self) -> None:
        self._consecutive = 0
        self._opened_at = None

    def record_failure(self, *, now: float | None = None) -> None:
        self._consecutive += 1
        if self._consecutive >= self._threshold:
            self._opened_at = time.monotonic() if now is None else now


class RecentContext:
    """Shingles of passages `search_documents` recently returned.

    This exists for exactly one leak, docs/16 section 6.1: an agent that has just
    retrieved a passage decides it wants more context and searches the web for a
    sentence lifted straight out of it. Nothing about that looks like a mistake --
    the tool call is well formed, the policy is broken, and the audit log records
    it after the fact.

    The check is a word-shingle overlap, which is approximate by construction. It
    converts the common copy-paste case into a refusal; it is not a guarantee, and
    docs/16's Reflect section says so plainly. The strict answer is turning web
    search off for that workspace, which is already supported.
    """

    def __init__(self, *, shingle_words: int = 12, max_passages: int = 40) -> None:
        self._n = shingle_words
        self._passages: deque[frozenset[str]] = deque(maxlen=max_passages)

    def remember(self, texts: list[str]) -> None:
        for text in texts:
            shingles = _shingles(text, self._n)
            if shingles:
                self._passages.append(shingles)

    def overlaps(self, query: str) -> bool:
        """True if the query shares an n-word run with anything recently retrieved."""
        candidate = _shingles(query, self._n)
        if not candidate:
            return False
        return any(candidate & seen for seen in self._passages)

    def clear(self) -> None:
        self._passages.clear()


_WORD = re.compile(r"[a-z0-9']+")


def _shingles(text: str, n: int) -> frozenset[str]:
    """Overlapping n-word runs, lowercased. Empty when the text is shorter than n."""
    words = _WORD.findall(text.lower())
    if len(words) < n:
        return frozenset()
    return frozenset(" ".join(words[i : i + n]) for i in range(len(words) - n + 1))


# ------------------------------------------------------------------------ text


def truncate(text: str, limit: int) -> str:
    """Cap a string with an ellipsis.

    Used everywhere the alternative is hoping: python-pptx cannot measure text, so
    a bullet that is too long produces a slide with words running off the bottom
    rather than an error (docs/15 section 3.3).
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
