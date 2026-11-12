#!/usr/bin/env python3
"""Export concrete failures from the locked external benchmark results."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "validation/external/results"
CACHE = ROOT / ".benchmark-cache"
DEFAULT_JSON = RESULTS / "failure-audit.json"
DEFAULT_MARKDOWN = RESULTS / "failure-audit.md"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def compact(value: str, limit: int = 240) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def escape_markdown(value: str) -> str:
    return value.replace("|", r"\|")


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def source_window(lines: list[str], line_number: int, radius: int = 2) -> list[str]:
    if not lines or line_number < 1:
        return []
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return [
        f"{number}: {compact(lines[number - 1])}"
        for number in range(start, end + 1)
        if lines[number - 1].strip()
    ]


def pointer_tail(expression: str) -> str:
    value = re.split(r"->|\.", expression)[-1]
    value = re.sub(r"\[[^\]]+\]", "", value)
    return value.strip(" *&()")


def classify_cgbench(case: dict[str, Any]) -> str:
    kind = str(case.get("type") or "")
    if kind == "fnptr-virtual":
        return "virtual_dispatch"
    if kind == "fnptr-callback":
        return "callback_parameter_flow"
    if kind in {
        "fnptr-global-struct",
        "fnptr-global-struct-array",
        "fnptr-struct",
        "fnptr-global-array",
    }:
        return "struct_or_table_dispatch"
    if kind in {"fnptr-cast", "fnptr-only"}:
        return "cast_or_global_initializer"
    if kind in {"fnptr-library", "fnptr-dynamic-call"}:
        return "external_or_dynamic_registration"
    return "other"


def cgbench_context(case: dict[str, Any]) -> list[str]:
    pointer = pointer_tail(str(case.get("fnptr") or ""))
    targets = [re.split(r"::", value)[-1] for value in case.get("targets", [])]
    tokens = [pointer, *targets]
    matches = []
    for block in case.get("code_blocks", []):
        for line in block.get("source_code", []):
            if any(token and token in line for token in tokens) or (
                "=" in line and any(token and token in line for token in targets)
            ):
                matches.append(compact(str(line)))
            if len(matches) >= 5:
                return matches
    if not matches:
        for block in case.get("code_blocks", []):
            for line in block.get("source_code", []):
                stripped = str(line).strip()
                if stripped and not stripped.startswith("//"):
                    matches.append(compact(stripped))
                if len(matches) >= 4:
                    return matches
    return matches


def cgbench_tool_facts(prediction: dict[str, Any]) -> dict[str, Any]:
    details = prediction.get("details") or {}
    trace = details.get("tool_trace") or []
    return {
        "predicted_targets": prediction.get("targets", []),
        "confidence": prediction.get("confidence"),
        "candidate_reasons": details.get("candidate_reasons", {}),
        "tool_trace": [
            {
                "tool": str(item.get("tool", "")),
                "status": str(item.get("status", "")),
                "candidates": item.get("candidates", []),
            }
            for item in trace
            if isinstance(item, dict)
        ],
    }


def audit_cgbench() -> dict[str, Any]:
    cases = {
        str(case["id"]): case
        for case in read_json(RESULTS / "cgbench/cases.json")["cases"]
    }
    predictions = read_json(RESULTS / "cgbench/predictions.json")["predictions"]
    unknown = []
    categories = Counter()
    projects = Counter()
    types = Counter()
    for prediction in predictions:
        if prediction.get("status") != "unknown":
            continue
        case = cases[str(prediction["id"])]
        category = classify_cgbench(case)
        categories[category] += 1
        projects[str(case["project"])] += 1
        types[str(case["type"])] += 1
        facts = cgbench_tool_facts(prediction)
        unknown.append(
            {
                "id": case["id"],
                "project": case["project"],
                "callsite": case["callsite"],
                "pointer_expression": case["fnptr"],
                "callsite_type": case["type"],
                "expected_targets": case["targets"],
                "failure_category": category,
                "source_context": cgbench_context(case),
                **facts,
            }
        )
    return {
        "count": len(unknown),
        "by_project": dict(projects),
        "by_type": dict(types),
        "by_failure_category": dict(categories),
        "cases": unknown,
    }


def datarace_context(source: Path) -> list[str]:
    lines = read_lines(source)
    priority_patterns = (
        r"\bindexSet\b",
        r"\bxa[12]\b",
        r"\b(?:X|A|B|base)\s*(?:\[[^\]]+\])+\s*(?:\+=|-=|=)",
    )
    pragma_pattern = r"#pragma\s+omp"
    priority = []
    pragmas = []
    for number, line in enumerate(lines, 1):
        if any(re.search(pattern, line) for pattern in priority_patterns):
            priority.append((number, f"{number}: {compact(line)}"))
        elif re.search(pragma_pattern, line):
            pragmas.append((number, f"{number}: {compact(line)}"))
    selected = priority[:14] + pragmas[:4]
    selected = sorted(dict(selected).items())[:18]
    return [text for _, text in selected]


def index_set_facts(source: Path, distance: int = 12) -> dict[str, Any]:
    lines = read_lines(source)
    text = "\n".join(lines)
    match = re.search(r"indexSet\s*\[[^\]]+\]\s*=\s*\{(.*?)\};", text, re.S)
    if not match:
        return {}
    initializer = "\n".join(
        line.split("//", 1)[0] for line in match.group(1).splitlines()
    )
    values = [int(value) for value in re.findall(r"(?<!\w)-?\d+(?!\w)", initializer)]
    offsets = {
        value: index for index, value in enumerate(values)
    }
    close_pairs = [
        {
            "first": value,
            "second": value + distance,
            "first_position": offsets[value],
            "second_position": offsets[value + distance],
        }
        for value in values
        if value + distance in offsets
    ]
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "distance_checked": distance,
        "pairs_at_distance": close_pairs,
    }


def classify_datarace(case: dict[str, Any]) -> str:
    name = str(case["id"])
    if "indirectaccess" in name:
        return "indirect_index_or_pointer_alias"
    if "adi-tile" in name:
        return "affine_tiled_loop_dependence"
    return "other"


def audit_datarace() -> dict[str, Any]:
    report = read_json(RESULTS / "dataracebench/report.json")
    case_index = {
        str(case["id"]): case
        for case in read_json(RESULTS / "dataracebench/cases.json")["cases"]
    }
    rows = []
    categories = Counter()
    for case in report["cases"]:
        if case.get("status") != "unknown":
            continue
        metadata = case_index[str(case["id"])]
        category = classify_datarace(metadata)
        categories[category] += 1
        source_path = CACHE / "dataracebench" / metadata["source_file"]
        rows.append(
            {
                **case,
                "source_file": metadata["source_file"],
                "failure_category": category,
                "source_context": datarace_context(source_path),
                "index_set": index_set_facts(source_path),
            }
        )
    return {
        "count": len(rows),
        "by_failure_category": dict(categories),
        "cases": rows,
    }


def callgraph_source_context(
    case_dir: Path,
    manifest: dict[str, Any],
    symbol_id: str,
) -> tuple[str, int, list[str]]:
    symbol = next(
        (item for item in manifest.get("symbols", []) if item.get("id") == symbol_id),
        None,
    )
    if not symbol:
        return "", 0, []
    source_file = str(symbol.get("file") or "")
    line_number = int(symbol.get("line") or 0)
    source_path = case_dir / source_file
    return (
        source_file,
        line_number,
        source_window(read_lines(source_path), line_number),
    )


def audit_callgraph() -> dict[str, Any]:
    benchmark = CACHE / "callgraphbench/datasets"
    prediction_root = RESULTS / "callgraphbench/predictions"
    rows = []
    categories = Counter()
    for manifest_path in sorted(benchmark.glob("*/*/manifest.json")):
        manifest = read_json(manifest_path)
        language = manifest_path.parent.parent.name
        case_id = str(manifest["case_id"])
        key = f"{language}/{case_id}"
        prediction_path = prediction_root / key / "callgraph.json"
        predicted = {
            (edge["caller"], edge["callee"])
            for edge in read_json(prediction_path).get("edges", [])
        }
        expected = {
            (edge["caller"], edge["callee"]): edge
            for edge in manifest["ground_truth"]["edges"]
        }
        category = (
            "function_pointer_dispatch"
            if case_id == "c_dynamic"
            else "cross_file_callback_registration"
            if case_id == "c_multifile"
            else "constructor_or_member_call"
            if case_id == "cpp_basic"
            else "virtual_dispatch"
            if language == "cpp"
            else "other"
        )
        for caller, callee in sorted(set(expected) - predicted):
            gold_edge = expected[(caller, callee)]
            caller_file, caller_line, caller_context = callgraph_source_context(
                manifest_path.parent, manifest, caller
            )
            callee_file, callee_line, callee_context = callgraph_source_context(
                manifest_path.parent, manifest, callee
            )
            categories[category] += 1
            rows.append(
                {
                    "case": key,
                    "caller": caller,
                    "callee": callee,
                    "failure_category": category,
                    "gold_kind": gold_edge.get("kind", ""),
                    "gold_evidence": gold_edge.get("evidence", ""),
                    "caller_source": {
                        "file": caller_file,
                        "line": caller_line,
                        "context": caller_context,
                    },
                    "callee_source": {
                        "file": callee_file,
                        "line": callee_line,
                        "context": callee_context,
                    },
                    "manifest": str(manifest_path.relative_to(CACHE)),
                }
            )
    return {
        "count": len(rows),
        "by_failure_category": dict(categories),
        "edges": rows,
    }


MEMSAFETY_CAUSES = {
    "960521-1-2": "multi_branch_pointer_adjustment",
    "lockfree-3_0": "linked_list_ownership_loop",
    "test-0019-1": "struct_field_alias_and_early_return",
    "test-0102-1": "linked_list_field_alias",
    "test-0134": "container_of_pointer_arithmetic",
    "test-0214": "linked_list_branch_ownership",
    "test-0217": "linked_list_branch_ownership",
    "test-0219": "nested_struct_and_custom_calloc",
}


def audit_memsafety() -> dict[str, Any]:
    report = read_json(RESULTS / "svcomp/memsafety/report.json")
    predictions = {
        str(item["id"]): item
        for item in read_json(RESULTS / "svcomp/memsafety/predictions.json")["predictions"]
    }
    cases = {
        str(case["id"]): case
        for case in read_json(RESULTS / "svcomp/cases.json")["groups"]["memsafety"]
    }
    rows = []
    categories = Counter()
    for row in report["cases"]:
        if row.get("expected_leak") or row.get("status") != "leak":
            continue
        case_id = str(row["id"])
        category = MEMSAFETY_CAUSES.get(case_id, "other")
        categories[category] += 1
        evidence = predictions[case_id].get("evidence", [])[:5]
        task_dir = CACHE / "svcomp26" / Path(cases[case_id]["task_file"]).parent
        input_files = {
            Path(name).name: task_dir / name
            for name in cases[case_id].get("input_files", [])
        }
        source_context = []
        for item in evidence:
            source_path = input_files.get(Path(str(item.get("file", ""))).name)
            if source_path is None:
                continue
            source_context.extend(
                source_window(
                    read_lines(source_path),
                    int(item.get("line") or 0),
                    radius=1,
                )
            )
        source_context = list(dict.fromkeys(source_context))
        rows.append(
            {
                **row,
                "task_file": cases[case_id]["task_file"],
                "input_files": cases[case_id]["input_files"],
                "failure_category": category,
                "evidence": evidence,
                "source_context": source_context,
            }
        )
    return {
        "count": len(rows),
        "by_failure_category": dict(categories),
        "cases": rows,
    }


def build_audit() -> dict[str, Any]:
    cgbench = audit_cgbench()
    datarace = audit_datarace()
    callgraph = audit_callgraph()
    memsafety = audit_memsafety()
    return {
        "schema_version": "external-failure-audit-v1",
        "protocol_id": "external-v1",
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "gold_used_for_diagnosis_only": True,
        "summary": {
            "unknown_units": cgbench["count"] + datarace["count"],
            "cgbench_unknown_callsites": cgbench["count"],
            "datarace_unknown_cases": datarace["count"],
            "callgraph_missing_edges": callgraph["count"],
            "memsafety_false_positives": memsafety["count"],
            "skipped_cases": 0,
        },
        "cgbench_unknown": cgbench,
        "datarace_unknown": datarace,
        "callgraph_missing": callgraph,
        "memsafety_false_positive": memsafety,
    }


def markdown(audit: dict[str, Any]) -> str:
    summary = audit["summary"]
    lines = [
        "# External validation failure audit",
        "",
        f"Generated: `{audit['generated_at']}`",
        "",
        "This audit uses Gold only for diagnosis. It does not modify the frozen protocol or predictions.",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|---|---:|",
        f"| CG-Bench unknown callsites | {summary['cgbench_unknown_callsites']} |",
        f"| DataRaceBench unknown cases | {summary['datarace_unknown_cases']} |",
        f"| CallGraphBench missed edges | {summary['callgraph_missing_edges']} |",
        f"| MemSafety false positives | {summary['memsafety_false_positives']} |",
        f"| Skipped cases | {summary['skipped_cases']} |",
        "",
        "## Failure Categories",
        "",
        "| Benchmark | Category | Count |",
        "|---|---|---:|",
    ]
    for name, values in (
        ("CG-Bench", audit["cgbench_unknown"]["by_failure_category"]),
        ("DataRaceBench", audit["datarace_unknown"]["by_failure_category"]),
        ("CallGraphBench", audit["callgraph_missing"]["by_failure_category"]),
        ("SV-COMP MemSafety", audit["memsafety_false_positive"]["by_failure_category"]),
    ):
        for category, count in sorted(values.items()):
            lines.append(f"| {name} | {category} | {count} |")
    lines += [
        "",
        "## CG-Bench Unknown",
        "",
        f"Total callsites: **{audit['cgbench_unknown']['count']}**",
        "",
        "| Project | Callsite | Pointer | Type | Expected targets | Diagnosis |",
        "|---|---|---|---|---|---|",
    ]
    for case in audit["cgbench_unknown"]["cases"]:
        lines.append(
            "| {project} | `{callsite}` | `{pointer}` | {kind} | `{targets}` | {cause} |".format(
                project=case["project"],
                callsite=case["callsite"],
                pointer=case["pointer_expression"],
                kind=case["callsite_type"],
                targets=", ".join(case["expected_targets"]),
                cause=case["failure_category"],
            )
        )
    lines += ["", "### CG-Bench Extracted Evidence", ""]
    for case in audit["cgbench_unknown"]["cases"]:
        lines.append(f"#### `{case['id']}`")
        lines.append("")
        lines.append(
            f"- Project/type: `{case['project']}` / `{case['callsite_type']}`; "
            f"pointer: `{case['pointer_expression']}`"
        )
        lines.append(
            f"- Expected targets: `{', '.join(case['expected_targets'])}`; "
            f"candidate reasons: `{json.dumps(case['candidate_reasons'], ensure_ascii=False)}`"
        )
        trace = ", ".join(
            f"{item['tool']}={item['status']}"
            for item in case["tool_trace"]
        )
        lines.append(f"- Tool trace: `{trace or 'none'}`")
        lines.append("- Source context:")
        for context in case["source_context"]:
            lines.append(f"  - `{escape_markdown(context)}`")
        lines.append("")
    lines += [
        "",
        "## DataRaceBench Unknown",
        "",
        f"Total cases: **{audit['datarace_unknown']['count']}**",
        "",
        "| Case | Expected race | Diagnosis | Existing reason |",
        "|---|---:|---|---|",
    ]
    for case in audit["datarace_unknown"]["cases"]:
        lines.append(
            "| {case} | {race} | {cause} | {reason} |".format(
                case=case["id"],
                race=str(case["expected_race"]).lower(),
                cause=case["failure_category"],
                reason=case["reason"].replace("|", "\\|"),
            )
        )
    lines += ["", "### DataRaceBench Extracted Evidence", ""]
    for case in audit["datarace_unknown"]["cases"]:
        lines.append(f"#### `{case['id']}`")
        lines.append("")
        index_set = case.get("index_set") or {}
        if index_set:
            pairs = ", ".join(
                f"{item['first']}->{item['second']}"
                for item in index_set.get("pairs_at_distance", [])
            ) or "none"
            lines.append(
                f"- Index set: count={index_set.get('count')}, "
                f"range={index_set.get('min')}..{index_set.get('max')}, "
                f"pairs at distance {index_set.get('distance_checked')}: `{pairs}`"
            )
        else:
            lines.append("- Index set: no constant `indexSet` initializer found")
        lines.append("- Source context:")
        for context in case["source_context"]:
            lines.append(f"  - `{escape_markdown(context)}`")
        lines.append("")
    lines += [
        "",
        "## CallGraphBench Missing Edges",
        "",
        f"Total edges: **{audit['callgraph_missing']['count']}**",
        "",
        "| Case | Caller | Callee | Diagnosis |",
        "|---|---|---|---|",
    ]
    for edge in audit["callgraph_missing"]["edges"]:
        lines.append(
            f"| {edge['case']} | `{edge['caller']}` | `{edge['callee']}` | {edge['failure_category']} |"
        )
    lines += ["", "### CallGraphBench Extracted Evidence", ""]
    for edge in audit["callgraph_missing"]["edges"]:
        lines.append(f"#### `{edge['case']}`: `{edge['caller']}` -> `{edge['callee']}`")
        lines.append("")
        lines.append(f"- Diagnosis: `{edge['failure_category']}`")
        lines.append(f"- Gold kind/evidence: `{edge['gold_kind']}`; {edge['gold_evidence']}")
        caller = edge["caller_source"]
        callee = edge["callee_source"]
        lines.append(
            f"- Caller source: `{caller['file']}:{caller['line']}`; "
            + "<br>".join(f"`{line}`" for line in caller["context"])
        )
        lines.append(
            f"- Callee source: `{callee['file']}:{callee['line']}`; "
            + "<br>".join(f"`{line}`" for line in callee["context"])
        )
        lines.append("")
    lines += [
        "",
        "## MemSafety False Positives",
        "",
        f"Total safe tasks misclassified as leaks: **{audit['memsafety_false_positive']['count']}**",
        "",
        "| Case | Diagnosis | Evidence |",
        "|---|---|---|",
    ]
    for case in audit["memsafety_false_positive"]["cases"]:
        evidence = "<br>".join(
            f"{item['file']}:{item['line']} {item['snippet']}"
            for item in case["evidence"][:3]
        )
        escaped_evidence = evidence.replace("|", r"\|")
        lines.append(
            f"| {case['id']} | {case['failure_category']} | {escaped_evidence} |"
        )
    lines += ["", "### MemSafety Extracted Evidence", ""]
    for case in audit["memsafety_false_positive"]["cases"]:
        lines.append(f"#### `{case['id']}`")
        lines.append("")
        lines.append(
            f"- Task: `{case['task_file']}`; inputs: `{', '.join(case['input_files'])}`"
        )
        lines.append("- Source context:")
        for context in case.get("source_context", []):
            lines.append(f"  - `{escape_markdown(context)}`")
        lines.append("")
    lines += [
        "",
        "## Improvement Actions",
        "",
        "- CG-Bench: add interprocedural callback argument mapping, struct field-index resolution, table initializer parsing, cast evaluation and virtual dispatch.",
        "- DataRaceBench: add constant index-set analysis, pointer-offset alias tracking and affine loop dependence checks.",
        "- CallGraphBench: add function-pointer dispatch resolution, cross-file callback registration, C++ constructor/member and virtual-dispatch edges.",
        "- MemSafety: add branch-sensitive resource pairing, pointer arithmetic/container_of normalization, field aliases and ownership transfer.",
        "- Re-run the same frozen v1 cases first; publish any sample or Gold change only as external-v2.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    audit = build_audit()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(markdown(audit).rstrip() + "\n", encoding="utf-8")
    print(json.dumps(audit["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
