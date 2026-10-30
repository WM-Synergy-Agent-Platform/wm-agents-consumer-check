"""The local-library mode: a wm-agents checkout's wheel, pinned to that checkout's lock (WMA-197).

``python -m consumer_check.check --library <checkout>`` builds the wheel from the checkout,
clones nothing and checks no commit, writes only the run-scoped copy ``.run/library``
(never the committed ``pyproject.toml``, ``uv.lock`` or lock snapshot), re-locks the copy to
the checkout's ``uv.lock`` and starts Compose from the copy. git, uv and Docker are replaced
here by a recorder; the real run is in the pull request.
"""

from __future__ import annotations

import hashlib
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from consumer_check import check
from consumer_check.sync_constraints import (
    library_manifest,
    lock_differences,
    lock_versions,
    registry_versions,
)

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ("pyproject.toml", "uv.lock", "constraints/wm-agents-lock.json")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
WHEEL = "wm_agents-0.3.0-py3-none-any.whl"
HEALTHY = "agent-tenant-a=healthy\nagent-tenant-b=healthy\nscripted-gateway=healthy\n"
#: A synthetic wm-agents lock: two registry packages and the workspace member.
CHECKOUT_VERSIONS = {"anyio": "4.15.1", "pydantic": "2.13.5"}


def _lock(versions: Mapping[str, str]) -> str:
    packages = "".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        for name, version in versions.items()
    )
    member = '[[package]]\nname = "wm-agents"\nversion = "0.3.0"\nsource = { editable = "x" }\n'
    return f"version = 1\n{packages}{member}"


