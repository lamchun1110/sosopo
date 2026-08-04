"""Discover the HTTP surface from the route mixins.

Shared by the OpenAPI coverage test and by the spec generator, so the spec and
the check can never disagree about what the routes are. This reads the source
rather than importing it: a route is a fact about the code, and reading it
statically keeps the check honest even if a module fails to import.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# Path segments that a route matches by prefix are templated in the spec. The
# name chosen here is cosmetic, but it has to be stable for the check to pass.
TEMPLATE_NAMES = {
    "/api/invitations/": "token",
    "/api/social-oauth/": "provider",
    "/api/workspaces/ai-providers/": "provider",
    "/api/admin/ai-providers/": "provider",
}
# Predicates that gate the dispatcher rather than name a route.
NOT_ROUTES = {"/api/"}


def _method(function_name: str) -> str | None:
    if function_name.startswith("get_") or function_name == "do_GET":
        return "GET"
    if function_name.startswith("post_") or function_name == "do_POST":
        return "POST"
    if function_name == "do_HEAD":
        return "HEAD"
    return None


def discover_routes(root: Path) -> set[tuple[str, str]]:
    """Return every (METHOD, templated path) pair the Handler answers."""
    routes: set[tuple[str, str]] = set()
    sources = sorted((root / "app" / "routes").glob("*.py")) + [root / "app" / "server.py"]
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            method = _method(function.name)
            if method is None:
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.If):
                    continue
                test = ast.unparse(node.test)
                exact = re.findall(r"path == '(/api/[^']*)'", test)
                prefix = re.findall(r"path\.startswith\('(/api/[^']*)'\)", test)
                suffix = re.findall(r"path\.endswith\('([^']*)'\)", test)
                for value in exact:
                    if value not in NOT_ROUTES:
                        routes.add((method, value))
                if prefix and prefix[0] not in NOT_ROUTES:
                    name = TEMPLATE_NAMES.get(prefix[0], "id")
                    tail = suffix[0] if suffix else ""
                    routes.add((method, f"{prefix[0]}{{{name}}}{tail}"))
    return routes
