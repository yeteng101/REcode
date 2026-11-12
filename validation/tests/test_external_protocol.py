from __future__ import annotations

import json
import hashlib
import tempfile
import shutil
from pathlib import Path
import unittest
from unittest.mock import patch

from clang_pipeline.evaluation_protocol import (
    PROTOCOL_ID,
    PROTOCOL_PATH,
    RESULTS_ROOT,
    prediction_envelope,
    validate_prediction_document,
)


ROOT = Path(__file__).resolve().parents[2]


class ExternalProtocolTests(unittest.TestCase):
    def test_current_status_cannot_aggregate_historical_predictions(self):
        from scripts import update_external_status
        document = prediction_envelope(dataset='callgraphbench',
            generator={'name': 'test', 'version': '1', 'mode': 'deterministic'}, predictions=[])
        document['protocol_id'] = 'external-v1'
        with patch.object(update_external_status, 'read', return_value=document):
            with self.assertRaisesRegex(ValueError, 'protocol_id'):
                update_external_status.main()

    def test_historical_prediction_requires_its_archived_schema(self):
        from scripts.score_cgbench import validate_prediction_schema
        document = prediction_envelope(dataset='cgbench',
            generator={'name': 'test', 'version': '1', 'mode': 'deterministic'}, predictions=[])
        document['protocol_id'] = 'external-v1'
        archive = ROOT / 'validation/external/history/external-v1/locked-files'
        validate_prediction_schema(document, archive / 'validation/external/prediction.schema.json')
        with self.assertRaisesRegex(ValueError, 'protocol_id'):
            validate_prediction_schema(document, ROOT / 'validation/external/prediction.schema.json')

    def test_active_freeze_covers_new_analyzer_modules_and_detects_changes(self):
        from scripts import freeze_external_protocol as freezer
        self.assertIn('clang_pipeline/semantic_sync.py', freezer.LOCKED_FILES)
        self.assertIn('clang_pipeline/concurrency_facts.py', freezer.LOCKED_FILES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in freezer.LOCKED_FILES:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            lock_path = root / 'validation/external/protocol-lock.json'
            with patch.object(freezer, 'ROOT', root), patch.object(freezer, 'LOCK_PATH', lock_path), \
                 patch.object(freezer, 'PROTOCOL_PATH', root / 'validation/external/protocol-v2.json'):
                lock = freezer.build_lock()
                lock_path.write_text(json.dumps(lock), encoding='utf-8')
                self.assertEqual([], freezer.verify_lock())
                model = root / 'clang_pipeline/semantic_sync.py'
                model.write_bytes(model.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
                self.assertEqual([], freezer.verify_lock())
                for relative in ('clang_pipeline/race_detector.py', 'clang_pipeline/semantic_sync.py',
                                 'clang_pipeline/concurrency_facts.py'):
                    with self.subTest(path=relative):
                        model = root / relative
                        original = model.read_bytes()
                        model.write_bytes(original + b'\n# changed\n')
                        self.assertIn('hash mismatch: ' + relative, freezer.verify_lock())
                        model.write_bytes(original)

    def test_current_revision_is_separate_from_historical_scores(self):
        self.assertEqual('external-v2', PROTOCOL_ID)
        self.assertEqual(ROOT / 'validation/external/results/external-v2', RESULTS_ROOT)
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding='utf-8'))
        self.assertEqual('external-v1', protocol['predecessor']['protocol_id'])
        archive = ROOT / 'validation/external/history/external-v1'
        attribution = json.loads((archive / 'attribution.json').read_text(encoding='utf-8'))
        baseline = json.loads((archive / 'baseline-original.json').read_text(encoding='utf-8'))
        self.assertEqual(attribution['lock_digest'], baseline['protocol_lock']['lock_digest'])
        self.assertEqual(attribution['lock_digest'], protocol['predecessor']['lock_digest'])
        for entry in attribution['files']:
            self.assertEqual(entry['sha256'], hashlib.sha256((archive / entry['path']).read_bytes()).hexdigest())

    def test_active_prediction_rejects_historical_protocol(self):
        document = prediction_envelope(dataset='dataracebench',
            generator={'name': 'test', 'version': '1', 'mode': 'deterministic'}, predictions=[])
        self.assertEqual('external-v2', document['protocol_id'])
        document['protocol_id'] = 'external-v1'
        with self.assertRaisesRegex(ValueError, 'protocol_id'):
            validate_prediction_document(document)
        validate_prediction_document(document, expected_protocol_id='external-v1')

    def test_selection_manifests_have_unique_units(self) -> None:
        directory = ROOT / "validation/external/selections"
        protocol = json.loads(
            PROTOCOL_PATH.read_text(encoding="utf-8")
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
