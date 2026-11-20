from __future__ import annotations

import unittest

from scripts.audit_sync_closed_world import classify_prediction, evidence_anchor


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


if __name__ == "__main__":
    unittest.main()
