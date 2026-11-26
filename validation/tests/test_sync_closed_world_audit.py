from __future__ import annotations

import unittest

from scripts.audit_sync_closed_world import (
    build_gate,
    classify_prediction,
    evidence_anchor,
    evaluate_universe,
)


def item(
    *,
    source_line: int,
    target_line: int,
    order: str = "happens_before",
    condition: str = "same-function program order worker:10->20",
    source_function: str = "worker",
    target_function: str = "worker",
    source_snippet: str = "counter++;",
    target_snippet: str = "counter--;",
) -> dict:
    return {
        "relation_type": "happens_before",
        "order": order,
        "condition": condition,
        "source": {
            "function": source_function,
            "file": "src/worker.c",
            "line": source_line,
            "snippet": source_snippet,
        },
        "target": {
            "function": target_function,
            "file": "src/worker.c",
            "line": target_line,
            "snippet": target_snippet,
        },
    }


class SyncClosedWorldAuditTests(unittest.TestCase):
    def test_evidence_anchor_supports_nested_location(self) -> None:
        self.assertEqual(
            ("src/worker.c", 12, "counter++;"),
            evidence_anchor(
                {
                    "location": {
                        "file": "src/worker.c",
                        "line": 12,
                        "snippet": "counter++;",
                    }
                }
            ),
        )

    def test_ordered_same_function_relation_is_accepted(self) -> None:
        decision, reason = classify_prediction(
            item(source_line=10, target_line=20),
            evidence_ok=True,
            evidence_reason="",
            gold_state="none",
        )
        self.assertEqual("accepted", decision)
        self.assertIn("source order", reason)

    def test_generic_notification_pair_is_rejected(self) -> None:
        decision, reason = classify_prediction(
            item(
                source_line=10,
                target_line=20,
                order="unknown",
                source_function="post",
                target_function="worker",
                condition=(
                    "notification does not identify a specific queued work item "
                    "or callback instance"
                ),
            ),
            evidence_ok=True,
            evidence_reason="",
            gold_state="none",
        )
        self.assertEqual("rejected", decision)
        self.assertIn("no shared resource", reason)

    def test_queue_notification_callback_requires_support_evidence(self) -> None:
        candidate = item(
            source_line=10,
            target_line=20,
            order="unknown",
            source_function="post",
            target_function="worker",
            target_snippet="w->work(w);",
            condition=(
                "queue notification boundary wq: post -> worker; "
                "specific item instance remains unknown"
            ),
        )
        candidate["evidence_ids"] = ["source", "queue", "consumer"]
        decision, reason = classify_prediction(
            candidate,
            evidence_ok=True,
            evidence_reason="",
            gold_state="none",
        )
        self.assertEqual("accepted", decision)
        self.assertIn("queue publication", reason)

    def test_audit_gate_rejects_regressions(self) -> None:
        summary = {
            "decisions": {"accepted": 90, "rejected": 2},
            "needs_review": 1,
            "gold_tp": 32,
            "gold_fp": 0,
            "gold_fn": 1,
            "reasons": {
                "generic notification/callback pair has no shared resource witness": 2
            },
        }
        gate = build_gate(
            summary,
            max_rejected=0,
            max_needs_review=0,
            min_accepted=100,
            min_gold_precision=1.0,
            min_gold_recall=1.0,
            min_universe_recall=None,
            require_zero_generic_notification=True,
        )
        self.assertEqual("failed", gate["status"])
        failed = {
            check["name"]
            for check in gate["checks"]
            if check["status"] == "failed"
        }
        self.assertEqual(
            {
                "rejected",
                "needs_review",
                "accepted",
                "gold_recall",
                "generic_notification_pairs",
            },
            failed,
        )

    def test_universe_uses_same_function_path_coverage(self) -> None:
        candidates = [
            {
                "source": {
                    "function": "worker",
                    "file": "src/worker.c",
                    "line": 10,
                },
                "target": {
                    "function": "worker",
                    "file": "src/worker.c",
                    "line": 30,
                },
                "order": "happens_before",
            }
        ]
        records = [
            {
                "decision": "accepted",
                "order": "happens_before",
                "source_file": "src/worker.c",
                "source_function": "worker",
                "source_line": 10,
                "target_file": "src/worker.c",
                "target_function": "worker",
                "target_line": 20,
            },
            {
                "decision": "accepted",
                "order": "happens_before",
                "source_file": "src/worker.c",
                "source_function": "worker",
                "source_line": 20,
                "target_file": "src/worker.c",
                "target_function": "worker",
                "target_line": 30,
            },
        ]
        result = evaluate_universe(candidates, records)
        self.assertEqual(1, result["covered"])
        self.assertEqual(0, result["missing"])


if __name__ == "__main__":
    unittest.main()
