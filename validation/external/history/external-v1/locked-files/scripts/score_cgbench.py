#!/usr/bin/env python3
"""Score schema-valid CG-Bench callsite target-set predictions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clang_pipeline.evaluation_protocol import validate_prediction_document


DEFAULT_SELECTION = ROOT / "validation/external/selections/cgbench-v1.json"
DEFAULT_SCHEMA = ROOT / "validation/external/prediction.schema.json"
DEFAULT_REPORT = ROOT / "validation/external/results/cgbench/report.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(predictions: dict[str, Any] | list[dict[str, Any]]) -> dict[str, set[str]]:
    """Return the legacy callsite-to-target-set view."""
    return {
        callsite: row["targets"]
        for callsite, row in normalize_detailed(predictions).items()
    }


def normalize_detailed(
    predictions: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if isinstance(predictions, list):
        rows: Any = predictions
    elif isinstance(predictions, dict) and "predictions" in predictions:
        rows = predictions["predictions"]
    elif isinstance(predictions, dict):
        rows = predictions
    else:
        raise ValueError("predictions must be a list or object")

    if isinstance(rows, dict):
        return {
            str(callsite): {
                "status": "predicted",
                "targets": set(map(str, targets)),
            }
            for callsite, targets in rows.items()
        }
    if not isinstance(rows, list):
        raise ValueError("predictions must contain a list or object")

    result: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("each prediction must be an object")
        identity = item.get("identity")
        callsite = (
            identity.get("callsite")
            if isinstance(identity, dict)
            else item.get("callsite")
        )
        if not callsite:
            raise ValueError("prediction is missing callsite identity")
        callsite = str(callsite)
        if callsite in result:
            raise ValueError(f"duplicate callsite prediction: {callsite}")
        status = str(item.get("status") or "predicted")
        if status not in {"predicted", "unknown"}:
            raise ValueError(f"CG-Bench cannot use status {status}")
        targets = set(map(str, item.get("targets", [])))
        if status == "unknown" and targets:
            raise ValueError("unknown prediction must not contain guessed targets")
        result[callsite] = {
            "status": status,
            "targets": targets,
        }
    return result


def validate_prediction_schema(predictions: dict[str, Any], schema_path: Path) -> None:
    if predictions.get("schema_version") != "external-prediction-v1":
        raise ValueError("missing external-prediction-v1 envelope")
    if not schema_path.is_file():
        raise ValueError(f"prediction schema does not exist: {schema_path}")
    validate_prediction_document(predictions, expected_dataset="cgbench")


def score(
    predictions: dict[str, Any] | list[dict[str, Any]],
    selection: dict[str, Any],
) -> dict[str, Any]:
    actual_rows = normalize_detailed(predictions)
    units_by_callsite: dict[str, set[str]] = {}
    case_by_callsite: dict[str, str] = {}
    for unit in selection["units"]:
        identity = unit["identity"]
        callsite = str(identity["callsite"])
        target = str(identity["target"])
        units_by_callsite.setdefault(callsite, set()).add(target)
        case_by_callsite[callsite] = str(unit["case_id"])

    tp = fp = fn = 0
    unknown_callsites = 0
    unknown_expected_units = 0
    per_case = []
    for callsite, expected in sorted(units_by_callsite.items()):
        row = actual_rows.get(callsite)
        status = row["status"] if row else "predicted"
        actual = row["targets"] if row else set()
        if status == "unknown":
            unknown_callsites += 1
            unknown_expected_units += len(expected)
            case_tp = case_fp = case_fn = 0
        else:
            case_tp = len(expected & actual)
            case_fp = len(actual - expected)
            case_fn = len(expected - actual)
            tp += case_tp
            fp += case_fp
            fn += case_fn
        per_case.append(
            {
                "id": case_by_callsite[callsite],
                "callsite": callsite,
                "status": status,
                "expected_count": len(expected),
                "predicted_count": len(actual),
                "tp": case_tp,
                "fp": case_fp,
                "fn": case_fn,
            }
        )
    answered_units = sum(
        len(units)
        for callsite, units in units_by_callsite.items()
        if (actual_rows.get(callsite) or {}).get("status", "predicted") != "unknown"
    )
    total_units = sum(len(units) for units in units_by_callsite.values())
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    f1_denominator = 2 * tp + fp + fn
    return {
        "schema_version": "1.0",
        "dataset": "CG-Bench",
        "selection_id": selection["selection_id"],
        "commit": selection["source"]["revision"],
        "metric": "callsite target-set prediction; callsite-target micro average",
        "totals": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / precision_denominator if precision_denominator else None,
            "recall": tp / recall_denominator if recall_denominator else None,
            "f1": 2 * tp / f1_denominator if f1_denominator else None,
            "total_expected_units": total_units,
            "binary_answered_units": answered_units,
            "unknown_callsites": unknown_callsites,
            "unknown_expected_units": unknown_expected_units,
            "unknown_callsite_rate": (
                unknown_callsites / len(units_by_callsite)
                if units_by_callsite
                else None
            ),
            "coverage": answered_units / total_units if total_units else None,
        },
        "cases": per_case,
    }


def format_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="accept the pre-protocol list/dict format",
    )
    args = parser.parse_args()
    predictions = read_json(args.predictions)
    if not args.allow_legacy:
        validate_prediction_schema(predictions, args.schema)
    selection = read_json(args.selection)
    report = score(predictions, selection)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path = args.output.with_suffix(".md")
    totals = report["totals"]
    markdown_path.write_text(
        "\n".join(
            [
                "# CG-Bench target-set baseline",
                "",
                "| TP | FP | FN | Precision | Recall | F1 | Unknown callsites | "
                "Unknown rate | Coverage |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                "| {tp} | {fp} | {fn} | {precision} | {recall} | {f1} | "
                "{unknown} | {unknown_rate} | {coverage} |".format(
                    tp=totals["tp"],
                    fp=totals["fp"],
                    fn=totals["fn"],
                    precision=format_rate(totals["precision"]),
                    recall=format_rate(totals["recall"]),
                    f1=format_rate(totals["f1"]),
                    unknown=totals["unknown_callsites"],
                    unknown_rate=format_rate(totals["unknown_callsite_rate"]),
                    coverage=format_rate(totals["coverage"]),
                ),
                "",
                "Metrics use callsite-target pairs. Unknown callsites are excluded",
                "from binary TP/FP/FN and remain visible in the prediction JSON.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["totals"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
