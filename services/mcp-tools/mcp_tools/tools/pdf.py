"""`generate_pdf` -- the model supplies content, the template supplies form.

docs/15 section 1 states the rule and section 2.4 states the threat: Typst can
read files, and model output is untrusted input. The obvious implementation --
paste `body_markdown` into a template and compile -- is both unreliable (a 14-30B
model emits subtly invalid markup routinely) and an injection vector.

**The one design decision in this module.** docs/15 section 2.3 sketches a
converter that escapes markup characters with backslashes. This implementation
does something stricter: every character the model produced is emitted inside a
Typst **string literal**, and the only structure comes from function calls this
module writes (`#heading`, `#strong`, `#raw`, `#table`). Escaping a string literal
needs exactly three rules -- backslash, quote, control character -- all of which
are unambiguous, whereas "escape every character Typst treats as markup" is a set
that has to stay correct across Typst releases. `#read("/etc/passwd")` in the body
becomes the seventeen literal characters of that text, because there is no
context in the output where a `#` from the model could begin an expression.

The `--root` confinement and the subprocess timeout in `_compile` are the second
and third layers. Defend twice, as the doc says.
"""

from __future__ import annotations

import re
import subprocess  # nosec - argument list only, never a shell; see _compile
import tempfile
from pathlib import Path

import anyio.to_thread

from mcp_tools.tools import (
    Timer,
    ToolResult,
    log_tool_call,
    ok,
    refused,
    store_artifact,
    truncate,
    unavailable,
)

MAX_BODY_CHARS = 200_000
"""A runaway compile must not pin a core on the host everything else depends on."""

MAX_TABLE_COLUMNS = 12
MAX_STDERR_CHARS = 1_200


# --------------------------------------------------------------------- templates

_REPORT = """
#let report(title: "", body) = {
  set document(title: title)
  set page(paper: "a4", margin: (x: 2.2cm, y: 2.4cm),
           footer: context align(center, counter(page).display("1")))
  set text(font: ("Inter", "DejaVu Sans"), size: 10.5pt, lang: "en")
  set par(justify: true, leading: 0.65em)
  show heading.where(level: 1): it => block(above: 1.4em, below: 0.7em, text(size: 16pt, it))
  show heading.where(level: 2): it => block(above: 1.1em, below: 0.6em, text(size: 13pt, it))
  block(text(size: 22pt, weight: "bold", title))
  line(length: 100%, stroke: 0.4pt + luma(180))
  v(1em)
  body
}
"""

_MEMO = """
#let report(title: "", body) = {
  set document(title: title)
  set page(paper: "a4", margin: 2.5cm)
  set text(font: ("Inter", "DejaVu Sans"), size: 11pt, lang: "en")
  set par(leading: 0.7em)
  block(text(size: 15pt, weight: "bold", title))
  v(0.8em)
  body
}
"""

TYPST_TEMPLATES: dict[str, str] = {"report": _REPORT, "memo": _MEMO}
"""Templates as source strings, not files on disk.

Two reasons. The whole compilation unit then lives inside the scratch directory
that `--root` confines Typst to, so there is no path outside it that a template
could even name. And the template name from the model is a dict key, never a
path component -- docs/15 acceptance test 3 asks for a clear refusal on a
nonsense template name with no traversal attempted, and a lookup that cannot
build a path is a stronger answer than a lookup that validates one.

The font tuple is a deployment concern: install these inside the image and keep
the metric-compatible fallback, or documents rendered on two hosts will disagree.
"""


class TypstUnavailable(RuntimeError):
    """Typst is missing, or took longer than we are willing to wait."""


class TypstCompileError(RuntimeError):
    """Typst rejected the document we generated. Carries truncated stderr.

    Handed back to the model because Typst's errors are actionable -- docs/15
    section 2.4 -- so a failed render can be retried usefully. If this fires on
    ordinary prose it is a bug in the converter below, not in the model's input.
    """


# ---------------------------------------------------------------- string literal


