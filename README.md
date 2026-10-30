# wm-agents-consumer-check

This repository exists only to prove the public API of the
[wm-agents](https://github.com/WM-Synergy-Agent-Platform/wm-agents) library from outside the
monorepo. It is rebuilt on every library tag. It holds no real client workflow, no secret and
only synthetic data.

It installs the library the way any consumer does, from a git tag:

```
wm-agents @ git+https://github.com/WM-Synergy-Agent-Platform/wm-agents@v0.2.0#subdirectory=packages/agent-core
```

and imports only `wm_agents`, never `agent_core`. It serves one agent as two tenant processes
(`tenant-a`, `tenant-b`) and checks that each tenant's key is refused by the other tenant's
process.

The wm-agents CI runs the check on every library pull request from WMA-197 on.

## What is here

| Path | Holds |
|---|---|
| `pyproject.toml`, `uv.lock` | The one dependency, the `v0.2.0` tag above, and its lock |
| `constraints/wm-agents-lock.json` | wm-agents' own `uv.lock` versions at that tag, which `pyproject.toml` constrains this repo to |
| `src/consumer_check/agent.py` | The agent module: imports only `wm_agents` (and `pydantic` for its output schema) |
| `tenants/tenant-a/`, `tenants/tenant-b/` | The two synthetic tenant configs, each with the same agent |
| `config/approved-models.yaml` | The synthetic approved-models list: one model, answered by the scripted gateway |
| `src/consumer_check/scripted_gateway.py` | The scripted model gateway (standard library only); no model is ever called |
| `Dockerfile`, `compose.yaml` | One image; two agent containers and the scripted gateway |
| `src/consumer_check/check.py` | The run script |

## Running the check

Needs Docker with Compose, uv, and git read access to the wm-agents repository.

```bash
uv sync --locked
uv run python -m consumer_check.check
```

The script:

1. generates one inbound API key and one gateway token per tenant into the gitignored
   `.run/secrets` (never printed, deleted when the run ends);
2. builds the wm-agents wheel on the host, from the commit `uv.lock` pins, into the
   gitignored `.run/wheelhouse`, so the Docker build needs no credential;
3. starts the Compose project `wma191` (both agent processes and the scripted gateway, every
   published port on `127.0.0.1`) and waits until all three are healthy;
4. checks, on `POST /invoke`, that each tenant's key is refused with `401` by the other
   tenant's process and answered with `200` and the scripted gateway's answer by its own,
   that the scripted gateway answered one model call per tenant, each with that tenant's
   own gateway token, and that the gateway refuses a model call with a random token or with
   none (`401`) without counting it;
5. runs `docker compose down -v`, whatever happened, and exits non-zero on any failure.

Each agent container gets only settings from the wm-agents shared contract:
`WM_TENANT_FILE`, `WM_APPROVED_MODELS_FILE`, `WM_AGENT_NAME`, `WM_API_KEYS_FILE`,
`WM_GATEWAY_URL`, `WM_GATEWAY_CREDENTIAL` (`local-token`) and `WM_GATEWAY_TOKEN_FILE`.
`OTEL_EXPORTER_OTLP_ENDPOINT` is unset, so spans are made and exported nowhere.

The repo's own checks:

```bash
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run mypy --strict .
uv run pytest
```

## The same lock as wm-agents

This repo resolves to the third-party versions in wm-agents' own `uv.lock` at the tag it
installs, so the check proves the library's known-good set
(`kb/decisions/2026-10-26-library-dependency-ranges-and-git-tag-releases.md` in wm-agents).
`python -m consumer_check.sync_constraints` reads that lock from a local wm-agents checkout
(`git show <tag>:uv.lock`, changing nothing there), writes the snapshot
`constraints/wm-agents-lock.json` and the generated `[tool.uv] constraint-dependencies`
block in `pyproject.toml`. `tests/test_constraints.py` checks that every package in both
locks has the same version, against the committed snapshot, so it needs no wm-agents access.

## On a new library tag

1. Change the tag in `pyproject.toml`, the tag and commit in `tests/test_check.py`, and the tag in
   `tests/test_constraints.py`.
2. `uv run python -m consumer_check.sync_constraints --wm-agents <path to a wm-agents checkout>`
3. `uv lock`, then the repo's checks and the run above.

The wm-agents library repository is private, so installing from the tag needs read access to it.
