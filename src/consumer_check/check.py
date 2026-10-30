"""The Gate 1 run: start both tenant processes and check each refuses the other's key.

    uv run python -m consumer_check.check

1. Generates, into the gitignored ``.run/secrets``, one inbound API key and one gateway
   token per tenant. No value is printed, logged or committed; the files go when the run
   ends.
2. Builds the wm-agents wheel on the host, from the commit ``uv.lock`` pins, into
   ``.run/wheelhouse``. The clone uses the host's own git credentials and writes none into
   a file, so the Docker build needs no credential.
3. Starts the Compose project ``wma191``: the scripted gateway and two agent containers from
   one image, and waits until every service is healthy.
4. Asserts, on ``POST /invoke``: tenant A's key gets ``401`` from tenant B's process and
   ``200`` from A's, whose output is the scripted gateway's answer; the same the other way
   round; that the scripted gateway answered exactly one model call per tenant, each with
   that tenant's own gateway token; and that the gateway itself refuses a model call with a
   random token or with none (``401``), counting neither.
5. Tears the project down (``docker compose down -v``) whatever happened, and exits
   non-zero on any failure.

The local-library mode (WMA-197), for the wm-agents CI and for trying a library change:

    python -m consumer_check.check --library <path to a wm-agents checkout>

It runs the same assertions against the library as that checkout holds it, not the tag. It
clones nothing and checks no commit: it builds the wheel from the checkout's
``packages/agent-core``, and constrains this repo's third-party dependencies to the
checkout's own ``uv.lock``. Nothing committed here changes. The run works in a run-scoped
copy of this project, ``.run/library`` (gitignored), whose ``pyproject.toml`` installs
wm-agents from that wheel and pins the checkout's lock, and which ``uv lock`` re-locks;
the Compose project starts from that copy. ``--prepare-only`` stops once the copy is
locked, so ``uv sync``, ``mypy`` and ``ruff`` can run in it first.

Standard library only.
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from consumer_check.sync_constraints import library_manifest, lock_differences, lock_versions

ROOT: Final = Path(__file__).resolve().parents[2]
RUN_DIR: Final = ROOT / ".run"
SECRETS_DIR: Final = RUN_DIR / "secrets"
WHEELHOUSE: Final = RUN_DIR / "wheelhouse"
CLONE_DIR: Final = RUN_DIR / "build" / "wm-agents"
#: The local-library mode's run-scoped copy of this project (gitignored, under ``.run``).
LIBRARY_DIR: Final = RUN_DIR / "library"
#: What the copy holds: everything the image, Compose, ``mypy`` and ``ruff`` read.
LIBRARY_FILES: Final = (
    "Dockerfile",
    "compose.yaml",
    ".dockerignore",
    ".python-version",
    "README.md",
)
LIBRARY_TREES: Final = ("src", "tests", "config", "tenants")
#: The wm-agents package in a wm-agents checkout.
LIBRARY_MEMBER: Final = "packages/agent-core"
PROJECT: Final = "wma191"
TENANTS: Final = ("tenant-a", "tenant-b")
AGENT_SERVICES: Final = {"tenant-a": "agent-tenant-a", "tenant-b": "agent-tenant-b"}
GATEWAY_SERVICE: Final = "scripted-gateway"
#: The caller name in each tenant's API keys file.
CALLER: Final = "consumer-check"
WAIT_SECONDS: Final = 180
HTTP_TIMEOUT_SECONDS: Final = 30
#: What a 200 from ``POST /invoke`` must carry: the scripted gateway's answer, spelled out
#: here rather than imported, so a change on the gateway's side fails the check.
EXPECTED_OUTPUT: Final = {"answer": "synthetic answer from the scripted gateway"}
#: The scripted gateway's chat route and its call counts, as the agents and the run see them.
GATEWAY_CHAT_PATH: Final = "/v1/chat/completions"
GATEWAY_CALLS_PATH: Final = "/_scripted/calls"
#: A chat call the run sends to the gateway itself, to see it refused.
PROBE_CHAT: Final = {
    "model": "scripted-chat-model",
    "messages": [{"role": "user", "content": "a probe that must be refused"}],
}


class CheckFailedError(Exception):
    """A step of the run failed; the message says which, never a key or token."""


@dataclass(frozen=True, slots=True)
class LockedSource:
    """Where uv.lock says wm-agents comes from."""

    repository: str
    rev: str
    commit: str
    subdirectory: str


def locked_source(lock_text: str) -> LockedSource:
    """The git source a uv.lock pins for wm-agents."""
    lock = tomllib.loads(lock_text)
    git = next(
        (
            package.get("source", {}).get("git")
            for package in lock.get("package", [])
            if package.get("name") == "wm-agents"
        ),
        None,
    )
    if not isinstance(git, str):
        raise CheckFailedError("uv.lock pins no git source for wm-agents")
    url, _, commit = git.partition("#")
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    rev = query.get("rev", [""])[0]
    subdirectory = query.get("subdirectory", [""])[0]
    if not (commit and rev and subdirectory):
        raise CheckFailedError("uv.lock's wm-agents source lacks a rev, commit or subdirectory")
    repository = parts._replace(query="", fragment="").geturl()
    return LockedSource(repository, rev, commit, subdirectory)


def write_secrets(directory: Path, tenants: Sequence[str]) -> dict[str, str]:
    """Generate one API key and one gateway token per tenant into ``directory``.

    Returns each tenant's API key, held in memory for the run only. The folder is
    owner-only; the files in it are readable by all so the containers' non-root user can
    read them once Compose bind-mounts them as secrets.
    """
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    keys: dict[str, str] = {}
    for tenant in tenants:
        keys[tenant] = secrets.token_urlsafe(32)
        files = {
            f"{tenant}-api-keys.json": json.dumps({CALLER: keys[tenant]}),
            f"{tenant}-gateway-token": secrets.token_urlsafe(32),
        }
        for name, content in files.items():
            path = directory / name
            path.write_text(content, encoding="ascii")
            path.chmod(0o644)
    return keys


def _run(command: Sequence[str], *, quiet: bool = False, cwd: Path = ROOT) -> str:
    """Run ``command`` in ``cwd`` (the repo root by default); return its standard output."""
    result = subprocess.run(  # noqa: S603 - fixed commands, no shell
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if quiet else None,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CheckFailedError(f"`{' '.join(command[:3])} ...` exited {result.returncode}")
    return result.stdout


def build_wheelhouse(source: LockedSource) -> Path:
    """Build the wm-agents wheel at the locked commit into the wheelhouse; return it."""
    shutil.rmtree(CLONE_DIR.parent, ignore_errors=True)
    shutil.rmtree(WHEELHOUSE, ignore_errors=True)
    CLONE_DIR.parent.mkdir(parents=True)
    try:
        clone = [
            "git",
            "-c",
            "advice.detachedHead=false",
            "clone",
            "--quiet",
            "--depth=1",
            f"--branch={source.rev}",
        ]
        _run([*clone, source.repository, str(CLONE_DIR)])
        head = _run(["git", "-C", str(CLONE_DIR), "rev-parse", "HEAD"]).strip()
        if head != source.commit:
            raise CheckFailedError(
                f"{source.rev} is at {head[:12]}, but uv.lock pins {source.commit[:12]}"
            )
        member = str(CLONE_DIR / source.subdirectory)
        _run(["uv", "build", "--quiet", "--wheel", "--out-dir", str(WHEELHOUSE), member])
    finally:
        shutil.rmtree(CLONE_DIR.parent, ignore_errors=True)
    wheels = sorted(WHEELHOUSE.glob("wm_agents-*.whl"))
    if len(wheels) != 1:
        raise CheckFailedError(f"expected one wm-agents wheel, built {len(wheels)}")
    return wheels[0]


def _checkout_commit(checkout: Path) -> str:
    """The checkout's ``HEAD`` commit, for the log only; the run checks no commit."""
    try:
        return _run(["git", "-C", str(checkout), "rev-parse", "HEAD"], quiet=True).strip()
    except CheckFailedError:
        return "not a git checkout"


