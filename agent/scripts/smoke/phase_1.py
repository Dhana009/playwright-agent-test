from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from rich.logging import RichHandler  # noqa: E402

from agent.core.config import Settings  # noqa: E402
from agent.core.ids import generate_event_id, generate_run_id, generate_step_id  # noqa: E402
from agent.core.logging import configure_logging, get_logger  # noqa: E402
from agent.storage.sqlite import discover_migrations, init_db, resolve_migrations_dir  # noqa: E402
from _runner import SmokeRunner  # noqa: E402

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _decode_crockford_base32(value: str) -> int:
    decoded = 0
    for char in value:
        index = _CROCKFORD_ALPHABET.find(char)
        if index < 0:
            raise AssertionError(f"Unsupported ULID character {char!r} in {value!r}")
        decoded = (decoded << 5) | index
    return decoded


def _ulid_part(prefixed_id: str) -> str:
    if "_" not in prefixed_id:
        raise AssertionError(f"Expected '<prefix>_<ulid>' format, got {prefixed_id!r}")
    _, _, ulid = prefixed_id.partition("_")
    return ulid


def _assert_ulid_layout(prefixed_id: str, label: str) -> None:
    ulid = _ulid_part(prefixed_id)
    if len(ulid) != 26:
        raise AssertionError(f"Expected 26-char ULID for {label}, got {len(ulid)} chars")

    timestamp_chunk = ulid[:10]
    random_chunk = ulid[10:]
    timestamp = _decode_crockford_base32(timestamp_chunk)
    random_bits = _decode_crockford_base32(random_chunk)
    full_value = _decode_crockford_base32(ulid)

    if timestamp > ((1 << 48) - 1):
        raise AssertionError(f"Timestamp overflow for {label}: {timestamp}")
    if random_bits > ((1 << 80) - 1):
        raise AssertionError(f"Random component overflow for {label}: {random_bits}")
    if full_value > ((1 << 128) - 1):
        raise AssertionError(f"ULID integer overflow for {label}: {full_value}")

    bytes_payload = full_value.to_bytes(16, byteorder="big")
    if len(bytes_payload[:6]) != 6 or len(bytes_payload[6:]) != 10:
        raise AssertionError(f"Expected ULID byte layout 6+10 for {label}")


def _assert_strictly_increasing(values: list[str], label: str) -> None:
    for index in range(1, len(values)):
        if values[index] <= values[index - 1]:
            raise AssertionError(
                f"{label} is not strictly increasing at index {index}: "
                f"{values[index - 1]} >= {values[index]}"
            )


