#!/usr/bin/env python3
"""The schema sketches in docs/architecture/contracts.md must parse and build as pydantic v2 models.

Only the python fences of contracts.md are executed; python examples in other docs are prose
and never run. The blocks run in order in one module namespace (later blocks use names from
earlier ones). Each block is compiled against its real line numbers in contracts.md, so every
error is reported as contracts.md:<line>. A top-level name may be defined only once, and the
REQUIRED symbols must exist, so a renamed or silently skipped block cannot shrink what CI checks.
"""
import ast
import re
import sys
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "docs" / "architecture" / "contracts.md"
UNIONS = ("SurfaceContext", "DeliveryTarget")
REQUIRED = (
    "ExternalIdentity", "ResourceGrant",
    "SurfaceContext", "ProductTurn", "ProductAnswer", "RenderedNumber",
    "DomainToolSpec", "DomainToolResult", "RouteDecision", "RuntimeTaskBinding",
    "DeliveryTarget", "RecipientResolution",
    "EdgeHarnessSpec", "EdgeExecutionRequest", "EdgeExecutionHandle",
)
# Fence opener: ``` or ~~~ (3 or more), optional spaces, optional info string (first word = language).
FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<lang>[^`\s]*)(?P<rest>.*)$")
PYTHON_LANGS = {"python", "py", "python3"}


class CheckError(Exception):
    pass


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def at(line: int | None) -> str:
    return f"{rel(CONTRACTS)}:{line}" if line else rel(CONTRACTS)


def python_blocks(path: Path) -> list[tuple[int, str]]:
    """Return (first content line, source) per python fence; malformed or near-miss fences fail loudly."""
    lines = path.read_text(encoding="utf-8").splitlines()  # also strips CRLF line endings
    blocks, i = [], 0
    while i < len(lines):
        m = FENCE_OPEN.match(lines[i])
        if not m or (m["fence"][0] == "`" and "`" in m["rest"]):
            i += 1
            continue
        fence, lang, opened = m["fence"], m["lang"].lower(), i + 1
        if lang not in PYTHON_LANGS and lang.startswith("py"):
            raise CheckError(f"{at(opened)}: unrecognised fence language {m['lang']!r}; use ~~~python")
        if lang in PYTHON_LANGS and m["indent"]:
            raise CheckError(f"{at(opened)}: indented python fence; contract blocks start at column 0")
        close = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            j += 1
        if j == len(lines):
            raise CheckError(f"{at(opened)}: unterminated {fence} fence")
        if lang in PYTHON_LANGS:
            blocks.append((opened + 1, "\n".join(lines[i + 1:j]) + "\n"))
        i = j + 1
    return blocks


def top_level_names(tree: ast.Module) -> list[tuple[str, int]]:
    names = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append((node.name, node.lineno))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names += [((a.asname or a.name).split(".")[0], node.lineno) for a in node.names]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names += [(n.id, node.lineno) for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
    return names


def run(what: str, line: int | None, fn, *args):
    """Call fn; report any exception as contracts.md:<line> (the innermost frame in the doc wins)."""
    try:
        return fn(*args)
    except Exception as e:
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == str(CONTRACTS)]
        raise CheckError(
            f"{at(frames[-1].lineno if frames else line)}: {what}: {type(e).__name__}: {e}"
        ) from None


def check() -> str:
    if not CONTRACTS.is_file():
        raise CheckError(f"{rel(CONTRACTS)} not found")
    blocks = python_blocks(CONTRACTS)
    if not blocks:
        raise CheckError(f"{rel(CONTRACTS)}: no python contract blocks found")

    defined: dict[str, int] = {}
    codes = []
    for start, source in blocks:
        padded = "\n" * (start - 1) + source  # keep the doc's own line numbers
        try:
            tree = ast.parse(padded, str(CONTRACTS))
        except SyntaxError as e:
            raise CheckError(f"{at(e.lineno)}: SyntaxError: {e.msg}") from None
        for name, line in top_level_names(tree):
            if name in defined:
                raise CheckError(f"{at(line)}: {name} is already defined at line {defined[name]}")
            defined[name] = line
        codes.append((start, compile(tree, str(CONTRACTS), "exec")))

    try:
        from pydantic import BaseModel, TypeAdapter
    except ImportError:
        raise CheckError("pydantic v2 is required: pip install -r scripts/requirements-ci.txt") from None

    module = types.ModuleType("aisis_contracts")
    sys.modules[module.__name__] = module  # pydantic resolves annotations through the module
    for start, code in codes:
        run(f"block starting at line {start}", start, exec, code, module.__dict__)

    missing = [name for name in REQUIRED if name not in module.__dict__]
    if missing:
        raise CheckError(
            f"{rel(CONTRACTS)}: required contracts missing: {', '.join(missing)} "
            "(renamed, removed, or in a fence the checker skipped; update REQUIRED if intentional)"
        )

    models = [
        v for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, BaseModel) and v.__module__ == module.__name__
    ]
    for model in models:
        run(f"model {model.__name__}", defined.get(model.__name__), model.model_json_schema)

    def union_schema(union: object) -> dict:
        return TypeAdapter(union).json_schema()

    for name in UNIONS:
        run(f"union {name}", defined.get(name), union_schema, module.__dict__[name])
    return (
        f"ok: {len(blocks)} python blocks in {rel(CONTRACTS)}; {len(models)} models and "
        f"{len(UNIONS)} unions build; {len(REQUIRED)} required contracts present"
    )


def main() -> int:
    try:
        print(check())
    except CheckError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
