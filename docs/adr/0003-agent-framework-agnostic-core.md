# ADR-0003: An agent-framework-agnostic core with a small adapter protocol

- Status: accepted
- Date: 2026-10-04

## Context

The project must evaluate agents built on any LLM provider or framework (or none), and
compare them fairly. Coupling the core to one SDK would bias the infrastructure, add
heavy dependencies and make tests require paid API keys.

## Options considered

1. **Built-in LLM loop** with provider plugins: convenient, but it turns infrastructure into
   an agent framework and every provider change becomes our maintenance burden.
2. **Process protocol only** (the agent is always an external program): very decoupled,
   but host-side agents (an LLM tool-use loop) would have to re-implement sandbox access.
3. **A small Python protocol plus a generic command adapter.**

## Decision

Option 3. `AgentAdapter` (`agentenvforge.agents.base`) is the public extension point:

```python
class AgentAdapter(Protocol):
    @property
    def name(self) -> str: ...
    def run(self, context: AgentContext) -> AgentResult: ...
```

Adapters act only through `context.session` (`SandboxSession`): `run(argv)`,
`read_file`, `write_file`, `upload_directory`. Built-ins: `command` (runs a program
*inside* the sandbox), `manual` (a human), `noop` (baseline). Arbitrary host-side
adapters are loaded from agent spec files with `type: python, adapter: module:callable`.

Tasks never name an agent; agents are chosen per run.

The interface is **synchronous**. Concurrency will come from running several runs in
parallel (one thread or process per run), not from async inside a run. Async would add
complexity to every adapter for no benefit at the granularity of one run.

## Consequences

- The core has no LLM dependency, and the whole test suite runs offline.
- Every agent action is observable and bounded by the run's deadline, because the session
  is the only path to the sandbox (a timed-out adapter's session is revoked).
- Secrets for agents are passed explicitly (`secrets:` in the agent spec) and redacted.

## Limitations

In-process adapters cannot be forcibly stopped (Python threads cannot be killed). On
timeout the orchestrator revokes the session and destroys the sandbox, so the abandoned
thread can no longer act, but it may keep consuming host CPU until it returns. Untrusted
adapters should be `command` agents, which run inside the sandbox.
