"""Persistence for guild installation state."""

from datetime import UTC, datetime

from bhbot.db.engine import Database


class GuildRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def mark_installed(self, guild_id: int, name: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self.database.execute(
            """
            INSERT INTO guilds (id, name, installed_at, removed_at, updated_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                removed_at = NULL,
                updated_at = excluded.updated_at
            """,
            (str(guild_id), name, now, now),
        )

    async def mark_removed(self, guild_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        await self.database.execute(
            """
            INSERT INTO guilds (id, name, installed_at, removed_at, updated_at)
            VALUES (?, 'Unknown server', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                removed_at = excluded.removed_at,
                updated_at = excluded.updated_at
            """,
            (str(guild_id), now, now, now),
        )