def prepare_library(checkout: Path) -> Path:
    """Make ``LIBRARY_DIR``, this project against the wm-agents ``checkout``; return the wheel.

    Copies what the image, Compose, ``mypy`` and ``ruff`` read into the run-scoped
    ``LIBRARY_DIR``; builds the wm-agents wheel from the checkout into its
    ``.run/wheelhouse``; writes its ``pyproject.toml`` to install that wheel and pin the
    checkout's ``uv.lock``; re-locks it; and fails unless every third-party package in the
    new lock is at the checkout lock's version. Nothing outside ``LIBRARY_DIR`` is written.
    """
    checkout = checkout.resolve()
    lock = checkout / "uv.lock"
    member = checkout / LIBRARY_MEMBER
    if not (lock.is_file() and (member / "pyproject.toml").is_file()):
        raise CheckFailedError(f"{checkout} has no uv.lock or {LIBRARY_MEMBER}: not wm-agents")
    try:
        reference = lock_versions(lock)
    except ValueError as error:
        raise CheckFailedError(f"the checkout's uv.lock: {error}") from None
    commit = _checkout_commit(checkout)
    print(f"wm-agents: the library checkout at {checkout} ({commit[:12]}), not the tag")
    shutil.rmtree(LIBRARY_DIR, ignore_errors=True)
    LIBRARY_DIR.mkdir(parents=True)
    for name in LIBRARY_FILES:
        shutil.copy2(ROOT / name, LIBRARY_DIR / name)
    for name in LIBRARY_TREES:
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(ROOT / name, LIBRARY_DIR / name, ignore=ignore)
    wheelhouse = LIBRARY_DIR / ".run" / "wheelhouse"
    _run(["uv", "build", "--quiet", "--wheel", "--out-dir", str(wheelhouse), str(member)])
    wheels = sorted(wheelhouse.glob("wm_agents-*.whl"))
    if len(wheels) != 1:
        raise CheckFailedError(f"expected one wm-agents wheel, built {len(wheels)}")
    print(f"built {wheels[0].name} from the checkout into .run/library/.run/wheelhouse")
    try:
        manifest = library_manifest(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
            wheel=wheels[0].relative_to(LIBRARY_DIR).as_posix(),
            versions=reference,
            checkout=str(checkout),
            commit=commit,
        )
    except ValueError as error:
        raise CheckFailedError(str(error)) from None
    (LIBRARY_DIR / "pyproject.toml").write_text(manifest, encoding="utf-8")
    _run(["uv", "lock"], cwd=LIBRARY_DIR)
    locked = lock_versions(LIBRARY_DIR / "uv.lock")
    differing = lock_differences(reference, locked)
    if differing:
        raise CheckFailedError(
            f"{len(differing)} package(s) in .run/library/uv.lock are not at the checkout "
            f"lock's version: {', '.join(differing)}"
        )
    print(
        f"locked .run/library to the checkout's uv.lock: {len(locked)} third-party "
        "packages, 0 at another version"
    )
    return wheels[0]


