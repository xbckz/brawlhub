"""Plain ticket records shared by the libSQL repositories and Discord cogs."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Guild:
    id: str
    name: str
    installed_at: datetime
    removed_at: datetime | None
    updated_at: datetime


@dataclass(slots=True)
class TicketAdmin:
    guild_id: str
    user_id: str
    added_by_discord_user_id: str
    added_at: datetime


@dataclass(slots=True)
class AuditEvent:
    id: uuid.UUID
    guild_id: str
    occurred_at: datetime
    event_type: str
    actor_discord_user_id: str | None
    target_discord_id: str | None
    channel_id: str | None
    correlation_id: uuid.UUID
    details: dict[str, Any]


@dataclass(slots=True)
class TicketPanel:
    id: uuid.UUID
    guild_id: str
    panel_channel_id: str
    open_category_id: str
    archive_category_id: str | None
    created_by_discord_user_id: str
    title: str
    description: str
    opening_message: str
    color: int
    emoji: str
    footer_text: str | None
    select_placeholder: str
    thumbnail_url: str | None
    image_url: str | None
    staff_role_ids: list[str]
    notify_staff_on_create: bool
    name_template: str
    max_open_per_user: int
    duplicate_scope: str
    inactivity_reminder_hours: int | None
    auto_close_hours: int | None
    transcript_retention_days: int
    status: str
    message_id: str | None
    error_code: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class TicketPanelCategory:
    id: uuid.UUID
    panel_id: uuid.UUID
    target_category_id: str | None
    subcategory_prompt: str
    subcategories: list[dict[str, Any]]
    key: str
    label: str
    description: str | None
    emoji: str | None
    staff_role_ids: list[str]
    notify_staff: bool
    form_fields: list[dict[str, Any]]
    position: int
    active: bool


@dataclass(slots=True)
class Ticket:
    id: uuid.UUID
    ticket_number: int
    panel_id: uuid.UUID
    category_id: uuid.UUID
    guild_id: str
    requester_id: str
    channel_id: str | None
    control_message_id: str | None
    staff_thread_id: str | None
    staff_control_message_id: str | None
    status: str
    priority: str
    assignee_id: str | None
    subject: str
    answers: dict[str, str]
    close_reason: str | None
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    first_staff_response_at: datetime | None
    closed_at: datetime | None
    archived_at: datetime | None
    reminded_at: datetime | None
    reopen_count: int


@dataclass(slots=True)
class TicketInternalNote:
    id: uuid.UUID
    ticket_id: uuid.UUID
    author_id: str
    content: str
    created_at: datetime


@dataclass(slots=True)
class TicketTranscript:
    ticket_id: uuid.UUID
    content: str
    message_count: int
    generated_at: datetime
    expires_at: datetime
