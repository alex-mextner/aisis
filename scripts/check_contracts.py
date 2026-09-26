#!/usr/bin/env python3
"""The schema sketches in docs/architecture/contracts.md must parse and build as pydantic v2 models.

Only the python fences of contracts.md are executed; python examples in other docs are prose
and never run. The blocks run in order in one module namespace (later blocks use names from
earlier ones). Each block is compiled against its real line numbers in contracts.md, so every
error is reported as contracts.md:<line>. A top-level name may be defined only once, and the
REQUIRED symbols must exist, so a renamed or silently skipped block cannot shrink what CI checks.
Tests: scripts/test_check_contracts.py.
"""
import ast
import re
import sys
import traceback
import types
import typing
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "docs" / "architecture" / "contracts.md"
# Required contracts: discriminated unions that must build, and names that must be pydantic models.
UNIONS = ("SurfaceContext", "DeliveryTarget", "DomainToolResult")
REQUIRED_MODELS = (
    "ExternalIdentity", "ResourceGrant",
    "ProductTurn", "ProductAnswer", "RenderedNumber",
    "DomainToolSpec", "RouteDecision", "RuntimeTaskBinding",
    "RecipientResolution",
    "EdgeHarnessSpec", "EdgeExecutionRequest", "EdgeExecutionHandle",
)
REQUIRED = UNIONS + REQUIRED_MODELS
# Fence opener: ``` or ~~~ (3 or more), optional spaces, optional info string (first word = language).
FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<lang>[^`\s]*)(?P<rest>.*)$")
# A python-looking fence behind any mix of blockquote and list markers, which the checker would not see.
PREFIXED_PY_FENCE = re.compile(
    r"^[ \t]*(?:>[ \t]*|[-*+][ \t]+|\d+[.)][ \t]+)+(?:`{3,}|~{3,})[ \t]*py", re.I
)
PYTHON_LANGS = {"python", "py", "python3"}
TYPE_ALIAS = getattr(ast, "TypeAlias", ())  # `type X = ...` (Python 3.12+)


class CheckError(Exception):
    pass


class Block(NamedTuple):
    start: int  # 1-based line of the block's first content line in the doc
    source: str


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def at(path: Path, line: int | None) -> str:
    return f"{rel(path)}:{line}" if line is not None else rel(path)


def fence_open(line: str) -> re.Match | None:
    m = FENCE_OPEN.match(line)
    # CommonMark: a backtick fence's info string cannot contain a backtick.
    if m and m["fence"][0] == "`" and "`" in m["rest"]:
        return None
    return m


def python_blocks(path: Path) -> list[Block]:
    """Every python fence in the doc; malformed, near-miss or swallowed fences fail loudly."""
    # read_text turns CRLF/CR into "\n"; split only on "\n" (not splitlines(), which also splits on
    # form feeds and Unicode separators) so line numbers match what editors and GitHub show.
    lines = path.read_text(encoding="utf-8").split("\n")
    blocks, i = [], 0
    while i < len(lines):
        m = fence_open(lines[i])
        if not m:
            if PREFIXED_PY_FENCE.match(lines[i]):
                raise CheckError(
                    f"{at(path, i + 1)}: python fence inside a blockquote or list is not checked; "
                    "contract blocks start at column 0"
                )
            i += 1
            continue
        fence, lang, opened = m["fence"], m["lang"].lower(), i + 1
        is_python = lang in PYTHON_LANGS
        if not is_python and lang.startswith("py"):
            raise CheckError(
                f"{at(path, opened)}: unrecognised fence language {m['lang']!r}; "
                f"use one of {', '.join(sorted(PYTHON_LANGS))}"
            )
        if is_python and m["indent"]:
            raise CheckError(f"{at(path, opened)}: indented python fence; contract blocks start at column 0")
        # CommonMark: a closing fence is indented by at most 3 spaces.
        close = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            inner = fence_open(lines[j])
            # Deliberately broad (any py* language): better a loud false alarm than a hidden block.
            if not is_python and (
                (inner and inner["lang"].lower().startswith("py")) or PREFIXED_PY_FENCE.match(lines[j])
            ):
                raise CheckError(
                    f"{at(path, j + 1)}: python fence inside the {fence} fence opened at line {opened}; "
                    "an unbalanced fence would hide this block from the check"
                )
            j += 1
        if j == len(lines):
            raise CheckError(f"{at(path, opened)}: unterminated {fence} fence")
        if is_python:
            blocks.append(Block(opened + 1, "\n".join(lines[i + 1:j]) + "\n"))
        i = j + 1
    return blocks


