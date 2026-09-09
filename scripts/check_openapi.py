"""CI sanity check for the generated OpenAPI schema.

Confirms `/openapi.json` still generates as valid JSON with a `paths` key,
the expected route count, and no untagged routes — cheap insurance against a
silent regression of the M8/M9 OpenAPI fixes (readiness-audit remediation
Step 5). Run by hand or from CI — must be run as a module (`-m`), not as a
bare script path, so `main` resolves on `sys.path` from the repo root:

    python -m scripts.check_openapi

Exit code is non-zero if anything is flagged.
"""
import sys

from fastapi.testclient import TestClient

from main import app

EXPECTED_ROUTE_COUNT = 46
_HTTP_METHODS = ("get", "post", "put", "delete", "patch")


def main() -> int:
    """Fetch `/openapi.json` from an in-process test client and validate it."""
    client = TestClient(app)
    response = client.get("/openapi.json")
    if response.status_code != 200:
        print(f"FAIL: /openapi.json returned {response.status_code}", file=sys.stderr)
        return 1

    spec = response.json()
    if "paths" not in spec:
        print("FAIL: openapi spec is missing a 'paths' key", file=sys.stderr)
        return 1

    routes = [
        (method.upper(), path)
        for path, methods in spec["paths"].items()
        for method in methods
        if method in _HTTP_METHODS
    ]

    if len(routes) != EXPECTED_ROUTE_COUNT:
        print(
            f"FAIL: expected {EXPECTED_ROUTE_COUNT} routes, found {len(routes)}",
            file=sys.stderr,
        )
        return 1

    untagged = [
        (method, path)
        for path, methods in spec["paths"].items()
        for method, op in methods.items()
        if method in _HTTP_METHODS and not op.get("tags")
    ]
    if untagged:
        print(f"FAIL: {len(untagged)} untagged route(s): {untagged}", file=sys.stderr)
        return 1

    print(f"OK: openapi.json valid, {len(routes)} routes, all tagged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
