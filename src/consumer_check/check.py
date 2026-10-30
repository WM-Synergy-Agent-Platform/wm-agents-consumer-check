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
   round; and the scripted gateway answered exactly one model call per tenant, each with
   that tenant's own gateway token.
5. Tears the project down (``docker compose down -v``) whatever happened, and exits
   non-zero on any failure.

Standard library only.
"""

from __future__ import annotations

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

from consumer_check.scripted_gateway import ANSWER, CALLS_PATH

ROOT: Final = Path(__file__).resolve().parents[2]
RUN_DIR: Final = ROOT / ".run"
SECRETS_DIR: Final = RUN_DIR / "secrets"
WHEELHOUSE: Final = RUN_DIR / "wheelhouse"
CLONE_DIR: Final = RUN_DIR / "build" / "wm-agents"
PROJECT: Final = "wma191"
TENANTS: Final = ("tenant-a", "tenant-b")
AGENT_SERVICES: Final = {"tenant-a": "agent-tenant-a", "tenant-b": "agent-tenant-b"}
GATEWAY_SERVICE: Final = "scripted-gateway"
#: The caller name in each tenant's API keys file.
CALLER: Final = "consumer-check"
WAIT_SECONDS: Final = 180
HTTP_TIMEOUT_SECONDS: Final = 30
EXPECTED_OUTPUT: Final = {"answer": ANSWER}


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


def _run(command: Sequence[str], *, quiet: bool = False) -> str:
    """Run ``command`` in the repo root; return its standard output, or raise naming it."""
    result = subprocess.run(  # noqa: S603 - fixed commands, no shell
        list(command),
        cwd=ROOT,
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


def compose(*args: str, quiet: bool = False) -> str:
    return _run(["docker", "compose", "--project-name", PROJECT, *args], quiet=quiet)


def published_port(service: str) -> int:
    """The 127.0.0.1 port Docker published for ``service``'s 8080."""
    address = compose("port", service, "8080").strip()
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


def assert_isolation(results: Results, keys: Mapping[str, str]) -> None:
    """The cross-tenant 401s, the same-tenant 200s and the gateway's call counts."""
    ports = {tenant: published_port(service) for tenant, service in AGENT_SERVICES.items()}
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
    gateway = published_port(GATEWAY_SERVICE)
    status, body = request("GET", f"http://127.0.0.1:{gateway}{CALLS_PATH}")
    calls = body.get("calls") if isinstance(body, dict) else None
    results.check(
        status == 200 and calls == dict.fromkeys(TENANTS, 1),
        "the scripted gateway answered one model call per tenant, each with that tenant's "
        "own gateway token; the refused calls reached no model",
        f"calls by token: {calls}",
    )


def _print_logs() -> None:
    """The services' last log lines, for a failed run. Agent logs are JSON with no secret."""
    print("--- docker compose logs (tail) ---", flush=True)
    subprocess.run(  # noqa: S603 - fixed command, no shell
        ["docker", "compose", "--project-name", PROJECT, "logs", "--no-color", "--tail=40"],  # noqa: S607
        cwd=ROOT,
        check=False,
    )


def run() -> int:
    """The whole run; the exit code is 1 on any failure."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)  # keep the PASS lines in order
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


if __name__ == "__main__":
    sys.exit(run())
