from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


CONTROL_RE = re.compile(
    r"^\s*(if|for|while|switch|else\s+if)\s*\((.*)\)"
)
ELSE_RE = re.compile(r"^\s*else\b")
DO_RE = re.compile(r"^\s*do\b")
CPP_BRANCH_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)")


@dataclass(frozen=True)
class ControlRegion:
    id: str
    kind: str
    condition: str
    start: int
    end: int
    group: str
    branch: str


def _code_only(line: str) -> str:
    line = re.sub(r"/\*.*?\*/", "", line)
    line = re.sub(r"//.*$", "", line)
    return line


def _brace_pairs(lines: list[str]) -> dict[int, int]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for line_number, raw in enumerate(lines, start=1):
        line = _code_only(raw)
        for character in line:
            if character == "{":
                stack.append(line_number)
            elif character == "}" and stack:
                pairs[stack.pop()] = line_number
    return pairs


def _next_code_line(lines: list[str], line_number: int, end: int) -> int | None:
    for candidate in range(line_number + 1, end + 1):
        value = _code_only(lines[candidate - 1]).strip()
        if value:
            return candidate
    return None


def _statement_end(
    lines: list[str],
    line_number: int,
    end: int,
    brace_pairs: dict[int, int],
) -> int:
    line = _code_only(lines[line_number - 1])
    if "{" in line and line_number in brace_pairs:
        return brace_pairs[line_number]
    next_line = _next_code_line(lines, line_number, end)
    if next_line is None:
        return line_number
    next_text = _code_only(lines[next_line - 1]).strip()
    if next_text.startswith("{"):
        return brace_pairs.get(next_line, next_line)
    return next_line


def _line_regions(
    lines: list[str],
    start: int,
    end: int,
) -> list[ControlRegion]:
    brace_pairs = _brace_pairs(lines)
    regions: list[ControlRegion] = []
    last_if_by_end: list[ControlRegion] = []
    for line_number in range(start, end + 1):
        raw = _code_only(lines[line_number - 1])
        control_text = re.sub(r"^\s*}\s*", "", raw)
        match = CONTROL_RE.match(control_text)
        if match:
            keyword, condition = match.groups()
            kind = "loop" if keyword in {"for", "while"} else "if"
            region_end = _statement_end(lines, line_number, end, brace_pairs)
            region = ControlRegion(
                id=f"control:{start}:{line_number}:{kind}",
                kind=kind,
                condition=f"{keyword} ({condition.strip()})",
                start=line_number,
                end=region_end,
                group=f"branch:{start}:{line_number}",
                branch="true",
            )
            regions.append(region)
            if kind == "if":
                last_if_by_end.append(region)
            continue
        if DO_RE.match(control_text):
            region_end = _statement_end(lines, line_number, end, brace_pairs)
            regions.append(
                ControlRegion(
                    id=f"control:{start}:{line_number}:do",
                    kind="loop",
                    condition="do-while",
                    start=line_number,
                    end=region_end,
                    group=f"loop:{start}:{line_number}",
                    branch="body",
                )
            )
            continue
        if ELSE_RE.match(control_text):
            parent = next(
                (
                    region
                    for region in reversed(last_if_by_end)
                    if region.end <= line_number
                ),
                None,
            )
            if parent is None:
                continue
            region_end = _statement_end(lines, line_number, end, brace_pairs)
            regions.append(
                ControlRegion(
                    id=f"control:{start}:{line_number}:else",
                    kind="else",
                    condition=f"else for {parent.condition}",
                    start=line_number,
                    end=region_end,
                    group=parent.group,
                    branch="false",
                )
            )
    return regions


def _cpp_regions(lines: list[str]) -> list[ControlRegion]:
    regions: list[ControlRegion] = []
    stack: list[tuple[int, str, str, str]] = []
    group_index = 0
    for line_number, raw in enumerate(lines, start=1):
        match = CPP_BRANCH_RE.match(raw)
        if not match:
            continue
        directive, rest = match.groups()
        if directive in {"if", "ifdef", "ifndef"}:
            group_index += 1
            group = f"cpp:{group_index}"
            stack.append((line_number, group, "true", rest.strip()))
        elif directive in {"elif", "else"} and stack:
            start, group, branch, condition = stack.pop()
            regions.append(
                ControlRegion(
                    id=f"cpp:{start}:{branch}",
                    kind="preprocessor",
                    condition=f"#{directive} {condition}".strip(),
                    start=start,
                    end=max(start, line_number - 1),
                    group=group,
                    branch=branch,
                )
            )
            next_branch = "false" if directive == "else" else f"elif:{line_number}"
            stack.append((line_number, group, next_branch, rest.strip()))
        elif directive == "endif" and stack:
            start, group, branch, condition = stack.pop()
            regions.append(
                ControlRegion(
                    id=f"cpp:{start}:{branch}",
                    kind="preprocessor",
                    condition=f"preprocessor {condition}".strip(),
                    start=start,
                    end=line_number,
                    group=group,
                    branch=branch,
                )
            )
    return regions


def regions_for_file(
    lines: list[str],
    function_ranges: Iterable[tuple[int, int]],
) -> list[ControlRegion]:
    regions = _cpp_regions(lines)
    for start, end in function_ranges:
        regions.extend(_line_regions(lines, start, end))
    return regions


def containing_regions(
    regions: Iterable[ControlRegion],
    line_number: int,
) -> tuple[ControlRegion, ...]:
    return tuple(
        sorted(
            (
                region
                for region in regions
                if region.start <= line_number <= region.end
            ),
            key=lambda region: (region.start, -region.end, region.id),
        )
    )


def order_decision(
    regions: Iterable[ControlRegion],
    source_line: int,
    target_line: int,
) -> tuple[str, str]:
    regions = tuple(regions)
    source_context = containing_regions(regions, source_line)
    target_context = containing_regions(regions, target_line)

    for source_region in source_context:
        for target_region in target_context:
            if (
                source_region.group == target_region.group
                and source_region.branch != target_region.branch
                and not (
                    source_region.start <= target_region.start
                    and source_region.end >= target_region.end
                )
                and not (
                    target_region.start <= source_region.start
                    and target_region.end >= source_region.end
                )
            ):
                return (
                    "unknown",
                    "mutually exclusive control-flow branches",
                )

    shared_loops = {
        region.id
        for region in source_context
        if region.kind == "loop"
    } & {
        region.id
        for region in target_context
        if region.kind == "loop"
    }
    if shared_loops and source_line >= target_line:
        return "unknown", "loop back-edge may reverse the claimed order"
    if source_line >= target_line:
        return "unknown", "source line does not precede target line"

    path = [
        region.condition
        for region in target_context
        if region.kind in {"if", "else", "loop", "preprocessor"}
    ]
    condition = " | ".join(path) if path else "straight-line source order"
    return "happens_before", condition
