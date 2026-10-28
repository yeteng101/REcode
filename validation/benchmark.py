"""Common fact-level evaluation and external result validation."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
FILES = {"call_chain": "call-chains", "resource_flow": "resource-flow", "sync_relation": "sync-relations"}
STATUSES = {"confirmed", "unconfirmed", "refuted"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def anchor(e):
    e = e.get("location", e)
    return (e.get("file", "").replace("\\", "/"), e.get("line"), " ".join(e.get("snippet", "").split()))


def key(record):
    return (record["category"], tuple(record["identity"]), record["kind"])


def validate_external(data, category):
    Draft202012Validator(read(ROOT / "schemas" / (FILES[category] + ".schema.json"))).validate(data)
    if data["result_type"] != category:
        raise ValueError("result_type does not match input file")
    if data["status"] in {"not_available", "failed"} and data["items"]:
        raise ValueError("unavailable/failed result must not contain predictions")
    evidence = {e["id"]: e for e in data["evidence"]}
    if len(evidence) != len(data["evidence"]):
        raise ValueError("duplicate evidence IDs")
    ids = set()
    for item in data["items"]:
        if item["id"] in ids:
            raise ValueError("duplicate finding IDs")
        ids.add(item["id"])
        facts = [item] + item.get("edges", []) + item.get("operations", [])
        if category == "call_chain":
            node_ids = {n["id"] for n in item["nodes"]}
            if len(node_ids) != len(item["nodes"]) or any(
                    e["source"] not in node_ids or e["target"] not in node_ids for e in item["edges"]):
                raise ValueError("duplicate or unresolved call-chain node IDs")
        for fact in facts:
            if fact.get("status") not in STATUSES:
                raise ValueError("finding/edge/operation requires explicit status")
            confidence = fact.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not 0 <= confidence <= 1:
                raise ValueError("invalid fact confidence")
            if not fact.get("evidence_ids") or any(e not in evidence for e in fact["evidence_ids"]):
                raise ValueError("missing or dangling fact evidence")
    return evidence


def flatten(data, category, identity_profile="default"):
    evidence = validate_external(data, category)
    facts = []

    def symbol_name(value):
        return str(value).split(":", 1)[-1]

    for item in data["items"]:
        if category == "call_chain":
            nodes = {n["id"]: n.get("symbol", n["id"]) for n in item["nodes"]}
            rows = [(e, [nodes.get(e["source"], e["source"]), nodes.get(e["target"], e["target"])], e["kind"]) for e in item["edges"]]
        elif category == "resource_flow":
            if identity_profile == "source_anchor_v1":
                rows = [(o, [o.get("function", ""), o["file"], o["line"]], o["kind"]) for o in item["operations"]]
            else:
                rows = [(o, [item["resource"], o.get("function", ""), o["file"], o["line"]], o["kind"]) for o in item["operations"]]
        else:
            a, b = item["source"], item["target"]
            rows = [(item, [a["function"], b["function"], a["file"], a["line"], b["file"], b["line"]], item["order"])]
        for fact, identity, kind in rows:
            if category == "call_chain":
                source_function = symbol_name(identity[0])
                functions = [source_function, symbol_name(identity[1])]
            elif category == "resource_flow":
                source_function = str(fact.get("function") or "")
                functions = [source_function]
            else:
                source_function = str(item["source"].get("function") or "")
                functions = [
                    source_function,
                    str(item["target"].get("function") or ""),
                ]
            facts.append(
                {
                    "category": category,
                    "identity": identity,
                    "kind": kind,
                    "status": fact["status"],
                    "confidence": fact["confidence"],
                    "primary_function": source_function,
                    "functions": functions,
                    "evidence": [
                        evidence[evidence_id]
                        for evidence_id in fact["evidence_ids"]
                    ],
                }
            )
    return facts


def ratio(n, d):
    return n / d if d else None


def _fact_files(fact):
    return {
        str(item.get("location", {}).get("file") or item.get("file") or "")
        .replace("\\", "/")
        .removeprefix("./")
        for item in fact.get("evidence", [])
        if str(item.get("location", {}).get("file") or item.get("file") or "")
    }


def _fact_in_scope(fact, paths, functions):
    normalized = [
        str(path).replace("\\", "/").removeprefix("./").rstrip("/")
        for path in paths
    ]
    files = _fact_files(fact)
    if normalized:
        if not files or not all(
            any(file == path or file.startswith(path + "/") for path in normalized)
            for file in files
        ):
            return False
    if functions and str(fact.get("primary_function") or "") not in set(functions):
        return False
    return True


def evaluate(truth, documents):
    Draft202012Validator(read(ROOT / "schemas/validation-benchmark.schema.json")).validate(truth)
    if truth.get("schema_version") != "1.0" or not isinstance(truth.get("cases"), list):
        raise ValueError("invalid ground truth envelope")
    known, ids = {}, set()
    for case in truth["cases"]:
        if case["id"] in ids or key(case) in known:
            raise ValueError("duplicate ground truth ID or semantic key")
        if case["category"] not in FILES or type(case["expected_exists"]) is not bool:
            raise ValueError("invalid ground truth category or expected_exists")
        if not case.get("evidence"):
            raise ValueError("ground truth requires evidence")
        ids.add(case["id"])
        known[key(case)] = case
    annotation_scope = dict(truth.get("annotation_scope") or {})
    scope_mode = str(annotation_scope.get("mode") or "open_world")
    scope_categories = set(annotation_scope.get("categories") or [])
    scope_paths = list(annotation_scope.get("paths") or [])
    scope_functions = list(annotation_scope.get("functions") or [])
    identity_profile = str(truth.get("identity_profile") or "default")
    report = {"schema_version": "1.0", "dataset_id": truth["dataset_id"],
              "scope": (
                  "closed-world annotated subset; every in-scope prediction is scored"
                  if scope_mode == "closed_world"
                  else "annotated facts only; unlabelled predictions excluded from precision"
              ),
              "scoring": "candidate facts including unconfirmed; confirmed-only metrics reported separately",
              "review": truth.get("review", {}),
              "annotation_scope": annotation_scope,
              "identity_profile": identity_profile,
              "categories": {}}
    for category in FILES:
        doc = documents[category]
        if doc.get("source", {}).get("commit") != truth["commit"]:
            raise ValueError("prediction commit differs from ground truth")
        if doc.get("source", {}).get("repository") != truth["repository"]:
            raise ValueError("prediction repository differs from ground truth")
        cases = {k: c for k, c in known.items() if c["category"] == category}
        raw = flatten(doc, category, identity_profile)
        pred = {}
        for fact in raw:
            if fact["status"] == "refuted":
                continue
            k = key(fact)
            if k not in pred:
                pred[k] = fact
            else:
                pred[k]["evidence"] += fact["evidence"]
                if fact["status"] == "confirmed":
                    pred[k]["status"] = "confirmed"
        positives = {k for k, c in cases.items() if c["expected_exists"]}
        negatives = set(cases) - positives
        # Wrong type on an annotated identity is a false positive even if not explicitly enumerated.
        identities = {k[:2] for k in cases}
        closed_world = (
            scope_mode == "closed_world"
            and (not scope_categories or category in scope_categories)
        )
        if closed_world:
            scoped = {
                k
                for k, fact in pred.items()
                if _fact_in_scope(fact, scope_paths, scope_functions)
            }
        else:
            scoped = {k for k in pred if k[:2] in identities}
        out_of_scope = set(pred) - scoped
        tp, fp, fn = scoped & positives, scoped - positives, positives - scoped
        confirmed = {k for k in scoped if pred[k]["status"] == "confirmed"}
        p, r = ratio(len(tp), len(tp) + len(fp)), ratio(len(tp), len(positives))
        matching_identities = {k[:2] for k in scoped}
        typed_predictions = {k for k in scoped if k[:2] in {p[:2] for p in positives}}
        evidence_hits = 0
        details = []
        for k in sorted(cases, key=str):
            c = cases[k]
            hit = k in pred
            verdict = "tp" if hit and c["expected_exists"] else "fp" if hit else "fn" if c["expected_exists"] else "tn"
            ev_ok = hit and {anchor(e) for e in c["evidence"]}.issubset({anchor(e) for e in pred[k]["evidence"]})
            if verdict == "tp" and ev_ok:
                evidence_hits += 1
            details.append({"case_id": c["id"], "verdict": verdict, "evidence_hit": bool(ev_ok),
                            "kind": c["kind"], "identity": c["identity"], "notes": c.get("notes", "")})
        for k in sorted(scoped - set(cases), key=str):
            details.append({"case_id": None, "verdict": "fp", "evidence_hit": False,
                            "kind": k[2], "identity": list(k[1]), "notes": "wrong type/order on annotated identity"})
        cp, cr = ratio(len(confirmed & positives), len(confirmed)), ratio(len(confirmed & positives), len(positives))
        report["categories"][category] = {
            "input_status": doc["status"], "gold_positive": len(positives), "gold_negative": len(negatives),
            "tp": len(tp), "fp": len(fp), "fn": len(fn), "tn": len(negatives - scoped),
            "precision": p, "recall": r,
            "f1": 2 * len(tp) / (2 * len(tp) + len(fp) + len(fn)) if 2 * len(tp) + len(fp) + len(fn) else None,
            "type_accuracy": ratio(len(tp), len(typed_predictions)),
            "evidence_hit_rate": ratio(evidence_hits, len(tp)),
            "coverage": ratio(len({k[:2] for k in positives} & matching_identities), len({k[:2] for k in positives})),
            "unconfirmed_ratio": ratio(sum(f["status"] != "confirmed" for f in pred.values()), len(pred)),
            "unlabelled_predictions": 0 if closed_world else len(out_of_scope),
            "out_of_scope_predictions": len(out_of_scope),
            "prediction_count": len(pred),
            "duplicate_predictions": len([f for f in raw if f["status"] != "refuted"]) - len(pred),
            "confirmed_only": {"precision": cp, "recall": cr, "tp": len(confirmed & positives), "fp": len(confirmed - positives),
                               "f1": ratio(2 * len(confirmed & positives), len(confirmed) + len(positives))},
            "errors_by_type": dict(Counter(d["kind"] for d in details if d["verdict"] in {"fp", "fn"})),
            "cases": details,
        }
    return report


def markdown(report):
    lines = ["# Validation report", "", report["scope"], "", report["scoring"], "",
             "Unavailable/zero-denominator metrics are N/A. No quality gate is implied.", "",
             "| Category | TP | FP | FN | Precision | Recall | F1 | Type accuracy | Evidence | Coverage | Unconfirmed |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    fmt = lambda x: "N/A" if x is None else f"{x:.2%}"
    for category, m in report["categories"].items():
        values = [category, str(m["tp"]), str(m["fp"]), str(m["fn"])]
        values += [fmt(m[k]) for k in ["precision", "recall", "f1", "type_accuracy", "evidence_hit_rate", "coverage", "unconfirmed_ratio"]]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"
