"""Validate the eval set before anything is ever scored against it.

The eval set is the instrument every retrieval decision gets measured with
(docs/17-evaluation.md), so a malformed record must fail loudly here rather than
silently skew recall@5 at M5. The checks are purely structural -- schema
conformance, id uniqueness, label consistency, progress against the category
targets -- because whether a question is any *good* is a human judgement this
script cannot make.

Usage:
    uv run python eval/check.py                # validate the working draft
    uv run python eval/check.py --frozen       # freeze gate: exact targets met,
                                               # every synthetic example deleted
    uv run python eval/check.py other.jsonl    # validate a frozen version instead

Exit codes: 0 valid, 1 validation errors, 2 file missing or unreadable.

Stdlib only, deliberately: the real questions never leave the network (N1), so
this must run on whatever machine holds them with nothing installed but Python.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

# Categories, letters and target counts come from docs/17-evaluation.md section
# 3.1. Targets are a starting shape, not a rule: a shortfall is reported as
# progress, and only --frozen treats it as an error.
CATEGORIES: dict[str, str] = {
    "A": "easy",
    "B": "multihop",
    "C": "exact-token",
    "D": "paraphrase",
    "E": "unanswerable",
    "F": "near-miss",
}
TARGETS: dict[str, int] = {"A": 12, "B": 8, "C": 10, "D": 10, "E": 8, "F": 2}

# docs/17 section 6.1: A-D are answerable, E-F are not. Enforced because the
# `answerable` label calibrates the relevance gate -- one mislabelled record
# corrupts both the recall measurement and the refusal measurement at once.
ANSWERABLE_PREFIXES = frozenset({"A", "B", "C", "D"})

_ID_RE = re.compile(r"[A-F]-\d{2}")

_REQUIRED = ("id", "category", "answerable", "question", "expected", "answer", "author", "written")
_OPTIONAL = ("notes", "example")


def _is_page(value: object) -> bool:
    # bool is a subclass of int, and `"pages": [true]` should not validate.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _expected_errors(expected: object) -> list[str]:
    """Errors in the `expected` field: a list of {"doc": str, "pages": [int >= 1]}."""
    if not isinstance(expected, list):
        return ["'expected' must be a list"]
    errors: list[str] = []
    for i, item in enumerate(expected):
        where = f"expected[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object with 'doc' and 'pages'")
            continue
        extra = sorted(set(item) - {"doc", "pages"})
        if extra:
            errors.append(f"{where} has unknown keys: {extra}")
        doc = item.get("doc")
        if not isinstance(doc, str) or not doc.strip():
            errors.append(f"{where}.doc must be a non-empty string (the filename as ingested)")
        pages = item.get("pages")
        if not isinstance(pages, list) or not pages or not all(_is_page(p) for p in pages):
            errors.append(f"{where}.pages must be a non-empty list of physical page numbers >= 1")
    return errors


def validate_record(rec: dict[str, object]) -> list[str]:
    """Every schema violation in one record, as readable messages."""
    errors: list[str] = []

    missing = [f for f in _REQUIRED if f not in rec]
    if missing:
        errors.append(f"missing fields: {missing}")
    unknown = [k for k in rec if k not in _REQUIRED and k not in _OPTIONAL]
    if unknown:
        errors.append(f"unknown fields: {unknown} (the scorer reads only the documented schema)")

    rec_id = rec.get("id")
    prefix: str | None = None
    if "id" in rec:
        if isinstance(rec_id, str) and _ID_RE.fullmatch(rec_id):
            prefix = rec_id[0]
        else:
            errors.append(f"id {rec_id!r} must match [A-F]-NN, e.g. 'A-01'")

    category = rec.get("category")
    if "category" in rec:
        if category not in CATEGORIES.values():
            errors.append(f"category {category!r} must be one of {sorted(CATEGORIES.values())}")
        elif prefix is not None and CATEGORIES[prefix] != category:
            errors.append(
                f"id prefix {prefix!r} means category {CATEGORIES[prefix]!r}, not {category!r}"
            )

    answerable = rec.get("answerable")
    if "answerable" in rec and not isinstance(answerable, bool):
        errors.append("'answerable' must be true or false")

    question = rec.get("question")
    if "question" in rec and (not isinstance(question, str) or not question.strip()):
        errors.append("'question' must be a non-empty string")

    if "expected" in rec:
        errors.extend(_expected_errors(rec["expected"]))

    if prefix is not None and isinstance(answerable, bool):
        should_answer = prefix in ANSWERABLE_PREFIXES
        answer = rec.get("answer")
        expected = rec.get("expected")
        if answerable is not should_answer:
            errors.append(
                f"category {CATEGORIES[prefix]!r} requires answerable == "
                f"{str(should_answer).lower()} (docs/17 section 6.1)"
            )
        elif should_answer:
            if not (isinstance(expected, list) and expected):
                errors.append("answerable questions need at least one expected (doc, pages) entry")
            if not isinstance(answer, str) or not answer.strip():
                errors.append("answerable questions need a short reference 'answer' string")
        else:
            if expected != []:
                errors.append("unanswerable questions must have expected == []")
            if answer is not None:
                errors.append("unanswerable questions must have answer == null")

    author = rec.get("author")
    if "author" in rec and (not isinstance(author, str) or not author.strip()):
        errors.append("'author' must be a non-empty string (initials are fine)")

    written = rec.get("written")
    if "written" in rec:
        if not isinstance(written, str):
            errors.append("'written' must be an ISO date string, YYYY-MM-DD")
        else:
            try:
                date.fromisoformat(written)
            except ValueError:
                errors.append(f"'written' {written!r} is not a valid ISO date (YYYY-MM-DD)")

    if "notes" in rec and not isinstance(rec["notes"], str):
        errors.append("'notes' must be a string when present")
    if "example" in rec and not isinstance(rec["example"], bool):
        errors.append("'example' must be true or false when present")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the eval set before anything is ever scored against it."
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("questions.jsonl"),
        help="JSONL file to validate (default: eval/questions.jsonl)",
    )
    parser.add_argument(
        "--frozen",
        action="store_true",
        help="freeze gate: exact category targets and no synthetic examples remaining",
    )
    args = parser.parse_args(argv)
    path: Path = args.path
    frozen: bool = args.frozen

    try:
        # utf-8-sig: Windows editors (and PowerShell redirection) prepend a BOM,
        # which plain utf-8 would hand to json.loads as part of line 1.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    ids: Counter[str] = Counter()
    real: Counter[str] = Counter()
    examples: Counter[str] = Counter()

    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {line_no}: not valid JSON ({exc.msg})")
            continue
        if not isinstance(obj, dict):
            problems.append(f"line {line_no}: each line must be one JSON object")
            continue
        rec: dict[str, object] = obj
        problems.extend(f"line {line_no}: {err}" for err in validate_record(rec))
        rec_id = rec.get("id")
        if isinstance(rec_id, str):
            ids[rec_id] += 1
            if _ID_RE.fullmatch(rec_id):
                bucket = examples if rec.get("example") is True else real
                bucket[rec_id[0]] += 1

    problems.extend(
        f"id {dup!r} appears {n} times; ids must be unique"
        for dup, n in sorted(ids.items())
        if n > 1
    )

    if frozen:
        n_examples = sum(examples.values())
        if n_examples:
            problems.append(
                f"--frozen: {n_examples} synthetic example record(s) remain; delete them"
            )
        problems.extend(
            f"--frozen: category {letter} ({CATEGORIES[letter]}) has "
            f"{real[letter]} records, target {target}"
            for letter, target in TARGETS.items()
            if real[letter] != target
        )

    print(f"{path.name}: {sum(real.values())} real + {sum(examples.values())} example record(s)")
    print(f"  {'category':<16}{'target':>7}{'real':>6}{'example':>9}")
    for letter, name in CATEGORIES.items():
        print(f"  {letter} {name:<14}{TARGETS[letter]:>7}{real[letter]:>6}{examples[letter]:>9}")
    print(
        f"  {'total':<16}{sum(TARGETS.values()):>7}{sum(real.values()):>6}"
        f"{sum(examples.values()):>9}"
    )

    if problems:
        print(file=sys.stderr)
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        print(f"\n{len(problems)} problem(s) found", file=sys.stderr)
        return 1
    print("ok" + (" -- frozen shape satisfied" if frozen else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
