"""Build a version-bound review-input.v1 from an acquired GitHub PR."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from review_input import require_review_input


class PRAdapterError(ValueError):
    pass


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
_FUNCTION = re.compile(
    r"(?m)^[ \t]*(?!if\b|for\b|while\b|switch\b|catch\b|return\b)"
    r"(?:[\w:*&<>~]+[ \t]+)+[*& \t]*(?P<name>[A-Za-z_~][\w:]*)[ \t]*"
    r"\([^;{}]*\)[ \t]*(?:const[ \t]*)?(?:noexcept[ \t]*)?\{"
)
_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "core.autocrlf=false", *args],
        text=True, encoding="utf-8", errors="replace", capture_output=True,
    )
    if result.returncode:
        raise PRAdapterError("Git failed: " + (result.stderr.strip().splitlines() or ["unknown error"])[-1])
    return result.stdout


def _changed_lines(patch: str) -> set[int]:
    lines: set[int] = set()
    for match in _HUNK.finditer(patch):
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        # A deleted-only hunk is anchored to its surviving line in the head.
        lines.update(range(max(1, start), max(1, start) + max(1, count)))
    return lines


def _functions(source: str) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    for match in _FUNCTION.finditer(source):
        depth = 1
        cursor = match.end()
        # This lightweight scanner is deliberately conservative; callers may provide
        # explicit targets when macros or complex declarations defeat it.
        while cursor < len(source) and depth:
            char = source[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            cursor += 1
        if depth == 0:
            found.append((match.group("name"), source.count("\n", 0, match.start()) + 1,
                          source.count("\n", 0, cursor) + 1))
    return found


def discover_changed_functions(checkout: Path, base: str, head: str, files: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    functions: list[dict[str, Any]] = []
    gaps: list[str] = []
    for path in files:
        if Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        source_path = checkout / path
        if not source_path.is_file():
            gaps.append(f"{path}: source absent at head (deleted or renamed)")
            continue
        patch = _git(checkout, "diff", "--no-ext-diff", "--unified=0", base, head, "--", path)
        changed = _changed_lines(patch)
        if not changed:
            gaps.append(f"{path}: no head-side changed lines")
            continue
        source = source_path.read_text(encoding="utf-8", errors="replace")
        located = _functions(source)
        matched = [item for item in located if any(item[1] <= line <= item[2] for line in changed)]
        uncovered = sorted(line for line in changed if not any(start <= line <= end for _, start, end in located))
        if uncovered:
            gaps.append(f"{path}: changed head lines not assigned to a function: {uncovered[:12]}")
        for name, start, _ in matched:
            functions.append({"kind": "function", "name": name.split("::")[-1],
                              "qualified_name": name, "file": path, "start_line": start})
    return functions, gaps


def build_review_input(metadata: Mapping[str, Any], checkout: Path, build_profile: str,
                       explicit_functions: list[dict[str, Any]] | None = None,
                       requested_routes: list[str] | None = None,
                       explicit_targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    checkout = checkout.resolve()
    base = str(metadata["base"]["sha"])
    head = str(metadata["head"]["sha"])
    if _git(checkout, "rev-parse", "HEAD").strip() != head:
        raise PRAdapterError("checkout HEAD differs from acquired PR head")
    merge_base = _git(checkout, "merge-base", base, head).strip()
    files = [line for line in _git(checkout, "diff", "--name-only", "--diff-filter=ACMRT", merge_base, head).splitlines() if line]
    if not files:
        raise PRAdapterError("PR has no changed files at the acquired head")
    diff = _git(checkout, "diff", "--no-ext-diff", "--binary", merge_base, head)
    functions, gaps = discover_changed_functions(checkout, merge_base, head, files)
    if explicit_functions is not None:
        functions = explicit_functions
    if not functions and not explicit_targets:
        raise PRAdapterError("no changed functions resolved; supply --changed-functions-json or --targets-json. Gaps: " + "; ".join(gaps))
    value: dict[str, Any] = {
        "schema_version": "review-input.v1",
        "repository": {"provider": "github", "name": metadata["repository"]["name"],
                       "root": str(checkout), "commit_sha": head, "commit_role": "head",
                       "base_sha": base, "head_sha": head,
                       "build_profile": build_profile},
        "review_target": {"id": f"github-pr-{metadata['number']}", "kind": "github_pull_request",
                          "base_revision": base, "head_revision": head,
                          "url": metadata["pr_url"], "diff_base_revision": merge_base},
        "diff": {"format": "unified", "text": diff, "changed_files": files},
        "changed_functions": functions,
        "agent_input_policy": {"oracle_included": False, "healthy_source_included": False,
                               "source_is_head_revision": True},
        "acquisition_coverage_gaps": gaps,
    }
    if requested_routes is not None:
        value["requested_routes"] = requested_routes
    if explicit_targets is not None:
        value["targets"] = explicit_targets
    return require_review_input(value)
