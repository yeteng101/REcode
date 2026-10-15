from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analysis_viz import write_visualization


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "fixtures" / "example"


class RenderTests(unittest.TestCase):
    def test_generic_workspace_renders_self_contained_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "dashboard.html"
            written = write_visualization(EXAMPLE, output=output)
            text = written.read_text(encoding="utf-8")

            self.assertEqual(output.resolve(), written.resolve())
            self.assertIn("alpha", text)
            self.assertIn("beta", text)
            self.assertIn("read_data", text)
            self.assertIn("Module Graph", text)
            self.assertIn("Calls", text)
            self.assertNotIn("uv_run", text)
            self.assertNotIn("pthread_mutex_lock", text)

            payload_start = text.index('<script id="viz-data"')
            payload_end = text.index("</script>", payload_start)
            payload = json.loads(
                text[text.index(">", payload_start) + 1 : payload_end]
            )
            self.assertEqual(payload["metrics"]["nodes"], 6)
            self.assertEqual(payload["metrics"]["edges"], 5)
            self.assertEqual(payload["metrics"]["resource_items"], 1)
            self.assertEqual(payload["metrics"]["sync_items"], 1)

    def test_renderer_does_not_require_external_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "offline.html"
            text = write_visualization(EXAMPLE, output=output).read_text(encoding="utf-8")
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)
            self.assertNotIn("cdn.", text)


if __name__ == "__main__":
    unittest.main()
