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

The check itself (the agent module, the two tenant configs, the Compose file and the run
script) arrives with WMA-191 (Gate 1). The wm-agents CI runs it on every library pull
request from WMA-197 on.

The wm-agents library repository is private, so installing from the tag needs read access to it.
