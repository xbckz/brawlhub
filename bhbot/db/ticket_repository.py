"""SQLite/libSQL ticket persistence with the same public workflow as BH Bot."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from bhbot.db.engine import Database
from bhbot.db.models import (
    Ticket,
    TicketInternalNote,
    TicketPanel,
    TicketPanelCategory,
    TicketTranscript,
)


class TicketFormFieldConfig(TypedDict):
    key: str
    label: str
    style: str
    required: bool
    minimum_length: int
    maximum_length: int
    placeholder: str | None


class TicketCategoryConfig(TypedDict):
    target_category_id: int | None
    subcategory_prompt: str
    # Each option may contain a response and a nested {prompt, options} node.
    # Keeping the tree in the existing JSON column also keeps old panels
    # compatible: flat options simply have no children.
    subcategories: list[dict[str, Any]]
    key: str
    label: str
    description: str | None
    emoji: str | None
    staff_role_ids: list[int]
    form_fields: list[TicketFormFieldConfig]


@dataclass(frozen=True, slots=True)
class TicketPanelRecord:
    panel: TicketPanel
    categories: list[TicketPanelCategory]


@dataclass(frozen=True, slots=True)
class TicketRecord:
    ticket: Ticket
    panel: TicketPanel
    category: TicketPanelCategory


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def _required_datetime(value: object) -> datetime:
    result = _datetime(value)
    if result is None:
        raise ValueError("Database returned a missing timestamp")
    return result


def _json(value: object, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(str(value))


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _one(connection: Any, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    cursor = connection.execute(sql, parameters)
    names = [str(item[0]) for item in (cursor.description or ())]
    row = cursor.fetchone()
    return dict(zip(names, row, strict=True)) if row is not None else None


def _all(connection: Any, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, parameters)
    names = [str(item[0]) for item in (cursor.description or ())]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _panel(row: dict[str, Any]) -> TicketPanel:
    return TicketPanel(
        id=_uuid(row["id"]),
        guild_id=str(row["guild_id"]),
        panel_channel_id=str(row["panel_channel_id"]),
        open_category_id=str(row["open_category_id"]),
        archive_category_id=(str(row["archive_category_id"]) if row["archive_category_id"] else None),
        created_by_discord_user_id=str(row["created_by_discord_user_id"]),
        title=str(row["title"]),
        description=str(row["description"]),
        opening_message=str(row.get("opening_message") or ""),
        color=int(row["color"]),
        emoji=str(row["emoji"]),
        footer_text=str(row["footer_text"]) if row["footer_text"] is not None else None,
        select_placeholder=str(row["select_placeholder"]),
        thumbnail_url=str(row["thumbnail_url"]) if row["thumbnail_url"] is not None else None,
        image_url=str(row["image_url"]) if row["image_url"] is not None else None,
        staff_role_ids=[str(value) for value in _json(row["staff_role_ids"], [])],
        notify_staff_on_create=bool(row["notify_staff_on_create"]),
        name_template=str(row["name_template"]),
        max_open_per_user=int(row["max_open_per_user"]),
        duplicate_scope=str(row["duplicate_scope"]),
        inactivity_reminder_hours=(
            int(row["inactivity_reminder_hours"])
            if row["inactivity_reminder_hours"] is not None
            else None
        ),
        auto_close_hours=int(row["auto_close_hours"]) if row["auto_close_hours"] is not None else None,
        transcript_retention_days=int(row["transcript_retention_days"]),
        status=str(row["status"]),
        message_id=str(row["message_id"]) if row["message_id"] is not None else None,
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        revision=int(row["revision"]),
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
    )


def _category(row: dict[str, Any]) -> TicketPanelCategory:
    def normalize_option(value: object) -> dict[str, Any] | None:
        if not isinstance(value, dict) or not value.get("key") or not value.get("label"):
            return None
        option: dict[str, Any] = {
            "key": str(value["key"]),
            "label": str(value["label"]),
            "description": str(value["description"]) if value.get("description") else None,
            "emoji": str(value["emoji"]) if value.get("emoji") else None,
            "response": str(value.get("response") or ""),
        }
        children = value.get("children")
        if isinstance(children, dict):
            options = [
                child
                for raw_child in children.get("options", [])
                if (child := normalize_option(raw_child)) is not None
            ][:25]
            option["children"] = {
                "prompt": str(children.get("prompt") or ""),
                "options": options,
            }
        return option

    return TicketPanelCategory(
        id=_uuid(row["id"]),
        panel_id=_uuid(row["panel_id"]),
        target_category_id=(
            str(row["target_category_id"]) if row.get("target_category_id") else None
        ),
        subcategory_prompt=str(row.get("subcategory_prompt") or ""),
        subcategories=[
            option
            for raw_option in _json(row.get("subcategories"), [])
            if (option := normalize_option(raw_option)) is not None
        ][:25],
        key=str(row["key"]),
        label=str(row["label"]),
        description=str(row["description"]) if row["description"] is not None else None,
        emoji=str(row["emoji"]) if row["emoji"] is not None else None,
        staff_role_ids=[str(value) for value in _json(row["staff_role_ids"], [])],
        form_fields=list(_json(row["form_fields"], [])),
        position=int(row["position"]),
        active=bool(row["active"]),
    )


def _ticket(row: dict[str, Any]) -> Ticket:
    return Ticket(
        id=_uuid(row["id"]),
        ticket_number=int(row.get("ticket_number") or 0),
        panel_id=_uuid(row["panel_id"]),
        category_id=_uuid(row["category_id"]),
        guild_id=str(row["guild_id"]),
        requester_id=str(row["requester_id"]),
        channel_id=str(row["channel_id"]) if row["channel_id"] is not None else None,
        control_message_id=(str(row["control_message_id"]) if row["control_message_id"] is not None else None),
        staff_thread_id=str(row["staff_thread_id"]) if row["staff_thread_id"] is not None else None,
        staff_control_message_id=(
            str(row["staff_control_message_id"]) if row["staff_control_message_id"] is not None else None
        ),
        status=str(row["status"]),
        priority=str(row["priority"]),
        assignee_id=str(row["assignee_id"]) if row["assignee_id"] is not None else None,
        subject=str(row["subject"]),
        answers={str(key): str(value) for key, value in dict(_json(row["answers"], {})).items()},
        close_reason=str(row["close_reason"]) if row["close_reason"] is not None else None,
        created_at=_required_datetime(row["created_at"]),
        updated_at=_required_datetime(row["updated_at"]),
        last_activity_at=_required_datetime(row["last_activity_at"]),
        first_staff_response_at=_datetime(row["first_staff_response_at"]),
        closed_at=_datetime(row["closed_at"]),
        archived_at=_datetime(row["archived_at"]),
        reminded_at=_datetime(row["reminded_at"]),
        reopen_count=int(row["reopen_count"]),
    )


def _note(row: dict[str, Any]) -> TicketInternalNote:
    return TicketInternalNote(
        id=_uuid(row["id"]),
        ticket_id=_uuid(row["ticket_id"]),
        author_id=str(row["author_id"]),
        content=str(row["content"]),
        created_at=_required_datetime(row["created_at"]),
    )


def _transcript(row: dict[str, Any]) -> TicketTranscript:
    return TicketTranscript(
        ticket_id=_uuid(row["ticket_id"]),
        content=str(row["content"]),
        message_count=int(row["message_count"]),
        generated_at=_required_datetime(row["generated_at"]),
        expires_at=_required_datetime(row["expires_at"]),
    )


_JOINED_SELECT = """
SELECT t.*,
    p.id AS panel_record_id, p.guild_id AS panel_guild_id,
    p.panel_channel_id AS panel_channel_id, p.open_category_id AS panel_open_category_id,
    p.archive_category_id AS panel_archive_category_id,
    p.created_by_discord_user_id AS panel_created_by_discord_user_id,
    p.title AS panel_title, p.description AS panel_description,
    p.opening_message AS panel_opening_message, p.color AS panel_color,
    p.emoji AS panel_emoji, p.footer_text AS panel_footer_text,
    p.select_placeholder AS panel_select_placeholder, p.thumbnail_url AS panel_thumbnail_url,
    p.image_url AS panel_image_url, p.staff_role_ids AS panel_staff_role_ids,
    p.notify_staff_on_create AS panel_notify_staff_on_create, p.name_template AS panel_name_template,
    p.max_open_per_user AS panel_max_open_per_user, p.duplicate_scope AS panel_duplicate_scope,
    p.inactivity_reminder_hours AS panel_inactivity_reminder_hours,
    p.auto_close_hours AS panel_auto_close_hours,
    p.transcript_retention_days AS panel_transcript_retention_days,
    p.status AS panel_status, p.message_id AS panel_message_id, p.error_code AS panel_error_code,
    p.revision AS panel_revision, p.created_at AS panel_created_at, p.updated_at AS panel_updated_at,
    c.id AS category_record_id, c.panel_id AS category_panel_id,
    c.target_category_id AS category_target_category_id,
    c.subcategory_prompt AS category_subcategory_prompt, c.subcategories AS category_subcategories,
    c.key AS category_key, c.label AS category_label, c.description AS category_description,
    c.emoji AS category_emoji, c.staff_role_ids AS category_staff_role_ids,
    c.form_fields AS category_form_fields, c.position AS category_position, c.active AS category_active