def compose(*args: str, quiet: bool = False, cwd: Path = ROOT) -> str:
    return _run(["docker", "compose", "--project-name", PROJECT, *args], quiet=quiet, cwd=cwd)


def published_port(service: str, cwd: Path = ROOT) -> int:
    """The 127.0.0.1 port Docker published for ``service``'s 8080."""
    address = compose("port", service, "8080", cwd=cwd).strip()
    host, _, port = address.rpartition(":")
    if host != "127.0.0.1":
        raise CheckFailedError(f"{service} is published on {host}, not 127.0.0.1")
    return int(port)


def request(
    method: str, url: str, *, body: Mapping[str, Any] | None = None, key: str | None = None
) -> tuple[int, Any]:
    """Send one request; return its status and JSON body (``None`` if not JSON)."""
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    data = None if body is None else json.dumps(body).encode()
    call = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(call, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, None


class Results:
    """Each assertion's outcome, printed as it is made."""

    def __init__(self) -> None:
        self.failures = 0

    def check(self, passed: bool, description: str, detail: str = "") -> None:
        suffix = f" ({detail})" if detail else ""
        print(f"{'PASS' if passed else 'FAIL'}: {description}{suffix}", flush=True)
        if not passed:
            self.failures += 1


def invoke(port: int, key: str) -> tuple[int, Any]:
    url = f"http://127.0.0.1:{port}/invoke"
    return request("POST", url, body={"input": "What is the answer?"}, key=key)


def assert_isolation(results: Results, keys: Mapping[str, str], cwd: Path = ROOT) -> None:
    """The cross-tenant 401s, the same-tenant 200s and the gateway's call counts."""
    ports = {tenant: published_port(service, cwd) for tenant, service in AGENT_SERVICES.items()}
    for tenant, other in ((TENANTS[0], TENANTS[1]), (TENANTS[1], TENANTS[0])):
        status, _ = invoke(ports[other], keys[tenant])
        results.check(
            status == 401,
            f"{tenant}'s key -> {other}'s process: POST /invoke is refused with 401",
            f"got {status}",
        )
        status, body = invoke(ports[tenant], keys[tenant])
        output = body.get("output") if isinstance(body, dict) else None
        results.check(
            status == 200 and output == EXPECTED_OUTPUT,
            f"{tenant}'s key -> {tenant}'s process: POST /invoke answers 200 with the "
            "scripted gateway's answer",
            f"got {status}, output {'matches' if output == EXPECTED_OUTPUT else 'differs'}",
        )
    gateway = f"http://127.0.0.1:{published_port(GATEWAY_SERVICE, cwd)}"
    calls = gateway_calls(gateway)
    results.check(
        calls == dict.fromkeys(TENANTS, 1),
        "the scripted gateway answered one model call per tenant, each with that tenant's "
        "own gateway token; the refused calls reached no model",
        f"calls by token: {calls}",
    )
    assert_gateway_refuses(results, gateway)


def gateway_calls(gateway: str) -> dict[str, int] | None:
    """The scripted gateway's answered model calls by token label, or ``None``."""
    status, body = request("GET", gateway + GATEWAY_CALLS_PATH)
    calls = body.get("calls") if status == 200 and isinstance(body, dict) else None
    return calls if isinstance(calls, dict) else None


def assert_gateway_refuses(results: Results, gateway: str) -> None:
    """The gateway itself refuses a model call without a tenant's token, and counts none.

    One call carries a fresh random token, one no ``Authorization`` header; both must get
    ``401``, and the per-token call counts must be the same afterwards.
    """
    before = gateway_calls(gateway)
    probes = {"a random token": secrets.token_urlsafe(32), "no Authorization header": None}
    for description, token in probes.items():
        status, _ = request("POST", gateway + GATEWAY_CHAT_PATH, body=PROBE_CHAT, key=token)
        results.check(
            status == 401,
            f"the scripted gateway refuses a model call with {description} with 401",
            f"got {status}",
        )
    after = gateway_calls(gateway)
    results.check(
        before is not None and after == before,
        "the refused gateway calls changed no per-token call count",
        f"calls by token: {after}",
    )


def _print_logs(cwd: Path = ROOT) -> None:
    """The services' last log lines, for a failed run. Agent logs are JSON with no secret."""
    print("--- docker compose logs (tail) ---", flush=True)
    subprocess.run(  # noqa: S603 - fixed command, no shell
        ["docker", "compose", "--project-name", PROJECT, "logs", "--no-color", "--tail=40"],  # noqa: S607
        cwd=cwd,
        check=False,
    )


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m consumer_check.check",
        description="The Gate 1 run. With no option: wm-agents from the tag uv.lock pins.",
    )
    parser.add_argument(
        "--library",
        type=Path,
        metavar="CHECKOUT",
        help="a wm-agents checkout: build the wheel from it and constrain to its uv.lock",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="with --library: stop once .run/library is locked, before Docker",
    )
    options = parser.parse_args(argv)
    if options.prepare_only and options.library is None:
        parser.error("--prepare-only needs --library")
    return options


