#!/usr/bin/env python3
"""Build deterministic external benchmark selection manifests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation/external"
RESULTS = VALIDATION / "results"
OUTPUT = VALIDATION / "selections"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(*values: Any) -> str:
    payload = "\0".join(str(value) for value in values)
    return "unit_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def selection(
    *,
    selection_id: str,
    dataset: str,
    source: dict[str, Any],
    unit: str,
    identity_fields: list[str],
    rules: list[str],
    cases: list[dict[str, Any]],
    units: list[dict[str, Any]],
    exclusions: list[dict[str, Any]] | None = None,
    extra_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    identities = [
        json.dumps(item["identity"], sort_keys=True, ensure_ascii=False)
        for item in units
    ]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{selection_id}: duplicate unit identity")
    return {
        "schema_version": "selection-v1",
        "selection_id": selection_id,
        "dataset": dataset,
        "source": source,
        "unit": unit,
        "identity_fields": identity_fields,
        "rules": rules,
        "counts": {
            "cases": len(cases),
            "units": len(units),
            **(extra_counts or {}),
        },
        "cases": cases,
        "units": units,
        "exclusions": exclusions or [],
    }


def build_callgraphbench() -> dict[str, Any]:
    benchmark = ROOT / ".benchmark-cache/callgraphbench"
    cases = []
    units = []
    for manifest_path in sorted((benchmark / "datasets").glob("*/*/manifest.json")):
        manifest = read_json(manifest_path)
        language = manifest_path.parent.parent.name
        case_id = str(manifest["case_id"])
        key = f"{language}/{case_id}"
        cases.append(
            {
                "id": key,
                "language": language,
                "case_id": case_id,
                "manifest": manifest_path.relative_to(benchmark).as_posix(),
                "manifest_sha256": sha256_path(manifest_path),
            }
        )
        for edge in manifest["ground_truth"]["edges"]:
            identity = {
                "caller": str(edge["caller"]),
                "callee": str(edge["callee"]),
            }
            units.append(
                {
                    "id": stable_id(key, identity["caller"], identity["callee"]),
                    "case_id": key,
                    "identity": identity,
                    "label": "edge",
                }
            )
    return selection(
        selection_id="callgraphbench-v1",
        dataset="callgraphbench",
        source={
            "repo": "https://github.com/lvyahui8/CallGraphBench.git",
            "revision": "fe003b9e9de38d06aa67cabb8643a55dbac7dcbf",
            "license": "MIT",
        },
        unit="call_edge",
        identity_fields=["caller", "callee"],
        rules=[
            "include every official C and C++ manifest under datasets/",
            "score only edges present in manifest ground_truth.edges",
            "deduplicate repeated caller-callee edges by identity",
        ],
        cases=cases,
        units=units,
    )


def build_cgbench() -> dict[str, Any]:
    source = read_json(RESULTS / "cgbench/cases.json")
    cases = []
    units = []
    excluded = []
    for case in source["cases"]:
        if case["project"] == "unknown" or case["type"] == "README":
            excluded.append(
                {
                    "id": case["id"],
                    "reason": "official markdown template, not a benchmark case",
                }
            )
            continue
        cases.append(
            {
                "id": case["id"],
                "project": case["project"],
                "callsite": case["callsite"],
                "pointer_expression": case["fnptr"],
                "type": case["type"],
            }
        )
        for target in case["targets"]:
            identity = {
                "callsite": str(case["callsite"]),
                "target": str(target),
            }
            units.append(
                {
                    "id": stable_id(case["id"], target),
                    "case_id": case["id"],
                    "identity": identity,
                    "label": "target",
                }
            )
    return selection(
        selection_id="cgbench-v1",
        dataset="cgbench",
        source={
            "repo": "https://github.com/timmyyuan/CG-Bench.git",
            "revision": "b277b7329f62fdddc6d4a341057d045bdf69def2",
            "license": "MIT",
            "parsed_cases_sha256": sha256_path(RESULTS / "cgbench/cases.json"),
        },
        unit="callsite_target_pair",
        identity_fields=["callsite", "target"],
        rules=[
            "include every parsed non-template callsite",
            "expand each callsite target set into callsite-target pairs for scoring",
            "treat an omitted prediction as an empty target set",
            "unknown predictions are not scored as correct binary answers",
        ],
        cases=cases,
        units=units,
        exclusions=excluded,
    )


def build_juliet() -> dict[str, Any]:
    source = read_json(RESULTS / "juliet/cases.json")
    cases = []
    units = []
    for case in source["cases"]:
        if case["cwe"] != "CWE401":
            raise ValueError("Juliet selection may only contain CWE401")
        cases.append(
            {
                "id": case["id"],
                "language": case["language"],
                "source_file": case["source_file"],
                "cwe": case["cwe"],
            }
        )
        for function in case["labels"]["defect_prefixed_functions"]:
            identity = {
                "case_id": case["id"],
                "source_file": case["source_file"],
                "function": function,
            }
            units.append(
                {
                    "id": stable_id(*identity.values()),
                    "case_id": case["id"],
                    "identity": identity,
                    "label": "leak",
                }
            )
        for function in case["labels"]["clean_marked_functions"]:
            identity = {
                "case_id": case["id"],
                "source_file": case["source_file"],
                "function": function,
            }
            units.append(
                {
                    "id": stable_id(*identity.values()),
                    "case_id": case["id"],
                    "identity": identity,
                    "label": "no_leak",
                }
            )
    return selection(
        selection_id="juliet-cwe401-v1",
        dataset="juliet-cwe401",
        source={
            "url": "https://samate.nist.gov/SARD/downloads/test-suites/2017-10-01-juliet-test-suite-for-c-cplusplus-v1-3.zip",
            "revision": source["official_sha256"],
            "license": source["license"],
        },
        unit="function",
        identity_fields=["case_id", "source_file", "function"],
        rules=[
            "include only CWE401_Memory_Leak",
            "use the defect-prefixed bad functions as leak positives",
            "use marked good functions as no-leak negatives",
            "do not mix CWE404, CWE415, or CWE416 into the leak metric",
        ],
        cases=cases,
        units=units,
        extra_counts={
            "positive_units": sum(item["label"] == "leak" for item in units),
            "negative_units": sum(item["label"] == "no_leak" for item in units),
        },
    )


def build_svcomp() -> tuple[dict[str, Any], dict[str, Any]]:
    source = read_json(RESULTS / "svcomp/cases.json")
    result = []
    for group, dataset, selection_id, identity_fields in (
        (
            "memsafety",
            "svcomp-memsafety",
            "svcomp-memsafety-v1",
            ["task_file", "property_file", "subproperty"],
        ),
        (
            "concurrency",
            "svcomp-concurrency",
            "svcomp-concurrency-v1",
            ["task_file", "property_file"],
        ),
    ):
        cases = source["groups"][group]
        units = []
        for case in cases:
            identity = {
                "task_file": case["task_file"],
                "property_file": case["property"],
            }
            if group == "memsafety":
                identity["subproperty"] = case["subproperty"]
            if group == "memsafety":
                label = "no_leak" if case["expected_verdict"] else "leak"
            else:
                label = "no_race" if case["expected_verdict"] else "race"
            units.append(
                {
                    "id": stable_id(*identity.values()),
                    "case_id": case["id"],
                    "identity": identity,
                    "label": label,
                    "expected_verdict": case["expected_verdict"],
                }
            )
        selected = selection(
            selection_id=selection_id,
            dataset=dataset,
            source={
                "repo": "https://gitlab.com/sosy-lab/benchmarking/sv-benchmarks.git",
                "revision": source["commit"],
                "license": source["license"],
            },
            unit="task_property",
            identity_fields=identity_fields,
            rules=[
                "use the pinned SV-COMP case index",
                "expected_verdict=true is the safe negative class",
                "expected_verdict=false is the property-violation positive class",
                "do not drop unparsable tasks; emit unknown instead",
            ],
            cases=[
                {
                    "id": case["id"],
                    "task_file": case["task_file"],
                    "property": case["property"],
                    "subproperty": case["subproperty"],
                }
                for case in cases
            ],
            units=units,
            extra_counts={
                "positive_units": sum(
                    item["label"] in {"leak", "race"} for item in units
                ),
                "negative_units": sum(
                    item["label"] in {"no_leak", "no_race"} for item in units
                ),
            },
        )
        result.append(selected)
    return result[0], result[1]


def build_dataracebench() -> dict[str, Any]:
    source = read_json(RESULTS / "dataracebench/cases.json")
    cases = [
        {
            "id": case["id"],
            "source_file": case["source_file"],
        }
        for case in source["cases"]
    ]
    units = [
        {
            "id": stable_id(case["id"]),
            "case_id": case["id"],
            "identity": {"case_id": case["id"]},
            "label": "race" if case["expected_race"] else "no_race",
            "source_file": case["source_file"],
        }
        for case in source["cases"]
    ]
    return selection(
        selection_id="dataracebench-v1",
        dataset="dataracebench",
        source={
            "repo": "https://github.com/LLNL/dataracebench.git",
            "revision": source["commit"],
            "license": source["license"],
        },
        unit="case",
        identity_fields=["case_id"],
        rules=[
            "use the pinned deterministic first-12-per-label subset",
            "official -yes suffix is race and -no suffix is no-race",
            "unknown is not converted into no-race",
        ],
        cases=cases,
        units=units,
        extra_counts={
            "positive_units": sum(item["label"] == "race" for item in units),
            "negative_units": sum(item["label"] == "no_race" for item in units),
        },
    )


def main() -> int:
    selections = {
        "callgraphbench-v1": build_callgraphbench(),
        "cgbench-v1": build_cgbench(),
        "juliet-cwe401-v1": build_juliet(),
        "dataracebench-v1": build_dataracebench(),
    }
    svcomp_memsafety, svcomp_concurrency = build_svcomp()
    selections["svcomp-memsafety-v1"] = svcomp_memsafety
    selections["svcomp-concurrency-v1"] = svcomp_concurrency
    for selection_id, payload in selections.items():
        write_json(OUTPUT / f"{selection_id}.json", payload)
    print(
        json.dumps(
            {
                selection_id: payload["counts"]
                for selection_id, payload in selections.items()
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