def typst_str(value: str) -> str:
    """Quote a Python string as a Typst string literal.

    The single escaping rule in this module, and the reason the rest of it is
    safe. Three cases and a catch-all for control characters; nothing here has to
    track what Typst considers markup.
    """
    out = ['"']
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            continue
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{{{ord(char):x}}}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


# -------------------------------------------------------- markdown -> typst


_INLINE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<strong>[^*\n]+)\*\*"
    r"|__(?P<strong2>[^_\n]+)__"
    r"|\*(?P<emph>[^*\n]+)\*"
    r"|_(?P<emph2>[^_\n]+)_"
)
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^\s{0,6}[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s{0,6}\d{1,3}[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*```+\s*([A-Za-z0-9_+-]{0,20})\s*$")


def _inline(text: str) -> str:
    """One line of markdown as a concatenation of Typst content expressions.

    Unsupported constructs are not stripped and not errors -- they fall through
    into a `#text(...)` run and appear as literal characters, which is docs/15
    section 2.3's last table row and the whole point of a closed subset.
    """
    parts: list[str] = []
    cursor = 0
    for match in _INLINE.finditer(text):
        if match.start() > cursor:
            parts.append(f"#text({typst_str(text[cursor : match.start()])})")
        if (code := match.group("code")) is not None:
            parts.append(f"#raw({typst_str(code)})")
        elif (strong := match.group("strong") or match.group("strong2")) is not None:
            parts.append(f"#strong({typst_str(strong)})")
        elif (emph := match.group("emph") or match.group("emph2")) is not None:
            parts.append(f"#emph({typst_str(emph)})")
        cursor = match.end()
    if cursor < len(text):
        parts.append(f"#text({typst_str(text[cursor:])})")
    return "".join(parts) or '#text("")'


def _table_rows(lines: list[str]) -> list[list[str]] | None:
    """Parse a pipe table, or None if these lines are not one.

    Requires the `|---|---|` separator, so a paragraph that happens to contain a
    pipe stays a paragraph.
    """
    if len(lines) < 2 or not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", lines[1]):
        return None
    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        if index == 1:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells[:MAX_TABLE_COLUMNS])
    width = max(len(row) for row in rows)
    if width < 1:
        return None
    return [row + [""] * (width - len(row)) for row in rows]


def convert(markdown: str) -> str:
    """Markdown to Typst, over the closed subset in docs/15 section 2.3."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    ordered: list[str] = []
    index = 0

    def flush() -> None:
        if paragraph:
            out.append(_inline(" ".join(paragraph)))
            paragraph.clear()
        if bullets:
            out.append("#list(" + ", ".join(f"[{item}]" for item in bullets) + ")")
            bullets.clear()
        if ordered:
            out.append("#enum(" + ", ".join(f"[{item}]" for item in ordered) + ")")
            ordered.clear()

    while index < len(lines):
        line = lines[index]

        if fence := _FENCE.match(line):
            flush()
            lang = fence.group(1)
            body: list[str] = []
            index += 1
            while index < len(lines) and not _FENCE.match(lines[index]):
                body.append(lines[index])
                index += 1
            lang_arg = f"lang: {typst_str(lang)}, " if lang else ""
            out.append(f"#raw(block: true, {lang_arg}{typst_str(chr(10).join(body))})")
            index += 1
            continue

        if line.lstrip().startswith("|"):
            block = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                block.append(lines[index])
                index += 1
            if (rows := _table_rows(block)) is not None:
                flush()
                cells = ", ".join(f"[{_inline(cell)}]" for row in rows for cell in row)
                out.append(f"#table(columns: {len(rows[0])}, {cells})")
                continue
            paragraph.extend(block)
            continue

        if heading := _HEADING.match(line):
            flush()
            level = len(heading.group(1))
            out.append(f"#heading(level: {level})[{_inline(heading.group(2))}]")
        elif bullet := _BULLET.match(line):
            if paragraph or ordered:
                flush()
            bullets.append(_inline(bullet.group(1)))
        elif item := _ORDERED.match(line):
            if paragraph or bullets:
                flush()
            ordered.append(_inline(item.group(1)))
        elif not line.strip():
            flush()
        else:
            if bullets or ordered:
                flush()
            paragraph.append(line.strip())
        index += 1

    flush()
    return "\n\n".join(out)


# ------------------------------------------------------------------ compilation


def _compile(source: str, *, typst_bin: str, timeout_s: float) -> bytes:
    """Compile Typst source in a throwaway root and return the PDF bytes.

    Every defence from docs/15 section 2.4 is in these few lines: a fresh temp
    directory per render (no shared scratch between requests), `--root` pointed at
    it so Typst refuses every path outside, an argument list rather than a shell,
    and a timeout. The output is written inside the sandbox and read back, so the
    artefact volume is not somewhere the renderer can address at all.
    """
    with tempfile.TemporaryDirectory(prefix="mcp-typst-") as tmp:
        root = Path(tmp)
        src = root / "main.typ"
        out = root / "main.pdf"
        src.write_text(source, encoding="utf-8")
        try:
            subprocess.run(  # noqa: S603 - argument list, no shell, fixed binary
                [typst_bin, "compile", "--root", str(root), str(src), str(out)],
                check=True,
                timeout=timeout_s,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise TypstUnavailable(f"typst binary '{typst_bin}' not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise TypstUnavailable(f"compile exceeded {timeout_s:.0f}s") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", "replace")
            raise TypstCompileError(truncate(stderr, MAX_STDERR_CHARS)) from exc
        return out.read_bytes()


def build_source(title: str, body_markdown: str, template: str) -> str:
    """The complete Typst document. Separated out so tests can read it."""
    return (
        f"{TYPST_TEMPLATES[template]}\n"
        f"#show: report.with(title: {typst_str(title)})\n\n"
        f"{convert(body_markdown)}\n"
    )


async def generate_pdf(
    title: str,
    body_markdown: str,
    template: str,
    *,
    typst_bin: str,
    timeout_s: float,
    artifact_dir: Path,
    artifact_base_url: str,
) -> ToolResult:
    """Render a report to PDF and store it. Never raises."""
    timer = Timer()
    title = title.strip()
    if not title:
        return refused("generate_pdf", "A title is required.")
    if template not in TYPST_TEMPLATES:
        valid = ", ".join(sorted(TYPST_TEMPLATES))
        return refused("generate_pdf", f"Unknown template '{template}'. Valid: {valid}.")
    if len(body_markdown) > MAX_BODY_CHARS:
        return refused(
            "generate_pdf",
            f"Body is {len(body_markdown)} characters; the limit is {MAX_BODY_CHARS}.",
        )

    source = build_source(title, body_markdown, template)
    try:
        # Typst blocks for as long as it takes; the event loop serves four other
        # tools while it does.
        pdf = await anyio.to_thread.run_sync(
            lambda: _compile(source, typst_bin=typst_bin, timeout_s=timeout_s)
        )
    except TypstUnavailable as exc:
        log_tool_call("generate_pdf", outcome="unavailable", duration_ms=timer.ms)
        return unavailable("typst", f"PDF rendering is offline: {exc}")
    except TypstCompileError as exc:
        # A refusal rather than an unavailability: retrying the same body will
        # fail the same way, and the stderr says what to change.
        log_tool_call("generate_pdf", outcome="compile_error", duration_ms=timer.ms)
        return refused("generate_pdf", f"Typst could not compile the document: {exc}")

    artifact = store_artifact(
        pdf,
        suffix=".pdf",
        filename=title,
        directory=artifact_dir,
        base_url=artifact_base_url,
    )
    log_tool_call(
        "generate_pdf",
        outcome="ok",
        duration_ms=timer.ms,
        template=template,
        bytes=artifact["bytes"],
    )
    return ok(**artifact)