def run(argv: Sequence[str] | None = None) -> int:
    """The whole run; the exit code is 1 on any failure."""
    options = _arguments(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)  # keep the PASS lines in order
    if options.library is not None:
        return run_library(options.library, prepare_only=options.prepare_only)
    results = Results()
    started = False
    try:
        source = locked_source((ROOT / "uv.lock").read_text(encoding="utf-8"))
        print(f"wm-agents: {source.rev} at {source.commit[:12]} (from uv.lock)")
        keys = write_secrets(SECRETS_DIR, TENANTS)
        print(
            f"generated {len(keys)} inbound API keys and {len(TENANTS)} gateway tokens "
            "into .run/secrets (values not shown)"
        )
        wheel = build_wheelhouse(source)
        print(f"built {wheel.name} on the host into .run/wheelhouse")
        started = True
        compose("up", "--build", "--detach", "--wait", f"--wait-timeout={WAIT_SECONDS}")
        states = compose("ps", "--format", "{{.Service}}={{.Health}}").split()
        print("services up: " + ", ".join(sorted(states)))
        assert_isolation(results, keys)
    except CheckFailedError as error:
        results.check(False, str(error))
    finally:
        if started:
            if results.failures:
                _print_logs()
            try:
                compose("down", "-v", "--remove-orphans", quiet=True)
                print("torn down: docker compose down -v")
            except CheckFailedError as error:
                results.check(False, f"teardown: {error}")
        shutil.rmtree(SECRETS_DIR, ignore_errors=True)
    print(f"{'FAILED' if results.failures else 'PASSED'}: {results.failures} failure(s)")
    return 1 if results.failures else 0


