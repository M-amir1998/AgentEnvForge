"""Settings precedence, redaction, artifact storage, persistence and timeouts."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentenvforge.artifacts import LocalArtifactStore
from agentenvforge.config.settings import load_settings
from agentenvforge.domain.events import EventType, ExecutionEvent
from agentenvforge.domain.failures import ArtifactError, ConfigurationError
from agentenvforge.domain.manifest import RunManifest
from agentenvforge.domain.results import RunResult
from agentenvforge.domain.status import RunStatus
from agentenvforge.persistence.repository import NewRun
from agentenvforge.persistence.sqlite import SqliteRunRepository
from agentenvforge.persistence.sqlite_database import MIGRATIONS, SqliteDatabase
from agentenvforge.telemetry.redaction import REDACTED, Redactor
from agentenvforge.timeouts import CallTimeoutError, call_with_timeout

# ----------------------------------------------------------------------- settings


def test_settings_precedence_defaults_file_env_cli(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        'log_level = "INFO"\nlog_format = "json"\ncleanup_timeout_seconds = 5\n'
    )
    environ = {"AGENTENV_HOME": str(home), "AGENTENV_LOG_LEVEL": "ERROR"}

    settings = load_settings(cli_overrides={"log_level": "DEBUG"}, environ=environ)
    assert settings.home == home.resolve()
    assert settings.log_level == "DEBUG"  # CLI beats env
    assert settings.log_format == "json"  # file beats default
    assert settings.cleanup_timeout_seconds == 5
    assert settings.sandbox_start_timeout_seconds == 120  # default

    assert load_settings(environ=environ).log_level == "ERROR"  # env beats file


def test_home_cannot_be_set_in_config_file(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('home = "/elsewhere"\n')
    with pytest.raises(ConfigurationError):
        load_settings(environ={"AGENTENV_HOME": str(tmp_path)})


def test_invalid_setting_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(environ={"AGENTENV_HOME": str(tmp_path), "AGENTENV_LOG_FORMAT": "xml"})
    assert caught.value.issues[0].field == "log_format"


# ---------------------------------------------------------------------- redaction


def test_redactor_masks_text_and_bytes_longest_first() -> None:
    redactor = Redactor(["abc", "abcdef", ""])
    assert redactor.redact_text("key=abcdef and abc") == f"key={REDACTED} and {REDACTED}"
    assert redactor.redact_bytes(b"abcdef") == REDACTED.encode()
    assert not Redactor().active


# ---------------------------------------------------------------------- artifacts


def test_artifact_store_round_trip_and_metadata(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    record = store.put("run_1", "grader/report.xml", b"<xml/>")
    assert store.get("run_1", "grader/report.xml") == b"<xml/>"
    assert record.size_bytes == 6
    assert len(record.sha256) == 64
    assert Path(store.location("run_1", "grader/report.xml")).is_file()


@pytest.mark.parametrize("name", ["../escape", "/abs", "a/../../b", "c:\\x", ""])
def test_artifact_names_cannot_escape_the_run(tmp_path: Path, name: str) -> None:
    with pytest.raises(ArtifactError):
        LocalArtifactStore(tmp_path).put("run_1", name, b"x")


# -------------------------------------------------------------------- persistence


def _manifest() -> RunManifest:
    return RunManifest(
        agentenvforge_version="0",
        python_version="3",
        platform="p",
        task_id="t",
        task_path="t.yaml",
        task_schema_version="1",
        task_definition_hash="h",
        task_definition={},
        agent="noop",
    )


def _new_run(run_id: str, task_id: str = "t") -> NewRun:
    return NewRun(
        run_id=run_id,
        task_id=task_id,
        task_path="t.yaml",
        agent="noop",
        created_at=datetime.now(UTC),
        manifest=_manifest(),
    )


def _event(run_id: str, sequence: int) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=f"evt_{run_id}_{sequence}",
        run_id=run_id,
        task_id="t",
        sequence=sequence,
        timestamp=datetime.now(UTC),
        event_type=EventType.COMMAND_COMPLETED,
        status=RunStatus.RUNNING_AGENT,
        data={"exit_code": 0},
    )


def test_repository_round_trip(tmp_path: Path) -> None:
    repository = SqliteRunRepository(tmp_path / "db.sqlite")
    repository.create_run(_new_run("run_a"))
    repository.update_status("run_a", RunStatus.RUNNING_AGENT, datetime.now(UTC))
    repository.append_event(_event("run_a", 1))
    now = datetime.now(UTC)
    repository.complete_run(
        RunResult(
            run_id="run_a",
            task_id="t",
            status=RunStatus.COMPLETED,
            passed=True,
            score=1.0,
            created_at=now,
            finished_at=now,
            duration_ms=10,
        )
    )

    stored = repository.get_run("run_a")
    assert stored is not None and stored.result is not None
    assert stored.summary.status is RunStatus.COMPLETED
    assert stored.summary.passed is True
    assert stored.manifest == _manifest()
    assert [event.data for event in repository.list_events("run_a")] == [{"exit_code": 0}]
    assert repository.get_run("missing") is None


def test_repository_filters_and_orders_runs(tmp_path: Path) -> None:
    repository = SqliteRunRepository(tmp_path / "db.sqlite")
    for index, task in enumerate(["x", "y", "x"]):
        repository.create_run(_new_run(f"run_{index}", task_id=task))
        time.sleep(0.002)
    assert [run.run_id for run in repository.list_runs()] == ["run_2", "run_1", "run_0"]
    assert [run.run_id for run in repository.list_runs(task_id="x", limit=1)] == ["run_2"]
    assert repository.list_runs(status=RunStatus.COMPLETED) == []


def test_migrations_are_applied_once(tmp_path: Path) -> None:
    SqliteRunRepository(tmp_path / "db.sqlite").close()
    reopened = SqliteRunRepository(tmp_path / "db.sqlite")
    assert reopened.schema_version == len(MIGRATIONS)


def test_existing_version_1_database_is_upgraded_without_losing_runs(tmp_path: Path) -> None:
    """A database created before experiments existed keeps its runs after migration 2."""
    path = tmp_path / "v1.sqlite"
    now = datetime.now(UTC).isoformat()
    connection = sqlite3.connect(path)
    connection.executescript(MIGRATIONS[0])
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    connection.execute("INSERT INTO schema_migrations VALUES (1, datetime('now'))")
    connection.execute(
        "INSERT INTO runs (run_id, task_id, task_path, agent, status, created_at, updated_at, "
        "manifest_json) VALUES ('run_old', 't', 't.yaml', 'noop', 'completed', ?, ?, ?)",
        (now, now, _manifest().model_dump_json()),
    )
    connection.commit()
    connection.close()

    database = SqliteDatabase(path)  # migrates 1 -> 2
    repository = SqliteRunRepository(database)
    assert database.schema_version == len(MIGRATIONS)
    tables = {
        row["name"] for row in database.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runs", "events", "artifacts", "experiments", "experiment_runs"} <= tables
    assert repository.get_run("run_old") is not None
    database.close()
    assert SqliteDatabase(path).schema_version == len(MIGRATIONS)  # idempotent on reopen


def test_concurrent_event_writes_are_serialised(tmp_path: Path) -> None:
    repository = SqliteRunRepository(tmp_path / "db.sqlite")
    repository.create_run(_new_run("run_c"))

    def write(offset: int) -> None:
        for index in range(50):
            repository.append_event(_event("run_c", offset * 1000 + index))

    threads = [threading.Thread(target=write, args=(offset,)) for offset in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(repository.list_events("run_c")) == 200


# ----------------------------------------------------------------------- timeouts


def test_call_with_timeout_returns_and_propagates() -> None:
    assert call_with_timeout(lambda: 42, 1, name="quick") == 42
    with pytest.raises(ZeroDivisionError):
        call_with_timeout(lambda: 1 / 0, 1, name="broken")


def test_call_with_timeout_abandons_slow_calls() -> None:
    started = time.monotonic()
    with pytest.raises(CallTimeoutError):
        call_with_timeout(lambda: time.sleep(5), 0.3, name="slow")
    assert time.monotonic() - started < 2
