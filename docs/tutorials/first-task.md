# Tutorial: your first task

You will build a task where an agent must fix a bug, grade it with hidden tests, and run
it. The finished version of this layout is `examples/basic/`.

## 1. Lay out the directory

```text
my-task/
├── task.yaml          the specification
├── project/           what the agent receives (becomes /workspace)
├── environment/
│   └── Dockerfile     the sandbox image
└── grader/            hidden tests (only in the grading sandbox)
```

Keep anything the agent must not see, such as hidden tests or reference solutions,
**outside** `project/`. Validation rejects `grading.assets` placed inside the source.

## 2. Write the environment

```dockerfile
FROM python:3.12-slim@sha256:<digest>   # pin by digest for reproducibility
RUN pip install "pytest==8.3.5"          # everything graders need belongs in the image
```

Find a digest with `docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'`.
The image needs `sleep` and GNU `timeout`; Debian-based images have both.

## 3. Write `task.yaml`

```yaml
schema_version: "1"
id: my-first-task                 # lowercase, unique
name: Fix the parser
instructions: |                   # this is what the agent reads
  `parse_duration("1h30m")` returns the wrong value. Fix it without changing the API.

source:
  type: local
  path: project

environment:
  dockerfile: environment/Dockerfile

timeouts:
  agent_seconds: 600

grading:
  assets: grader
  protected_paths: [tests]        # agents editing tests gains nothing
  graders:
    - type: pytest
      name: hidden
      paths: [/grader]
```

Every other field has a conservative default: 1 CPU, 2 GiB memory, 512 processes,
network disabled, user `1000:1000`. See them all with `agentenv task inspect`, or in
`schemas/task-v1.schema.json` (point your editor's YAML extension at it for completion).

## 4. Validate

```bash
uv run agentenv task validate my-task/task.yaml
```

Mistakes are reported with the field, the reason and the received value, for example:

```text
field:    resources.memory_mb
reason:   Input should be greater than or equal to 128
received: -20
```

## 5. Check the task discriminates

```bash
uv run agentenv run my-task/task.yaml --agent noop          # must FAIL (exit 1)
uv run agentenv run my-task/task.yaml --agent manual        # fix it yourself in a shell
```

If the noop baseline passes, your hidden tests do not test the bug. With `manual`,
AgentEnvForge prints a `docker exec -it ... sh` command, waits while you work, then grades.

## 6. Run a real agent

```bash
uv run agentenv run my-task/task.yaml --agent-command "python /agent/solve.py"   # inline
uv run agentenv run my-task/task.yaml --agent path/to/agent.yaml                  # spec file
```

See [custom agent adapters](custom-agent-adapter.md).
