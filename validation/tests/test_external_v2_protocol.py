from __future__ import annotations

import json
from pathlib import Path
import unittest

from scripts.freeze_external_v2_protocol import verify_lock


ROOT = Path(__file__).resolve().parents[2]


class ExternalV2ProtocolTests(unittest.TestCase):
    def test_lock_is_valid(self) -> None:
        self.assertEqual([], verify_lock())

    def test_callgraphbench_v2_is_complete(self) -> None:
        summary = json.loads(
            (
                ROOT
                / "validation/external-v2/results/callgraphbench/summary.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"tp": 31, "fp": 0, "fn": 0},
            {
                key: summary["totals"][key]
                for key in ("tp", "fp", "fn")
            },
        )
        self.assertEqual(1.0, summary["totals"]["precision"])
        self.assertEqual(1.0, summary["totals"]["recall"])

    def test_v2_selection_matches_v1_universe(self) -> None:
        selection = json.loads(
            (
                ROOT
                / "validation/external-v2/selections/callgraphbench-v2.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("callgraphbench-v2", selection["selection_id"])
        self.assertEqual(6, selection["counts"]["cases"])
        self.assertEqual(31, selection["counts"]["units"])

    def test_svcomp_memsafety_v2_reduces_false_positives(self) -> None:
        report = json.loads(
            (
                ROOT
                / "validation/external-v2/results/svcomp/memsafety/report.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(10, report["tp"])
        self.assertEqual(7, report["fp"])
        self.assertEqual(0, report["fn"])
        self.assertEqual(3, report["tn"])

    def test_juliet_v2_preserves_function_level_result(self) -> None:
        report = json.loads(
            (
                ROOT
                / "validation/external-v2/results/juliet/report.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(5, report["tp"])
        self.assertEqual(0, report["fp"])
        self.assertEqual(0, report["fn"])
        self.assertEqual(23, report["tn"])


if __name__ == "__main__":
    unittest.main()
