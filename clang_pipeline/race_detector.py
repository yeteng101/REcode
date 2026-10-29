from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .clang_ast import evidence_id, stable_digest, utc_now


SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".i"}
CONTROL_WORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "sizeof",
    "sizeof",
    "else",
}
TYPE_WORDS = {
    "char",
    "double",
    "float",
    "int",
    "long",
    "short",
    "signed",
    "size_t",
    "ssize_t",
    "unsigned",
    "void",
    "bool",
}
ATOMIC_TOKENS = (
    "__atomic_",
    "atomic_",
    "memory_order_",
    "__sync_",
    "__VERIFIER_atomic_",
)
LOCK_ACQUIRE = (
    "pthread_mutex_lock",
    "pthread_rwlock_rdlock",
    "pthread_rwlock_wrlock",
    "mtx_lock",
    "EnterCriticalSection",
    "omp_set_lock",
)
LOCK_RELEASE = (
    "pthread_mutex_unlock",
    "pthread_rwlock_unlock",
    "mtx_unlock",
    "LeaveCriticalSection",
    "omp_unset_lock",
)


@dataclass
class Access:
    function: str
    region: str
    base: str
    index: str
    kind: str
    file: str
    line: int
    snippet: str
    protected: bool = False
    atomic: bool = False
    private: bool = False
    loop_variables: set[str] = field(default_factory=set)
    parallel_variables: set[str] = field(default_factory=set)
    guard_single_thread: bool = False
    concurrent_context: bool = False


@dataclass
class RegionResult:
    label: str
    confidence: float
    reason: str
    accesses: list[Access]
    guard_single_thread: bool = False


