"""Tests for `generate_pdf`, mocked at the subprocess boundary.

docs/15 section 6 says tests 2 and 3 -- adversarial body text and a nonsense
template name -- "are the ones that will actually be skipped, and they are the
ones protecting the hub host from its own renderer". So they are the first two
classes here.

`TestNothingEscapesAStringLiteral` is the security property this module exists
for, stated as an invariant rather than a list of bad inputs: after removing
every Typst string literal from the generated source, none of the model's text
remains. If that holds, there is no context where a `#` the model wrote could
begin an expression, and the list of dangerous constructs stops mattering.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mcp_tools.tools import pdf as pdf_module
from mcp_tools.tools.pdf import build_source, convert, generate_pdf, typst_str

ADVERSARIAL = """
#read("/etc/passwd")
#include "../../../secrets.typ"
#image("/proc/self/environ")
$ x = { unbalanced
\\ backslash and "quotes" and #let evil = 1
"""


def outside_string_literals(source: str) -> str:
    """Everything in the generated source that is *not* inside a `"..."` literal."""
    out: list[str] = []
    in_string = False
    escaped = False
    for char in source:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        else:
            out.append(char)
    return "".join(out)


class TestNothingEscapesAStringLiteral:
    """The invariant the whole converter is built to hold."""

    @pytest.mark.parametrize(
        "canary",
        [
            "#read",
            "/etc/passwd",
            "#include",
            "secrets.typ",
            "#image",
            "evil",
            "unbalanced",
        ],
    )
    def test_adversarial_body_never_reaches_code_context(self, canary: str) -> None:
        source = build_source("Report", ADVERSARIAL, "report")
        assert canary not in outside_string_literals(source)

    def test_the_title_is_escaped_too(self) -> None:
        """docs/15: use the escaper for every value from the model, including the title."""
        source = build_source('#read("/etc/passwd")', "body", "report")
        assert "/etc/passwd" not in outside_string_literals(source)

    def test_a_body_of_pure_quotes_and_backslashes_stays_balanced(self) -> None:
        """A literal that ends early would put the rest of the body in code context."""
        source = build_source("T", '"" \\ \\" """ \\\\', "report")
        assert source.count('"') % 2 == 0
        assert "\\" not in outside_string_literals(source)


class TestTypstStr:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain", '"plain"'),
            ('say "hi"', '"say \\"hi\\""'),
            ("back\\slash", '"back\\\\slash"'),
            ("two\nlines", '"two\\nlines"'),
            ("tab\there", '"tab\\there"'),
            ("bell\x07", '"bell\\u{7}"'),
        ],
    )
    def test_escapes(self, raw: str, expected: str) -> None:
        assert typst_str(raw) == expected


class TestConverter:
    """The closed subset from docs/15 section 2.3."""

    def test_headings_become_heading_calls(self) -> None:
        out = convert("# One\n\n## Two\n\n### Three")
        assert "#heading(level: 1)" in out
        assert "#heading(level: 2)" in out
        assert "#heading(level: 3)" in out

    def test_inline_emphasis_and_code(self) -> None:
        out = convert("a **bold** and *italic* and `code` word")
        assert "#strong(" in out and "#emph(" in out and "#raw(" in out

    def test_fenced_code_block_keeps_its_language(self) -> None:
        out = convert('```python\nprint("hi")\n```')
        assert "#raw(block: true, lang: " in out
        assert '\\"hi\\"' in out, "code contents must still be a literal"

    def test_code_block_contents_are_not_interpreted_as_markdown(self) -> None:
        out = convert("```\n# not a heading\n- not a list\n```")
        assert "#heading" not in out
        assert "#list(" not in out

    def test_bullet_and_numbered_lists(self) -> None:
        assert "#list(" in convert("- one\n- two")
        assert "#enum(" in convert("1. one\n2. two")

    def test_pipe_table(self) -> None:
        out = convert("| a | b |\n|---|---|\n| 1 | 2 |")
        assert "#table(columns: 2," in out

    def test_a_paragraph_containing_a_pipe_is_not_a_table(self) -> None:
        """Without the `|---|` separator row, it is prose that happens to have a bar."""
        assert "#table(" not in convert("| this is just text about a | character")

    def test_unsupported_markup_becomes_visible_text_not_an_error(self) -> None:
        """docs/15's last table row: an unsupported construct is text, not a failure."""
        out = convert("> a blockquote\n\n![img](http://x/y.png)")
        assert "#text(" in out
        assert "blockquote" in out

    def test_an_empty_body_still_produces_a_whole_document(self) -> None:
        """A model that sends no body should get a title page, not a compile error."""
        assert convert("") == ""
        source = build_source("T", "", "report")
        assert '#show: report.with(title: "T")' in source
        assert source.count('"') % 2 == 0


