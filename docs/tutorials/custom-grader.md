# Tutorial: custom grading

## Any checker, today: the `command` grader

A command grader runs any program inside the grading sandbox; the exit code decides.
Put the checker in the hidden assets so the agent never sees it:

```text
my-task/grader/
├── check_api.py        # your checker
└── tests/              # optional hidden pytest tests
```

```yaml
grading:
  assets: grader
  protected_paths: [tests, pyproject.toml]
  aggregation: weighted
  pass_threshold: 0.8
  graders:
    - type: pytest
      name: hidden-tests
      paths: [/grader/tests]
      weight: 3
    - type: command
      name: api-compat
      command: ["python", "/grader/check_api.py"]
      success_exit_codes: [0]
      weight: 1
```

This gives score = (3 × pytest pass fraction + 1 × api-compat) / 4, and passes at 0.8 or
above. With `aggregation: all`, every grader must pass.

Guidelines:

- Exit non-zero when the *solution is wrong*. That is a failed grade, not an error.
- Make the checker deterministic; the grading sandbox has no network unless the task enables it.
- Print diagnostics: stdout/stderr are saved to `grader/<name>/`.

## Python graders (library use)

`agentenvforge.grading.base.Grader` is a public protocol (`name`, `grader_type`,
`grade(context) -> GradeResult`). In v0.1 task files can only select the built-in grader
types. Loading host Python code named by a task file would let an untrusted task execute
code on the host, so registration of custom Python graders will come with an explicit,
host-side plugin mechanism (roadmap v0.3). If you need a new built-in type, open a
"New grader" issue.
