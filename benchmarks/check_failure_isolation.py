"""Check the failure-isolation benchmark's results against the known expected outcomes.

    python benchmarks/check_failure_isolation.py benchmarks/results/failure-isolation

Exits 0 when every planned run ended with its agent's expected outcome, every run's cleanup
succeeded, the experiment completed, and (if Docker is reachable) no container of any of
its runs is left; otherwise prints each deviation and exits 1.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

EXPECTED = {
    "oracle": "passed",
    "noop": "failed",
    "failing": "failed",
    "sleeper": "agent_timeout",
    "crasher": "agent_error",
}
RUN_ID_LABEL = "io.agentenvforge.run-id"


def leftover_containers(run_ids: list[str]) -> list[str] | None:
    """Names of containers still present for these runs; ``None`` without Docker."""
    try:
        import docker  # noqa: PLC0415 - optional at check time
        from docker.errors import DockerException  # noqa: PLC0415
    except ImportError:
        return None
    try:
        client = docker.from_env()
        return [
            str(container.name)
            for run_id in run_ids
            for container in client.containers.list(
                all=True, filters={"label": f"{RUN_ID_LABEL}={run_id}"}
            )
        ]
    except DockerException:
        return None


def main(directory: Path) -> int:
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    with (directory / "runs.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    problems = []
    if summary["status"] != "completed":
        problems.append(f"experiment status is {summary['status']}, expected completed")
    observed: Counter[tuple[str, str]] = Counter()
    for row in rows:
        observed[(row["agent_id"], row["outcome"])] += 1
        expected = EXPECTED.get(row["agent_id"])
        if row["outcome"] != expected:
            problems.append(
                f"run {row['run_id']} ({row['agent_id']} on {row['task_id']}): "
                f"{row['outcome']}, expected {expected}"
            )
        if row["cleanup_ok"] != "True":
            problems.append(f"run {row['run_id']}: cleanup did not succeed")

    leftovers = leftover_containers([row["run_id"] for row in rows if row["run_id"]])
    if leftovers:
        problems.append(f"containers left behind: {', '.join(leftovers)}")

    for (agent, outcome), count in sorted(observed.items()):
        print(f"{agent:<10} {outcome:<15} {count}")
    checked = "not checked (no Docker)" if leftovers is None else str(len(leftovers))
    print(f"{len(rows)} planned runs; leftover containers: {checked}")
    for problem in problems:
        print(f"FAIL {problem}")
    print("PASS" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    DEFAULT = "benchmarks/results/failure-isolation"
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)))
