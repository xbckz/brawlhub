"""Per-server ticket administrator membership."""

from bhbot.db.engine import Database


class TicketAdminRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def is_admin(self, *, guild_id: int, user_id: int) -> bool:
        rows = await self.database.query(
            "SELECT 1 FROM ticket_admins WHERE guild_id = ? AND user_id = ? LIMIT 1",
            (str(guild_id), str(user_id)),
        )
        return bool(rows)

    async def list_admins(self, *, guild_id: int) -> list[int]:
        rows = await self.database.query(
            "SELECT user_id FROM ticket_admins WHERE guild_id = ? ORDER BY added_at, user_id",
            (str(guild_id),),
        )
        return [int(row["user_id"]) for row in rows]

    async def add(self, *, guild_id: int, user_id: int, actor_id: int) -> bool:
        changed = await self.database.execute(
            """
            INSERT INTO ticket_admins (guild_id, user_id, added_by_discord_user_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (str(guild_id), str(user_id), str(actor_id)),
        )
        return changed > 0

    async def remove(self, *, guild_id: int, user_id: int) -> bool:
        changed = await self.database.execute(
            "DELETE FROM ticket_admins WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        )
        return changed > 0
