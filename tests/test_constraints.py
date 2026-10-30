"""The consumer resolves to wm-agents' own lock at the tag it installs (review P2-2).

The reference is the committed snapshot ``constraints/wm-agents-lock.json``, which
``python -m consumer_check.sync_constraints`` writes from ``git show <tag>:uv.lock`` in a
wm-agents checkout. It is committed so this test needs neither wm-agents nor a credential.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from consumer_check.check import locked_source
from consumer_check.sync_constraints import (
    constraint_block,
    registry_versions,
    replace_block,
    tag_of,
)

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "constraints" / "wm-agents-lock.json"


def _snapshot() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return data


def _consumer_versions() -> dict[str, str]:
    return registry_versions((ROOT / "uv.lock").read_text(encoding="utf-8"))


def test_the_snapshot_is_wm_agents_lock_at_the_installed_tag() -> None:
    snapshot = _snapshot()
    source = locked_source((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert snapshot["tag"] == source.rev == "v0.2.0"
    assert snapshot["commit"] == source.commit
    assert snapshot["packages"]


def test_every_package_in_both_locks_has_the_same_version() -> None:
    reference = _snapshot()["packages"]
    consumer = _consumer_versions()
    shared = sorted(set(reference) & set(consumer))
    assert shared
    differing = {name: (consumer[name], reference[name]) for name in shared}
    differing = {name: pair for name, pair in differing.items() if pair[0] != pair[1]}
    assert differing == {}


def test_the_consumer_lock_holds_no_third_party_package_wm_agents_lock_lacks() -> None:
    assert sorted(set(_consumer_versions()) - set(_snapshot()["packages"])) == []


def test_the_pyproject_constraints_are_the_snapshot() -> None:
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    constraints = manifest["tool"]["uv"]["constraint-dependencies"]
    expected = [f"{name}=={version}" for name, version in _snapshot()["packages"].items()]
    assert constraints == expected


def test_registry_versions_skips_git_editable_and_virtual_sources() -> None:
    lock = """
version = 1
[[package]]
name = "a"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
[[package]]
name = "wm-agents"
version = "0.2.0"
source = { git = "https://example.test/wm-agents?rev=v0.2.0#0123" }
[[package]]
name = "member"
version = "0.1.0"
source = { editable = "member" }
[[package]]
name = "root"
version = "0.0.0"
source = { virtual = "." }
"""
    assert registry_versions(lock) == {"a": "1.0"}


def test_the_tag_is_read_from_the_dependency() -> None:
    dependency = (
        "wm-agents @ git+https://github.com/WM-Synergy-Agent-Platform/wm-agents@v0.3.1"
        "#subdirectory=packages/agent-core"
    )
    assert tag_of([dependency]) == "v0.3.1"


def test_the_generated_block_replaces_only_itself() -> None:
    block = constraint_block({"a": "1.0", "b": "2.0b1"}, tag="v0.2.0", commit="0" * 40)
    text = f"[project]\nname = 'x'\n\n{block}\n[tool.other]\nkey = 1\n"
    updated = replace_block(text, constraint_block({"a": "1.1"}, tag="v0.2.1", commit="1" * 40))
    manifest = tomllib.loads(updated)
    assert manifest["tool"]["uv"]["constraint-dependencies"] == ["a==1.1"]
    assert manifest["tool"]["other"] == {"key": 1}
    assert manifest["project"] == {"name": "x"}
