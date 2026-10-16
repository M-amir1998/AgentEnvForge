# Reference solution

`solution.patch` is the maintainers' fix for the LRU cache task, in `git apply` format. It
lives outside `project/` (the agent-visible workspace) and `grader/` (hidden test assets),
so AgentEnvForge never copies it into an agent's sandbox. The oracle agent
(`examples/agents/oracle/`) applies the same fix and exists only for infrastructure
validation.

The task has two bugs in `LRUCache`:

1. `get` does not mark the entry as recently used, so a just-read entry can be evicted;
2. `put` on an existing key evicts the least recently used entry even though no room is
   needed, and does not mark the updated entry as recently used.

The visible test exposes only bug 1. The hidden tests cover both, including a 500-step
comparison against a reference model, and they check only behaviour that the class
docstrings document.