class Recorder:
    """Stands in for ``check._run``: records each command; ``uv lock`` writes ``locked``."""

    def __init__(self, locked: Mapping[str, str]) -> None:
        self.locked = dict(locked)
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(
        self, command: Sequence[str], *, quiet: bool = False, cwd: Path = check.ROOT
    ) -> str:
        self.calls.append((list(command), Path(cwd)))
        if list(command[-2:]) == ["rev-parse", "HEAD"]:
            return COMMIT + "\n"
        if list(command[:2]) == ["uv", "build"]:
            out = Path(command[list(command).index("--out-dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / WHEEL).write_bytes(b"")
        if list(command[:2]) == ["uv", "lock"]:
            (Path(cwd) / "uv.lock").write_text(_lock(self.locked), encoding="utf-8")
        if "ps" in command:
            return HEALTHY
        return ""


def _digests() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in COMMITTED}


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A synthetic wm-agents checkout: its uv.lock and the agent-core member."""
    path = tmp_path / "wm-agents"
    (path / "packages" / "agent-core").mkdir(parents=True)
    (path / "packages" / "agent-core" / "pyproject.toml").write_text("[project]\n")
    (path / "uv.lock").write_text(_lock(CHECKOUT_VERSIONS), encoding="utf-8")
    return path


@pytest.fixture
def library_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "run" / "library"
    monkeypatch.setattr(check, "LIBRARY_DIR", path)
    before = _digests()
    yield path
    assert _digests() == before, "a committed file changed"


def _use(monkeypatch: pytest.MonkeyPatch, locked: Mapping[str, str]) -> Recorder:
    recorder = Recorder(locked)
    monkeypatch.setattr(check, "_run", recorder)
    return recorder


def test_the_copy_installs_the_checkout_s_wheel_and_pins_its_lock(
    checkout: Path, library_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _use(monkeypatch, CHECKOUT_VERSIONS)
    wheel = check.prepare_library(checkout)

    assert wheel == library_dir / ".run" / "wheelhouse" / WHEEL
    manifest = tomllib.loads((library_dir / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["dependencies"] == ["wm-agents"]
    assert manifest["tool"]["uv"]["sources"] == {"wm-agents": {"path": f".run/wheelhouse/{WHEEL}"}}
    assert manifest["tool"]["uv"]["constraint-dependencies"] == [
        "anyio==4.15.1",
        "pydantic==2.13.5",
    ]
    committed = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for table in ("dependency-groups", "build-system"):
        assert manifest[table] == committed[table]
    for key in ("ruff", "mypy", "pytest"):
        assert manifest["tool"][key] == committed["tool"][key]
    for name in ("Dockerfile", "compose.yaml", ".dockerignore", "README.md"):
        assert (library_dir / name).read_bytes() == (ROOT / name).read_bytes()
    assert (library_dir / "src" / "consumer_check" / "agent.py").is_file()
    assert (library_dir / "tenants" / "tenant-a" / "tenant.yaml").is_file()
    assert recorder.calls == [
        (["git", "-C", str(checkout), "rev-parse", "HEAD"], check.ROOT),
        (
            [
                "uv",
                "build",
                "--quiet",
                "--wheel",
                "--out-dir",
                str(library_dir / ".run" / "wheelhouse"),
                str(checkout / "packages" / "agent-core"),
            ],
            check.ROOT,
        ),
        (["uv", "lock"], library_dir),
    ]


def test_a_copy_locked_off_the_checkout_s_versions_fails(
    checkout: Path, library_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, {**CHECKOUT_VERSIONS, "anyio": "4.16.0", "extra": "1.0"})
    with pytest.raises(check.CheckFailedError, match=r"2 package\(s\).*anyio, extra"):
        check.prepare_library(checkout)


def test_a_path_that_is_not_a_wm_agents_checkout_fails(
    tmp_path: Path, library_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _use(monkeypatch, CHECKOUT_VERSIONS)
    with pytest.raises(check.CheckFailedError, match="not wm-agents"):
        check.prepare_library(tmp_path)
    assert recorder.calls == []
    assert not library_dir.exists()


def test_the_library_run_starts_compose_from_the_copy_with_the_same_assertions(
    checkout: Path,
    library_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = _use(monkeypatch, CHECKOUT_VERSIONS)
    isolation: list[tuple[list[str], tuple[object, ...]]] = []

    def fake_isolation(results: check.Results, keys: Mapping[str, str], *rest: object) -> None:
        isolation.append((sorted(keys), rest))

    monkeypatch.setattr(check, "assert_isolation", fake_isolation)
    assert check.run(["--library", str(checkout)]) == 0

    assert isolation == [(["tenant-a", "tenant-b"], (library_dir,))]
    docker = [(command, cwd) for command, cwd in recorder.calls if command[0] == "docker"]
    assert [command[4] for command, _ in docker] == ["up", "ps", "down"]
    assert all(cwd == library_dir for _, cwd in docker)
    assert all(
        command[:4] == ["docker", "compose", "--project-name", "wma191"] for command, _ in docker
    )
    assert not any("clone" in command for command, _ in recorder.calls)
    assert not (library_dir / ".run" / "secrets").exists()
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"wm-agents: the library checkout at {checkout} ({COMMIT[:12]}), not the tag"
    assert out[-1] == "PASSED: 0 failure(s)"


def test_prepare_only_locks_the_copy_and_starts_nothing(
    checkout: Path,
    library_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = _use(monkeypatch, CHECKOUT_VERSIONS)
    assert check.run(["--library", str(checkout), "--prepare-only"]) == 0
    assert (library_dir / "uv.lock").is_file()
    assert not any(command[0] == "docker" for command, _ in recorder.calls)
    assert capsys.readouterr().out.splitlines()[-1].startswith("PREPARED:")


def test_a_failed_preparation_fails_the_run(
    checkout: Path,
    library_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use(monkeypatch, {"anyio": "4.16.0"})
    assert check.run(["--library", str(checkout)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "FAILED: 1 failure(s)"


def test_prepare_only_needs_a_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["consumer_check.check"])
    with pytest.raises(SystemExit) as raised:
        check.run(["--prepare-only"])
    assert raised.value.code == 2


def test_lock_versions_reads_a_lock_from_a_path(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(_lock(CHECKOUT_VERSIONS), encoding="utf-8")
    assert lock_versions(lock) == registry_versions(_lock(CHECKOUT_VERSIONS)) == CHECKOUT_VERSIONS


def test_lock_differences_names_other_versions_and_unknown_packages() -> None:
    reference = {"a": "1.0", "b": "2.0"}
    assert lock_differences(reference, {"a": "1.0"}) == []
    assert lock_differences(reference, {"a": "1.1", "b": "2.0", "c": "3.0"}) == ["a", "c"]


def test_the_library_manifest_leaves_the_committed_text_alone() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = library_manifest(
        text, wheel=f".run/wheelhouse/{WHEEL}", versions={"a": "1.0"}, checkout="/x", commit=COMMIT
    )
    assert (ROOT / "pyproject.toml").read_text(encoding="utf-8") == text
    parsed = tomllib.loads(manifest)
    assert parsed["project"]["dependencies"] == ["wm-agents"]
    assert parsed["tool"]["uv"]["constraint-dependencies"] == ["a==1.0"]
    assert "git+" not in manifest


def test_the_library_manifest_refuses_a_path_toml_can_t_hold() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="wheel path"):
        library_manifest(text, wheel='a"b.whl', versions={}, checkout="/x", commit=COMMIT)