def run_library(checkout: Path, *, prepare_only: bool = False) -> int:
    """The local-library mode: the same run, from ``LIBRARY_DIR`` against ``checkout``."""
    results = Results()
    secrets_dir = LIBRARY_DIR / ".run" / "secrets"
    started = False
    try:
        prepare_library(checkout)
        if prepare_only:
            print("PREPARED: .run/library is locked; nothing was started")
            return 0
        keys = write_secrets(secrets_dir, TENANTS)
        print(
            f"generated {len(keys)} inbound API keys and {len(TENANTS)} gateway tokens "
            "into .run/library/.run/secrets (values not shown)"
        )
        started = True
        compose(
            "up", "--build", "--detach", "--wait", f"--wait-timeout={WAIT_SECONDS}", cwd=LIBRARY_DIR
        )
        states = compose("ps", "--format", "{{.Service}}={{.Health}}", cwd=LIBRARY_DIR).split()
        print("services up: " + ", ".join(sorted(states)))
        assert_isolation(results, keys, LIBRARY_DIR)
    except CheckFailedError as error:
        results.check(False, str(error))
    finally:
        if started:
            if results.failures:
                _print_logs(LIBRARY_DIR)
            try:
                compose("down", "-v", "--remove-orphans", quiet=True, cwd=LIBRARY_DIR)
                print("torn down: docker compose down -v")
            except CheckFailedError as error:
                results.check(False, f"teardown: {error}")
        shutil.rmtree(secrets_dir, ignore_errors=True)
    print(f"{'FAILED' if results.failures else 'PASSED'}: {results.failures} failure(s)")
    return 1 if results.failures else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