class TestTemplateSelection:
    def test_unknown_template_is_refused_with_the_valid_names(self, tmp_path: Path) -> None:
        """docs/15 acceptance test 3. The name is a dict key, so no path is built at all."""
        result = asyncio.run(
            generate_pdf(
                "T",
                "body",
                "../../etc/passwd",
                typst_bin="typst",
                timeout_s=5.0,
                artifact_dir=tmp_path,
                artifact_base_url="https://ai.internal/artifacts",
            )
        )
        assert result["error"] == "refused"
        assert "report" in result["detail"]
        assert list(tmp_path.iterdir()) == []


def _render(
    tmp_path: Path, *, typst_bin: str = "typst", title: str = "Quarterly report"
) -> dict[str, Any]:
    return asyncio.run(
        generate_pdf(
            title,
            "# Heading\n\nSome prose.",
            "report",
            typst_bin=typst_bin,
            timeout_s=5.0,
            artifact_dir=tmp_path,
            artifact_base_url="https://ai.internal/artifacts",
        )
    )


class TestCompilation:
    """Typst itself is never run. We mock at the subprocess boundary."""

    def test_happy_path_stores_the_pdf_and_returns_a_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            recorded.append(args)
            Path(args[-1]).write_bytes(b"%PDF-1.7\n")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)
        result = _render(tmp_path)

        assert result["ok"] is True
        assert result["filename"] == "Quarterly report.pdf"
        assert result["url"].startswith("https://ai.internal/artifacts/")
        assert (tmp_path / f"{result['file_id']}.pdf").read_bytes() == b"%PDF-1.7\n"

        argv = recorded[0]
        assert "--root" in argv, "sandboxing must not be optional"
        root = Path(argv[argv.index("--root") + 1])
        assert Path(argv[-2]).parent == root, "the source must live inside the root"
        assert not str(tmp_path).startswith(str(root)), (
            "the artefact volume must not be inside the renderer's root"
        )

    def test_never_uses_a_shell(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An f-string command is one careless interpolation from being a shell injection."""
        seen: dict[str, Any] = {}

        def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            seen.update(kwargs)
            seen["args"] = args
            Path(args[-1]).write_bytes(b"%PDF")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)
        _render(tmp_path)
        assert isinstance(seen["args"], list)
        assert seen.get("shell") is not True
        assert seen["timeout"] == 5.0, "a runaway compile must not pin a core"

    def test_a_missing_binary_is_unavailable_not_a_crash(self, tmp_path: Path) -> None:
        result = _render(tmp_path, typst_bin="typst-that-does-not-exist")
        assert result["error"] == "unavailable"
        assert result["service"] == "typst"

    def test_a_timeout_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(args, 5.0)

        monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)
        assert _render(tmp_path)["error"] == "unavailable"

    def test_a_compile_error_returns_truncated_stderr_so_a_retry_is_useful(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/15 section 2.4: Typst's errors are actionable, so hand them back."""

        def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.CalledProcessError(
                1, args, b"", b"error: unclosed delimiter\n  main.typ:12:4\n" + b"x" * 5000
            )

        monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)
        result = _render(tmp_path)
        assert result["error"] == "refused"
        assert "unclosed delimiter" in result["detail"]
        assert len(result["detail"]) < 2000, "stderr must not flood the context window"
