"""Flags snake_case Python identifiers that should be camelCase, per
docs/backend-standards.md rule 17.

This project deliberately uses camelCase (not PEP 8 snake_case) for
functions, methods, parameters, and local variables. Ruff's `N` (pep8-naming)
family assumes snake_case and would fight this convention, so it's off in
pyproject.toml -- this script is the naming check instead. There's no CI yet,
so run it by hand before opening a PR:

    python scripts/check_naming.py

Exit code is non-zero if anything is flagged.

Recognized exceptions (see rule 17 for the full rationale):
  - dunder methods (__init__, __repr__, ...)
  - alembic upgrade()/downgrade() -- alembic/ is skipped entirely, it's DB
    schema, not application code
  - UPPER_SNAKE module-level constants
  - pytest `test_...` function names (only the descriptive remainder should
    be camelCase)
  - FastAPI path parameters, which must match the literal `{name}` in the
    route's path string (best-effort: only flagged if the enclosing function
    isn't decorated with a router call whose path string contains a matching
    `{name}` placeholder)
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET_DIRS = [REPO / "src", REPO / "tests"]

UPPER_CONST_RE = re.compile(r"^_*[A-Z0-9]+(_[A-Z0-9]+)*_*$")
DUNDER_RE = re.compile(r"^__[a-zA-Z0-9_]+__$")
SNAKE_RE = re.compile(r"^_*[a-z][a-z0-9_]*$")

RESERVED_NAMES = {
    "model_config", "model_fields", "model_computed_fields", "model_extra",
    "model_fields_set", "model_post_init", "model_dump", "model_dump_json",
    "model_validate", "model_validate_json", "model_copy", "model_construct",
    "model_json_schema", "model_rebuild", "model_parametrized_name",
    "upgrade", "downgrade", "metadata", "registry", "query",
}


def is_dunder(name: str) -> bool:
    return bool(DUNDER_RE.match(name))


def is_upper_const(name: str) -> bool:
    return bool(UPPER_CONST_RE.match(name)) and any(c.isalpha() for c in name)


def is_snake_case(name: str) -> bool:
    if is_dunder(name) or name in RESERVED_NAMES or is_upper_const(name):
        return False
    core = name.lstrip("_")
    return bool(SNAKE_RE.match(name)) and "_" in core


def path_params_for(decorator_list: list[ast.expr]) -> set[str]:
    """Extract {placeholder} names from a @router.<method>("...") decorator's
    first string argument, if any.
    """
    params: set[str] = set()
    for dec in decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        if call is None:
            continue
        func = call.func
        is_router_call = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("router", "app")
        if not is_router_call:
            continue
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                params.update(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", arg.value))
    return params


def check_file(path: Path, findings: list[tuple[Path, int, str, str]]) -> None:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return

    is_test_file = "tests" in path.parts and path.name.startswith("test_")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if is_test_file and name.startswith("test_"):
                remainder = name[len("test_"):]
                if remainder and is_snake_case(remainder.lstrip("_")) and "_" in remainder:
                    findings.append((path, node.lineno, "test-function", name))
            elif is_snake_case(name):
                findings.append((path, node.lineno, "function/method", name))

            path_params = path_params_for(node.decorator_list)
            all_args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
            for a in all_args:
                if a.arg in ("self", "cls") or a.arg in path_params:
                    continue
                if is_snake_case(a.arg):
                    findings.append((path, a.lineno, "parameter", a.arg))

        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            # Attribute assignments (self.x = ...) are deliberately not
            # checked here: too often the target is a third-party object
            # (a MagicMock's `.return_value`/`.side_effect`, a real
            # SQLAlchemy/library method being mocked) whose name isn't ours
            # to rename, and there's no reliable way to tell without type
            # information. Bare local variables carry no such ambiguity.
            if is_snake_case(node.id):
                findings.append((path, node.lineno, "variable", node.id))


def main() -> int:
    findings: list[tuple[Path, int, str, str]] = []
    for base in TARGET_DIRS:
        for f in sorted(base.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            check_file(f, findings)

    if not findings:
        print("check_naming: clean -- no snake_case identifiers found.")
        return 0

    findings.sort(key=lambda f: (str(f[0]), f[1]))
    for path, lineno, kind, name in findings:
        rel = path.relative_to(REPO)
        print(f"{rel}:{lineno}: {kind} `{name}` should be camelCase")
    print(f"\ncheck_naming: {len(findings)} finding(s).")
    print(
        "If this is one of the documented exceptions (path parameters, DB-facing\n"
        "dict data, etc. -- see docs/backend-standards.md rule 17), it's a false\n"
        "positive; otherwise rename it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
