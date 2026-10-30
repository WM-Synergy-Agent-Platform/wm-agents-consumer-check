"""The consumer imports only ``wm_agents``, its own code, ``pydantic`` and the standard library."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "consumer_check"
OWN = "consumer_check"


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _third_party(path: Path) -> set[str]:
    return _imported_roots(path) - set(sys.stdlib_module_names) - {"__future__", OWN}


def test_the_agent_module_imports_only_wm_agents_and_pydantic() -> None:
    assert _third_party(PACKAGE / "agent.py") == {"wm_agents", "pydantic"}


@pytest.mark.parametrize(
    "module", ["scripted_gateway.py", "check.py", "sync_constraints.py", "__init__.py"]
)
def test_the_gateway_and_the_run_script_use_the_standard_library_only(module: str) -> None:
    assert _third_party(PACKAGE / module) == set()


def test_no_module_imports_agent_core() -> None:
    for path in PACKAGE.rglob("*.py"):
        assert "agent_core" not in _imported_roots(path), path.name


def _own_modules(path: Path) -> set[str]:
    """The modules of this package that ``path`` imports, by dotted name."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names if alias.name.startswith(OWN))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == OWN:
                modules.update(f"{OWN}.{alias.name}" for alias in node.names)
            elif node.module.startswith(OWN + "."):
                modules.add(node.module)
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            modules.add(f"relative import of {node.module or '.'}")
    return modules


def test_the_run_script_defines_its_own_expectations() -> None:
    """The check's expected answer and paths are its own, not the gateway module's.

    Its one import from this package is ``sync_constraints``, the lock helpers its
    local-library mode shares (WMA-197): never the scripted gateway or the agent module.
    """
    assert _own_modules(PACKAGE / "check.py") == {f"{OWN}.sync_constraints"}