def top_level_names(tree: ast.Module) -> list[tuple[str, int]]:
    names = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append((node.name, node.lineno))
        elif isinstance(node, TYPE_ALIAS):
            names.append((node.name.id, node.lineno))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names += [((a.asname or a.name).split(".")[0], node.lineno) for a in node.names]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names += [(n.id, node.lineno) for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
    return names


def run(path: Path, what: str, line: int | None, fn, *args):
    """Call fn; report any exception as <doc>:<line> (the innermost frame inside the doc wins)."""
    try:
        return fn(*args)
    except Exception as e:
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == str(path)]
        raise CheckError(
            f"{at(path, frames[-1].lineno if frames else line)}: {what}: {type(e).__name__}: {e}"
        ) from None


def check(path: Path = CONTRACTS) -> str:
    if not path.is_file():
        raise CheckError(f"{rel(path)} not found")
    blocks = python_blocks(path)
    if not blocks:
        raise CheckError(f"{rel(path)}: no python contract blocks found")

    defined: dict[str, int] = {}
    codes = []
    for block in blocks:
        padded = "\n" * (block.start - 1) + block.source  # keep the doc's own line numbers
        try:
            tree = ast.parse(padded, str(path))
        except SyntaxError as e:
            raise CheckError(f"{at(path, e.lineno)}: SyntaxError: {e.msg}") from None
        for name, line in top_level_names(tree):
            if name in defined:
                raise CheckError(f"{at(path, line)}: {name} is already defined at line {defined[name]}")
            defined[name] = line
        codes.append((block.start, compile(tree, str(path), "exec")))

    module = types.ModuleType("aisis_contracts")
    sys.modules[module.__name__] = module  # pydantic resolves annotations through the module
    try:
        n_models = build(path, module, codes, defined)
    finally:
        sys.modules.pop(module.__name__, None)
    return (
        f"ok: {len(blocks)} python blocks in {rel(path)}; {n_models} models and "
        f"{len(UNIONS)} unions build; {len(REQUIRED)} required contracts present"
    )


def is_discriminated_union(value: object) -> bool:
    """Annotated[A | B | ..., Field(discriminator=...)]"""
    if typing.get_origin(value) is not typing.Annotated:
        return False
    inner, *metadata = typing.get_args(value)
    return typing.get_origin(inner) in (typing.Union, types.UnionType) and any(
        getattr(m, "discriminator", None) for m in metadata
    )


def build(
    path: Path, module: types.ModuleType, codes: list[tuple[int, types.CodeType]], defined: dict[str, int]
) -> int:
    """Run the blocks in module, check the required contracts, build every schema; return the model count."""
    try:
        from pydantic import BaseModel, TypeAdapter
    except ImportError:
        raise CheckError("pydantic v2 is required: pip install -r scripts/requirements-ci.txt") from None

    def exec_block(code) -> None:
        exec(code, module.__dict__)

    def union_schema(union: object) -> dict:
        return TypeAdapter(union).json_schema()

    for start, code in codes:
        run(path, f"block starting at line {start}", start, exec_block, code)

    missing = [name for name in REQUIRED if name not in module.__dict__]
    if missing:
        raise CheckError(
            f"{rel(path)}: required contracts missing: {', '.join(missing)} "
            "(renamed, removed, or in a fence the checker skipped; update REQUIRED if intentional)"
        )
    not_models = [
        name for name in REQUIRED_MODELS
        if not (isinstance(module.__dict__[name], type) and issubclass(module.__dict__[name], BaseModel))
    ]
    if not_models:
        raise CheckError(
            f"{at(path, defined.get(not_models[0]))}: required contracts are not pydantic models: "
            f"{', '.join(not_models)}"
        )
    not_unions = [name for name in UNIONS if not is_discriminated_union(module.__dict__[name])]
    if not_unions:
        raise CheckError(
            f"{at(path, defined.get(not_unions[0]))}: required contracts are not discriminated unions "
            f"(Annotated[A | B, Field(discriminator=...)]): {', '.join(not_unions)}"
        )

    models = [
        v for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, BaseModel) and v.__module__ == module.__name__
    ]
    for model in models:
        run(path, f"model {model.__name__}", defined.get(model.__name__), model.model_json_schema)
    for name in UNIONS:
        run(path, f"union {name}", defined.get(name), union_schema, module.__dict__[name])
    return len(models)


def main() -> int:
    try:
        print(check())
    except CheckError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
