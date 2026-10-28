from __future__ import annotations

import json
from pathlib import Path
import unittest

from scripts.run_callgraphbench import map_graph_edges
from scripts.score_cgbench import normalize as normalize_cgbench_predictions


ROOT = Path(__file__).resolve().parents[2]


class ExternalBenchmarkTests(unittest.TestCase):
    def test_registry_pins_official_sources(self) -> None:
        registry = json.loads(
            (ROOT / "validation/external/registry.json").read_text(encoding="utf-8")
        )
        expected = {
            "callgraphbench",
            "cgbench",
            "juliet-c-cpp-1.3",
            "svcomp26",
            "dataracebench",
        }
        self.assertEqual(set(registry["benchmarks"]), expected)
        self.assertEqual(
            registry["benchmarks"]["callgraphbench"]["source"]["commit"],
            "fe003b9e9de38d06aa67cabb8643a55dbac7dcbf",
        )
        self.assertEqual(
            registry["benchmarks"]["juliet-c-cpp-1.3"]["source"]["sha256"],
            "ada9d7e1c323d283446df3f55bdee0d00bda1fed786785fe98764d58688f38eb",
        )
        self.assertEqual(
            registry["benchmarks"]["svcomp26"]["source"]["commit"],
            "7efe28dd29576b46927b7a34e8f742bd90966a75",
        )

    def test_callgraphbench_maps_file_line_and_method_symbols(self) -> None:
        manifest = {
            "symbols": [
                {"id": "c:case:main", "file": "src/main.c", "line": 3},
                {"id": "c:case:helper", "file": "src/main.c", "line": 8},
                {"id": "cpp:case:Rule.apply", "file": "src/rule.cpp", "line": 12},
            ]
        }
        graph = {
            "nodes": [
                {"id": "fn:main", "name": "main", "file": "src/main.c", "line": 3},
                {"id": "fn:helper", "name": "helper", "file": "src/main.c", "line": 8},
                {"id": "fn:apply", "name": "Rule::apply", "file": "src/rule.cpp", "line": 0},
            ],
            "edges": [
                {
                    "source": "fn:main",
                    "target": "fn:helper",
                    "kind": "direct_call",
                },
                {
                    "source": "fn:main",
                    "target": "fn:apply",
                    "kind": "indirect_call",
                },
            ],
        }
        edges = map_graph_edges(graph, manifest, ROOT)
        self.assertEqual(
            {(item["caller"], item["callee"], item["kind"]) for item in edges},
            {
                ("c:case:main", "c:case:helper", "direct"),
                ("c:case:main", "cpp:case:Rule.apply", "indirect"),
            },
        )

    def test_cgbench_prediction_normalization(self) -> None:
        predictions = {
            "predictions": [
                {
                    "callsite": "src/main.c:1",
                    "targets": ["alpha", "beta"],
                }
            ]
        }
        self.assertEqual(
            normalize_cgbench_predictions(predictions),
            {"src/main.c:1": {"alpha", "beta"}},
        )


if __name__ == "__main__":
    unittest.main()
