"""Pin the consumer to wm-agents' own lock at the tag it installs (review P2-2).

    uv run python -m consumer_check.sync_constraints --wm-agents ~/dev/wm-agents
    uv lock

It reads the tag from this repo's wm-agents dependency, reads ``uv.lock`` at that tag from a
local wm-agents checkout (``git show <tag>:uv.lock``; nothing in that checkout changes), and
writes:

- ``constraints/wm-agents-lock.json``: the tag, its commit and every third-party package's
  version in that lock (registry packages only; wm-agents' own workspace members are left
  out). ``tests/test_constraints.py`` checks this repo's ``uv.lock`` against it;
- the generated ``[tool.uv] constraint-dependencies`` block in ``pyproject.toml``: one
  ``name==version`` per package, so ``uv lock`` resolves to the same versions.

On a new library tag: change the tag in ``pyproject.toml``, run this, then ``uv lock``.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]
PYPROJECT: Final = ROOT / "pyproject.toml"
SNAPSHOT: Final = ROOT / "constraints" / "wm-agents-lock.json"
BEGIN: Final = "# BEGIN generated constraint-dependencies"
END: Final = "# END generated constraint-dependencies"
_TAG: Final = re.compile(r"^wm-agents @ git\+https://\S+@(v[0-9][^#\s]*)#subdirectory=\S+$")


def registry_versions(lock_text: str) -> dict[str, str]:
    """Each registry package's version in a uv.lock, by name, sorted by name."""
    lock = tomllib.loads(lock_text)
    versions: dict[str, str] = {}
    for package in lock.get("package", []):
        if "registry" not in package.get("source", {}):
            continue
        name, version = package["name"], package["version"]
        if versions.get(name, version) != version:
            raise ValueError(f"the lock holds two versions of {name}; constrain it by hand")
        versions[name] = version
    return dict(sorted(versions.items()))


def tag_of(dependencies: Sequence[str]) -> str:
    """The wm-agents tag this repo installs, from its dependency list."""
    tags = [match.group(1) for dep in dependencies if (match := _TAG.match(dep))]
    if len(tags) != 1:
        raise ValueError("expected exactly one wm-agents git dependency on a v* tag")
    return tags[0]


def constraint_block(versions: Mapping[str, str], *, tag: str, commit: str) -> str:
    """The generated ``[tool.uv]`` table, between its markers."""
    lines = [
        BEGIN,
        f"# by `python -m consumer_check.sync_constraints` from wm-agents' uv.lock at {tag}",
        f"# ({commit}). Don't edit by hand.",
        "[tool.uv]",
        "constraint-dependencies = [",
        *(f'    "{name}=={version}",' for name, version in versions.items()),
        "]",
        END,
    ]
    return "\n".join(lines) + "\n"


def replace_block(text: str, block: str) -> str:
    """``text`` with its generated block replaced by ``block``, or ``block`` appended."""
    start, end = text.find(BEGIN), text.find(END)
    if start == -1 and end == -1:
        return text.rstrip("\n") + "\n\n" + block
    if start == -1 or end < start:
        raise ValueError("pyproject.toml's generated block markers are broken")
    after = text.find("\n", end)
    return text[:start] + block + ("" if after == -1 else text[after + 1 :])


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed command, no shell
        ["git", "-C", str(checkout), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pin the consumer to the wm-agents lock at its tag."
    )
    parser.add_argument("--wm-agents", type=Path, required=True, help="a wm-agents checkout")
    args = parser.parse_args(argv)
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    tag = tag_of(manifest["project"]["dependencies"])
    try:
        commit = _git(args.wm_agents, "rev-parse", f"{tag}^{{commit}}").strip()
        versions = registry_versions(_git(args.wm_agents, "show", f"{tag}:uv.lock"))
    except (subprocess.CalledProcessError, ValueError) as error:
        print(f"sync_constraints: {error}", file=sys.stderr)
        return 1
    snapshot = {"tag": tag, "commit": commit, "source": "uv.lock", "packages": versions}
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    block = constraint_block(versions, tag=tag, commit=commit)
    PYPROJECT.write_text(replace_block(PYPROJECT.read_text(encoding="utf-8"), block))
    print(f"wm-agents {tag} ({commit[:12]}): {len(versions)} constraints; now run `uv lock`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
