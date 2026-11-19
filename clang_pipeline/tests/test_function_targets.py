from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clang_pipeline.function_targets import FunctionTargetResolver


class FunctionTargetResolverTests(unittest.TestCase):
    def test_resolves_c_struct_function_pointer_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "device.h").write_text(
                """
struct Driver {
    const char *name;
    int (*open)(const char *);
    int (*read)(int);
};
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (root / "device.c").write_text(
                """
#include "device.h"
static struct Driver tcp_driver = {"tcp", tcp_open, tcp_read};
static struct Driver udp_driver = {"udp", udp_open, udp_read};
struct Driver *pick_driver(const char *scheme) {
    if (scheme[0] == 'u') return &udp_driver;
    return &tcp_driver;
}
int process_device(const char *scheme) {
    struct Driver *driver = pick_driver(scheme);
    return driver->open(scheme) + driver->read(1);
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (root / "drivers.c").write_text(
                """
int tcp_open(const char *x) { return 1; }
int udp_open(const char *x) { return 2; }
int tcp_read(int x) { return x; }
int udp_read(int x) { return x; }
""".strip()
                + "\n",
                encoding="utf-8",
            )
            graph = {
                "nodes": [
                    {"id": "fn:process_device", "kind": "function", "name": "process_device", "file": "device.c", "line": 8},
                    {"id": "fn:pick_driver", "kind": "function", "name": "pick_driver", "file": "device.c", "line": 5},
                ],
                "edges": [
                    {
                        "id": "edge-pick",
                        "source": "fn:process_device",
                        "target": "fn:pick_driver",
                        "kind": "direct_call",
                        "call_site": {"file": "device.c", "line": 9, "snippet": "pick_driver(scheme)"},
                        "evidence_ids": ["ev-pick"],
                    }
                ],
                "evidence": [{"id": "ev-pick", "file": "device.c", "line": 9}],
            }
            enriched = FunctionTargetResolver(graph, root).enrich()
            nodes = {node["id"]: node["name"] for node in enriched["nodes"]}
            edges = {
                (nodes[edge["source"]], nodes[edge["target"]])
                for edge in enriched["edges"]
                if edge["kind"] == "indirect_call"
            }
            self.assertEqual(
                {
                    ("process_device", "tcp_open"),
                    ("process_device", "udp_open"),
                    ("process_device", "tcp_read"),
                    ("process_device", "udp_read"),
                },
                edges,
            )

    def test_resolves_cpp_virtual_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rule.h").write_text(
                """
class Rule {
public:
    virtual bool apply(const char *value) const = 0;
};
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (root / "workflow.cpp").write_text(
                """
#include "rule.h"
bool execute_rule(const Rule& rule, const char *value) {
    return rule.apply(value);
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (root / "strategies.cpp").write_text(
                """
#include "rule.h"
class LengthRule : public Rule {
public:
    bool apply(const char *value) const override { return value[0]; }
};
class PrefixRule : public Rule {
public:
    bool apply(const char *value) const override { return value[1]; }
};
""".strip()
                + "\n",
                encoding="utf-8",
            )
            graph = {
                "nodes": [
                    {"id": "fn:execute_rule", "kind": "function", "name": "execute_rule", "file": "workflow.cpp", "line": 2},
                ],
                "edges": [],
                "evidence": [],
            }
            enriched = FunctionTargetResolver(graph, root).enrich()
            nodes = {node["id"]: node["name"] for node in enriched["nodes"]}
            targets = {
                nodes[edge["target"]]
                for edge in enriched["edges"]
                if nodes[edge["source"]] == "execute_rule"
            }
            self.assertIn("LengthRule::apply", targets)
            self.assertIn("PrefixRule::apply", targets)


if __name__ == "__main__":
    unittest.main()
