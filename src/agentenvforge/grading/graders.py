"""Built-in graders and their construction from a task's ``grading`` section."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from agentenvforge.domain.clock import elapsed_ms, monotonic_ms
from agentenvforge.domain.commands import CommandExecution
from agentenvforge.domain.failures import GraderError, GraderTimeoutError, SandboxPathNotFoundError
from agentenvforge.domain.results import GradeResult
from agentenvforge.grading.base import Grader, GradingContext
from agentenvforge.grading.junit import parse_junit_counts
from agentenvforge.tasks.schema import CommandGraderSpec, GradingSpec, PytestGraderSpec

_REPORT_DIR = PurePosixPath("/tmp/agentenv-grader")  # noqa: S108 - inside the sandbox
_MAX_REPORT_BYTES = 20 * 1024 * 1024

# pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html)
_PYTEST_TESTS_FAILED = 1
_PYTEST_INTERRUPTED = 2
_PYTEST_NO_TESTS = 5


def _save_output(context: GradingContext, name: str, execution: CommandExecution) -> None:
    context.save_artifact(f"grader/{name}/stdout.log", execution.stdout.encode("utf-8"))
    context.save_artifact(f"grader/{name}/stderr.log", execution.stderr.encode("utf-8"))


def _raise_if_timed_out(name: str, execution: CommandExecution) -> None:
    if execution.timed_out:
        raise GraderTimeoutError(
            f"grader {name!r} exceeded its time limit",
            details={"grader": name, "duration_ms": execution.duration_ms},
        )


@dataclass(frozen=True, slots=True)
class PytestGrader:
    """Runs pytest and scores the fraction of executed tests that passed.

    A failing or erroring test (including collection errors caused by a broken solution)
    yields ``passed=False``. Only situations where pytest could not judge at all -
    internal errors, usage errors, no tests collected, missing report - are grader errors.
    """

    name: str
    paths: tuple[str, ...]
    args: tuple[str, ...] = ()
    python: str = "python"

    @property
    def grader_type(self) -> str:
        """``pytest``."""
        return "pytest"

    def grade(self, context: GradingContext) -> GradeResult:
        """Run pytest with a JUnit report and convert the report into a grade."""
        started = monotonic_ms()
        report = _REPORT_DIR / self.name / "junit.xml"
        argv = [
            self.python, "-m", "pytest", *self.paths, *self.args,
            "-p", "no:cacheprovider", f"--junitxml={report}", "-o", "junit_family=xunit2",
        ]  # fmt: skip
        execution = context.session.run(
            argv, cwd=context.workspace, env={"PYTHONDONTWRITEBYTECODE": "1"}
        )
        _save_output(context, self.name, execution)
        _raise_if_timed_out(self.name, execution)

        exit_code = execution.exit_code
        if exit_code == _PYTEST_NO_TESTS:
            raise GraderError(f"pytest grader {self.name!r} collected no tests; check its paths")
        if exit_code not in {0, _PYTEST_TESTS_FAILED, _PYTEST_INTERRUPTED}:
            raise GraderError(
                f"pytest grader {self.name!r} could not run (exit code {exit_code}): "
                f"{execution.stderr.strip()[-500:] or execution.stdout.strip()[-500:]}",
                details={"exit_code": exit_code},
            )
        try:
            document = context.session.read_file(report, max_bytes=_MAX_REPORT_BYTES)
        except SandboxPathNotFoundError as error:
            raise GraderError(f"pytest grader {self.name!r} produced no JUnit report") from error
        context.save_artifact(f"grader/{self.name}/junit.xml", document)

        counts = parse_junit_counts(document)
        score = counts.passed / counts.executed if counts.executed else 0.0
        passed = exit_code == 0 and counts.failures == 0 and counts.errors == 0
        return GradeResult(
            grader=self.name,
            grader_type=self.grader_type,
            passed=passed and counts.executed > 0,
            score=round(score, 6),
            tests_passed=counts.passed,
            tests_failed=counts.failures,
            tests_errored=counts.errors,
            tests_skipped=counts.skipped,
            duration_ms=elapsed_ms(started),
            summary=f"{counts.passed}/{counts.executed} tests passed",
            details={"exit_code": exit_code},
        )


@dataclass(frozen=True, slots=True)
class CommandGrader:
    """Runs a command; the solution passes when the exit status is a success code."""

    name: str
    command: tuple[str, ...]
    success_exit_codes: tuple[int, ...] = (0,)

    @property
    def grader_type(self) -> str:
        """``command``."""
        return "command"

    def grade(self, context: GradingContext) -> GradeResult:
        """Run the command in the workspace and grade by exit status."""
        execution = context.session.run(self.command, cwd=context.workspace)
        _save_output(context, self.name, execution)
        _raise_if_timed_out(self.name, execution)
        passed = execution.exit_code in self.success_exit_codes
        return GradeResult(
            grader=self.name,
            grader_type=self.grader_type,
            passed=passed,
            score=1.0 if passed else 0.0,
            duration_ms=execution.duration_ms,
            summary=f"exit code {execution.exit_code}",
            details={"exit_code": execution.exit_code},
        )


@dataclass(frozen=True, slots=True)
class WeightedGrader:
    """A grader together with its weight in an aggregate score."""

    grader: Grader
    weight: float


@dataclass(frozen=True, slots=True)
class CompositeGrader:
    """Runs several graders and aggregates them.

    ``all``: passes only if every component passes. ``weighted``: passes when the
    weighted mean score reaches ``pass_threshold``. The score is always the weighted mean.
    """

    components: tuple[WeightedGrader, ...]
    aggregation: str = "all"
    pass_threshold: float = 1.0
    name: str = "composite"

    @property
    def grader_type(self) -> str:
        """``composite``."""
        return "composite"

    def grade(self, context: GradingContext) -> GradeResult:
        """Run every component in order and combine their results."""
        started = monotonic_ms()
        results = [component.grader.grade(context) for component in self.components]
        total_weight = sum(component.weight for component in self.components)
        score = (
            sum(r.score * c.weight for r, c in zip(results, self.components, strict=True))
            / total_weight
        )
        if self.aggregation == "all":
            passed = all(result.passed for result in results)
        else:
            passed = score >= self.pass_threshold
        return GradeResult(
            grader=self.name,
            grader_type=self.grader_type,
            passed=passed,
            score=round(score, 6),
            tests_passed=_sum_optional([r.tests_passed for r in results]),
            tests_failed=_sum_optional([r.tests_failed for r in results]),
            tests_errored=_sum_optional([r.tests_errored for r in results]),
            tests_skipped=_sum_optional([r.tests_skipped for r in results]),
            duration_ms=elapsed_ms(started),
            summary=f"{sum(r.passed for r in results)}/{len(results)} graders passed",
            components=tuple(results),
        )


def _sum_optional(values: Sequence[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def build_grader(spec: GradingSpec) -> Grader:
    """Construct the grader described by a task's ``grading`` section."""
    graders: list[WeightedGrader] = []
    for grader_spec in spec.graders:
        name = grader_spec.name or grader_spec.type
        grader: Grader
        if isinstance(grader_spec, PytestGraderSpec):
            grader = PytestGrader(
                name=name, paths=grader_spec.paths, args=grader_spec.args, python=grader_spec.python
            )
        elif isinstance(grader_spec, CommandGraderSpec):
            grader = CommandGrader(
                name=name,
                command=grader_spec.command,
                success_exit_codes=grader_spec.success_exit_codes,
            )
        graders.append(WeightedGrader(grader=grader, weight=grader_spec.weight))
    if len(graders) == 1 and spec.aggregation == "all":
        return graders[0].grader
    return CompositeGrader(
        components=tuple(graders), aggregation=spec.aggregation, pass_threshold=spec.pass_threshold
    )