FROM tickets t
JOIN ticket_panels p ON p.id = t.panel_id
JOIN ticket_panel_categories c ON c.id = t.category_id
"""


def _audit(
    connection: Any,
    *,
    guild_id: str,
    event_type: str,
    actor_id: int | None = None,
    target_id: str | None = None,
    channel_id: str | None = None,
    correlation_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    event_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO audit_events
        (id, guild_id, event_type, actor_discord_user_id, target_discord_id,
         channel_id, correlation_id, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(event_id),
            guild_id,
            event_type,
            str(actor_id) if actor_id is not None else None,
            target_id,
            channel_id,
            str(correlation_id or event_id),
            _json_text(details or {}),
        ),
    )


class TicketRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def _panel_record_connection(self, connection: Any, panel_row: dict[str, Any]) -> TicketPanelRecord:
        panel = _panel(panel_row)
        categories = [
            _category(row)
            for row in _all(
                connection,
                "SELECT * FROM ticket_panel_categories WHERE panel_id = ? AND active = 1 ORDER BY position",
                (str(panel.id),),
            )
        ]
        return TicketPanelRecord(panel, categories)

    async def _panel_record(self, panel_id: uuid.UUID, guild_id: int | None = None) -> TicketPanelRecord | None:
        conditions = ["id = ?"]
        parameters: list[Any] = [str(panel_id)]
        if guild_id is not None:
            conditions.append("guild_id = ?")
            parameters.append(str(guild_id))
        rows = await self.database.query(
            f"SELECT * FROM ticket_panels WHERE {' AND '.join(conditions)}", parameters
        )
        if not rows:
            return None
        categories = await self.database.query(
            "SELECT * FROM ticket_panel_categories WHERE panel_id = ? AND active = 1 ORDER BY position",
            (str(panel_id),),
        )
        return TicketPanelRecord(_panel(rows[0]), [_category(row) for row in categories])

    async def get_panel(self, *, guild_id: int, panel_id: uuid.UUID) -> TicketPanelRecord | None:
        return await self._panel_record(panel_id, guild_id)

    async def list_panels(self, *, guild_id: int) -> list[TicketPanelRecord]:
        rows = await self.database.query(
            "SELECT * FROM ticket_panels WHERE guild_id = ? AND status != 'deleted' ORDER BY created_at DESC",
            (str(guild_id),),
        )
        result: list[TicketPanelRecord] = []
        for row in rows:
            record = await self._panel_record(_uuid(row["id"]), guild_id)
            if record is not None:
                result.append(record)
        return result

    async def history(self, *, guild_id: int, limit: int = 100, open_only: bool = False) -> list[TicketRecord]:
        status_clause = "AND t.status IN ('open', 'claimed')" if open_only else ""
        rows = await self.database.query(
            f"""
            {_JOINED_SELECT}
            WHERE t.guild_id = ? AND t.status != 'deleted' {status_clause}
            ORDER BY t.created_at DESC LIMIT ?
            """,
            (str(guild_id), limit),
        )
        return [_record_from_joined_row(row) for row in rows]

    @staticmethod
    def _category_rows(panel_id: uuid.UUID, categories: list[TicketCategoryConfig]) -> list[tuple[Any, ...]]:
        return [
            (
                str(category_id := uuid.uuid4()),
                str(panel_id),
                str(category["target_category_id"]) if category["target_category_id"] else None,
                category["subcategory_prompt"],
                _json_text(category["subcategories"]),
                category["key"],
                category["label"],
                category["description"],
                category["emoji"],
                _json_text([str(value) for value in category["staff_role_ids"]]),
                _json_text(category["form_fields"]),
                position,
                1,
            )
            for position, category in enumerate(categories)
        ]

    async def create_panel(
        self,
        *,
        guild_id: int,
        actor_id: int,
        panel_channel_id: int,
        open_category_id: int,
        archive_category_id: int | None,
        title: str,
        description: str,
        opening_message: str,
        color: int,
        emoji: str,
        footer_text: str | None,
        select_placeholder: str,
        thumbnail_url: str | None,
        image_url: str | None,
        staff_role_ids: list[int],
        notify_staff_on_create: bool,
        name_template: str,
        max_open_per_user: int,
        duplicate_scope: str,
        inactivity_reminder_hours: int | None,
        auto_close_hours: int | None,
        transcript_retention_days: int,
        status: str,
        categories: list[TicketCategoryConfig],
    ) -> TicketPanelRecord:
        panel_id = uuid.uuid4()
        now = _timestamp(_now())
        category_rows = self._category_rows(panel_id, categories)

        def create(connection: Any) -> None:
            connection.execute(
                """
                INSERT INTO ticket_panels
                (id, guild_id, panel_channel_id, open_category_id, archive_category_id,
                 created_by_discord_user_id, title, description, color, emoji, footer_text,
                 opening_message, select_placeholder, thumbnail_url, image_url, staff_role_ids,
                 notify_staff_on_create, name_template, max_open_per_user, duplicate_scope,
                 inactivity_reminder_hours, auto_close_hours, transcript_retention_days,
                 status, revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    str(panel_id), str(guild_id), str(panel_channel_id), str(open_category_id),
                    str(archive_category_id) if archive_category_id else None, str(actor_id),
                    title, description, color, emoji, footer_text, opening_message, select_placeholder,
                    thumbnail_url, image_url, _json_text([str(value) for value in staff_role_ids]),
                    int(notify_staff_on_create), name_template, max_open_per_user, duplicate_scope,
                    inactivity_reminder_hours, auto_close_hours, transcript_retention_days, status, now, now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO ticket_panel_categories
                (id, panel_id, target_category_id, subcategory_prompt, subcategories, key, label, description, emoji, staff_role_ids, form_fields, position, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                category_rows,
            )
            _audit(
                connection,
                guild_id=str(guild_id),
                event_type="tickets.panel_created",
                actor_id=actor_id,
                channel_id=str(panel_channel_id),
                correlation_id=panel_id,
                details={"status": status, "categories": len(category_rows)},
            )

        await self.database.transaction(create)
        record = await self.get_panel(guild_id=guild_id, panel_id=panel_id)
        if record is None:
            raise RuntimeError("Created panel could not be loaded")
        return record

    async def update_panel(
        self,
        *,
        guild_id: int,
        panel_id: uuid.UUID,
        actor_id: int,
        requested_status: str,
        values: dict[str, Any],
        categories: list[TicketCategoryConfig],
        expected_revision: int,
    ) -> TicketPanelRecord | None:
        category_rows = self._category_rows(panel_id, categories)

        def update(connection: Any) -> bool:
            row = _one(
                connection,
                """
                SELECT * FROM ticket_panels
                WHERE id = ? AND guild_id = ? AND status IN ('draft', 'queued', 'published', 'failed') AND revision = ?
                """,
                (str(panel_id), str(guild_id), expected_revision),
            )
            if row is None:
                return False
            status = "queued" if row["message_id"] else requested_status
            now = _timestamp(_now())
            connection.execute(
                """
                UPDATE ticket_panels SET panel_channel_id=?, open_category_id=?, archive_category_id=?,
                title=?, description=?, color=?, emoji=?, footer_text=?, select_placeholder=?,
                opening_message=?,
                thumbnail_url=?, image_url=?, staff_role_ids=?, notify_staff_on_create=?,
                name_template=?, max_open_per_user=?, duplicate_scope=?, inactivity_reminder_hours=?,
                auto_close_hours=?, transcript_retention_days=?, status=?, revision=revision+1,
                error_code=NULL, updated_at=? WHERE id=?
                """,
                (
                    values["panel_channel_id"], values["open_category_id"], values["archive_category_id"],
                    values["title"], values["description"], values["color"], values["emoji"],
                    values["footer_text"], values["opening_message"], values["select_placeholder"], values["thumbnail_url"],
                    values["image_url"], _json_text(values["staff_role_ids"]),
                    int(values["notify_staff_on_create"]), values["name_template"], values["max_open_per_user"],
                    values["duplicate_scope"], values["inactivity_reminder_hours"], values["auto_close_hours"],
                    values["transcript_retention_days"], status, now, str(panel_id),
                ),
            )
            connection.execute(
                "UPDATE ticket_panel_categories SET active = 0 WHERE panel_id = ? AND active = 1",
                (str(panel_id),),
            )
            connection.executemany(
                """
                INSERT INTO ticket_panel_categories
                (id, panel_id, target_category_id, subcategory_prompt, subcategories, key, label, description, emoji, staff_role_ids, form_fields, position, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                category_rows,
            )
            _audit(
                connection,
                guild_id=str(guild_id),
                event_type="tickets.panel_updated",
                actor_id=actor_id,
                channel_id=str(values["panel_channel_id"]),
                correlation_id=panel_id,
                details={"revision": expected_revision + 1, "status": status},
            )
            return True

        if not await self.database.transaction(update):
            return None
        return await self.get_panel(guild_id=guild_id, panel_id=panel_id)

    async def queue_panel(self, *, guild_id: int, panel_id: uuid.UUID, actor_id: int) -> bool:
        def queue(connection: Any) -> bool:
            row = _one(
                connection,
                """
                SELECT * FROM ticket_panels WHERE id = ? AND guild_id = ?
                AND status IN ('draft', 'published', 'failed')
                """,
                (str(panel_id), str(guild_id)),
            )
            if row is None:
                return False
            now = _timestamp(_now())
            connection.execute(
                "UPDATE ticket_panels SET status='queued', error_code=NULL, updated_at=? WHERE id=?",
                (now, str(panel_id)),
            )
            _audit(
                connection,
                guild_id=str(guild_id), event_type="tickets.panel_queued", actor_id=actor_id,
                channel_id=str(row["panel_channel_id"]), correlation_id=panel_id,
            )
            return True

        return await self.database.transaction(queue)

    async def request_panel_delete(self, *, guild_id: int, panel_id: uuid.UUID, actor_id: int) -> bool:
        def request(connection: Any) -> bool:
            row = _one(
                connection,
                """
                SELECT * FROM ticket_panels WHERE id = ? AND guild_id = ?
                AND status IN ('draft', 'queued', 'published', 'failed')
                """,
                (str(panel_id), str(guild_id)),
            )
            if row is None:
                return False
            has_tickets = _one(connection, "SELECT 1 AS found FROM tickets WHERE panel_id = ? LIMIT 1", (str(panel_id),))
            if row["message_id"]:
                connection.execute("UPDATE ticket_panels SET status='queued_delete' WHERE id=?", (str(panel_id),))
            elif has_tickets:
                connection.execute(
                    "UPDATE ticket_panels SET status='deleted', error_code=NULL, updated_at=? WHERE id=?",
                    (_timestamp(_now()), str(panel_id)),
                )
            else:
                connection.execute("DELETE FROM ticket_panels WHERE id=?", (str(panel_id),))
            _audit(
                connection, guild_id=str(guild_id), event_type="tickets.panel_delete_requested", actor_id=actor_id,
                channel_id=str(row["panel_channel_id"]), correlation_id=panel_id,
            )
            return True

        return await self.database.transaction(request)

    async def force_delete_panel_record(self, *, guild_id: int, panel_id: uuid.UUID, actor_id: int) -> bool:
        def force_delete(connection: Any) -> bool:
            row = _one(
                connection,
                "SELECT * FROM ticket_panels WHERE id=? AND guild_id=? AND status NOT IN ('syncing','deleting')",
                (str(panel_id), str(guild_id)),
            )
            if row is None:
                return False
            _audit(
                connection, guild_id=str(guild_id), event_type="tickets.panel_force_deleted", actor_id=actor_id,
                target_id=str(row["panel_channel_id"]), channel_id=str(row["panel_channel_id"]),
                correlation_id=panel_id, details={"previous_status": row["status"], "error_code": row["error_code"]},
            )
            connection.execute("DELETE FROM ticket_panels WHERE id=?", (str(panel_id),))
            return True

        return await self.database.transaction(force_delete)

    async def claim_panel_sync(self) -> TicketPanelRecord | None:
        def claim(connection: Any) -> TicketPanelRecord | None:
            row = _one(
                connection,
                "SELECT * FROM ticket_panels WHERE status IN ('queued','queued_delete') ORDER BY updated_at LIMIT 1",
            )
            if row is None:
                return None
            status = "deleting" if row["status"] == "queued_delete" else "syncing"
            now = _timestamp(_now())
            connection.execute("UPDATE ticket_panels SET status=?, updated_at=? WHERE id=?", (status, now, row["id"]))
            row["status"] = status
            row["updated_at"] = now
            return self._panel_record_connection(connection, row)

        return await self.database.transaction(claim)

    async def recover_stale_work(self, *, minimum_age: timedelta) -> dict[str, int]:
        cutoff = _timestamp(_now() - minimum_age)

        def recover(connection: Any) -> dict[str, int]:
            panels = _all(connection, "SELECT * FROM ticket_panels WHERE status IN ('syncing','deleting') AND updated_at <= ? LIMIT 100", (cutoff,))
            for row in panels:
                previous = str(row["status"])
                status = "queued_delete" if previous == "deleting" else "queued"
                connection.execute(
                    "UPDATE ticket_panels SET status=?, error_code='recovered_stale_claim', updated_at=? WHERE id=?",
                    (status, _timestamp(_now()), row["id"]),
                )
                _audit(connection, guild_id=str(row["guild_id"]), event_type="tickets.panel_recovered", channel_id=str(row["panel_channel_id"]), correlation_id=_uuid(row["id"]), details={"previous_status": previous})
            tickets = _all(connection, "SELECT * FROM tickets WHERE status='creating' AND updated_at <= ? LIMIT 100", (cutoff,))
            for row in tickets:
                connection.execute("UPDATE tickets SET status='failed', close_reason='recovered_stale_creation', updated_at=? WHERE id=?", (_timestamp(_now()), row["id"]))
                _audit(connection, guild_id=str(row["guild_id"]), event_type="tickets.creation_failed", actor_id=int(row["requester_id"]), target_id=str(row["requester_id"]), correlation_id=_uuid(row["id"]), details={"error_code": "recovered_stale_creation"})
            return {"panels": len(panels), "tickets": len(tickets)}

        return await self.database.transaction(recover)

    async def mark_panel_published(self, panel_id: uuid.UUID, message_id: int) -> None:
        await self.database.execute(
            "UPDATE ticket_panels SET status='published', message_id=?, error_code=NULL, updated_at=? WHERE id=? AND status='syncing'",
            (str(message_id), _timestamp(_now()), str(panel_id)),
        )

    async def mark_panel_failed(self, panel_id: uuid.UUID, error_code: str) -> None:
        await self.database.execute(
            "UPDATE ticket_panels SET status='failed', error_code=?, updated_at=? WHERE id=? AND status IN ('syncing','deleting')",
            (error_code[:100], _timestamp(_now()), str(panel_id)),
        )

    async def finish_panel_delete(self, panel_id: uuid.UUID) -> None:
        def finish(connection: Any) -> None:
            row = _one(connection, "SELECT * FROM ticket_panels WHERE id=? AND status='deleting'", (str(panel_id),))
            if row is None:
                return
            has_tickets = _one(connection, "SELECT 1 AS found FROM tickets WHERE panel_id=? LIMIT 1", (str(panel_id),))
            if has_tickets:
                connection.execute("UPDATE ticket_panels SET status='deleted', message_id=NULL, error_code=NULL, updated_at=? WHERE id=?", (_timestamp(_now()), str(panel_id)))
            else:
                connection.execute("DELETE FROM ticket_panels WHERE id=?", (str(panel_id),))

        await self.database.transaction(finish)

    async def published_panels(self) -> list[TicketPanelRecord]:
        rows = await self.database.query("SELECT * FROM ticket_panels WHERE status='published' AND message_id IS NOT NULL")
        result: list[TicketPanelRecord] = []
        for row in rows:
            record = await self._panel_record(_uuid(row["id"]))
            if record is not None:
                result.append(record)
        return result

    async def panel_category(self, category_id: uuid.UUID) -> tuple[TicketPanel, TicketPanelCategory] | None:
        rows = await self.database.query(
            """
            SELECT p.* FROM ticket_panels p JOIN ticket_panel_categories c ON c.panel_id=p.id
            WHERE c.id=? AND c.active=1 AND p.status IN ('published', 'queued', 'syncing')
            """,
            (str(category_id),),
        )
        if not rows:
            return None
        panel = _panel(rows[0])
        category_rows = await self.database.query(
            "SELECT * FROM ticket_panel_categories WHERE id=? AND active=1", (str(category_id),)
        )
        return (panel, _category(category_rows[0])) if category_rows else None

    def _lock_key(self, guild_id: int, requester_id: int) -> int:
        digest = hashlib.blake2b(f"{guild_id}:{requester_id}".encode(), digest_size=8).digest()
        return int.from_bytes(digest, signed=True)

    async def begin_ticket(
        self,
        *,
        panel: TicketPanel,
        category: TicketPanelCategory,
        requester_id: int,
        subject: str,
        answers: dict[str, str],
    ) -> tuple[Ticket | None, str | None]:
        def begin(connection: Any) -> tuple[Ticket | None, str | None]:
            conditions = ["guild_id=?", "requester_id=?", "status IN ('creating','open','claimed')"]
            parameters: list[Any] = [panel.guild_id, str(requester_id)]
            if panel.duplicate_scope != "guild":
                conditions.append("category_id=?")
                parameters.append(str(category.id))
            row = _one(connection, f"SELECT COUNT(*) AS count FROM tickets WHERE {' AND '.join(conditions)}", tuple(parameters))
            if int(row["count"] if row else 0) >= panel.max_open_per_user:
                return None, "ticket_limit_reached"
            ticket_id = uuid.uuid4()
            number_row = _one(
                connection,
                "SELECT COALESCE(MAX(ticket_number), 0) + 1 AS next_number FROM tickets WHERE guild_id=?",
                (panel.guild_id,),
            )
            next_number = int(number_row["next_number"] if number_row else 1)
            ticket_number = ((next_number - 1) % 999) + 1
            now = _timestamp(_now())
            connection.execute(
                "INSERT INTO tickets (id,ticket_number,panel_id,category_id,guild_id,requester_id,status,priority,subject,answers,created_at,updated_at,last_activity_at,reopen_count) VALUES (?,?,?,?,?,?,'creating','normal',?,?,?,?,?,0)",
                (str(ticket_id), ticket_number, str(panel.id), str(category.id), panel.guild_id, str(requester_id), subject, _json_text(answers), now, now, now),
            )
            _audit(connection, guild_id=panel.guild_id, event_type="tickets.creation_started", actor_id=requester_id, target_id=str(requester_id), correlation_id=ticket_id, details={"category": category.key})
            row = _one(connection, "SELECT * FROM tickets WHERE id=?", (str(ticket_id),))
            return (_ticket(row) if row else None), None

        return await self.database.transaction(begin)

    async def finish_ticket_creation(self, ticket_id: uuid.UUID, channel_id: int, control_message_id: int, staff_thread_id: int, staff_control_message_id: int, staff_notification_role_ids: list[int]) -> Ticket | None:
        def finish(connection: Any) -> Ticket | None:
            now = _timestamp(_now())
            connection.execute(
                "UPDATE tickets SET status='open', channel_id=?, control_message_id=?, staff_thread_id=?, staff_control_message_id=?, updated_at=? WHERE id=? AND status='creating'",
                (str(channel_id), str(control_message_id), str(staff_thread_id), str(staff_control_message_id), now, str(ticket_id)),
            )
            row = _one(connection, "SELECT * FROM tickets WHERE id=?", (str(ticket_id),))
            if row is None or row["status"] != "open":
                return None
            _audit(connection, guild_id=str(row["guild_id"]), event_type="tickets.opened", actor_id=int(row["requester_id"]), target_id=str(row["requester_id"]), channel_id=str(channel_id), correlation_id=ticket_id, details={"staff_notification_role_ids": [str(value) for value in staff_notification_role_ids]})
            return _ticket(row)

        return await self.database.transaction(finish)

    async def set_staff_controls(self, *, ticket_id: uuid.UUID, thread_id: int, message_id: int) -> bool:
        return await self.database.execute(
            "UPDATE tickets SET staff_thread_id=?, staff_control_message_id=?, updated_at=? WHERE id=? AND staff_thread_id IS NULL",
            (str(thread_id), str(message_id), _timestamp(_now()), str(ticket_id)),
        ) > 0

    async def replace_staff_controls(self, *, ticket_id: uuid.UUID, thread_id: int, message_id: int) -> bool:
        return await self.database.execute(
            "UPDATE tickets SET staff_thread_id=?, staff_control_message_id=?, updated_at=? WHERE id=?",
            (str(thread_id), str(message_id), _timestamp(_now()), str(ticket_id)),
        ) > 0

    async def fail_ticket_creation(self, ticket_id: uuid.UUID, error_code: str) -> None:
        def fail(connection: Any) -> None:
            connection.execute("UPDATE tickets SET status='failed', close_reason=?, updated_at=? WHERE id=? AND status='creating'", (error_code[:1000], _timestamp(_now()), str(ticket_id)))
            row = _one(connection, "SELECT * FROM tickets WHERE id=?", (str(ticket_id),))
            if row is not None and row["status"] == "failed":
                _audit(connection, guild_id=str(row["guild_id"]), event_type="tickets.creation_failed", actor_id=int(row["requester_id"]), target_id=str(row["requester_id"]), correlation_id=ticket_id, details={"error_code": error_code[:100]})

        await self.database.transaction(fail)

    async def _ticket_record(self, where: str, parameters: tuple[Any, ...]) -> TicketRecord | None:
        rows = await self.database.query(
            f"{_JOINED_SELECT} WHERE {where}",
            parameters,
        )
        return _record_from_joined_row(rows[0]) if rows else None

    async def get_by_channel(self, channel_id: int) -> TicketRecord | None:
        return await self._ticket_record("t.channel_id=?", (str(channel_id),))

    async def get_ticket(self, ticket_id: uuid.UUID) -> TicketRecord | None:
        return await self._ticket_record("t.id=?", (str(ticket_id),))

    async def change_ticket_category(
        self,
        *,
        ticket_id: uuid.UUID,
        category_id: uuid.UUID,
        actor_id: int,
    ) -> bool:
        def change(connection: Any) -> bool:
            ticket = _one(
                connection,
                """
                SELECT t.*, old_category.key AS old_category_key
                FROM tickets t
                JOIN ticket_panel_categories old_category ON old_category.id=t.category_id
                WHERE t.id=? AND t.status NOT IN ('creating', 'failed', 'deleted')
                """,
                (str(ticket_id),),
            )
            if ticket is None:
                return False
            category = _one(
                connection,
                """
                SELECT c.* FROM ticket_panel_categories c
                JOIN tickets t ON t.panel_id=c.panel_id
                WHERE t.id=? AND c.id=? AND c.active=1
                """,
                (str(ticket_id), str(category_id)),
            )
            if category is None:
                return False
            if str(ticket["category_id"]) == str(category_id):
                return True
            now = _timestamp(_now())
            connection.execute(
                "UPDATE tickets SET category_id=?, updated_at=? WHERE id=?",
                (str(category_id), now, str(ticket_id)),
            )
            _audit(
                connection,
                guild_id=str(ticket["guild_id"]),
                event_type="tickets.category_changed",
                actor_id=actor_id,
                target_id=str(ticket["requester_id"]),
                channel_id=str(ticket["channel_id"]) if ticket["channel_id"] else None,
                correlation_id=ticket_id,
                details={
                    "old_category_id": str(ticket["category_id"]),
                    "old_category_key": str(ticket["old_category_key"]),
                    "new_category_id": str(category_id),
                    "new_category_key": str(category["key"]),
                },
            )
            return True

        return await self.database.transaction(change)

    async def active_channel_order(self, *, guild_id: int, target_category_id: int) -> list[str]:
        rows = await self.database.query(
            """
            SELECT t.channel_id, t.priority, t.created_at FROM tickets t
            JOIN ticket_panels p ON p.id=t.panel_id
            JOIN ticket_panel_categories c ON c.id=t.category_id
            WHERE t.guild_id=?
            AND COALESCE(c.target_category_id, p.open_category_id)=?
            AND t.channel_id IS NOT NULL
            AND t.status IN ('open','claimed')
            """,
            (str(guild_id), str(target_category_id)),
        )
        rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        rows.sort(key=lambda row: (rank.get(str(row["priority"]), 2), str(row["created_at"]), str(row["channel_id"])))
        return [str(row["channel_id"]) for row in rows]

    async def assign(self, *, ticket_id: uuid.UUID, actor_id: int, assignee_id: int) -> Ticket | None:
        def assign_ticket(connection: Any) -> Ticket | None:
            now = _timestamp(_now())
            connection.execute("UPDATE tickets SET status='claimed', assignee_id=?, first_staff_response_at=COALESCE(first_staff_response_at, ?), updated_at=? WHERE id=? AND status IN ('open','claimed')", (str(assignee_id), now, now, str(ticket_id)))
            row = _one(connection, "SELECT * FROM tickets WHERE id=?", (str(ticket_id),))
            if row is not None and row["status"] == "claimed":
                _audit(connection, guild_id=str(row["guild_id"]), event_type="tickets.assigned", actor_id=actor_id, target_id=str(row["requester_id"]), channel_id=row["channel_id"], correlation_id=ticket_id, details={"assignee_id": str(assignee_id)})
                return _ticket(row)
            return None

        return await self.database.transaction(assign_ticket)

    async def set_priority(self, *, ticket_id: uuid.UUID, actor_id: int, priority: str) -> Ticket | None:
        def set_ticket_priority(connection: Any) -> Ticket | None:
            connection.execute("UPDATE tickets SET priority=?, updated_at=? WHERE id=? AND status IN ('open','claimed')", (priority, _timestamp(_now()), str(ticket_id)))
            row = _one(connection, "SELECT * FROM tickets WHERE id=?", (str(ticket_id),))
            if row is not None and row["priority"] == priority and row["status"] in {"open", "claimed"}:
                _audit(connection, guild_id=str(row["guild_id"]), event_type="tickets.priority_changed", actor_id=actor_id, target_id=str(row["requester_id"]), channel_id=row["channel_id"], correlation_id=ticket_id, details={"priority": priority})
                return _ticket(row)
            return None

        return await self.database.transaction(set_ticket_priority)

    async def transition(self, *, ticket_id: uuid.UUID, actor_id: int, from_statuses: tuple[str, ...], to_status: str, reason: str | None = None) -> Ticket | None:
        def change(connection: Any) -> Ticket | None:
            now = _timestamp(_now())
            sets = ["status=?", "updated_at=?"]
            parameters: list[Any] = [to_status, now]
            if to_status == "closed":
                sets.extend(["closed_at=?", "close_reason=?"])
                parameters.extend([now, reason])
            elif to_status == "archived":
                sets.append("archived_at=?")
                parameters.append(now)
            elif to_status in {"open", "claimed"}:
                sets.extend(["closed_at=NULL", "archived_at=NULL", "close_reason=NULL", "reopen_count=reopen_count+1"])
                if to_status == "open":
                    sets.append("assignee_id=NULL")
            placeholders = ",".join("?" for _ in from_statuses)
            parameters.extend([str(ticket_id), *from_statuses])
            connection.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=? AND status IN ({placeholders})", tuple(parameters))
            row = _one(connection, "SELECT * FROM tickets WHERE id=?", (str(ticket_id),))
            if row is None or row["status"] != to_status:
                return None
            _audit(connection, guild_id=str(row["guild_id"]), event_type=f"tickets.{to_status}", actor_id=actor_id, target_id=str(row["requester_id"]), channel_id=row["channel_id"], correlation_id=ticket_id, details={"reason": reason})
            return _ticket(row)

        return await self.database.transaction(change)

    async def mark_deleted(self, *, ticket_id: uuid.UUID, actor_id: int, reason: str, guild_id: int | None = None) -> Ticket | None:
        def delete_ticket(connection: Any) -> Ticket | None:
            conditions = ["id=?", "status != 'deleted'"]
            parameters: list[Any] = [str(ticket_id)]
            if guild_id is not None:
                conditions.append("guild_id=?")
                parameters.append(str(guild_id))
            now = _timestamp(_now())
            connection.execute(f"UPDATE tickets SET status='deleted', close_reason=?, closed_at=COALESCE(closed_at, ?), archived_at=?, updated_at=? WHERE {' AND '.join(conditions)}", (reason[:1000], now, now, now, *parameters))
            row = _one(connection, "SELECT * FROM tickets WHERE id=?", (str(ticket_id),))
            if row is not None and row["status"] == "deleted":
                _audit(connection, guild_id=str(row["guild_id"]), event_type="tickets.deleted", actor_id=actor_id, target_id=str(row["requester_id"]), channel_id=row["channel_id"], correlation_id=ticket_id, details={"reason": reason[:1000]})
                return _ticket(row)
            return None

        return await self.database.transaction(delete_ticket)

    async def add_note(self, *, ticket_id: uuid.UUID, author_id: int, content: str) -> bool:
        def add(connection: Any) -> bool:
            ticket = _one(connection, "SELECT * FROM tickets WHERE id=?", (str(ticket_id),))
            if ticket is None:
                return False
            note_id = uuid.uuid4()
            connection.execute("INSERT INTO ticket_internal_notes (id,ticket_id,author_id,content) VALUES (?,?,?,?)", (str(note_id), str(ticket_id), str(author_id), content))
            _audit(connection, guild_id=str(ticket["guild_id"]), event_type="tickets.note_added", actor_id=author_id, target_id=str(ticket["requester_id"]), channel_id=ticket["channel_id"], correlation_id=ticket_id, details={"note_id": str(note_id), "content_length": len(content)})
            return True

        return await self.database.transaction(add)

    async def notes_for_ticket(self, ticket_id: uuid.UUID, *, limit: int = 10) -> list[TicketInternalNote]:
        rows = await self.database.query("SELECT * FROM ticket_internal_notes WHERE ticket_id=? ORDER BY created_at DESC LIMIT ?", (str(ticket_id), limit))
        return list(reversed([_note(row) for row in rows]))

    async def touch_activity(self, *, channel_id: int, actor_id: int, staff: bool) -> bool:
        """Record ticket activity, skipping writes that are too close together.

        Discord can deliver many messages in a busy ticket. Inactivity actions
        use hour-scale thresholds, so updating the same row for every message
        only creates unnecessary Turso writes and timestamp churn. Staff's
        first response is always allowed through even when the last activity
        update was recent.
        """
        now = _now()
        timestamp = _timestamp(now)
        cutoff = _timestamp(now - timedelta(seconds=15))
        if staff:
            return (
                await self.database.execute(
                    """
                    UPDATE tickets
                    SET last_activity_at=?, reminded_at=NULL,
                        first_staff_response_at=COALESCE(first_staff_response_at, ?)
                    WHERE channel_id=? AND status IN ('open','claimed')
                    AND (last_activity_at <= ? OR first_staff_response_at IS NULL)
                    """,
                    (timestamp, timestamp, str(channel_id), cutoff),
                )
                > 0
            )
        return (
            await self.database.execute(
                """
                UPDATE tickets
                SET last_activity_at=?, reminded_at=NULL
                WHERE channel_id=? AND status IN ('open','claimed')
                AND last_activity_at <= ?
                """,
                (timestamp, str(channel_id), cutoff),
            )
            > 0
        )

    async def tickets_with_controls(
        self,
        *,
        guild_id: int | None = None,
        panel_id: uuid.UUID | None = None,
    ) -> list[Ticket]:
        conditions = [
            "control_message_id IS NOT NULL",
            "channel_id IS NOT NULL",
            "status IN ('open','claimed','closed')",
        ]
        parameters: list[str] = []
        if guild_id is not None:
            conditions.append("guild_id=?")
            parameters.append(str(guild_id))
        if panel_id is not None:
            conditions.append("panel_id=?")
            parameters.append(str(panel_id))
        rows = await self.database.query(
            f"SELECT * FROM tickets WHERE {' AND '.join(conditions)}",
            tuple(parameters),
        )
        return [_ticket(row) for row in rows]

    async def active_for_inactivity(self, *, limit: int = 200) -> list[TicketRecord]:
        rows = await self.database.query(f"{_JOINED_SELECT} WHERE t.status IN ('open','claimed') ORDER BY t.last_activity_at LIMIT ?", (limit,))
        return [_record_from_joined_row(row) for row in rows]

    async def mark_reminded(self, ticket_id: uuid.UUID) -> bool:
        return await self.database.execute("UPDATE tickets SET reminded_at=? WHERE id=? AND status IN ('open','claimed') AND reminded_at IS NULL", (_timestamp(_now()), str(ticket_id))) > 0

    async def save_transcript(self, *, ticket_id: uuid.UUID, content: str, message_count: int, retention_days: int) -> None:
        now = _now()
        await self.database.execute(
            """
            INSERT INTO ticket_transcripts (ticket_id,content,message_count,generated_at,expires_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(ticket_id) DO UPDATE SET content=excluded.content,
            message_count=excluded.message_count, generated_at=excluded.generated_at, expires_at=excluded.expires_at
            """,
            (str(ticket_id), content, message_count, _timestamp(now), _timestamp(now + timedelta(days=retention_days))),
        )
        record = await self.get_ticket(ticket_id)
        if record is not None:
            await self.database.transaction(lambda connection: _audit(connection, guild_id=record.ticket.guild_id, event_type="tickets.transcript_generated", target_id=record.ticket.requester_id, channel_id=record.ticket.channel_id, correlation_id=ticket_id, details={"message_count": message_count, "retention_days": retention_days}))

    async def get_transcript(self, *, guild_id: int, ticket_id: uuid.UUID) -> TicketTranscript | None:
        rows = await self.database.query("SELECT tr.* FROM ticket_transcripts tr JOIN tickets t ON t.id=tr.ticket_id WHERE tr.ticket_id=? AND t.guild_id=?", (str(ticket_id), str(guild_id)))
        return _transcript(rows[0]) if rows else None

    async def delete_expired_transcripts(self) -> int:
        return await self.database.execute("DELETE FROM ticket_transcripts WHERE expires_at <= ?", (_timestamp(_now()),))

    async def summary(self, *, guild_id: int) -> dict[str, Any]:
        rows = await self.database.query("SELECT status, COUNT(*) AS count FROM tickets WHERE guild_id=? AND status != 'deleted' GROUP BY status", (str(guild_id),))
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {"total": sum(counts.values()), "backlog": counts.get("open", 0) + counts.get("claimed", 0), "closed": counts.get("closed", 0), "average_response_seconds": 0.0, "average_resolution_seconds": 0.0, "by_category": [], "by_staff": []}


def _record_from_joined_row(row: dict[str, Any]) -> TicketRecord:
    ticket_columns = {
        "id", "panel_id", "category_id", "guild_id", "requester_id", "channel_id", "control_message_id",
        "staff_thread_id", "staff_control_message_id", "status", "priority", "assignee_id", "subject",
        "answers", "close_reason", "created_at", "updated_at", "last_activity_at", "first_staff_response_at",
        "closed_at", "archived_at", "reminded_at", "reopen_count",
    }
    ticket_row = {key: value for key, value in row.items() if key in ticket_columns}
    panel_row = {
        "id": row["panel_record_id"], "guild_id": row["panel_guild_id"],
        "panel_channel_id": row["panel_channel_id"], "open_category_id": row["panel_open_category_id"],
        "archive_category_id": row["panel_archive_category_id"],
        "created_by_discord_user_id": row["panel_created_by_discord_user_id"],
        "title": row["panel_title"], "description": row["panel_description"],
        "opening_message": row["panel_opening_message"], "color": row["panel_color"],
        "emoji": row["panel_emoji"], "footer_text": row["panel_footer_text"],
        "select_placeholder": row["panel_select_placeholder"], "thumbnail_url": row["panel_thumbnail_url"],
        "image_url": row["panel_image_url"], "staff_role_ids": row["panel_staff_role_ids"],
        "notify_staff_on_create": row["panel_notify_staff_on_create"],
        "name_template": row["panel_name_template"], "max_open_per_user": row["panel_max_open_per_user"],
        "duplicate_scope": row["panel_duplicate_scope"],
        "inactivity_reminder_hours": row["panel_inactivity_reminder_hours"],
        "auto_close_hours": row["panel_auto_close_hours"],
        "transcript_retention_days": row["panel_transcript_retention_days"],
        "status": row["panel_status"], "message_id": row["panel_message_id"],
        "error_code": row["panel_error_code"], "revision": row["panel_revision"],
        "created_at": row["panel_created_at"], "updated_at": row["panel_updated_at"],
    }
    category_row = {
        "id": row["category_record_id"], "panel_id": row["category_panel_id"],
        "target_category_id": row["category_target_category_id"], "key": row["category_key"],
        "subcategory_prompt": row["category_subcategory_prompt"],
        "subcategories": row["category_subcategories"],
        "label": row["category_label"], "description": row["category_description"],
        "emoji": row["category_emoji"], "staff_role_ids": row["category_staff_role_ids"],
        "form_fields": row["category_form_fields"], "position": row["category_position"],
        "active": row["category_active"],
    }
    return TicketRecord(_ticket(ticket_row), _panel(panel_row), _category(category_row))
