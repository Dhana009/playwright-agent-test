from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiosqlite

from agent.core.logging import get_logger

LOGGER = get_logger(__name__)
MIGRATION_PATTERN = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")
ROLLBACK_FILE_TEMPLATE = "{version:03d}_rollback.sql"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_path: Path
    rollback_path: Path


async def init_db(
    sqlite_path: str | Path | None = None,
    migrations_dir: str | Path | None = None,
) -> Path:
    db_path = resolve_sqlite_path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    migration_directory = resolve_migrations_dir(migrations_dir)
    migrations = discover_migrations(migration_directory)
    if not migrations:
        raise RuntimeError(
            f"No migration files found in '{migration_directory}'. "
            "Create at least one 00N_*.sql migration."
        )

    async with aiosqlite.connect(db_path) as connection:
        await connection.execute("PRAGMA foreign_keys = ON;")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await connection.commit()

        applied_versions = await _load_applied_versions(connection)
        _validate_migration_state(
            available_migrations=migrations,
            applied_versions=applied_versions,
        )

        pending_migrations = [
            migration
            for migration in migrations
            if migration.version not in applied_versions
        ]

        if not pending_migrations:
            LOGGER.info("sqlite_schema_up_to_date", db_path=str(db_path))
            return db_path

        for migration in pending_migrations:
            migration_sql = migration.up_path.read_text(encoding="utf-8").strip()
            if not migration_sql:
                raise RuntimeError(
                    f"Migration file '{migration.up_path}' is empty. "
                    "Migrations must contain SQL statements."
                )

            try:
                await connection.execute("BEGIN;")
                await connection.executescript(migration_sql)
                await connection.execute(
                    """
                    INSERT INTO schema_version (version, name)
                    VALUES (?, ?);
                    """,
                    (migration.version, migration.name),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

            LOGGER.info(
                "sqlite_migration_applied",
                db_path=str(db_path),
                version=migration.version,
                name=migration.name,
                migration=str(migration.up_path),
            )

    return db_path


def resolve_sqlite_path(sqlite_path: str | Path | None = None) -> Path:
    if sqlite_path is None:
        return _project_root() / "runs" / "agent.sqlite"

    candidate = Path(sqlite_path)
    if candidate.is_absolute():
        return candidate
    return _project_root() / candidate


def resolve_migrations_dir(migrations_dir: str | Path | None = None) -> Path:
    if migrations_dir is None:
        return Path(__file__).resolve().parent / "migrations"

    candidate = Path(migrations_dir)
    if candidate.is_absolute():
        return candidate
    return _project_root() / candidate


def discover_migrations(migrations_dir: str | Path) -> list[Migration]:
    directory = Path(migrations_dir)
    if not directory.exists():
        raise FileNotFoundError(f"Migration directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Migration path is not a directory: {directory}")

    migrations: list[Migration] = []
    seen_versions: set[int] = set()

    for up_path in sorted(directory.glob("*.sql")):
        if up_path.name.endswith("_rollback.sql"):
            continue

        match = MIGRATION_PATTERN.fullmatch(up_path.name)
        if match is None:
            raise RuntimeError(
                f"Invalid migration file name '{up_path.name}'. "
                "Expected pattern '00N_<name>.sql'."
            )

        version = int(match.group("version"))
        if version in seen_versions:
            raise RuntimeError(
                f"Duplicate migration version {version:03d} in '{directory}'."
            )
        seen_versions.add(version)

        rollback_path = directory / ROLLBACK_FILE_TEMPLATE.format(version=version)
        if not rollback_path.exists():
            raise RuntimeError(
                f"Missing rollback migration for {version:03d}: "
                f"expected '{rollback_path.name}'."
            )

        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                up_path=up_path,
                rollback_path=rollback_path,
            )
        )

    migrations.sort(key=lambda item: item.version)
    return migrations


async def _load_applied_versions(connection: aiosqlite.Connection) -> set[int]:
    cursor = await connection.execute("SELECT version FROM schema_version;")
    rows = await cursor.fetchall()
    return {int(row[0]) for row in rows}


def _validate_migration_state(
    available_migrations: Iterable[Migration],
    applied_versions: set[int],
) -> None:
    available = list(available_migrations)
    if not available and applied_versions:
        raise RuntimeError(
            "Database has applied schema versions but no migration files are available."
        )
    if not available:
        return

    available_versions = {migration.version for migration in available}
    latest_available = max(available_versions)

    if applied_versions and max(applied_versions) > latest_available:
        raise RuntimeError(
            "Database schema version is newer than available migrations. "
            "Refusing to silently downgrade."
        )

    unknown_applied_versions = applied_versions - available_versions
    if unknown_applied_versions:
        versions = ", ".join(str(version) for version in sorted(unknown_applied_versions))
        raise RuntimeError(
            "Database contains applied versions missing from migration files: "
            f"{versions}."
        )


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]
