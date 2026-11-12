#!/usr/bin/env python3
"""Run the evidence-first CG-Bench target-set agent."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "validation/external/results/cgbench/cases.json"
SELECTION = ROOT / "validation/external/selections/cgbench-v1.json"
PROMPT = ROOT / "validation/external/prompts/cgbench-target-agent-v1.txt"
DEFAULT_OUTPUT = ROOT / "validation/external/results/cgbench/predictions.json"

KEYWORDS = {
    "alignas",
    "alignof",
    "auto",
    "bool",
    "break",
    "case",
    "catch",
    "char",
    "class",
    "const",
    "constexpr",
    "continue",
    "decltype",
    "default",
    "delete",
    "do",
    "double",
    "else",
    "enum",
    "explicit",
    "extern",
    "false",
    "float",
    "for",
    "friend",
    "goto",
    "if",
    "inline",
    "int",
    "long",
    "namespace",
    "new",
    "noexcept",
    "nullptr",
    "operator",
    "private",
    "protected",
    "public",
    "register",
    "return",
    "short",
    "signed",
    "sizeof",
    "static",
    "struct",
    "switch",
    "template",
    "this",
    "throw",
    "true",
    "try",
    "typedef",
    "typename",
    "union",
    "unsigned",
    "using",
    "virtual",
    "void",
    "volatile",
    "while",
}
TYPE_HINTS = {
    "CURLcode",
    "CURLMstate",
    "FILE",
    "bool",
    "char",
    "double",
    "float",
    "int",
    "long",
    "short",
    "size_t",
    "ssize_t",
    "unsigned",
    "void",
}
IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
ASSIGNMENT = re.compile(
    r"(?P<lhs>[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*|\s*\[[^\]]+\])*)"
    r"\s*=\s*(?P<rhs>[^;{}]+);",
    re.DOTALL,
)
MACRO = re.compile(
    r"^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)\s*\([^)]*\)\s*"
    r"(?P<body>.+?)\s*$",
    re.MULTILINE,
)
STRUCT_DEFINITION = re.compile(
    r"(?:typedef\s+)?struct\s+(?P<name>[A-Za-z_]\w*)\s*"
    r"\{(?P<body>.*?)\}\s*;",
    re.DOTALL,
)


@dataclass
class Candidate:
    name: str
    confidence: float
    reasons: set[str] = field(default_factory=set)
    snippet: str = ""


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence_id(file: str, line: int, snippet: str) -> str:
    raw = f"{file}|{line}|{snippet}"
    return "ev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def strip_comments_and_literals(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
    return text


def callsite_location(callsite: str) -> tuple[str, int]:
    match = re.match(r"^(.*):(\d+):(\d+)$", callsite)
    if not match:
        return callsite, 1
    return match.group(1), int(match.group(2))


def pointer_keys(expression: str) -> set[str]:
    keys = {
        token
        for token in IDENTIFIER.findall(expression)
        if token not in KEYWORDS and token not in {"NULL", "const", "true", "false"}
    }
    tail = re.split(r"(?:->|\.)", expression)[-1]
    tail = re.sub(r"\[[^\]]+\]", "", tail).strip(" *&()")
    if re.fullmatch(r"[A-Za-z_]\w*", tail):
        keys.add(tail)
    return keys


def pointer_selector_keys(expression: str) -> set[str]:
    parts = re.split(r"->|\.", expression)
    tail = re.sub(r"\[[^\]]+\]", "", parts[-1]).strip(" *&()")
    return {
        token
        for token in IDENTIFIER.findall(tail)
        if token not in KEYWORDS and token not in {"NULL", "const"}
    }


def pointer_base_keys(expression: str) -> set[str]:
    tail = expression.strip(" *&()")
    first = re.split(r"->|\.|\[", tail, maxsplit=1)[0]
    return {
        token
        for token in IDENTIFIER.findall(first)
        if token not in KEYWORDS and token not in {"NULL", "const"}
    }


def likely_identifier(token: str) -> bool:
    if token in KEYWORDS or token in TYPE_HINTS:
        return False
    if token in {"NULL", "TRUE", "FALSE", "EOF"}:
        return False
    if token.endswith("_t"):
        return False
    if token.startswith("__"):
        return False
    return True


def split_top_level(value: str, delimiter: str = ",") -> list[str]:
    result = []
    start = 0
    stack: list[str] = []
    quote = ""
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(value):
        if quote:
            if char == quote and (index == 0 or value[index - 1] != "\\"):
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
            result.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        result.append(tail)
    return result


def balanced_initializer(text: str, brace_start: int) -> str:
    depth = 0
    quote = ""
    for index in range(brace_start, len(text)):
        char = text[index]
        if quote:
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : index + 1]
    return text[brace_start:]


def candidate_tokens(value: str, *, allow_null: bool = False) -> list[str]:
    if allow_null and re.search(r"\bNULL\b", value):
        return ["NULL"]
    tokens = [
        token
        for token in IDENTIFIER.findall(strip_comments_and_literals(value))
        if likely_identifier(token)
    ]
    if not tokens:
        return []
    # A cast or address expression ends with the function symbol far more often
    # than with one of its type qualifiers.
    return [tokens[-1]]


def field_index(body: str, pointer: set[str]) -> tuple[list[str], int] | None:
    fields = []
    for line in body.splitlines():
        function_pointer = re.search(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)", line)
        if function_pointer:
            fields.append(function_pointer.group(1))
            continue
        declaration = re.search(
            r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;",
            line,
        )
        if declaration:
            fields.append(declaration.group(1))
    matches = [index for index, name in enumerate(fields) if name in pointer]
    if len(matches) != 1:
        return None
    return fields, matches[0]


class TargetAgent:
    def __init__(self, case: dict[str, Any], selection_ids: set[str]) -> None:
        if "targets" in case:
            raise ValueError("targets must be withheld from Agent input")
        self.case = case
        self.callsite = str(case["callsite"])
        self.pointer = str(case["fnptr"])
        self.pointer_keys = pointer_keys(self.pointer)
        self.selector_keys = pointer_selector_keys(self.pointer)
        self.base_keys = pointer_base_keys(self.pointer)
        self.selection_ids = selection_ids
        blocks = []
        for block in case.get("code_blocks", []):
            lines = block.get("source_code", [])
            if isinstance(lines, list):
                blocks.append("\n".join(map(str, lines)))
        self.text = "\n".join(blocks)
        self.clean = strip_comments_and_literals(self.text)
        self.file, self.line = callsite_location(self.callsite)
        self.trace: list[dict[str, Any]] = []
        self.candidates: dict[str, Candidate] = {}

    def _add(self, name: str, confidence: float, reason: str, snippet: str) -> None:
        if not name or not likely_identifier(name):
            return
        current = self.candidates.setdefault(name, Candidate(name, confidence, set(), snippet))
        current.confidence = max(current.confidence, confidence)
        current.reasons.add(reason)
        if snippet and not current.snippet:
            current.snippet = snippet

    def _record(self, tool: str, *, candidates: Iterable[str] = (), status: str = "ok") -> None:
        self.trace.append(
            {
                "tool": tool,
                "status": status,
                "candidates": sorted(set(candidates)),
            }
        )

    def inspect_pointer_assignments(self) -> None:
        found = []
        for match in ASSIGNMENT.finditer(self.clean):
            lhs = " ".join(match.group("lhs").split())
            rhs = " ".join(match.group("rhs").split())
            lhs_tokens = set(IDENTIFIER.findall(lhs))
            if self.selector_keys.isdisjoint(lhs_tokens):
                continue
            for token in candidate_tokens(rhs):
                self._add(token, 0.86, "pointer assignment", rhs)
                found.append(token)
        self._record("inspect_pointer_assignments", candidates=found)

    def inspect_pointer_tables(self) -> None:
        found = []
        bases = {
            token
            for token in self.base_keys
            if token not in {"s", "p", "ctx", "cf", "context", "this"}
        }
        for base in bases:
            pattern = re.compile(
                rf"\b{re.escape(base)}(?:\s*\[[^\]]*\])?\s*=\s*\{{",
                re.MULTILINE,
            )
            for match in pattern.finditer(self.clean):
                brace = self.clean.find("{", match.start())
                initializer = balanced_initializer(self.clean, brace)
                for token in re.findall(
                    r"\b(?:NULL|[A-Za-z_]\w*)\b",
                    initializer,
                ):
                    if token == "NULL" or likely_identifier(token):
                        self._add(
                            token,
                            0.72,
                            f"pointer table {base}",
                            initializer[:300],
                        )
                        found.append(token)
        self._record("inspect_pointer_tables", candidates=found)

    def inspect_struct_tables(self) -> None:
        found = []
        definitions = {}
        for definition in STRUCT_DEFINITION.finditer(self.clean):
            parsed = field_index(definition.group("body"), self.selector_keys)
            if parsed:
                definitions[definition.group("name")] = parsed
        for name, (fields, index) in definitions.items():
            assignment = re.compile(
                rf"(?:static\s+)?(?:const\s+)?(?:struct\s+)?{re.escape(name)}"
                r"[^;=]*=\s*\{",
                re.MULTILINE,
            )
            for match in assignment.finditer(self.clean):
                brace = self.clean.find("{", match.start())
                initializer = balanced_initializer(self.clean, brace)
                body = initializer[1:-1]
                rows = split_top_level(body)
                row_values = [
                    split_top_level(row.strip()[1:-1])
                    for row in rows
                    if row.strip().startswith("{") and row.strip().endswith("}")
                ]
                if not row_values:
                    row_values = [rows]
                for values in row_values:
                    if index >= len(values):
                        continue
                    value = values[index]
                    field_name = fields[index]
                    for token in candidate_tokens(value, allow_null=True):
                        self._add(
                            token,
                            0.74,
                            f"struct table {name}.{field_name}",
                            value,
                        )
                        found.append(token)
        self._record("inspect_struct_tables", candidates=found)

    def inspect_callback_registration(self) -> None:
        found = []
        for raw_line in self.text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if not re.search(r"[A-Za-z_]\w*\s*\(", line):
                continue
            option = re.search(r"\b[A-Z][A-Z0-9_]{3,}\b", line)
            option_matches = bool(
                option
                and option.group(0).split("_")[-1].lower()
                in {key.lower() for key in self.selector_keys}
            )
            if not option_matches:
                continue
            identifiers = [
                token
                for token in IDENTIFIER.findall(strip_comments_and_literals(line))
                if likely_identifier(token)
            ]
            if len(identifiers) < 2:
                continue
            for token in identifiers[-3:]:
                if token.startswith(("CURL", "AV_", "CURLSH", "CURLOPT")):
                    continue
                self._add(token, 0.58, "callback registration", line)
                found.append(token)
        self._record("inspect_callback_registration", candidates=found)

    def inspect_macros(self) -> None:
        found = []
        for match in MACRO.finditer(self.text):
            name = match.group("name")
            body = match.group("body").strip()
            body_tokens = set(IDENTIFIER.findall(strip_comments_and_literals(body)))
            direct = name in self.selector_keys
            reverse = bool(self.selector_keys & body_tokens)
            if not direct and not reverse:
                continue
            replacement = re.search(
                r"\b([A-Za-z_]\w*)\s*\(",
                strip_comments_and_literals(body),
            )
            replacement_tokens = {
                token
                for token in (
                    name,
                    replacement.group(1) if direct and replacement else "",
                )
                if token and likely_identifier(token)
            }
            for token in replacement_tokens:
                self._add(token, 0.66, "macro dispatch", body)
                found.append(token)
        self._record("inspect_macros", candidates=found)

    def run(self) -> dict[str, Any]:
        tools = (
            self.inspect_pointer_assignments,
            self.inspect_pointer_tables,
            self.inspect_struct_tables,
            self.inspect_callback_registration,
            self.inspect_macros,
        )
        for tool in tools:
            try:
                tool()
            except Exception as exc:
                self.trace.append(
                    {"tool": tool.__name__, "status": "error", "error": str(exc)}
                )
        self.trace.append(
            {
                "tool": "finalize_target_set",
                "status": "ok",
                "candidates": sorted(self.candidates),
            }
        )
        targets = sorted(
            name
            for name, candidate in self.candidates.items()
            if candidate.confidence >= 0.58
        )
        status = "predicted" if targets else "unknown"
        evidence = []
        if self.candidates:
            snippets = sorted(
                {
                    candidate.snippet
                    for candidate in self.candidates.values()
                    if candidate.snippet
                }
            )
            for snippet in snippets[:5]:
                evidence.append(
                    {
                        "id": evidence_id(self.file, self.line, snippet),
                        "file": self.file,
                        "line": self.line,
                        "snippet": snippet,
                    }
                )
        return {
            "id": self.case["id"],
            "identity": {"callsite": self.callsite},
            "status": status,
            "targets": targets,
            "confidence": (
                sum(self.candidates[target].confidence for target in targets) / len(targets)
                if targets
                else 0.0
            ),
            "evidence": evidence,
            "details": {
                "pointer_expression": self.pointer,
                "callsite_type": self.case.get("type", ""),
                "candidate_reasons": {
                    name: sorted(candidate.reasons)
                    for name, candidate in sorted(self.candidates.items())
                },
                "tool_trace": self.trace,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--selection", type=Path, default=SELECTION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--project")
    parser.add_argument("--case")
    args = parser.parse_args()
    source = read_json(args.cases)
    selection = read_json(args.selection)
    selected_cases = {
        str(case["id"]): case
        for case in selection["cases"]
    }
    rows = []
    for case in source["cases"]:
        if str(case["id"]) not in selected_cases:
            continue
        if args.project and case["project"] != args.project:
            continue
        if args.case and case["id"] != args.case:
            continue
        rows.append(case)
        if args.limit is not None and len(rows) >= args.limit:
            break
    predictions = []
    for case in rows:
        public_case = {key: value for key, value in case.items() if key != "targets"}
        predictions.append(
            TargetAgent(public_case, set(selected_cases)).run()
        )
    output = {
        "schema_version": "external-prediction-v1",
        "protocol_id": "external-v1",
        "dataset": "cgbench",
        "generator": {
            "name": "cgbench-target-agent",
            "version": "1.0.0",
            "mode": "deterministic",
            "prompt_sha256": sha256_path(PROMPT),
            "parameters": {
                "strategy": "evidence-first",
                "gold_exposed_to_agent": False,
                "minimum_candidate_confidence": 0.58,
            },
        },
        "predictions": predictions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "predictions": len(predictions),
                "predicted": sum(item["status"] == "predicted" for item in predictions),
                "unknown": sum(item["status"] == "unknown" for item in predictions),
                "path": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