def main() -> int:
    run_id = generate_run_id()
    runner = SmokeRunner(phase="A1", run_id=run_id, default_task="A1.1")

    with runner.case("a1_1_config_loads_default_yaml", task="A1.1", feature="config"):
        settings = Settings.load()
        runner.check(settings.mode == "manual", "Expected default mode to be 'manual'")
        runner.check(
            settings.storage.sqlite_path == "runs/agent.sqlite",
            "Expected default sqlite path from config/default.yaml",
        )

    with runner.case("a1_1_config_env_override_merge", task="A1.1", feature="config"):
        env_prefix = "A1_SMOKE_"
        keys = {
            f"{env_prefix}MODE": "hybrid",
            f"{env_prefix}CACHE__DECISION_TTL_SECONDS": "42",
        }
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ.update(keys)
            settings = Settings.load(env_prefix=env_prefix)
        finally:
            for key, old_value in previous.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value

        runner.check(settings.mode == "hybrid", "Expected env override for mode")
        runner.check(
            settings.cache.decision_ttl_seconds == 42,
            "Expected nested env override for cache.decision_ttl_seconds",
        )

    with runner.case("a1_1_config_rejects_invalid_values", task="A1.1", feature="config"):
        with tempfile.TemporaryDirectory() as tmp:
            invalid_path = Path(tmp) / "invalid.yaml"
            invalid_path.write_text("mode: definitely_not_valid\n", encoding="utf-8")
            try:
                Settings.load(config_path=invalid_path, env_prefix="A1_NONE_")
            except ValueError as exc:
                message = str(exc)
                runner.check(
                    f"Invalid configuration values in '{invalid_path}'" in message,
                    "Expected config path in validation error wrapper message",
                )
                runner.check(
                    "mode" in message,
                    "Expected failing field name in validation error",
                )
                runner.check(
                    "Input should be 'manual', 'llm' or 'hybrid'" in message,
                    "Expected exact pydantic enum validation message fragment for mode",
                )
            else:
                raise AssertionError("Expected ValueError for invalid settings payload")

    with runner.case("a1_2_ulids_unique_monotonic_and_layout_10k", task="A1.2", feature="ids"):
        sample_size = 10_000
        id_groups = [
            ("run_id", "run_", [generate_run_id() for _ in range(sample_size)]),
            ("step_id", "step_", [generate_step_id() for _ in range(sample_size)]),
            ("event_id", "event_", [generate_event_id() for _ in range(sample_size)]),
        ]
        for label, prefix, values in id_groups:
            runner.check(
                len(values) == len(set(values)),
                f"Expected unique {label}s over {sample_size} generations",
            )
            _assert_strictly_increasing(values, label)
            for value in values:
                runner.check(value.startswith(prefix), f"Expected prefix {prefix!r} for {label}")
                _assert_ulid_layout(value, label)

    with runner.case("a1_3_structlog_file_lines_include_run_id", task="A1.3", feature="logging"):
        with tempfile.TemporaryDirectory() as tmp:
            log_run_id = generate_run_id()
            log_path = configure_logging(run_id=log_run_id, runs_root=Path(tmp))
            logger = get_logger(__name__)
            logger.info("a1_logging_case", smoke_phase=1, sequence=1)
            logger.info("a1_logging_case", smoke_phase=1, sequence=2)
            logger.info("a1_logging_case", smoke_phase=1, sequence=3)

            handlers = logging.getLogger().handlers
            runner.check(
                any(isinstance(handler, RichHandler) for handler in handlers),
                "Expected Rich console handler to be configured",
            )
            runner.check(
                any(isinstance(handler, logging.FileHandler) for handler in handlers),
                "Expected file handler to be configured",
            )
            for handler in handlers:
                handler.flush()

            runner.check(log_path.exists(), f"Expected log file to exist: {log_path}")
            lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
            runner.check(len(lines) == 3, f"Expected exactly 3 JSON log lines in {log_path}")
            parsed = [json.loads(line) for line in lines]
            runner.check(
                all(entry.get("run_id") == log_run_id for entry in parsed),
                "Expected run_id to be present and correct on every log line",
            )
            runner.check(
                all({"event", "level", "logger", "timestamp"} <= set(entry.keys()) for entry in parsed),
                "Expected event/level/logger/timestamp fields on every log line",
            )
            runner.check(
                all(entry.get("event") == "a1_logging_case" for entry in parsed),
                "Expected deterministic event name across all log lines",
            )

    with runner.case("a1_4_sqlite_init_idempotent_and_downgrade_rejected", task="A1.4", feature="sqlite"):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "phase_1.sqlite"
            migrations_dir = resolve_migrations_dir()
            migrations = discover_migrations(migrations_dir)
            latest_version = migrations[-1].version

            first_path = asyncio.run(init_db(sqlite_path=db_path, migrations_dir=migrations_dir))
            runner.check(first_path == db_path, "Expected init_db to return the provided sqlite path")

            expected_tables = {
                "schema_version",
                "runs",
                "events",
                "checkpoints",
                "step_graph",
                "compiled_memory",
                "learned_repairs",
                "cache_records",
                "llm_calls",
                "raw_evidence",
            }
            with sqlite3.connect(db_path) as connection:
                table_rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).fetchall()
                table_names = {str(row[0]) for row in table_rows}
                missing_tables = expected_tables - table_names
                runner.check(
                    not missing_tables,
                    f"Expected all tables to exist, missing: {sorted(missing_tables)}",
                )
                versions = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_version ORDER BY version ASC;"
                    ).fetchall()
                ]

            expected_versions = [migration.version for migration in migrations]
            runner.check(
                versions == expected_versions,
                f"Expected schema_version entries {expected_versions}, got {versions}",
            )
            runner.check(
                versions[-1] == latest_version,
                "Expected latest applied schema version to match latest migration file",
            )

            second_path = asyncio.run(init_db(sqlite_path=db_path, migrations_dir=migrations_dir))
            runner.check(second_path == db_path, "Expected re-run to return the same sqlite path")
            with sqlite3.connect(db_path) as connection:
                rerun_versions = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_version ORDER BY version ASC;"
                    ).fetchall()
                ]
            runner.check(
                rerun_versions == versions,
                "Expected init_db re-run to be a no-op for schema_version",
            )

            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "INSERT INTO schema_version (version, name) VALUES (?, ?);",
                    (latest_version + 1, "future_schema"),
                )
                connection.commit()

            try:
                asyncio.run(init_db(sqlite_path=db_path, migrations_dir=migrations_dir))
            except RuntimeError as exc:
                message = str(exc)
                runner.check(
                    "Database schema version is newer than available migrations" in message,
                    "Expected clear downgrade rejection preamble",
                )
                runner.check(
                    "Refusing to silently downgrade." in message,
                    "Expected explicit downgrade rejection message",
                )
            else:
                raise AssertionError("Expected init_db to reject newer applied schema version")

    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(main())
