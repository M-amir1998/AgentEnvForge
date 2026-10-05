# Trade-offs

The main trade-offs made in v0.1. Larger decisions have full ADRs.

| Choice | Benefit | Cost | Revisit when |
|---|---|---|---|
| Archive copy instead of bind mounts | no host exposure; remote daemons work; Windows/macOS friendly | O(size) copy per stage | workspaces of several GB |
| Separate grading container | agent cannot influence the grading environment | one extra container start (~0.4 s) | never for correctness; optimise start instead |
| Diff on the host in Python | no git in images; agent cannot fake the diff | `difflib` is slower than git for huge files | repositories with very large text files |
| Synchronous adapters + threads | simple adapter code; easy to reason about | timed-out in-process adapters cannot be killed | if untrusted in-process adapters become common (use processes) |
| stdlib `sqlite3` | no ORM dependency | a second backend needs hand-written SQL | adding PostgreSQL |
| Pydantic in the domain layer | validation and JSON round-trips for free | domain depends on a library | unlikely |
| No disk limit | portable across storage drivers | disk exhaustion possible | storage-driver-specific quotas or tmpfs workspaces |
| Setup commands only in the agent sandbox | simple, grader environment is the pristine image | grader dependencies must be in the image | tasks needing per-run grader setup |
| No automatic retries | failures stay visible | flaky infrastructure needs manual reruns | batch execution (retries for infra reasons only) |
| `--agent` required (no default) | explicit about what is evaluated | one more flag in the quick start | never |
