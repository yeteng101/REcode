from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clang_pipeline.race_detector import analyze_race_file, analyze_race_source


class RaceDetectorTests(unittest.TestCase):
    def _write(self, root: Path, name: str, source: str) -> Path:
        path = root / name
        path.write_text(source, encoding="utf-8")
        return path

    def test_openmp_shifted_index_is_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir),
                "shift.c",
                """
                int a[16];
                int main(void) {
                #pragma omp parallel for
                  for (int i = 0; i < 15; i++)
                    a[i] = a[i + 1] + 1;
                  return 0;
                }
                """,
            )
            result = analyze_race_file(path)
            self.assertEqual("race", result["label"])
            self.assertTrue(result["items"])
            self.assertIn("i + 1", result["items"][0]["reason"])

    def test_openmp_disjoint_index_is_no_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir),
                "partitioned.c",
                """
                int a[16];
                int main(void) {
                #pragma omp parallel for
                  for (int i = 0; i < 16; i++)
                    a[i] = a[i] + 1;
                  return 0;
                }
                """,
            )
            result = analyze_race_file(path)
            self.assertEqual("no_race", result["label"])

    def test_indirect_index_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir),
                "indirect.c",
                """
                int index_set[16];
                int data[32];
                int main(void) {
                #pragma omp parallel for
                  for (int i = 0; i < 16; i++) {
                    int index = index_set[i];
                    data[index] = data[index] + 1;
                  }
                  return 0;
                }
                """,
            )
            result = analyze_race_file(path)
            self.assertEqual("unknown", result["label"])
            self.assertTrue(result["items"])

    def test_pthread_atomic_and_lifetime_handling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir),
                "threads.c",
                """
                int shared;
                void *worker1(void *arg) { shared = 1; return 0; }
                void *worker2(void *arg) { return 0; }
                int main(void) {
                  pthread_t a, b;
                  pthread_create(&a, 0, worker1, 0);
                  pthread_join(a, 0);
                  pthread_create(&b, 0, worker2, 0);
                  pthread_join(b, 0);
                  return 0;
                }
                """,
            )
            result = analyze_race_file(path)
            self.assertEqual("no_race", result["label"])
            self.assertTrue(result["items"])

    def test_prediction_has_evidence_when_a_candidate_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir),
                "race.c",
                """
                int x;
                void *one(void *arg) { x++; return 0; }
                void *two(void *arg) { x++; return 0; }
                int main(void) {
                  pthread_t a, b;
                  pthread_create(&a, 0, one, 0);
                  pthread_create(&b, 0, two, 0);
                  pthread_join(a, 0);
                  pthread_join(b, 0);
                  return 0;
                }
                """,
            )
            result = analyze_race_source(path.parent, source_files=[path])
            self.assertEqual("race", result["label"])
            self.assertTrue(result["evidence"])


if __name__ == "__main__":
    unittest.main()
