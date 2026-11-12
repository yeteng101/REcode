"""Incomplete native facts must survive preparation without gaining proof status."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "v2-concurrency/prototype"))
from analyze_sync_flow import build_candidates


class RecodeFactGapTests(unittest.TestCase):
    def setUp(self):
        self.response = json.loads((ROOT / "v2-concurrency/fixtures/PARTIAL-001-unknown-context/sync-flow.json").read_text(encoding="utf-8"))

    def test_explicit_nonformal_producer_cannot_become_formal_by_name(self):
        self.response["warnings"] = []
        self.response["analysis_context"]["analyzer"] = {"name": "REcode-native-export-adapter", "formal_evidence": False}
        result = build_candidates(self.response)
        self.assertEqual("first_group", result["provenance"]["source_kind"])
        self.assertFalse(result["provenance"]["formal_evidence"])
        self.assertEqual(0.6, result["provenance"]["review_confidence_cap"])

    def test_missing_declaration_stays_a_gap_without_fabricated_location(self):
        self.response["findings"][0]["shared_objects"][0]["declaration"] = None
        result = build_candidates(self.response)
        self.assertTrue(result["candidates"])
        for candidate in result["candidates"]:
            self.assertIsNone(candidate["shared_object"]["declaration"])
            self.assertEqual("needs_more_context", candidate["candidate_status"])
            self.assertIn("object_declaration_unavailable", candidate["coverage"]["unresolved"])
