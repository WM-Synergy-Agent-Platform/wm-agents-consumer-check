"""The run script's pieces that need no Docker, and the repo's own pins."""

from __future__ import annotations

import json
import re
import stat
import tomllib
from pathlib import Path

from wm_agents import AgentDefinition
from wm_agents.security import MIN_KEY_LENGTH

from consumer_check import agent
from consumer_check.check import TENANTS, locked_source, write_secrets

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY = (
    "wm-agents @ git+https://github.com/WM-Synergy-Agent-Platform/wm-agents@v0.2.0"
    "#subdirectory=packages/agent-core"
)


def test_the_project_depends_on_exactly_the_v0_2_0_tag() -> None:
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["dependencies"] == [DEPENDENCY]


def test_the_lock_pins_the_tag_s_commit() -> None:
    source = locked_source((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert source.repository == "https://github.com/WM-Synergy-Agent-Platform/wm-agents"
    assert source.rev == "v0.2.0"
    assert source.subdirectory == "packages/agent-core"
    assert source.commit == "74aad978e7645cec609627f6969dc51ddc742e8f"


def test_the_agent_is_defined_through_wm_agents() -> None:
    assert isinstance(agent.DEFINITION, AgentDefinition)


def test_each_tenant_gets_its_own_generated_key_and_token(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    keys = write_secrets(directory, TENANTS)
    assert sorted(keys) == sorted(TENANTS)
    assert len(set(keys.values())) == len(TENANTS)
    assert all(len(key) >= MIN_KEY_LENGTH for key in keys.values())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    tokens = set()
    for tenant in TENANTS:
        stored = json.loads((directory / f"{tenant}-api-keys.json").read_text(encoding="ascii"))
        assert list(stored.values()) == [keys[tenant]]
        tokens.add((directory / f"{tenant}-gateway-token").read_text(encoding="ascii"))
    assert len(tokens) == len(TENANTS)
    assert tokens.isdisjoint(keys.values())


def test_a_second_run_replaces_every_secret(tmp_path: Path) -> None:
    first = write_secrets(tmp_path / "secrets", TENANTS)
    second = write_secrets(tmp_path / "secrets", TENANTS)
    assert set(first.values()).isdisjoint(second.values())


def test_every_published_port_is_bound_to_loopback() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    published = re.findall(r'^\s*-\s*"([^"]*:8080)"\s*$', compose, flags=re.MULTILINE)
    assert len(published) == 1 + 1  # the agents' shared anchor and the gateway
    assert all(port.startswith("127.0.0.1:") for port in published)


def test_every_base_image_is_pinned_by_digest() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bases = re.findall(r"^FROM\s+(\S+)", dockerfile, flags=re.MULTILINE)
    external = [base for base in bases if "/" in base or ":" in base]
    assert external
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", base) for base in external)
