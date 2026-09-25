#!/usr/bin/env python3
"""The Python schema sketches embedded in docs must parse (per file and concatenated)."""
import ast
import re
import sys
from pathlib import Path

FENCE = re.compile(r"^(?:~~~|```)python\n(.*?)^(?:~~~|```)\s*$", re.S | re.M)


def main() -> int:
    files = sorted(Path("docs").rglob("*.md"))
    blocks_all, count = [], 0
    for f in files:
        blocks = FENCE.findall(f.read_text(encoding="utf-8"))
        if not blocks:
            continue
        try:
            ast.parse("\n".join(blocks))
        except SyntaxError as e:
            print(f"{f}: {e}")
            return 1
        blocks_all += blocks
        count += len(blocks)
    ast.parse("\n".join(blocks_all))
    if count == 0:
        print("no python contract blocks found")
        return 1
    print(f"ok: {count} python contract blocks parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
