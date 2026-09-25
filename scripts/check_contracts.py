#!/usr/bin/env python3
"""The Python schema sketches embedded in docs must parse and build as pydantic v2 models."""
import ast
import re
import sys
import types
from pathlib import Path

FENCE = re.compile(r"^(?:~~~|```)(?:python|py)\n(.*?)^(?:~~~|```)\s*$", re.S | re.M)
UNIONS = ("SurfaceContext", "DeliveryTarget")


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
    if count == 0:
        print("no python contract blocks found")
        return 1
    source = "\n".join(blocks_all)
    ast.parse(source)

    try:
        from pydantic import BaseModel, TypeAdapter
    except ImportError:
        print("pydantic v2 is required: pip install -r scripts/requirements-ci.txt")
        return 1

    # Execute the concatenated blocks as one module, then build every model and union.
    module = types.ModuleType("aisis_contracts")
    sys.modules[module.__name__] = module
    exec(compile(source, "<docs contracts>", "exec"), module.__dict__)
    models = [
        v for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, BaseModel) and v is not BaseModel
    ]
    for model in models:
        model.model_json_schema()
    for name in UNIONS:
        TypeAdapter(getattr(module, name)).json_schema()
    print(f"ok: {count} python contract blocks parse; {len(models)} models and {len(UNIONS)} unions build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
