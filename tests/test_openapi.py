"""docs/openapi.yaml must describe exactly the HTTP surface the code serves.

CLAUDE.md asks for API-first design, which is only true if the spec cannot
drift. This checks both directions: no route missing from the spec, and no
path in the spec that no longer exists.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    from tests.openapi_routes import discover_routes
except ImportError:
    from openapi_routes import discover_routes

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "openapi.yaml"
METHODS = {"get", "post", "head", "put", "patch", "delete"}


def spec_operations() -> set[tuple[str, str]]:
    """Parse `paths:` from the spec.

    PyYAML is not a runtime dependency, so this walks the two indentation
    levels under `paths:` directly. When PyYAML happens to be installed,
    test_the_spec_is_valid_yaml cross-checks that this parse is faithful.
    """
    operations: set[tuple[str, str]] = set()
    path = None
    inside = False
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        if line.startswith("paths:"):
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            break
        if not inside or not line.strip():
            continue
        if (match := re.fullmatch(r"  (/\S*):", line)):
            path = match.group(1)
        elif path and (match := re.fullmatch(r"    ([a-z]+):", line)) and match.group(1) in METHODS:
            operations.add((match.group(1).upper(), path))
    return operations


class OpenApiCoverageTest(unittest.TestCase):
    def test_the_spec_exists_and_declares_the_auth_model(self) -> None:
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn("sosopo_session", text)
        self.assertIn("X-CSRF-Token", text)

    def test_every_route_is_described(self) -> None:
        missing = sorted(discover_routes(ROOT) - spec_operations())
        self.assertEqual(missing, [], f"routes missing from docs/openapi.yaml: {missing}")

    def test_the_spec_describes_no_route_that_is_gone(self) -> None:
        stale = sorted(spec_operations() - discover_routes(ROOT))
        self.assertEqual(stale, [], f"docs/openapi.yaml describes removed routes: {stale}")

    def test_state_changing_routes_require_the_csrf_token(self) -> None:
        """Every authenticated POST must declare csrfToken, matching _require_auth(csrf=True)."""
        text = SPEC.read_text(encoding="utf-8")
        blocks = re.findall(r"\n    post:\n(.*?)(?=\n    [a-z]+:|\n  /|\Z)", text, re.S)
        insecure = [block for block in blocks if "security: []" not in block and "csrfToken" not in block]
        self.assertEqual(insecure, [], "an authenticated POST is missing csrfToken")

    def test_the_spec_is_valid_yaml(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not a runtime dependency; structural parse covers the rest")
        document = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertIn("paths", document)
        parsed = {(method.upper(), path) for path, item in document["paths"].items() for method in item}
        self.assertEqual(parsed, spec_operations())


if __name__ == "__main__":
    unittest.main()
