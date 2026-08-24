"""Serialized async access to a local or remote Turso/libSQL database."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import libsql

T = TypeVar("T")
logger = logging.getLogger(__name__)

# SQLite/libSQL does not have PostgreSQL's table-level autovacuum settings or
# GiST/GIN indexes. This migration applies the equivalent safe maintenance:
# compact the existing SQLite indexes once and refresh the query planner's
# statistics. The marker prevents paying that cost on every bot restart.
STORAGE_MAINTENANCE_MIGRATION = "2026-08-21-turso-storage-optimization-v2"

SCHEMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA foreign_keys = ON",
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        id TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guilds (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        installed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        removed_at TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_admins (
        guild_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        added_by_discord_user_id TEXT NOT NULL,
        added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (guild_id, user_id),
        FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id TEXT PRIMARY KEY,
        guild_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        event_type TEXT NOT NULL,
        actor_discord_user_id TEXT,
        target_discord_id TEXT,
        channel_id TEXT,
        correlation_id TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_panels (
        id TEXT PRIMARY KEY,
        guild_id TEXT NOT NULL,
        panel_channel_id TEXT NOT NULL,
        open_category_id TEXT NOT NULL,
        archive_category_id TEXT,
        created_by_discord_user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        opening_message TEXT NOT NULL DEFAULT '',
        color INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        footer_text TEXT,
        select_placeholder TEXT NOT NULL,
        thumbnail_url TEXT,
        image_url TEXT,
        staff_role_ids TEXT NOT NULL DEFAULT '[]',
        notify_staff_on_create INTEGER NOT NULL DEFAULT 1,
        name_template TEXT NOT NULL,
        max_open_per_user INTEGER NOT NULL,
        duplicate_scope TEXT NOT NULL,
        inactivity_reminder_hours INTEGER,
        auto_close_hours INTEGER,
        transcript_retention_days INTEGER NOT NULL,
        status TEXT NOT NULL,
        message_id TEXT,
        error_code TEXT,
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_panel_categories (
        id TEXT PRIMARY KEY,
        panel_id TEXT NOT NULL,
        target_category_id TEXT,
        subcategory_prompt TEXT NOT NULL DEFAULT '',
        subcategories TEXT NOT NULL DEFAULT '[]',
        key TEXT NOT NULL,
        label TEXT NOT NULL,
        description TEXT,
        emoji TEXT,
        staff_role_ids TEXT NOT NULL DEFAULT '[]',
        notify_staff INTEGER NOT NULL DEFAULT 1,
        form_fields TEXT NOT NULL DEFAULT '[]',
        position INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (panel_id) REFERENCES ticket_panels(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_ticket_panel_categories_panel_position
    ON ticket_panel_categories(panel_id, position) WHERE active = 1
    """,
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        ticket_number INTEGER NOT NULL DEFAULT 0,
        panel_id TEXT NOT NULL,
        category_id TEXT NOT NULL,
        guild_id TEXT NOT NULL,
        requester_id TEXT NOT NULL,
        channel_id TEXT UNIQUE,
        control_message_id TEXT,
        staff_thread_id TEXT,
        staff_control_message_id TEXT,
        status TEXT NOT NULL,
        priority TEXT NOT NULL,
        assignee_id TEXT,
        subject TEXT NOT NULL,
        answers TEXT NOT NULL DEFAULT '{}',
        close_reason TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_activity_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        first_staff_response_at TEXT,
        closed_at TEXT,
        archived_at TEXT,
        reminded_at TEXT,
        reopen_count INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (panel_id) REFERENCES ticket_panels(id) ON DELETE RESTRICT,
        FOREIGN KEY (category_id) REFERENCES ticket_panel_categories(id) ON DELETE RESTRICT,
        FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ticket_panels_guild_status ON ticket_panels(guild_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_guild_status_created ON tickets(guild_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_requester_status ON tickets(guild_id, requester_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_panel_status ON tickets(panel_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_status_activity ON tickets(status, last_activity_at)",
    "CREATE TABLE IF NOT EXISTS ticket_internal_notes ("
    "id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, author_id TEXT NOT NULL, "
    "content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE)",
    "CREATE INDEX IF NOT EXISTS ix_ticket_notes_ticket_created ON ticket_internal_notes(ticket_id, created_at)",
    "CREATE TABLE IF NOT EXISTS ticket_transcripts ("
    "ticket_id TEXT PRIMARY KEY, content TEXT NOT NULL, message_count INTEGER NOT NULL, "
    "generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL, "
    "FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE)",
    "CREATE INDEX IF NOT EXISTS ix_audit_events_guild_occurred ON audit_events(guild_id, occurred_at)",
    "CREATE INDEX IF NOT EXISTS ix_audit_events_guild_type_occurred ON audit_events(guild_id, event_type, occurred_at)",
    "CREATE INDEX IF NOT EXISTS ix_audit_events_actor ON audit_events(actor_discord_user_id)",
)


class Database:
    """A single serialized libSQL connection exposed through async methods."""

    def __init__(self, database_url: str, auth_token: str = "") -> None:
        if not database_url:
            raise ValueError("TURSO_DATABASE_URL must not be empty")
        self.is_remote = database_url.startswith(("libsql://", "https://", "http://"))
        if database_url == ":memory:" or database_url.startswith("file:"):
            database = database_url
        elif database_url.startswith(("libsql://", "https://", "http://")):
            database = database_url
        else:
            database = str(Path(database_url))
        self.connection = libsql.connect(
            database=database,
            auth_token=auth_token,
            _check_same_thread=False,
        )
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.commit()
        self._lock = asyncio.Lock()

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        names = [str(item[0]) for item in (cursor.description or ())]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    async def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self._lock:
            def run() -> list[dict[str, Any]]:
                return self._rows(self.connection.execute(sql, tuple(parameters)))

            return await asyncio.to_thread(run)

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        async with self._lock:
            def run() -> int:
                cursor = self.connection.execute(sql, tuple(parameters))
                self.connection.commit()
                return int(cursor.rowcount)

            return await asyncio.to_thread(run)

    async def transaction(self, callback: Callable[[Any], T]) -> T:
        async with self._lock:
            def run() -> T:
                self.connection.execute("BEGIN IMMEDIATE")
                try:
                    result = callback(self.connection)
                    self.connection.commit()
                    return result
                except BaseException:
                    self.connection.rollback()
                    raise

            return await asyncio.to_thread(run)

    async def ping(self) -> bool:
        try:
            await self.query("SELECT 1")
            return True
        except Exception:
            return False

    async def initialize(self) -> None:
        def create_schema(connection: Any) -> None:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            # The bot originally shipped without a configurable ticket
            # opening message. Keep existing Turso databases compatible.
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(ticket_panels)").fetchall()
            }
            if "opening_message" not in columns:
                connection.execute(
                    "ALTER TABLE ticket_panels ADD COLUMN opening_message TEXT NOT NULL DEFAULT ''"
                )
            category_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(ticket_panel_categories)"
                ).fetchall()
            }
            if "target_category_id" not in category_columns:
                connection.execute(
                    "ALTER TABLE ticket_panel_categories ADD COLUMN target_category_id TEXT"
                )
            if "subcategory_prompt" not in category_columns:
                connection.execute(
                    "ALTER TABLE ticket_panel_categories ADD COLUMN subcategory_prompt TEXT NOT NULL DEFAULT ''"
                )
            if "subcategories" not in category_columns:
                connection.execute(
                    "ALTER TABLE ticket_panel_categories ADD COLUMN subcategories TEXT NOT NULL DEFAULT '[]'"
                )
            if "notify_staff" not in category_columns:
                connection.execute(
                    "ALTER TABLE ticket_panel_categories ADD COLUMN notify_staff INTEGER NOT NULL DEFAULT 1"
                )
            ticket_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(tickets)").fetchall()
            }
            if "ticket_number" not in ticket_columns:
                connection.execute(
                    "ALTER TABLE tickets ADD COLUMN ticket_number INTEGER NOT NULL DEFAULT 0"
                )
            next_numbers = {
                str(row[0]): int(row[1] or 0)
                for row in connection.execute(
                    "SELECT guild_id, MAX(ticket_number) FROM tickets WHERE ticket_number > 0 GROUP BY guild_id"
                ).fetchall()
            }
            for row in connection.execute(
                "SELECT id, guild_id FROM tickets WHERE ticket_number=0 ORDER BY guild_id, created_at, id"
            ).fetchall():
                guild_id = str(row[1])
                next_number = next_numbers.get(guild_id, 0) + 1
                ticket_number = ((next_number - 1) % 999) + 1
                connection.execute(
                    "UPDATE tickets SET ticket_number=? WHERE id=?",
                    (ticket_number, str(row[0])),
                )
                next_numbers[guild_id] = ticket_number
            connection.execute(
                "UPDATE ticket_panels SET name_template=? WHERE name_template=?",
                ("{number}-{category}-{username}", "ticket-{category}-{username}"),
            )
            connection.execute(
                "UPDATE ticket_panels SET duplicate_scope='category' WHERE duplicate_scope='panel'"
            )

            self._apply_storage_maintenance(connection)

        await self.transaction(create_schema)

    @staticmethod
    def _table_exists(connection: Any, table_name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None

    def _apply_storage_maintenance(self, connection: Any) -> None:
        """Run the Turso/libSQL equivalent of the storage maintenance once.

        PostgreSQL's REINDEX, GiST trigram indexes, and per-table autovacuum
        settings cannot be used on Turso/libSQL. SQLite/libSQL indexes are
        B-trees, so REINDEX rebuilds them compactly. Local SQLite also supports
        ANALYZE and PRAGMA optimize; Turso's remote Hrana protocol rejects those
        maintenance statements, so they are deliberately skipped remotely.

        The optional member_activity_daily table is checked defensively so a
        database that also contains an external activity table benefits from
        the same rebuild without making this ticket-only schema depend on it.
        """
        applied = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE id=? LIMIT 1",
            (STORAGE_MAINTENANCE_MIGRATION,),
        ).fetchone()
        if applied is not None:
            return

        tables = ["audit_events"]
        if self._table_exists(connection, "member_activity_daily"):
            tables.append("member_activity_daily")

        maintenance_failed = False
        for table_name in tables:
            try:
                # These names are hard-coded above, not user input.
                connection.execute(f"REINDEX {table_name}")
                if not self.is_remote:
                    connection.execute(f"ANALYZE {table_name}")
            except Exception:
                maintenance_failed = True
                logger.warning(
                    "Could not rebuild or analyze Turso table %s",
                    table_name,
                    exc_info=True,
                )
        if not self.is_remote:
            try:
                connection.execute("PRAGMA optimize")
            except Exception:
                maintenance_failed = True
                logger.warning("Local SQLite PRAGMA optimize is unavailable", exc_info=True)

        connection.execute(
            "INSERT INTO schema_migrations (id) VALUES (?)",
            (STORAGE_MAINTENANCE_MIGRATION,),
        )
        if maintenance_failed:
            logger.warning("Applied Turso storage maintenance migration with warnings")
        else:
            logger.info("Applied Turso storage maintenance migration")

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self.connection.close)
