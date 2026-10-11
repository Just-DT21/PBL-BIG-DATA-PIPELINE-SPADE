#!/usr/bin/env python
"""Generate one sample of every supported format.

    python scripts/make_samples.py [output-dir] [--rows 40000]

The files are real documents in each format (not renamed text), so they can be
dragged straight into the upload screen.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spade import samples  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SPADE sample files")
    parser.add_argument("directory", nargs="?", default=str(ROOT / "samples"))
    parser.add_argument("--rows", type=int, default=40_000, help="rows in the tabular samples")
    parser.add_argument("--include-edge-cases", action="store_true", help="also write deliberately broken files")
    args = parser.parse_args()

    target = Path(args.directory).resolve()
    built = samples.build_all(target, rows=args.rows)
    print(f"Samples written to {target}")
    for name, path in sorted(built.items()):
        print(f"  {name:6} {path.name:28} {path.stat().st_size:>10,} bytes")

    if args.include_edge_cases:
        broken = target / "edge_cases"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "truncated.pdf").write_bytes(b"%PDF-1.7\n1 0 obj\n<< invalid >>\n")
        (broken / "empty.csv").write_bytes(b"")
        (broken / "renamed_picture.txt").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        (broken / "not_really_json.json").write_text('{"unclosed": [1, 2,', encoding="utf-8")
        (broken / "already_clean.csv").write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8", newline="")
        (broken / "ragged.csv").write_text("a,b,c\n1,2\n3,4,5,6\n\n7,8,9\n", encoding="utf-8", newline="")
        print(f"  edge cases written to {broken}")
        for path in sorted(broken.iterdir()):
            print(f"    {path.name:28} {path.stat().st_size:>10,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
