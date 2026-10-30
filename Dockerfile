# The consumer check's one image: Python 3.13 with wm-agents v0.2.0 and this repo's package.
# Both agent containers (tenant-a, tenant-b) and the scripted gateway run from it.
#
# The build needs no credential. wm-agents is a private repository, so it is never fetched
# here: the run script (consumer_check.check) builds the wm-agents wheel on the host, from the
# commit uv.lock pins, into the gitignored .run/wheelhouse, and Compose passes that folder as
# the named build context `wheelhouse`. Everything else comes from PyPI, by the hashes
# uv.lock records.
#
# Base image: uv 0.9 on Python 3.13, Debian bookworm-slim, pinned by digest (the same digest
# as wm-agents' agent base image); the tag is kept for readability only.
FROM ghcr.io/astral-sh/uv:0.9-python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS uv

# Stage 1: the exact third-party pins, with hashes, exported from the committed lock.
# `--frozen` reads uv.lock as it is and never resolves or fetches. wm-agents itself and this
# project are left out: both are installed from wheels below.
FROM uv AS requirements
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN UV_NO_CACHE=1 uv export --frozen --no-dev --no-emit-project \
    --no-emit-package wm-agents --output-file /requirements.txt >/dev/null

# Stage 2: the image.
FROM uv
WORKDIR /app

COPY --from=requirements /requirements.txt /opt/consumer/requirements.txt
RUN UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never uv venv /app/.venv \
 && UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
    uv pip install --python /app/.venv --no-deps --require-hashes \
    --requirement /opt/consumer/requirements.txt

# wm-agents, from the wheel the host built at the locked commit; its dependencies are above.
COPY --from=wheelhouse wm_agents-*.whl /tmp/wheelhouse/
RUN UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
    uv pip install --python /app/.venv --no-deps /tmp/wheelhouse/wm_agents-*.whl \
 && rm -rf /tmp/wheelhouse

# This repo's package (the agent module and the scripted gateway), not editable.
COPY pyproject.toml README.md ./
COPY src src
RUN UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
    uv pip install --python /app/.venv --no-deps . \
 && rm -rf /app/src /app/pyproject.toml /app/README.md

# The synthetic configs: the approved-models list and the two tenants.
COPY config/approved-models.yaml /opt/consumer/approved-models.yaml
COPY tenants /opt/consumer/tenants

# Non-root runtime user; everything above stays owned by root.
RUN useradd --create-home --uid 10001 --user-group agent

ENV PATH="/app/.venv/bin:${PATH}"
USER agent

# Clear uv's inherited entrypoint; Compose sets each service's command.
ENTRYPOINT []
CMD ["python", "-m", "consumer_check.agent"]
