from __future__ import annotations

import unittest

from scripts.check_external_regressions import check_v1, check_v2


class ExternalRegressionTests(unittest.TestCase):
    def test_locked_external_v1_has_no_regression(self) -> None:
        self.assertEqual([], check_v1())

    def test_locked_external_v2_has_no_regression(self) -> None:
        self.assertEqual([], check_v2())


if __name__ == "__main__":
    unittest.main()
