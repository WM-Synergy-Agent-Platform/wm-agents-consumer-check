"""The default mode (no option) runs exactly the Gate 1 steps, in order (WMA-197).

Gate 1's record stands on ``python -m consumer_check.check`` with no option: clone the tag
``uv.lock`` pins, check its commit, build the wheel, start Compose from the repo root,
assert, tear down. The local-library mode must not change any of it. This test records
every command the run would execute, with its working directory, and every line it prints,
with Docker, git and uv replaced by a recorder. It needs nothing but the repo.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from consumer_check import check

COMMIT = "74aad978e7645cec609627f6969dc51ddc742e8f"
WHEEL = "wm_agents-0.2.0-py3-none-any.whl"
HEALTHY = "agent-tenant-a=healthy\nagent-tenant-b=healthy\nscripted-gateway=healthy\n"


class Recorder:
    """Stands in for ``check._run``: records each command and answers as the tools would."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool, Path]] = []

    def __call__(
        self, command: Sequence[str], *, quiet: bool = False, cwd: Path = check.ROOT
    ) -> str:
        self.calls.append((list(command), quiet, Path(cwd)))
        if list(command[-2:]) == ["rev-parse", "HEAD"]:
            return COMMIT + "\n"
        if list(command[:2]) == ["uv", "build"]:
            out = Path(command[list(command).index("--out-dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / WHEEL).write_bytes(b"")
        if "ps" in command:
            return HEALTHY
        return ""


def test_the_default_mode_runs_exactly_the_gate_1_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets_dir, wheelhouse = tmp_path / "secrets", tmp_path / "wheelhouse"
    clone = tmp_path / "build" / "wm-agents"
    monkeypatch.setattr(check, "SECRETS_DIR", secrets_dir)
    monkeypatch.setattr(check, "WHEELHOUSE", wheelhouse)
    monkeypatch.setattr(check, "CLONE_DIR", clone)
    monkeypatch.setattr(check, "LIBRARY_DIR", tmp_path / "library", raising=False)
    recorder = Recorder()
    monkeypatch.setattr(check, "_run", recorder)
    isolation: list[tuple[list[str], tuple[object, ...]]] = []

    def fake_isolation(results: check.Results, keys: Mapping[str, str], *rest: object) -> None:
        isolation.append((sorted(keys), rest))

    monkeypatch.setattr(check, "assert_isolation", fake_isolation)
    monkeypatch.setattr(sys, "argv", ["consumer_check.check"])

    assert check.run() == 0

    root = check.ROOT
    compose = ["docker", "compose", "--project-name", "wma191"]
    assert recorder.calls == [
        (
            [
                "git",
                "-c",
                "advice.detachedHead=false",
                "clone",
                "--quiet",
                "--depth=1",
                "--branch=v0.2.0",
                "https://github.com/WM-Synergy-Agent-Platform/wm-agents",
                str(clone),
            ],
            False,
            root,
        ),
        (["git", "-C", str(clone), "rev-parse", "HEAD"], False, root),
        (
            [
                "uv",
                "build",
                "--quiet",
                "--wheel",
                "--out-dir",
                str(wheelhouse),
                str(clone / "packages" / "agent-core"),
            ],
            False,
            root,
        ),
        ([*compose, "up", "--build", "--detach", "--wait", "--wait-timeout=180"], False, root),
        ([*compose, "ps", "--format", "{{.Service}}={{.Health}}"], False, root),
        ([*compose, "down", "-v", "--remove-orphans"], True, root),
    ]
    assert isolation == [(["tenant-a", "tenant-b"], ())]
    assert capsys.readouterr().out.splitlines() == [
        "wm-agents: v0.2.0 at 74aad978e764 (from uv.lock)",
        "generated 2 inbound API keys and 2 gateway tokens into .run/secrets (values not shown)",
        f"built {WHEEL} on the host into .run/wheelhouse",
        "services up: agent-tenant-a=healthy, agent-tenant-b=healthy, scripted-gateway=healthy",
        "torn down: docker compose down -v",
        "PASSED: 0 failure(s)",
    ]
    assert not secrets_dir.exists()
    assert not (tmp_path / "library").exists()
