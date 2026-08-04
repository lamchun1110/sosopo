"""Structural invariants of the app package.

`app/server.py` reloads its siblings in `_SUBMODULES` order after the test
suite changes the environment. Reloading a module rebinds the names it
imported, so that is only correct if every module is reloaded *after* the
modules it imports from. Getting this wrong leaves stale bindings that
generally still pass tests — which is exactly why it needs a mechanical check.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def reload_order() -> list[str]:
    source = (APP / "server.py").read_text(encoding="utf-8")
    block = re.search(r"_SUBMODULES = \((.*?)\n\)", source, re.S)
    assert block, "_SUBMODULES is no longer a parenthesized tuple literal"
    return re.findall(r'"([^"]+)"', block.group(1))


def module_path(name: str) -> Path:
    return APP / (name.replace(".", "/") + ".py")


def app_dependencies(name: str) -> set[str]:
    """Sibling modules this module imports from, by top-level package name."""
    dependencies: set[str] = set()
    for node in ast.walk(ast.parse(module_path(name).read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        if node.module:
            dependencies.add(node.module.split(".")[0])
        else:  # from . import x
            dependencies.update(alias.name for alias in node.names)
    return dependencies


class ReloadOrderTest(unittest.TestCase):
    def test_every_submodule_exists(self) -> None:
        missing = [name for name in reload_order() if not module_path(name).is_file()]
        self.assertEqual(missing, [], f"_SUBMODULES names modules that do not exist: {missing}")

    def test_no_module_is_reloaded_before_something_it_imports(self) -> None:
        order = reload_order()
        position = {name: index for index, name in enumerate(order)}
        violations = [
            f"{name} imports {dependency}, which reloads later"
            for name in order
            for dependency in app_dependencies(name)
            if dependency in position and position[dependency] > position[name]
        ]
        self.assertEqual(violations, [], "; ".join(violations))

    def test_every_app_module_is_in_the_reload_order(self) -> None:
        order = set(reload_order())
        on_disk = {path.stem for path in APP.glob("*.py")} - {"server", "worker"}
        on_disk |= {f"routes.{path.stem}" for path in (APP / "routes").glob("*.py")}
        missing = sorted(on_disk - order)
        self.assertEqual(missing, [], f"modules never reloaded by app/server.py: {missing}")

    def test_route_families_reload_after_the_domain_modules(self) -> None:
        order = reload_order()
        first_route = min(index for index, name in enumerate(order) if name.startswith("routes."))
        late_domain = [name for name in order[first_route:] if not name.startswith("routes.")]
        self.assertEqual(late_domain, [], f"domain modules reload after route families: {late_domain}")


class ModuleSizeTest(unittest.TestCase):
    """A1's target: focused modules, so no file grows back into a monolith."""

    LIMIT = 800

    def test_no_module_exceeds_the_size_target(self) -> None:
        oversized = {
            path.relative_to(ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
            for path in [*APP.glob("*.py"), *(APP / "routes").glob("*.py")]
            if len(path.read_text(encoding="utf-8").splitlines()) > self.LIMIT
        }
        self.assertEqual(oversized, {}, f"modules over {self.LIMIT} lines: {oversized}")


if __name__ == "__main__":
    unittest.main()
