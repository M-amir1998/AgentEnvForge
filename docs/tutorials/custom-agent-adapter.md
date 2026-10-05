# Tutorial: connecting your own agent

There are two integration styles. Pick by *where your agent's code should run*.

## A. Command agents: your program runs inside the sandbox

Best for existing CLI agents and untrusted code, because the program is subject to every
sandbox limit.

```yaml
# my-agent.yaml
schema_version: "1"
type: command
name: my-solver
command: ["python", "/agent/main.py"]
files: my_agent_code        # directory next to this file, copied to /agent
environment:
  MODEL_TEMPERATURE: "0"
secrets: [MY_PROVIDER_API_KEY]   # read from your shell, redacted from all records
```

Inside the sandbox the program starts in the workspace and gets `AGENTENV_RUN_ID`,
`AGENTENV_TASK_ID`, `AGENTENV_WORKSPACE` and `AGENTENV_INSTRUCTIONS_FILE`. Exit code 0
means "done". Non-zero is recorded as a failed agent outcome, and the workspace is still graded.

An agent that calls a remote model needs `network.enabled: true` in the task. That is a
deliberate, visible opt-in.

## B. Python adapters: your code runs on the host

Best for LLM tool-use loops, where the model call happens in your process and actions go
to the sandbox:

```python
# my_package/agent.py
from agentenvforge.agents.base import AgentContext
from agentenvforge.domain.results import AgentOutcome, AgentResult


class LoopAgent:
    def __init__(self, max_steps: int = 20) -> None:
        self.max_steps = max_steps

    @property
    def name(self) -> str:
        return "loop-agent"

    def run(self, context: AgentContext) -> AgentResult:
        session = context.session
        listing = session.run(["ls", "-R"])  # argv, never a shell string
        source = session.read_file("ledger/store.py")  # workspace-relative
        # ... ask your model what to do, apply its edits ...
        session.write_file("ledger/store.py", source)
        tests = session.run(["python", "-m", "pytest", "-q"])
        context.save_artifact("agent/transcript.txt", listing.stdout.encode())
        return AgentResult(
            adapter=self.name,
            outcome=AgentOutcome.COMPLETED if tests.exit_code == 0 else AgentOutcome.FAILED,
            command_count=session.command_count,
        )


def build(max_steps: int = 20) -> LoopAgent:
    return LoopAgent(max_steps)
```

```yaml
type: python
name: loop-agent
adapter: my_package.agent:build
options: {max_steps: 30}
```

Rules the session enforces for you: each command's timeout is clipped to the remaining
time budget, nothing runs after the budget is spent, and every command is recorded as
an event. After a timeout the session is revoked and the sandbox destroyed.

Raise `agentenvforge.domain.failures.AgentError` only when the agent could not run at
all; normal "gave up" outcomes should be returned as `AgentOutcome.FAILED`.

The core never imports a provider SDK. Keep yours in your own package.
