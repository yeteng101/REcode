from __future__ import annotations

import argparse
from pathlib import Path

from .render import write_visualization


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render an analysis JSON workspace as a standalone HTML dashboard."
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    graph = None
    if args.graph:
        import json

        graph = json.loads(args.graph.read_text(encoding="utf-8"))

    output = write_visualization(
        args.workspace,
        graph,
        output=args.output,
    )
    print(output)


if __name__ == "__main__":
    main()
