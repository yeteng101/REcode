#!/usr/bin/env python3
"""Evaluate the repository analyzer with the pinned CallGraphBench C/C++ cases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clang_pipeline.clang_ast import read_json, write_json
from clang_pipeline.evaluation_protocol import (
    prediction_envelope,
    validate_prediction_document,
)
from clang_pipeline.pipeline import run_pipeline
from clang_pipeline.repo_agent import _relativize_json_paths, generic_compile_commands


BENCHMARK = ROOT / ".benchmark-cache/callgraphbench"
OUTPUT = ROOT / "validation/external/results/callgraphbench"
SELECTION = ROOT / "validation/external/selections/callgraphbench-v1.json"
SUPPORTED_LANGUAGES = ("c", "cpp")


def normalize_path(value: str) -> str:
    return str(value).replace("\\", "/").removeprefix("./")


def name_keys(value: str) -> set[str]:
    text = normalize_path(value)
    if text.startswith(("fn:", "cb:")):
        text = text.split(":", 1)[1]
    text = text.replace("::", ".").replace(":", ".").replace("/", ".")
    parts = [part for part in text.split(".") if part]
    return {
        text,
        parts[-1] if parts else text,
        ".".join(parts[-2:]) if len(parts) >= 2 else text,
    }


def map_graph_edges(graph: dict, manifest: dict, case_root: Path) -> list[dict]:
    symbols = manifest["symbols"]
    by_file_line = {
        (normalize_path(str(item.get("file") or "")), int(item.get("line") or 0)): item["id"]
        for item in symbols
    }
    by_file: dict[str, list[dict]] = {}
    for item in symbols:
        by_file.setdefault(normalize_path(str(item.get("file") or "")), []).append(item)
    node_by_id = {node["id"]: node for node in graph.get("nodes", [])}
    node_symbol: dict[str, str] = {}
    for node_id, node in node_by_id.items():
        file = normalize_path(str(node.get("file") or ""))
        line = int(node.get("line") or 0)
        if not file:
            continue
        symbol_id = by_file_line.get((file, line))
        if symbol_id:
            node_symbol[node_id] = symbol_id
            continue
        node_names = name_keys(str(node.get("name") or ""))
        candidates = [
            item
            for item in by_file.get(file, [])
            if name_keys(str(item.get("id") or "")) & node_names
        ]
        if len(candidates) == 1:
            node_symbol[node_id] = candidates[0]["id"]

    kind_map = {
        "direct_call": "direct",
        "callback_edge": "callback",
        "indirect_call": "indirect",
    }
    edges: dict[tuple[str, str], dict] = {}
    for edge in graph.get("edges", []):
        caller = node_symbol.get(str(edge.get("source") or ""))
        callee = node_symbol.get(str(edge.get("target") or ""))
        if not caller or not callee:
            continue
        key = (caller, callee)
        edges[key] = {
            "caller": caller,
            "callee": callee,
            "kind": kind_map.get(str(edge.get("kind") or ""), "unknown"),
            "evidence_ids": list(edge.get("evidence_ids", [])),
        }
    return [edges[key] for key in sorted(edges)]


def discover_cases() -> list[tuple[str, str, Path, dict]]:
    cases = []
    for language in SUPPORTED_LANGUAGES:
        for manifest_path in sorted((BENCHMARK / "datasets" / language).glob("*/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            cases.append(
                (language, str(manifest["case_id"]), manifest_path.parent, manifest)
            )
    return cases


def score(predictions: dict[tuple[str, str], list[dict]], manifests: dict) -> dict:
    total = {"tp": 0, "fp": 0, "fn": 0}
    cases = {}
    for key, manifest in manifests.items():
        expected = {
            (str(edge["caller"]), str(edge["callee"]))
            for edge in manifest["ground_truth"]["edges"]
        }
        predicted = {
            (str(edge["caller"]), str(edge["callee"]))
            for edge in predictions[key]
        }
        tp = len(expected & predicted)
        fp = len(predicted - expected)
        fn = len(expected - predicted)
        total["tp"] += tp
        total["fp"] += fp
        total["fn"] += fn
        cases[key] = {"tp": tp, "fp": fp, "fn": fn}
    denominator = total["tp"] + total["fp"]
    recall_denominator = total["tp"] + total["fn"]
    f1_denominator = 2 * total["tp"] + total["fp"] + total["fn"]
    return {
        "schema_version": "1.0",
        "dataset": "CallGraphBench",
        "commit": "fe003b9e9de38d06aa67cabb8643a55dbac7dcbf",
        "scope": "official C and C++ cases",
        "cases": cases,
        "totals": {
            **total,
            "precision": total["tp"] / denominator if denominator else None,
            "recall": total["tp"] / recall_denominator if recall_denominator else None,
            "f1": 2 * total["tp"] / f1_denominator if f1_denominator else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if not BENCHMARK.is_dir():
        parser.error(
            "CallGraphBench is not fetched; run scripts/fetch_external_benchmarks.py "
            "--benchmark callgraphbench"
        )
    cases = discover_cases()
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("no C/C++ CallGraphBench cases found")
    args.output.mkdir(parents=True, exist_ok=True)
    predictions_root = args.output / "predictions"
    manifests = {}
    prediction_edges = {}
    evidence_by_case = {}
    with tempfile.TemporaryDirectory(prefix="callgraphbench-") as temp_dir:
        temp_root = Path(temp_dir)
        for language, case_id, case_root, manifest in cases:
            workspace = temp_root / language / case_id / "workspace"
            run_pipeline(
                source_root=case_root,
                workspace=workspace,
                run_id=f"cgb_{language}_{case_id}",
                build_profile="callgraphbench-v1",
                compile_commands=generic_compile_commands(case_root),
                repository=f"callgraphbench/{language}/{case_id}",
                commit="fe003b9e9de38d06aa67cabb8643a55dbac7dcbf",
            )
            graph = _relativize_json_paths(
                read_json(workspace / "07-report" / "graph.json"),
                case_root,
            )
            key = f"{language}/{case_id}"
            evidence_by_case[key] = {
                str(item["id"]): item
                for item in graph.get("evidence", [])
            }
            edges = map_graph_edges(graph, manifest, case_root)
            target = predictions_root / language / case_id / "callgraph.json"
            write_json(target, {"schema_version": "1.0", "tool": "code-reverse-agent", "edges": edges})
            manifests[key] = manifest
            prediction_edges[key] = edges

    summary = score(prediction_edges, manifests)
    write_json(args.output / "summary.json", summary)
    selection = read_json(SELECTION)
    selected_case_ids = {case["id"] for case in selection["cases"]}
    edge_predictions = []
    for key, edges in prediction_edges.items():
        if key not in selected_case_ids:
            continue
        for edge in edges:
            edge_predictions.append(
                {
                    "id": f"{key}::{edge['caller']}->{edge['callee']}",
                    "identity": {
                        "caller": edge["caller"],
                        "callee": edge["callee"],
                    },
                    "status": "predicted",
                    "confidence": 0.7,
                    "evidence": [
                        {
                            "id": evidence_id,
                            "file": evidence_by_case.get(key, {}).get(
                                evidence_id, {}
                            ).get("file", ""),
                            "line": evidence_by_case.get(key, {}).get(
                                evidence_id, {}
                            ).get("line", 1),
                            "snippet": evidence_by_case.get(key, {}).get(
                                evidence_id, {}
                            ).get("snippet", ""),
                        }
                        for evidence_id in edge.get("evidence_ids", [])
                        if evidence_id in evidence_by_case.get(key, {})
                    ],
                    "details": {"case_id": key, "kind": edge["kind"]},
                }
            )
    envelope = prediction_envelope(
        dataset="callgraphbench",
        generator={
            "name": "clang_pipeline-native",
            "version": "1.0.0",
            "mode": "deterministic",
            "parameters": {"gold_exposed_to_analyzer": False},
        },
        predictions=edge_predictions,
    )
    validate_prediction_document(envelope, expected_dataset="callgraphbench")
    write_json(args.output / "predictions.json", envelope)
    if args.limit is None:
        command = [
            sys.executable,
            str(BENCHMARK / "tools/evaluate.py"),
            "--benchmark",
            str(BENCHMARK / "datasets"),
            "--languages",
            "c,cpp",
            "--predictions",
            str(predictions_root),
            "--report",
            str(args.output / "report.md"),
        ]
        completed = subprocess.run(
            command,
            cwd=BENCHMARK,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise SystemExit(completed.stderr.strip() or completed.stdout.strip())
    else:
        totals = summary["totals"]
        (args.output / "report.md").write_text(
            "# CallGraphBench dry run\n\n"
            f"Cases: {len(cases)}\n\n"
            f"TP: {totals['tp']}  \n"
            f"FP: {totals['fp']}  \n"
            f"FN: {totals['fn']}\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
