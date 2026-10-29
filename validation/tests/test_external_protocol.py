from __future__ import annotations

import json
from pathlib import Path
import unittest

from clang_pipeline.evaluation_protocol import (
    prediction_envelope,
    validate_prediction_document,
)


ROOT = Path(__file__).resolve().parents[2]


class ExternalProtocolTests(unittest.TestCase):
    def test_selection_manifests_have_unique_units(self) -> None:
        directory = ROOT / "validation/external/selections"
        protocol = json.loads(
            (ROOT / "validation/external/protocol-v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(6, len(protocol["datasets"]))
        for config in protocol["datasets"].values():
            data = json.loads(
                (ROOT / config["selection"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                len(data["units"]),
                data["counts"]["units"],
            )
            identities = {
                json.dumps(unit["identity"], sort_keys=True)
                for unit in data["units"]
            }
            self.assertEqual(len(identities), len(data["units"]))
        self.assertTrue(directory.is_dir())

    def test_unknown_is_a_first_class_prediction(self) -> None:
        document = prediction_envelope(
            dataset="dataracebench",
            generator={
                "name": "test",
                "version": "1",
                "mode": "deterministic",
            },
            predictions=[
                {
                    "id": "case-1",
                    "identity": {"case_id": "case-1"},
                    "status": "unknown",
                    "evidence": [],
                }
            ],
        )
        validate_prediction_document(document, expected_dataset="dataracebench")
        document["predictions"][0]["targets"] = ["guessed"]
        with self.assertRaises(ValueError):
            validate_prediction_document(document, expected_dataset="dataracebench")

    def test_cgbench_prediction_requires_targets(self) -> None:
        document = prediction_envelope(
            dataset="cgbench",
            generator={
                "name": "test",
                "version": "1",
                "mode": "deterministic",
            },
            predictions=[
                {
                    "id": "case-1",
                    "identity": {"callsite": "a.c:1:1"},
                    "status": "predicted",
                    "evidence": [],
                }
            ],
        )
        with self.assertRaises(ValueError):
            validate_prediction_document(document, expected_dataset="cgbench")


if __name__ == "__main__":
    unittest.main()