def _strip_comments_and_literals(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda match: "\n" * match.group(0).count("\n"), text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
    return text


def _balanced_region(text: str, opening: int, left: str = "{", right: str = "}") -> tuple[int, int]:
    if opening < 0 or opening >= len(text) or text[opening] != left:
        return opening, opening
    depth = 0
    quote = ""
    for index in range(opening, len(text)):
        char = text[index]
        if quote:
            if char == quote and text[index - 1] != "\\":
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == left:
            depth += 1
        elif char == right:
            depth -= 1
            if depth == 0:
                return opening, index + 1
    return opening, len(text)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _function_ranges(text: str) -> list[tuple[str, int, int, int]]:
    ranges = []
    pattern = re.compile(
        r"(?m)^[\t ]*(?:[A-Za-z_][\w:\s*&<>,]*?)"
        r"([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{"
    )
    occupied: list[tuple[int, int]] = []
    for match in pattern.finditer(text):
        brace = text.find("{", match.start())
        start, end = _balanced_region(text, brace)
        if any(start >= left and end <= right for left, right in occupied):
            continue
        occupied.append((start, end))
        ranges.append((match.group(1), match.start(), start, end))
    if not ranges:
        ranges.append(("translation_unit", 0, 0, len(text)))
    return ranges


def _function_for_offset(
    ranges: list[tuple[str, int, int, int]],
    offset: int,
) -> str:
    for name, _declaration, start, end in ranges:
        if start <= offset < end:
            return name
    return "translation_unit"


def _clauses(pragma: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name, body in re.findall(
        r"\b(private|firstprivate|lastprivate|shared|reduction|linear)\s*\(([^)]*)\)",
        pragma,
    ):
        values = []
        for item in body.split(","):
            value = item.strip()
            if ":" in value:
                value = value.rsplit(":", 1)[-1].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", value):
                values.append(value)
        result.setdefault(name, []).extend(values)
    return result


def _find_parallel_region(text: str, pragma_end: int) -> tuple[str, int, int]:
    remainder = text[pragma_end:]
    loop = re.search(r"\bfor\s*\(", remainder)
    brace = re.search(r"\{", remainder)
    if loop and (not brace or loop.start() < brace.start()):
        loop_start = pragma_end + loop.start()
        paren_start = text.find("(", loop_start)
        _, paren_end = _balanced_region(text, paren_start, "(", ")")
        body_start = paren_end
        while body_start < len(text) and text[body_start].isspace():
            body_start += 1
        if body_start < len(text) and text[body_start] == "{":
            _, body_end = _balanced_region(text, body_start)
            return "parallel_for", body_start, body_end
        semicolon = text.find(";", body_start)
        return "parallel_for", body_start, semicolon + 1 if semicolon >= 0 else min(
            len(text), body_start + 300
        )
    if brace:
        opening = pragma_end + brace.start()
        _, end = _balanced_region(text, opening)
        return "parallel", opening, end
    return "parallel", pragma_end, min(len(text), pragma_end + 300)


def _loop_variables(body: str) -> set[str]:
    variables = set()
    for match in re.finditer(r"\bfor\s*\(\s*(?:int\s+)?([A-Za-z_]\w*)\s*=", body):
        variables.add(match.group(1))
    return variables


def _is_private(name: str, clauses: dict[str, list[str]], loop_vars: set[str]) -> bool:
    if name in loop_vars:
        return True
    for clause in ("private", "firstprivate", "lastprivate", "reduction", "linear"):
        if name in clauses.get(clause, []):
            return True
    return False


def _write_ranges(line: str) -> list[tuple[int, int]]:
    ranges = []
    for match in re.finditer(
        r"(?P<lhs>[A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*|\s*\[[^\]]+\])*)"
        r"\s*(?:\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|=|\+\+|--)",
        line,
    ):
        lhs = match.group("lhs")
        start = match.start("lhs")
        ranges.append((start, start + len(lhs)))
    for match in re.finditer(r"\+\+\s*([A-Za-z_]\w*)", line):
        ranges.append((match.start(1), match.end(1)))
    for match in re.finditer(r"--\s*([A-Za-z_]\w*)", line):
        ranges.append((match.start(1), match.end(1)))
    return ranges


def _accesses(
    body: str,
    *,
    function: str,
    region: str,
    file: str,
    start_line: int,
    clauses: dict[str, list[str]],
    loop_vars: set[str],
    guard_single_thread: bool,
) -> list[Access]:
    result = []
    atomic_depth = 0
    critical_depth = 0
    for offset, line in enumerate(body.splitlines(), start=start_line):
        stripped = line.strip()
        lowered = stripped.lower()
        if "pragma omp atomic" in lowered or "pragma omp critical" in lowered:
            atomic_depth += 1
            continue
        if stripped.startswith("//") or not stripped:
            continue
        protected = atomic_depth > 0 or critical_depth > 0
        atomic = any(token.lower() in lowered for token in ATOMIC_TOKENS)
        writes = _write_ranges(line)
        arrays = []
        for match in re.finditer(
            r"\b(?P<base>[A-Za-z_]\w*)\s*(?P<indices>(?:\[[^\]]+\]\s*)+)",
            line,
        ):
            base = match.group("base")
            index = " | ".join(
                part.strip()
                for part in re.findall(r"\[([^\]]+)\]", match.group("indices"))
            )
            writes_here = any(
                left <= match.start() < right for left, right in writes
            )
            arrays.append((match.start(), base, index, writes_here))
            result.append(
                Access(
                    function=function,
                    region=region,
                    base=base,
                    index=index,
                    kind="write" if writes_here else "read",
                    file=file,
                    line=offset,
                    snippet=stripped,
                    protected=protected,
                    atomic=atomic,
                    private=_is_private(base, clauses, loop_vars),
                    loop_variables=loop_vars,
                    guard_single_thread=guard_single_thread,
                )
            )
        assigned = {
            re.sub(r"\s+", "", match.group("lhs"))
            for match in re.finditer(
                r"(?P<lhs>[A-Za-z_]\w*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*|\s*\[[^\]]+\])*)"
                r"\s*(?:\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|=|\+\+|--)",
                line,
            )
        }
        declaration = re.match(
            r"^(?:const\s+)?(?:unsigned\s+|signed\s+)?"
            r"(?:int|long|short|char|float|double|size_t|ssize_t|bool)\s+",
            stripped,
        )
        for name in re.findall(r"\b[A-Za-z_]\w*\b", line):
            if name in CONTROL_WORDS or name in loop_vars:
                continue
            normalized = name
            if declaration and stripped.find(name) < stripped.find("=") and "=" in stripped:
                continue
            if any(base == name for _position, base, _index, _write in arrays):
                continue
            if normalized not in assigned:
                continue
            result.append(
                Access(
                    function=function,
                    region=region,
                    base=name,
                    index="",
                    kind="write",
                    file=file,
                    line=offset,
                    snippet=stripped,
                    protected=protected,
                    atomic=atomic,
                    private=_is_private(name, clauses, loop_vars),
                    loop_variables=loop_vars,
                    guard_single_thread=guard_single_thread,
                )
            )
        if atomic_depth and lowered:
            atomic_depth = 0
        if lowered.endswith("}") and critical_depth:
            critical_depth = max(0, critical_depth - 1)
    deduped: dict[tuple[str, str, int, str], Access] = {}
    for item in result:
        key = (item.base, item.index, item.line, item.kind)
        deduped[key] = item
    return list(deduped.values())


def _index_is_iteration_unique(access: Access, private_vars: set[str]) -> bool:
    index = " ".join(access.index.split())
    if not index:
        return False
    identifiers = set(re.findall(r"\b[A-Za-z_]\w*\b", index))
    induction = access.loop_variables | access.parallel_variables | private_vars
    if not identifiers & induction:
        return False
    return not (identifiers - induction)


def _analyze_openmp_region(
    text: str,
    pragma: str,
    pragma_offset: int,
    region_kind: str,
    body_start: int,
    body_end: int,
    *,
    file: str,
    ranges: list[tuple[str, int, int, int]],
) -> RegionResult:
    clauses = _clauses(pragma)
    body = text[body_start:body_end]
    guard_single_thread = bool(
        "omp_get_thread_num" in body
        and re.search(r"omp_get_thread_num\s*\(\s*\)\s*==\s*0", body)
    )
    outer_variable = None
    if region_kind == "parallel_for":
        header = text[max(0, body_start - 800):body_start]
        outer_matches = list(
            re.finditer(r"\bfor\s*\(\s*(?:int\s+)?([A-Za-z_]\w*)\s*=", header)
        )
        if outer_matches:
            outer_variable = outer_matches[-1].group(1)
    loop_vars = _loop_variables(body)
    if outer_variable:
        loop_vars.add(outer_variable)
    accesses = _accesses(
        body,
        function=_function_for_offset(ranges, body_start),
        region=region_kind,
        file=file,
        start_line=_line_number(text, body_start),
        clauses=clauses,
        loop_vars=loop_vars,
        guard_single_thread=guard_single_thread,
    )
    for access in accesses:
        access.parallel_variables = {outer_variable} if outer_variable else set()
    private_vars = set(loop_vars)
    for clause in ("private", "firstprivate", "lastprivate", "reduction", "linear"):
        private_vars.update(clauses.get(clause, []))
    if guard_single_thread:
        return RegionResult(
            "no_race",
            0.9,
            "all structured accesses are guarded by omp_get_thread_num()==0",
            accesses,
            guard_single_thread=True,
        )
    nested_loop_variables = set()
    for match in re.finditer(r"\bfor\s*\(\s*(?:int\s+)?([A-Za-z_]\w*)\s*=", body):
        if (
            match.group(1) not in clauses.get("private", [])
            and match.group(1) != outer_variable
        ):
            nested_loop_variables.add(match.group(1))
    if nested_loop_variables:
        used_nested = {
            variable
            for access in accesses
            for variable in nested_loop_variables
            if re.search(rf"\b{re.escape(variable)}\b", access.index)
        }
        if used_nested:
            return RegionResult(
                "race",
                0.88,
                "nested loop induction variable is shared across outer parallel iterations",
                accesses,
            )
    active = [
        access
        for access in accesses
        if not access.private and not access.protected and not access.atomic
    ]
    by_base: dict[str, list[Access]] = {}
    for access in active:
        by_base.setdefault(access.base, []).append(access)
    uncertain = []
    for base, group in by_base.items():
        writes = [access for access in group if access.kind == "write"]
        if not writes:
            continue
        if any(not access.index for access in writes):
            return RegionResult(
                "race",
                0.86,
                f"shared scalar {base} is written by parallel iterations",
                accesses,
            )
        by_line: dict[int, list[Access]] = {}
        for access in group:
            by_line.setdefault(access.line, []).append(access)
        for line, line_accesses in by_line.items():
            dimensional = [
                [
                    " ".join(part.split())
                    for part in access.index.split("|")
                ]
                for access in line_accesses
                if access.index
            ]
            max_dimensions = max((len(value) for value in dimensional), default=0)
            for dimension in range(max_dimensions):
                expressions = {
                    value[dimension]
                    for value in dimensional
                    if dimension < len(value)
                }
                parallel_expressions = {
                    expression
                    for expression in expressions
                    if set(re.findall(r"\b[A-Za-z_]\w*\b", expression))
                    & ({outer_variable} if outer_variable else set())
                }
                if len(parallel_expressions) > 1:
                    return RegionResult(
                        "race",
                        0.84,
                        f"{base} has conflicting parallel index expressions in "
                        f"one statement: {sorted(parallel_expressions)}",
                        accesses,
                    )
        indexes = {" ".join(access.index.split()) for access in group if access.index}
        for index in indexes:
            signature = Access(
                function=writes[0].function,
                region=writes[0].region,
                base=base,
                index=index,
                kind="write",
                file=writes[0].file,
                line=writes[0].line,
                snippet=writes[0].snippet,
                loop_variables=loop_vars,
            )
            if not _index_is_iteration_unique(signature, private_vars):
                uncertain.append((base, index))
    if uncertain:
        return RegionResult(
            "unknown",
            0.4,
            "shared accesses exist but index disjointness cannot be proven: "
            + ", ".join(f"{base}[{index}]" for base, index in uncertain[:5]),
            accesses,
        )
    return RegionResult(
        "no_race",
        0.72,
        "shared writes are private, atomic, synchronized, or partitioned by iteration indexes",
        accesses,
    )


def _analyze_openmp(
    text: str,
    *,
    file: str,
    ranges: list[tuple[str, int, int, int]],
) -> list[RegionResult]:
    results = []
    pragma_pattern = re.compile(r"(?m)^[\t ]*#\s*pragma\s+omp\s+([^\n]+)")
    for match in pragma_pattern.finditer(text):
        pragma = match.group(1)
        if not re.search(r"\bparallel\b", pragma):
            continue
        region_kind, start, end = _find_parallel_region(text, match.end())
        results.append(
            _analyze_openmp_region(
                text,
                pragma,
                match.start(),
                region_kind,
                start,
                end,
                file=file,
                ranges=ranges,
            )
        )
    return results


def _global_declarations(text: str) -> dict[str, bool]:
    names: dict[str, bool] = {}
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if depth == 0 and stripped and not stripped.startswith(("#", "//", "/*")):
            declaration = re.match(
                r"^(?P<prefix>(?:extern\s+|static\s+|const\s+|volatile\s+|"
                r"unsigned\s+|signed\s+|atomic_[A-Za-z_]\w*\s+|_Atomic\s*)*)"
                r"(?P<type>[A-Za-z_]\w*)\s+(?P<body>[^;{}]+);",
                stripped,
            )
            if declaration and "(" not in declaration.group("body"):
                prefix = declaration.group("prefix")
                type_name = declaration.group("type")
                atomic = type_name.startswith(("atomic_", "_Atomic")) or "_Atomic" in prefix
                for value in _split_top_level(declaration.group("body"), ","):
                    value = re.sub(r"=.*$", "", value).strip().lstrip("*& ")
                    match = re.match(r"([A-Za-z_]\w*)", value)
                    if match and match.group(1) not in TYPE_WORDS:
                        names[match.group(1)] = atomic
        depth += line.count("{") - line.count("}")
        depth = max(0, depth)
    return names


def _thread_lifecycle(
    text: str,
) -> tuple[dict[str, str], bool, bool, list[tuple[int, int]]]:
    targets: dict[str, str] = {}
    events: list[tuple[int, str, str, str]] = []
    unresolved = False
    for match in re.finditer(r"\b(?:pthread_create|thrd_create)\s*\(", text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_prefix = text[line_start:match.start()]
        if "extern" in line_prefix or "__restrict" in line_prefix:
            continue
        paren_start = text.find("(", match.start())
        _, paren_end = _balanced_region(text, paren_start, "(", ")")
        if paren_end <= paren_start:
            unresolved = True
            continue
        body = text[paren_start + 1 : paren_end - 1]
        if "__restrict" in body:
            continue
        args = [
            part.strip()
            for part in _split_top_level(body, ",")
        ]
        if len(args) < 3:
            unresolved = True
            continue
        target = args[2]
        target = re.sub(r"^\([^)]*\)", "", target).strip(" &")
        if re.fullmatch(r"[A-Za-z_]\w*", target):
            handle = args[0].strip().lstrip("&")
            targets[handle] = target
            events.append((match.start(), "create", handle, target))
        else:
            unresolved = True
    for match in re.finditer(r"\b(?:pthread_join|thrd_join)\s*\(", text):
        paren_start = text.find("(", match.start())
        _, paren_end = _balanced_region(text, paren_start, "(", ")")
        if paren_end <= paren_start:
            continue
        args = [
            part.strip()
            for part in _split_top_level(
                text[paren_start + 1 : paren_end - 1],
                ",",
            )
        ]
        if args:
            events.append((match.start(), "join", args[0].lstrip("&"), ""))
    active: set[str] = set()
    range_start = None
    overlap = False
    ranges = []
    for position, kind, handle, _target in sorted(events):
        if kind == "create":
            if active and range_start is None:
                range_start = position
            active.add(handle)
            if len(active) >= 2:
                overlap = True
            if range_start is None:
                range_start = position
        else:
            active.discard(handle)
            if not active and range_start is not None:
                ranges.append((range_start, position))
                range_start = None
    if range_start is not None:
        ranges.append((range_start, len(text)))
    return targets, unresolved, overlap, ranges


def _split_top_level(value: str, delimiter: str) -> list[str]:
    result = []
    start = 0
    stack: list[str] = []
    quote = ""
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(value):
        if quote:
            if char == quote and value[index - 1] != "\\":
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
        elif char == delimiter and not stack:
            result.append(value[start:index])
            start = index + 1
    result.append(value[start:])
    return result


def _analyze_pthread(text: str, *, file: str) -> RegionResult | None:
    thread_targets, unresolved, overlap, thread_ranges = _thread_lifecycle(text)
    if not thread_targets and not unresolved:
        return None
    function_ranges = _function_ranges(text)
    globals_ = _global_declarations(text)
    accesses: list[Access] = []
    locked_lines = 0
    range_lines = [
        (_line_number(text, start), _line_number(text, end))
        for start, end in thread_ranges
    ]
    main_overlaps = False
    for function, declaration, start, end in function_ranges:
        body = text[start:end]
        if function not in thread_targets.values() and function != "main":
            continue
        held = 0
        for offset, line in enumerate(body.splitlines(), start=_line_number(text, start)):
            if "__VERIFIER_atomic_begin" in line or any(
                api in line for api in LOCK_ACQUIRE
            ):
                held += 1
            in_active_range = any(left <= offset <= right for left, right in range_lines)
            if function == "main" and held == 0 and in_active_range:
                main_overlaps = True
            writes = _write_ranges(line)
            for name, atomic_global in globals_.items():
                for occurrence in re.finditer(rf"\b{re.escape(name)}\b", line):
                    if any(left <= occurrence.start() < right for left, right in writes):
                        kind = "write"
                    else:
                        kind = "read"
                    accesses.append(
                        Access(
                            function=function,
                            region="pthread",
                            base=name,
                            index="",
                            kind=kind,
                            file=file,
                            line=offset,
                            snippet=line.strip(),
                            protected=held > 0 or atomic_global or any(
                                token.lower() in line.lower() for token in ATOMIC_TOKENS
                            ),
                            atomic=any(
                                token.lower() in line.lower() for token in ATOMIC_TOKENS
                            ) or atomic_global,
                            concurrent_context=(
                                function != "main"
                                and (overlap or main_overlaps)
                            ) or (
                                function == "main"
                                and in_active_range
                            ),
                        )
                    )
            if "__VERIFIER_atomic_end" in line or any(
                api in line for api in LOCK_RELEASE
            ):
                held = max(0, held - 1)
                locked_lines += 1
    if unresolved:
        return RegionResult(
            "unknown",
            0.35,
            "thread entry function could not be resolved",
            accesses,
        )
    if not accesses:
        return RegionResult(
            "unknown",
            0.25,
            "threads were created but no shared memory accesses were resolved",
            accesses,
        )
    by_base: dict[str, list[Access]] = {}
    for access in accesses:
        by_base.setdefault(access.base, []).append(access)
    concurrent_accesses = [
        access for access in accesses if access.concurrent_context
    ]
    if not concurrent_accesses:
        return RegionResult(
            "no_race",
            0.72,
            "thread lifetimes do not overlap with the recognized shared accesses",
            accesses,
        )
    for base, group in by_base.items():
        if not any(access.kind == "write" for access in group):
            continue
        if all(access.protected or access.atomic for access in group):
            continue
        functions = {
            access.function
            for access in group
            if access.concurrent_context
        }
        if len(functions) > 1:
            return RegionResult(
                "race",
                0.78,
                f"shared variable {base} is written without a common lock across threads",
                accesses,
            )
    if locked_lines or accesses:
        return RegionResult(
            "no_race",
            0.6,
            "shared writes are protected by locks/atomics or occur in only one execution context",
            accesses,
        )
    return None


def _access_record(access: Access) -> dict[str, Any]:
    ev = evidence_id("race_access", access.file, access.line, access.base)
    return {
        "id": "access_" + stable_digest(
            [access.file, access.line, access.base, access.index, access.kind]
        )[:12],
        "function": access.function,
        "region": access.region,
        "memory_location": access.base + (f"[{access.index}]" if access.index else ""),
        "access": access.kind,
        "protected": access.protected,
        "atomic": access.atomic,
        "private": access.private,
        "file": access.file,
        "line": access.line,
        "snippet": access.snippet,
        "evidence_ids": [ev],
    }


def _evidence(accesses: Iterable[Access]) -> list[dict[str, Any]]:
    values = {}
    for access in accesses:
        ev = evidence_id("race_access", access.file, access.line, access.base)
        values[ev] = {
            "id": ev,
            "kind": "memory_access",
            "location": {
                "file": access.file,
                "line": access.line,
                "snippet": access.snippet,
            },
            "role": "supports",
        }
    return sorted(values.values(), key=lambda item: item["id"])


def analyze_race_source(
    source_root: str | Path,
    *,
    graph: dict[str, Any] | None = None,
    source_files: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Perform tri-state static race analysis over C/C++ source files."""

    root = Path(source_root).resolve()
    results: list[RegionResult] = []
    processed_files = []
    warnings = []
    candidates = (
        sorted(Path(path).resolve() for path in source_files)
        if source_files is not None
        else sorted(root.rglob("*"))
    )
    for path in candidates:
        if path.suffix not in SOURCE_SUFFIXES or ".workspace" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"{path}: {exc}")
            continue
        relative = path.relative_to(root).as_posix()
        processed_files.append(relative)
        clean = _strip_comments_and_literals(text)
        ranges = _function_ranges(clean)
        results.extend(_analyze_openmp(clean, file=relative, ranges=ranges))
        pthread = _analyze_pthread(clean, file=relative)
        if pthread:
            results.append(pthread)
    if not processed_files:
        label = "unknown"
        reason = "no C/C++ source files were available"
    elif any(result.label == "race" for result in results):
        winners = [result for result in results if result.label == "race"]
        label = "race"
        reason = winners[0].reason
    elif any(result.label == "unknown" for result in results):
        label = "unknown"
        reason = next(
            result.reason for result in results if result.label == "unknown"
        )
    elif results:
        label = "no_race"
        reason = "all recognized concurrent regions are synchronized or index-partitioned"
    else:
        label = "unknown"
        reason = "no supported concurrency primitives were found"
    accesses = [
        access
        for result in results
        for access in result.accesses
    ]
    evidence = _evidence(accesses)
    occurrences = []
    for index, result in enumerate(results):
        result_accesses = [_access_record(access) for access in result.accesses]
        referenced = {
            ev_id
            for access in result_accesses
            for ev_id in access["evidence_ids"]
        }
        occurrences.append(
            {
                "id": "race_region_" + stable_digest(
                    [processed_files, index, result.label, result.reason]
                )[:12],
                "status": result.label,
                "confidence": result.confidence,
                "reason": result.reason,
                "accesses": result_accesses,
                "evidence_ids": sorted(referenced),
            }
        )
    meta = dict((graph or {}).get("meta") or {})
    return {
        "schema_version": "1.0",
        "run_id": str((graph or {}).get("run_id", "")),
        "result_type": "race_analysis",
        "status": "succeeded" if results else "not_available",
        "generated_at": utc_now(),
        "source": {
            **meta,
            "origin": "analyzer",
            "provider": "native_race_detector_v1",
        },
        "label": label,
        "reason": reason,
        "confidence": (
            next(
                result.confidence
                for result in results
                if result.label == label
            )
            if results
            else 0.0
        ),
        "items": occurrences,
        "evidence": evidence,
        "files": processed_files,
        "warnings": warnings,
    }


def analyze_race_file(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    return analyze_race_source(source.parent, source_files=[source])
