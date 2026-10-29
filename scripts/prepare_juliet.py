#!/usr/bin/env python3
"""Build a deterministic Juliet C/C++ resource-safety case index."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / ".benchmark-cache/juliet-c-cpp-1.3"
OUTPUT = ROOT / "validation/external/results/juliet"
CWES = (
    "CWE401_Memory_Leak",
    "CWE404_Improper_Resource_Shutdown",
    "CWE415_Double_Free",
    "CWE416_Use_After_Free",
)
FUNCTION_PATTERN = re.compile(
    r"^\s*(?:[A-Za-z_][\w\s*&:<>]*?)\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?"
)


def functions(source: Path) -> list[str]:
    result = []
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        match = FUNCTION_PATTERN.match(line)
        if match:
            result.append(match.group(1))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-per-cwe", type=int, default=5)
    parser.add_argument(
        "--cwe",
        action="append",
        choices=CWES,
        help="CWE to include; defaults to CWE401 memory leak",
    )
    args = parser.parse_args()
    selected_cwes = tuple(args.cwe or ("CWE401_Memory_Leak",))
    if not DATASET.is_dir():
        parser.error(
            "Juliet is not fetched; run scripts/fetch_external_benchmarks.py "
            "--benchmark juliet-c-cpp-1.3"
        )
    cases = []
    for language in ("C", "Cpp"):
        for cwe in selected_cwes:
            root = DATASET / language / "testcases" / cwe
            if not root.is_dir():
                continue
            files = sorted(
                path
                for path in root.rglob("*")
                if path.suffix in {".c", ".cpp"}
            )
            preferred = [
                path
                for path in files
                if re.search(r"_(01|02|03|04|05)\.(c|cpp)$", path.name)
            ]
            selected = (preferred or files)[: args.limit_per_cwe]
            for source in selected:
                cases.append(
                    {
                        "id": f"juliet_{language}_{cwe}_{source.stem}",
                        "language": "cpp" if source.suffix == ".cpp" else "c",
                        "cwe": cwe.split("_", 1)[0],
                        "source_file": source.relative_to(DATASET).as_posix(),
                        "labels": {
                            "defect_prefixed_functions": [
                                name for name in functions(source) if "_bad" in name
                            ],
                            "clean_marked_functions": [
                                name
                                for name in functions(source)
                                if "good" in name.lower()
                            ],
                        },
                    }
                )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": "1.0",
        "dataset": "NIST Juliet C/C++ 1.3",
        "official_sha256": "ada9d7e1c323d283446df3f55bdee0d00bda1fed786785fe98764d58688f38eb",
        "license": "CC0-1.0 / public domain",
        "selection": {
            "cwes": list(selected_cwes),
            "limit_per_cwe": args.limit_per_cwe,
        },
        "cases": cases,
    }
    (OUTPUT / "cases.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"cases": len(cases), "path": str(OUTPUT / "cases.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
