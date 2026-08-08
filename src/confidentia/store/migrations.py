"""Forward-only SQL migrations for the platform metadata store.

Plain SQL files applied in filename order, tracked in ``platform.schema_migration``
with a checksum. Chosen over Alembic deliberately: this schema is owned entirely by
this project, its history is linear, and the ingestion hot path stays free of an
ORM. The whole runner is auditable in one screen, which matters more here than
autogeneration — a reviewer can confirm what will run against the database by
reading the ``.sql`` files, not by reading generated revision scripts.

The checksum is the point of the tracking table. If an already-applied migration
file is edited, the databases in existence no longer match the repository, and the
runner says so rather than silently diverging.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from confidentia.common.logging import get_logger
from confidentia.store import db

logger = get_logger(__name__)

SQL_DIR = Path(__file__).parent / "sql"

# Migrations targeting the source-of-record OLTP database rather than the platform
# database. Kept explicit so a stray file cannot be applied to the wrong database.
OLTP_MIGRATIONS = frozenset({"003_oltp_source.sql"})


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def targets_oltp(self) -> bool:
        return self.path.name in OLTP_MIGRATIONS


def discover(sql_dir: Path = SQL_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(sql_dir.glob("*.sql")):
        migrations.append(
            Migration(
                version=path.stem, path=path, sql=path.read_text(encoding="utf-8")
            )
        )
    return migrations


def _ensure_tracking_table(database: str | None) -> None:
    # Bootstrap: the tracking table cannot itself be tracked.
    db.execute("CREATE SCHEMA IF NOT EXISTS platform", database=database)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.schema_migration (
            version    TEXT PRIMARY KEY,
            checksum   TEXT        NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        database=database,
    )


def applied_versions(database: str | None) -> dict[str, str]:
    _ensure_tracking_table(database)
    rows = db.fetch_all(
        "SELECT version, checksum FROM platform.schema_migration", database=database
    )
    return {row["version"]: row["checksum"] for row in rows}


def migrate(*, platform_database: str | None = None, oltp_database: str | None = None) -> list[str]:
    """Apply pending migrations to both databases. Returns what was applied."""
    from confidentia.config.settings import get_settings

    settings = get_settings()
    platform_db = platform_database or settings.postgres.database
    oltp_db = oltp_database or settings.postgres.oltp_database

    applied: list[str] = []
    seen = {platform_db: applied_versions(platform_db), oltp_db: applied_versions(oltp_db)}

    for migration in discover():
        target = oltp_db if migration.targets_oltp else platform_db
        existing = seen[target].get(migration.version)

        if existing is not None:
            if existing != migration.checksum:
                # Editing an applied migration means deployed databases no longer
                # match the repository. Refusing loudly is the only safe response;
                # the fix is a new migration, not a rewritten one.
                raise RuntimeError(
                    f"migration {migration.version} was modified after being applied to "
                    f"{target} (checksum {existing[:12]} -> {migration.checksum[:12]}). "
                    "Add a new migration instead of editing history."
                )
            continue

        logger.info("applying_migration", version=migration.version, database=target)
        # Each migration runs in its own transaction: a failure leaves the database
        # at the last complete version rather than half-migrated.
        with db.connection(target) as conn, conn.cursor() as cur:
            cur.execute(migration.sql)
            cur.execute(
                "INSERT INTO platform.schema_migration (version, checksum) VALUES (%s, %s)",
                (migration.version, migration.checksum),
            )
        applied.append(f"{target}:{migration.version}")

    if not applied:
        logger.info("migrations_up_to_date")
    return applied


def status() -> list[dict[str, object]]:
    from confidentia.config.settings import get_settings

    settings = get_settings()
    platform_db = settings.postgres.database
    oltp_db = settings.postgres.oltp_database
    seen = {platform_db: applied_versions(platform_db), oltp_db: applied_versions(oltp_db)}

    rows: list[dict[str, object]] = []
    for migration in discover():
        target = oltp_db if migration.targets_oltp else platform_db
        existing = seen[target].get(migration.version)
        rows.append(
            {
                "version": migration.version,
                "database": target,
                "applied": existing is not None,
                "drifted": existing is not None and existing != migration.checksum,
            }
        )
    return rows
