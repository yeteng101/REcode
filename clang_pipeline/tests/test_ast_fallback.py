from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from clang_pipeline import stages


class AstFallbackTests(unittest.TestCase):
    def test_c_unit_uses_compiler_json_ast_when_compact_api_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = {"file": str(root / "sample.c"), "arguments": ["clang", str(root / "sample.c")]}
            (root / "request.json").write_text(json.dumps({"run_id": "fallback", "source_root": str(root), "repo_root": str(root)}), encoding="utf-8")
            (root / "compile_commands.json").write_text(json.dumps([unit]), encoding="utf-8")
            with patch.object(stages, "extract_compact_ast", None), patch.object(stages, "run_clang_ast", return_value={"kind": "TranslationUnitDecl"}) as extract:
                result = stages.stage01_index(root)
            extract.assert_called_once_with(unit, root)
            self.assertEqual("succeeded", result["status"])

    def test_missing_optional_libclang_does_not_prevent_stage_import(self):
        code = "import sys; sys.modules['clang_pipeline.libclang_extract'] = None; from clang_pipeline import stages; assert stages.extract_compact_ast is None"
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parents[2], timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
