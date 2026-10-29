from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DATASETS = {
    "callgraphbench",
    "cgbench",
    "juliet-cwe401",
    "svcomp-memsafety",
    "dataracebench",
    "svcomp-concurrency",
}
STATUSES = {
    "predicted",
    "race",
    "no_race",
    "unknown",
    "leak",
    "no_leak",
}
TARGET_STATUSES = {"predicted", "unknown"}
RACE_STATUSES = {"race", "no_race", "unknown"}
RESOURCE_STATUSES = {"leak", "no_leak", "unknown"}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def prediction_envelope(
    *,
    dataset: str,
    generator: dict[str, Any],
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "external-prediction-v1",
        "protocol_id": "external-v1",
        "dataset": dataset,
        "generator": generator,
        "predictions": predictions,
    }


def validate_prediction_document(
    document: dict[str, Any],
    *,
    expected_dataset: str | None = None,
) -> None:
    if not isinstance(document, dict):
        raise ValueError("prediction document must be an object")
    required = {
        "schema_version",
        "protocol_id",
        "dataset",
        "generator",
        "predictions",
    }
    missing = required - set(document)
    if missing:
        raise ValueError(f"prediction document is missing {sorted(missing)}")
    if document["schema_version"] != "external-prediction-v1":
        raise ValueError("invalid prediction schema_version")
    if document["protocol_id"] != "external-v1":
        raise ValueError("invalid protocol_id")
    if document["dataset"] not in DATASETS:
        raise ValueError(f"unknown prediction dataset: {document['dataset']}")
    if expected_dataset is not None and document["dataset"] != expected_dataset:
        raise ValueError(
            f"prediction dataset {document['dataset']} does not match {expected_dataset}"
        )
    generator = document["generator"]
    if not isinstance(generator, dict):
        raise ValueError("generator must be an object")
    for field in ("name", "version", "mode"):
        if not isinstance(generator.get(field), str) or not generator[field]:
            raise ValueError(f"generator.{field} must be a non-empty string")
    if generator["mode"] not in {"deterministic", "llm", "external-submission"}:
        raise ValueError("invalid generator.mode")
    prompt_hash = generator.get("prompt_sha256")
    if prompt_hash is not None and (
        not isinstance(prompt_hash, str)
        or len(prompt_hash) != 64
        or any(char not in "0123456789abcdef" for char in prompt_hash)
    ):
        raise ValueError("generator.prompt_sha256 must be a lowercase SHA-256")
    predictions = document["predictions"]
    if not isinstance(predictions, list):
        raise ValueError("predictions must be a list")
    ids = set()
    identities = set()
    for item in predictions:
        if not isinstance(item, dict):
            raise ValueError("each prediction must be an object")
        missing = {"id", "identity", "status", "evidence"} - set(item)
        if missing:
            raise ValueError(f"prediction is missing {sorted(missing)}")
        if not isinstance(item["id"], str) or not item["id"]:
            raise ValueError("prediction.id must be a non-empty string")
        if item["id"] in ids:
            raise ValueError(f"duplicate prediction id: {item['id']}")
        ids.add(item["id"])
        identity = item["identity"]
        if not isinstance(identity, dict) or not identity:
            raise ValueError("prediction.identity must be a non-empty object")
        for value in identity.values():
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ValueError("prediction identity values must be scalar")
        identity_key = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        if identity_key in identities:
            raise ValueError(f"duplicate prediction identity: {identity}")
        identities.add(identity_key)
        status = item["status"]
        if status not in STATUSES:
            raise ValueError(f"invalid prediction status: {status}")
        targets = item.get("targets", [])
        if not isinstance(targets, list) or any(
            not isinstance(target, str) or not target for target in targets
        ):
            raise ValueError("prediction.targets must be non-empty strings")
        if len(targets) != len(set(targets)):
            raise ValueError("prediction.targets must be unique")
        if status == "unknown" and targets:
            raise ValueError("unknown prediction must not contain guessed targets")
        if (
            document["dataset"] == "cgbench"
            and status == "predicted"
            and "targets" not in item
        ):
            raise ValueError("predicted target set requires targets")
        confidence = item.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise ValueError("confidence must be between zero and one")
        evidence = item["evidence"]
        if not isinstance(evidence, list):
            raise ValueError("prediction.evidence must be a list")
        evidence_ids = set()
        for record in evidence:
            if not isinstance(record, dict):
                raise ValueError("evidence must be an object")
            if not all(key in record for key in ("id", "file", "line")):
                raise ValueError("evidence requires id, file, and line")
            if not isinstance(record["line"], int) or record["line"] < 1:
                raise ValueError("evidence line must be a positive integer")
            if record["id"] in evidence_ids:
                raise ValueError("duplicate evidence id within a prediction")
            evidence_ids.add(record["id"])


def status_set_for_dataset(dataset: str) -> set[str]:
    if dataset == "cgbench":
        return TARGET_STATUSES
    if dataset in {"dataracebench", "svcomp-concurrency"}:
        return RACE_STATUSES
    if dataset in {"juliet-cwe401", "svcomp-memsafety"}:
        return RESOURCE_STATUSES
    return TARGET_STATUSES
