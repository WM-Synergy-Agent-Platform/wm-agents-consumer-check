"""The consumer agent: defined, built and served through ``wm_agents`` alone.

This is the one agent module of the consumer check. It imports ``wm_agents`` and, for its
own output schema, ``pydantic``; never ``agent_core``. One image runs it as two processes,
one per tenant: each reads its own runtime settings (the plan's shared contract), and
``wm_agents.deploy.main`` builds the runtime and serves ``GET /health`` and
``POST /invoke`` on ``PORT`` (8080 unset).

    python -m consumer_check.agent
"""

from __future__ import annotations

from pydantic import BaseModel
from wm_agents import AgentDefinition, deploy


class Answer(BaseModel):
    """The consumer agent's output schema."""

    answer: str


DEFINITION = AgentDefinition(
    base_instructions="You are the consumer-check agent. Answer in one short sentence.",
    output_type=Answer,
)


def main() -> int:
    """Read this process's runtime settings, build the runtime and serve it."""
    return deploy.main(DEFINITION)


if __name__ == "__main__":
    raise SystemExit(main())
