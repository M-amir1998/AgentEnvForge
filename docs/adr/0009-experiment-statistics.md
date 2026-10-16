# ADR-0009: How experiment results are counted and summarised

- Status: accepted
- Date: 2026-10-16

## Context

Agent evaluations on a laptop have small samples (a few tasks, a handful of repetitions),
binary outcomes, agents that are sometimes fully deterministic, runs that fail for reasons
unrelated to the agent, and time budgets that cut runs short. Common reporting habits mislead
in exactly this regime: a bare "67%" from 2/3 runs, a normal-approximation interval of
[13%, 100%] that leaves the unit interval, pass@k computed from repetitions that were all
identical, or an infrastructure crash counted as an agent failure.

## Decision

**Outcomes.** Every planned run gets exactly one outcome. Whether it counts towards an
agent's success rate depends on *when* it ended, not on how:

| Outcome | Scored | Meaning |
|---|---|---|
| `passed`, `failed` | yes | graded |
| `agent_timeout` | yes | the agent used its whole time budget |
| `agent_error` | yes | the agent (or its adapter) failed |
| `solution_timeout` | yes | grading the agent's solution exceeded the grader budget |
| `post_agent_error` | yes | after the agent ran, the solution could not be captured or graded |
| `infrastructure_error` | no | failed before the agent started (build, source, sandbox start) |
| `agent_unavailable` | no | the model server was unreachable or returned 5xx/429 |
| `cancelled` | no | the user (or the circuit breaker) stopped the run |
| `not_started`, `incomplete` | no | never started / started but recorded no result |

An outcome that is not scored is reported (counts, failure breakdown, validity warnings)
but never folded into a success rate.

**Intervals.** Success rates carry a 95% Wilson score interval, with the exact bounds 0
and 1 at 0/n and n/n. Wilson stays within [0, 1] and has close-to-nominal coverage at small
n, unlike the normal approximation, and is less conservative than Clopper-Pearson.
Intervals are computed per agent x task cell and per agent pooled over tasks. A pooled rate
is conditional on this experiment's task mix, so the unweighted mean of the per-task rates
is reported next to it. Task rates pool subject agents only.

**Identical repetitions.** When all scored repetitions of a cell have the same outcome *and*
the same solution patch (SHA-256), the cell is marked identical: the repetitions are
effectively one observation, so no interval is shown. An agent whose repeated cells are all
identical is marked deterministic.

**pass@k** uses the unbiased estimator of Chen et al. (2021),
`1 - C(n - c, k) / C(n, k)` per task, averaged over tasks, with k = the number of
repetitions. It is reported only when every task has k scored runs and the agent is not
deterministic (for a deterministic agent, pass@k equals pass@1). With n = k, the estimator
is the fraction of tasks solved in at least one of the k attempts.

**Durations.** Mean, median, minimum and maximum of scored runs. The 95th percentile
(linear interpolation, "type 7") is reported only from 20 runs on. Runs that hit their time
budget are counted as censored: the true duration is at least the budget, so the mean and
upper percentiles are lower bounds and are marked `>=`.

**Validity.** `results_valid` is false, with a warning, when the experiment did not
complete, a planned run did not start or recorded no result, a run failed for reasons
outside the agent's control (`infrastructure_error`, `agent_unavailable`), a reference agent
failed any run, a baseline agent passed any run, or a count invariant is violated (for
example `planned = sum of outcomes`). Reference and baseline
agents validate tasks; they are kept out of every comparison between subjects.

**No rankings.** Reports never name a "best" agent or apply significance tests. With samples
this small, overlapping intervals do not support an ordering. The intervals and sample sizes
are shown, and the reader can judge.

## Consequences

- Every number in `summary.json` and `report.md` carries its sample size; a statistic that
  cannot be computed honestly is `null` with a note saying why.
- The same persisted facts always give the same summary. `agentenv experiment report`
  recomputes it from the database at any time.

## Limitations

- Repetitions of a cell are treated as independent. Seeds, caching and server state can
  correlate them.
- The interval of a pooled rate treats runs as exchangeable across tasks; task difficulty
  varies, so the interval is optimistic for "this agent on tasks like these".
- No multiple-comparison correction, effect sizes or paired tests between agents.
