"""Postgres connection pool + a minimal numbered-SQL migration runner.

No ORM, no Alembic: migrations are plain ``.sql`` files under ``migrations/``,
named ``NNNN_description.sql``, applied in order inside one transaction each.
Only `manager.py` (and the test harness) ever calls `apply_migrations`;
worker/panel processes call `assert_version` and refuse to boot on a
mismatch, so a half-upgraded fleet can't run two schema versions at once.
"""

from __future__ import annotations

import re
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d{4})_.*\.sql$")


class MigrationError(RuntimeError):
    pass


async def create_pool(
    dsn: str,
    *,
    min_size: int = 4,
    max_size: int = 16,
    command_timeout: float = 30.0,
) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
    )


def _discover(directory: Path) -> list[tuple[int, str, Path]]:
    found: list[tuple[int, str, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        m = _FILENAME_RE.match(path.name)
        if not m:
            raise MigrationError(f"migration file does not match NNNN_name.sql: {path.name}")
        version = int(m.group(1))
        found.append((version, path.stem, path))
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration version numbers in {directory}")
    return found


async def current_version(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as con:
        # Unqualified so it resolves via the connection's search_path — the
        # test harness runs each test in its own throwaway schema.
        exists = await con.fetchval(
            "SELECT to_regclass('schema_migrations') IS NOT NULL"
        )
        if not exists:
            return 0
        version = await con.fetchval("SELECT max(version) FROM schema_migrations")
        return version or 0


async def apply_migrations(pool: asyncpg.Pool, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every migration newer than the current version, in order.

    Takes a Postgres advisory lock for the whole run so two processes
    racing to migrate the same database (e.g. two `manager.py` instances
    starting at once) serialize instead of corrupting each other.
    """
    applied: list[int] = []
    migrations = _discover(directory)
    async with pool.acquire() as con:
        await con.execute("SELECT pg_advisory_lock(hashtext('userbot_migrations'))")
        try:
            have = await current_version(pool)
            for version, name, path in migrations:
                if version <= have:
                    continue
                sql = path.read_text(encoding="utf-8")
                async with con.transaction():
                    await con.execute(sql)
                    await con.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                        version,
                        name,
                    )
                applied.append(version)
        finally:
            await con.execute("SELECT pg_advisory_unlock(hashtext('userbot_migrations'))")
    return applied


async def assert_version(pool: asyncpg.Pool, expected: int, directory: Path = MIGRATIONS_DIR) -> None:
    """Workers/panel call this at boot; they never migrate themselves."""
    have = await current_version(pool)
    if have != expected:
        raise MigrationError(
            f"database is at schema version {have}, this process expects {expected}. "
            "Run the manager (which applies migrations) before starting workers/panel."
        )


def latest_version(directory: Path = MIGRATIONS_DIR) -> int:
    migrations = _discover(directory)
    return max((v for v, _, _ in migrations), default=0)
