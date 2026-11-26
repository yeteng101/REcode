from __future__ import annotations

import unittest

from clang_pipeline.source_context import order_decision, regions_for_file


class SourceContextTests(unittest.TestCase):
    def test_mutually_exclusive_branches_are_unknown(self) -> None:
        lines = [
            "void f(void) {",
            "  if (ready) {",
            "    counter++;",
            "  } else {",
            "    counter--;",
            "  }",
            "}",
        ]
        regions = regions_for_file(lines, [(1, len(lines))])
        order, reason = order_decision(regions, 3, 5)
        self.assertEqual("unknown", order)
        self.assertIn("mutually exclusive", reason)

    def test_loop_back_edge_is_unknown(self) -> None:
        lines = [
            "void f(void) {",
            "  while (ready) {",
            "    first();",
            "    second();",
            "  }",
            "}",
        ]
        regions = regions_for_file(lines, [(1, len(lines))])
        order, reason = order_decision(regions, 4, 3)
        self.assertEqual("unknown", order)
        self.assertIn("loop back-edge", reason)

    def test_straight_line_order_is_happens_before(self) -> None:
        lines = [
            "void f(void) {",
            "  first();",
            "  second();",
            "}",
        ]
        regions = regions_for_file(lines, [(1, len(lines))])
        order, reason = order_decision(regions, 2, 3)
        self.assertEqual("happens_before", order)
        self.assertIn("straight-line", reason)


if __name__ == "__main__":
    unittest.main()
