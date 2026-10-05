"""Parsing JUnit XML reports (as written by ``pytest --junitxml``).

The report is produced inside the sandbox by code that runs alongside the agent's
solution, so it is treated as untrusted input: its size is bounded before parsing and
only aggregate counters are extracted. Python's bundled expat (>= 2.4.1) mitigates
entity-expansion attacks such as "billion laughs".
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree

from agentenvforge.domain.failures import GraderError


@dataclass(frozen=True, slots=True)
class TestCounts:
    """Aggregate outcome counters from a test report."""

    __test__ = False  # not a pytest test class, despite the name

    total: int
    failures: int
    errors: int
    skipped: int

    @property
    def passed(self) -> int:
        """Tests that ran and passed."""
        return max(0, self.total - self.failures - self.errors - self.skipped)

    @property
    def executed(self) -> int:
        """Tests that were not skipped."""
        return max(0, self.total - self.skipped)


def _count(element: ElementTree.Element, attribute: str) -> int:
    try:
        return int(element.get(attribute, "0"))
    except ValueError as error:
        raise GraderError(
            f"malformed JUnit report: {attribute}={element.get(attribute)!r}"
        ) from error


def parse_junit_counts(document: bytes) -> TestCounts:
    """Return summed counters over all ``<testsuite>`` elements in ``document``."""
    try:
        root = ElementTree.fromstring(document)  # noqa: S314 - size-bounded, see module doc
    except ElementTree.ParseError as error:
        raise GraderError(f"malformed JUnit report: {error}") from error
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if root.tag not in {"testsuite", "testsuites"}:
        raise GraderError(f"unexpected JUnit root element <{root.tag}>")
    return TestCounts(
        total=sum(_count(suite, "tests") for suite in suites),
        failures=sum(_count(suite, "failures") for suite in suites),
        errors=sum(_count(suite, "errors") for suite in suites),
        skipped=sum(_count(suite, "skipped") for suite in suites),
    )
