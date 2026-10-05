# Reference solution

`solution.patch` is the maintainers' fix for the ledger task, in `git apply` format. It
lives outside both `project/` (the agent-visible workspace) and `grader/` (hidden test
assets), so AgentEnvForge never copies it into any sandbox.

The task has three bugs, all in `Store.transaction()`:

1. the snapshot is a shallow copy, so in-place mutation of a stored list survives a
   rollback;
2. a rollback restores the *outermost* snapshot instead of the current transaction's;
3. the exception is swallowed instead of propagating.

The visible test only exposes bug 3; the hidden tests cover all three. A partial fix
therefore passes the visible tests but fails grading, which is the point.

Check the patch against a clean copy of the project:

```bash
cp -r examples/basic/project /tmp/ledger && cd /tmp/ledger
git init -q && git apply --check ../path/to/solution.patch
```
