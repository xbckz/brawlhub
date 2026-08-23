"""Persistent ticket panels, private channels, lifecycle controls, and transcripts."""

import asyncio
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bhbot.db.models import TicketPanel, TicketPanelCategory
from bhbot.db.ticket_repository import (
    TicketPanelRecord,
    TicketRecord,
    TicketRepository,
)
from bhbot.ui import (
    SafeModal,
    SafeView,
    respond_ephemeral,
    safe_component_emoji,
    themed_embed,
)

if TYPE_CHECKING:
    from bhbot.bot import TicketBot

logger = logging.getLogger(__name__)
PRIORITIES = ("low", "normal", "high", "urgent")
OPEN_NAME_DEBOUNCE_SECONDS = 15.0
CLOSED_NAME_DEBOUNCE_SECONDS = 30.0
STATUS_CHANNEL_DOTS = {
    "open": "🟢",
    "claimed": "🔵",
    "closed": "🔴",
    "archived": "⚫",
}
STATUS_DOT_PREFIX = re.compile(r"^[🟢🔵🔴⚫⚪]\s*[・•·-]?\s*")
MESSAGE_CHANNEL_CACHE_SECONDS = 60.0
NON_TICKET_CHANNEL_CACHE_SECONDS = 300.0
ACTIVITY_WRITE_COOLDOWN_SECONDS = 15.0
STAFF_MEMBER_CACHE_SECONDS = 300.0
STAFF_THREAD_SYNC_COOLDOWN_SECONDS = 300.0
VISIBLE_MEMBER_CACHE_SECONDS = 60.0


def is_ticket_staff(
    member: discord.Member,
    panel: TicketPanel,
    category: TicketPanelCategory | None = None,
) -> bool:
    return member.guild_permissions.manage_channels or has_ticket_staff_role(
        member, panel, category
    )


def has_ticket_staff_role_ids(member: discord.Member, staff_role_ids: list[str]) -> bool:
    return bool({str(role.id) for role in member.roles} & set(staff_role_ids))


def has_ticket_staff_role(
    member: discord.Member,
    panel: TicketPanel,
    category: TicketPanelCategory | None = None,
) -> bool:
    staff_role_ids = (
        category.staff_role_ids
        if category is not None and category.staff_role_ids
        else panel.staff_role_ids
    )
    return has_ticket_staff_role_ids(member, staff_role_ids)


def safe_channel_name(
    template: str,
    member: discord.Member,
    category: str,
    ticket_id: uuid.UUID,
    ticket_number: int,
) -> str:
    if template.strip() == "ticket-{category}-{username}":
        template = "{number}-{category}-{username}"
    raw = template.format(
        username=member.display_name,
        category=category,
        id=str(ticket_id)[:8],
        number=f"{ticket_number:03d}",
    )
    normalized = "".join(character for character in raw if character.isprintable())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return (normalized or f"{ticket_number:03d}-ticket")[:90]


def status_channel_name(channel_name: str, status: str) -> str:
    base_name = STATUS_DOT_PREFIX.sub("", channel_name).strip()
    dot = STATUS_CHANNEL_DOTS.get(status, "⚪")
    return f"{dot}・{base_name or 'ticket'}"[:100]


def ticket_staff_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        send_messages_in_threads=True,
        read_message_history=True,
        manage_threads=True,
        manage_messages=True,
        attach_files=True,
    )


def ticket_channel_overwrites(
    guild: discord.Guild,
    requester: discord.Member,
    bot_member: discord.Member,
    staff_role_ids: list[str],
) -> MutableMapping[
    discord.Role | discord.Member | discord.Object,
    discord.PermissionOverwrite,
]:
    requester_overwrite = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
        embed_links=True,
    )
    if not requester.guild_permissions.manage_channels and not has_ticket_staff_role_ids(
        requester, staff_role_ids
    ):
        # Private-thread visibility is governed by Manage Threads and the
        # thread messaging permission on the parent ticket channel. Ordinary
        # requesters must not inherit access to the staff-only control thread.
        requester_overwrite.update(
            manage_threads=False,
            send_messages_in_threads=False,
        )
    overwrites: MutableMapping[
        discord.Role | discord.Member | discord.Object,
        discord.PermissionOverwrite,
    ] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        requester: requester_overwrite,
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
            manage_threads=True,
            send_messages_in_threads=True,
        ),
    }
    for role_id in staff_role_ids:
        role = guild.get_role(int(role_id))
        if role:
            overwrites[role] = ticket_staff_overwrite()
    return overwrites


class TicketRequestModal(SafeModal):
    def __init__(
        self, cog: "TicketsCog", panel: TicketPanel, category: TicketPanelCategory
    ) -> None:
        super().__init__(
            title=f"Open: {category.label}"[:45],
            custom_id=f"ticket-request:{category.id}",
            timeout=300,
        )
        self.cog = cog
        self.panel = panel
        self.category = category
        self.inputs: dict[str, discord.ui.TextInput[discord.ui.Modal]] = {}
        for field in category.form_fields:
            item: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
                label=str(field["label"]),
                custom_id=str(field["key"]),
                style=(
                    discord.TextStyle.paragraph
                    if field["style"] == "paragraph"
                    else discord.TextStyle.short
                ),
                required=bool(field["required"]),
                min_length=(int(field["minimum_length"]) or None),
                max_length=int(field["maximum_length"]),
                placeholder=str(field["placeholder"]) if field.get("placeholder") else None,
            )
            self.inputs[str(field["key"])] = item
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.create_ticket(
            interaction,
            self.panel,
            self.category,
            {key: item.value.strip() for key, item in self.inputs.items()},
        )


class TicketPanelSelect(discord.ui.Select["TicketPanelView"]):
    def __init__(
        self,
        cog: "TicketsCog",
        record: TicketPanelRecord,
        *,
        placeholder: str | None = None,
        category_labels: dict[uuid.UUID, str] | None = None,
        category_descriptions: dict[uuid.UUID, str] | None = None,
        disabled: bool = False,
    ) -> None:
        category_labels = category_labels or {}
        category_descriptions = category_descriptions or {}
        super().__init__(
            custom_id=f"ticket-panel:{record.panel.id}",
            placeholder=(placeholder or record.panel.select_placeholder)[:150],
            disabled=disabled,
            options=[
                discord.SelectOption(
                    label=category_labels.get(item.id, item.label)[:100],
                    value=item.key,
                    description=(
                        category_descriptions.get(item.id, item.description or "")[:100] or None
                    ),
                    emoji=safe_component_emoji(item.emoji),
                )
                for item in record.categories[:25]
            ],
        )
        self.cog = cog
        self.record = record

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Tickets require a server.", ephemeral=True)
            return
        selected_key = self.values[0]
        current_record = await self.cog.repository.get_panel(
            guild_id=interaction.guild.id, panel_id=self.record.panel.id
        )
        if current_record is None:
            await interaction.response.send_message(
                "This ticket category is no longer available.", ephemeral=True
            )
            return
        panel = current_record.panel
        category = next((item for item in current_record.categories if item.key == selected_key), None)
        if category is None:
            # Accept legacy panel messages whose select values used category
            # UUIDs before the editor switched to stable category keys.
            try:
                legacy_id = uuid.UUID(selected_key)
            except ValueError:
                legacy_id = None
            legacy_category = next(
                (item for item in self.record.categories if item.id == legacy_id), None
            )
            if legacy_category is not None:
                category = next(
                    (item for item in current_record.categories if item.key == legacy_category.key),
                    None,
                )
        if category is None or panel.status not in {"published", "queued", "syncing"}:
            await interaction.response.send_message(
                "This panel is being updated. Please select the category again in a moment.", ephemeral=True
            )
            try:
                if interaction.message is not None:
                    await interaction.message.edit(view=TicketPanelView(self.cog, current_record))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            return
        if panel.guild_id != str(interaction.guild.id):
            await interaction.response.send_message("This ticket panel is invalid.", ephemeral=True)
            return
        elif category.form_fields:
            await interaction.response.send_modal(TicketRequestModal(self.cog, panel, category))
            await self.reset_panel_selector(interaction, current_record)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await self.cog.create_ticket(interaction, panel, category, {})
            finally:
                await self.reset_panel_selector(interaction, current_record)

    async def reset_panel_selector(self, interaction: discord.Interaction, record: TicketPanelRecord) -> None:
        """Clear Discord's remembered select value so the same category can be chosen again."""
        message = interaction.message
        if message is None:
            return
        try:
            await message.edit(view=TicketPanelView(self.cog, record))
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.debug("Could not reset ticket panel selector", exc_info=True)


class TicketPanelView(SafeView):
    def __init__(
        self,
        cog: "TicketsCog",
        record: TicketPanelRecord,
        *,
        timeout: float | None = None,
        select_placeholder: str | None = None,
        category_labels: dict[uuid.UUID, str] | None = None,
        category_descriptions: dict[uuid.UUID, str] | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=timeout)
        self.add_item(
            TicketPanelSelect(
                cog,
                record,
                placeholder=select_placeholder,
                category_labels=category_labels,
                category_descriptions=category_descriptions,
                disabled=disabled,
            )
        )


class TicketCloseModal(SafeModal, title="Close ticket"):
    reason: discord.ui.TextInput["TicketCloseModal"] = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=3,
        max_length=1000,
    )

    def __init__(self, cog: "TicketsCog", ticket_id: uuid.UUID) -> None:
        super().__init__(custom_id=f"ticket-close:{ticket_id}", timeout=300)
        self.cog = cog
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.close_ticket(interaction, self.ticket_id, str(self.reason.value))


class TicketNoteModal(SafeModal, title="Internal staff note"):
    note: discord.ui.TextInput["TicketNoteModal"] = discord.ui.TextInput(
        label="Note",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
    )

    def __init__(self, cog: "TicketsCog", ticket_id: uuid.UUID) -> None:
        super().__init__(custom_id=f"ticket-note:{ticket_id}", timeout=300)
        self.cog = cog
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        stored = await self.cog.repository.add_note(
            ticket_id=self.ticket_id,
            author_id=interaction.user.id,
            content=str(self.note.value),
        )
        if stored:
            await self.cog.refresh_ticket_state(self.ticket_id)
        await interaction.followup.send(
            "Internal note saved." if stored else "Ticket no longer exists.", ephemeral=True
        )


class TicketDeleteModal(SafeModal, title="Delete ticket permanently"):
    confirmation: discord.ui.TextInput["TicketDeleteModal"] = discord.ui.TextInput(
        label="Type DELETE to confirm",
        placeholder="DELETE",
        required=True,
        min_length=6,
        max_length=6,
    )
    reason: discord.ui.TextInput["TicketDeleteModal"] = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, cog: "TicketsCog", ticket_id: uuid.UUID) -> None:
        super().__init__(custom_id=f"ticket-delete:{ticket_id}", timeout=300)
        self.cog = cog
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirmation.value).strip().upper() != "DELETE":
            await interaction.response.send_message(
                "Ticket deletion cancelled. Type DELETE exactly to confirm.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.delete_ticket(
            interaction,
            self.ticket_id,
            str(self.reason.value).strip() or "Deleted by an administrator.",
        )


class TicketButton(discord.ui.Button["TicketControlsView"]):
    def __init__(
        self,
        cog: "TicketsCog",
        ticket_id: uuid.UUID,
        action: str,
        label: str,
        style: discord.ButtonStyle,
        row: int = 0,
    ) -> None:
        super().__init__(
            label=label,
            emoji={
                "claim": "🙋",
                "close": "🔒",
                "reopen": "🔓",
                "archive": "📦",
                "note": "📝",
                "delete": "🗑️",
            }.get(action),
            style=style,
            custom_id=f"ticket:{ticket_id}:{action}",
            row=row,
        )
        self.cog = cog
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        record = await self.cog.interaction_ticket(interaction)
        if record is None:
            return
        member = cast(discord.Member, interaction.user)
        staff = is_ticket_staff(member, record.panel, record.category)
        if self.action == "close":
            if not staff and str(member.id) != record.ticket.requester_id:
                await interaction.response.send_message(
                    "Only the requester or ticket staff can close this ticket.", ephemeral=True
                )
                return
            await interaction.response.send_modal(TicketCloseModal(self.cog, record.ticket.id))
            return
        if not staff:
            await interaction.response.send_message(
                "This control is restricted to ticket staff.", ephemeral=True
            )
            return
        if self.action == "note":
            await interaction.response.send_modal(TicketNoteModal(self.cog, record.ticket.id))
            return
        if self.action == "delete":
            await interaction.response.send_modal(TicketDeleteModal(self.cog, record.ticket.id))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.action == "claim":
            changed = await self.cog.repository.assign(
                ticket_id=record.ticket.id, actor_id=member.id, assignee_id=member.id
            )
            if changed:
                await self.cog.refresh_ticket_state(record.ticket.id)
            result = "Ticket claimed." if changed else "Ticket is no longer open."
        elif self.action == "reopen":
            result = await self.cog.reopen_ticket(interaction, record)
        elif self.action == "archive":
            result = await self.cog.archive_ticket(interaction, record)
        else:
            result = "Unknown ticket action."
        await interaction.followup.send(result, ephemeral=True)


class TicketAssigneeSelect(discord.ui.Select["TicketControlsView"]):
    def __init__(
        self,
        cog: "TicketsCog",
        ticket_id: uuid.UUID,
        members: list[discord.Member],
    ) -> None:
        options = [
            discord.SelectOption(
                label=member.display_name[:100],
                description=f"@{member.name}"[:100],
                value=str(member.id),
            )
            for member in members[:25]
        ]
        if not options:
            options = [
                discord.SelectOption(
                    label="No members can see this ticket",
                    value="none",
                )
            ]
        super().__init__(
            custom_id=f"ticket:{ticket_id}:assignee",
            placeholder="Transfer ticket to",
            options=options,
            row=1,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        record = await self.cog.interaction_ticket(interaction, require_staff=True)
        if record is None:
            return
        selected_id = self.values[0]
        selected = (
            interaction.guild.get_member(int(selected_id))
            if selected_id.isdigit()
            else None
        )
        if selected is None or selected.bot or selected.id == int(record.ticket.requester_id):
            await interaction.response.send_message(
                "Choose an eligible server member other than the ticket requester.",
                ephemeral=True,
            )
            return
        if not await self.cog.member_can_view_ticket(record, selected):
            await interaction.response.send_message(
                "That member cannot see this ticket and cannot be assigned to it.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        changed = await self.cog.repository.assign(
            ticket_id=record.ticket.id,
            actor_id=interaction.user.id,
            assignee_id=selected.id,
        )
        if changed:
            await self.cog.refresh_ticket_state(record.ticket.id)
        await interaction.followup.send(
            f"Assigned to {selected.mention}." if changed else "Ticket is no longer open.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class TicketAddMemberSelect(discord.ui.UserSelect["TicketControlsView"]):
    def __init__(self, cog: "TicketsCog", ticket_id: uuid.UUID) -> None:
        super().__init__(
            custom_id=f"ticket:{ticket_id}:member",
            placeholder="Add member to this ticket",
            min_values=1,
            max_values=1,
            row=3,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        record = await self.cog.interaction_ticket(interaction, require_staff=True)
        if record is None:
            return
        selected = self.values[0]
        if not isinstance(selected, discord.Member) or selected.bot:
            await interaction.response.send_message(
                "Choose a normal server member to add to this ticket.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        added = await self.cog.add_member_to_ticket(record, selected)
        if added:
            await self.cog.refresh_ticket_state(record.ticket.id)
        await interaction.followup.send(
            f"Added {selected.mention} to this ticket."
            if added
            else "This ticket is no longer open or its channel is unavailable.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class TicketPrioritySelect(discord.ui.Select["TicketControlsView"]):
    def __init__(self, cog: "TicketsCog", ticket_id: uuid.UUID) -> None:
        super().__init__(
            custom_id=f"ticket:{ticket_id}:priority",
            placeholder="Set priority",
            options=[
                discord.SelectOption(label=value.title(), value=value) for value in PRIORITIES
            ],
            row=2,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        record = await self.cog.interaction_ticket(interaction, require_staff=True)
        if record is None:
            return
        await interaction.response.defer(ephemeral=True)
        changed = await self.cog.repository.set_priority(
            ticket_id=record.ticket.id,
            actor_id=interaction.user.id,
            priority=self.values[0],
        )
        if changed:
            await self.cog.refresh_ticket_state(record.ticket.id)
        await interaction.followup.send(
            f"Priority set to {self.values[0]}." if changed else "Ticket is no longer open.",
            ephemeral=True,
        )


class TicketControlsView(SafeView):
    def __init__(
        self,
        cog: "TicketsCog",
        ticket_id: uuid.UUID,
        assignee_members: list[discord.Member] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        for action, label, style in (
            ("claim", "Claim", discord.ButtonStyle.primary),
            ("close", "Close", discord.ButtonStyle.danger),
            ("reopen", "Reopen", discord.ButtonStyle.success),
            ("archive", "Archive", discord.ButtonStyle.secondary),
            ("note", "Staff note", discord.ButtonStyle.secondary),
        ):
            self.add_item(TicketButton(cog, ticket_id, action, label, style))
        self.add_item(
            TicketButton(
                cog,
                ticket_id,
                "delete",
                "Delete ticket",
                discord.ButtonStyle.danger,
                row=4,
            )
        )
        self.add_item(TicketAssigneeSelect(cog, ticket_id, assignee_members or []))
        self.add_item(TicketPrioritySelect(cog, ticket_id))
        self.add_item(TicketAddMemberSelect(cog, ticket_id))


class TicketRequesterView(SafeView):
    """Requester-facing controls; staff administration stays ephemeral."""

    def __init__(self, cog: "TicketsCog", ticket_id: uuid.UUID) -> None:
        super().__init__(timeout=None)
        self.add_item(
            TicketButton(
                cog,
                ticket_id,
                "close",
                "Close ticket",
                discord.ButtonStyle.danger,
            )
        )


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = cast("TicketBot", bot)
        self.repository = TicketRepository(self.bot.database)
        self.ticket_locks: defaultdict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.channel_sync_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self.channel_sync_deadlines: dict[uuid.UUID, float] = {}
        self.channel_sync_events: defaultdict[uuid.UUID, asyncio.Event] = defaultdict(
            asyncio.Event
        )
        self.ticket_channel_names: dict[int, str] = {}
        self.ticket_channel_cache: dict[int, tuple[TicketRecord, float]] = {}
        self.non_ticket_channel_cache: dict[int, float] = {}
        self.activity_write_at: dict[int, float] = {}
        self.visible_member_cache: dict[int, tuple[float, list[discord.Member]]] = {}
        self.priority_sync_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self.pending_priority_syncs: set[uuid.UUID] = set()
        self.staff_thread_member_sync_at: dict[int, float] = {}
        self.staff_member_cache: dict[
            int, tuple[float, dict[int, discord.Member]]
        ] = {}
        self.staff_member_fetch_locks: dict[int, asyncio.Lock] = {}
        self.ticket_staff_role_index: dict[
            int, dict[int, set[uuid.UUID]]
        ] = defaultdict(lambda: defaultdict(set))
        self.ticket_staff_roles_by_ticket: dict[uuid.UUID, tuple[int, set[int]]] = {}
        self.staff_member_sync_tasks: set[asyncio.Task[None]] = set()
        self.staff_member_sync_locks: dict[int, asyncio.Lock] = {}

    def _remove_ticket_staff_role_index(self, ticket_id: uuid.UUID) -> None:
        previous = self.ticket_staff_roles_by_ticket.pop(ticket_id, None)
        if previous is not None:
            previous_guild_id, previous_role_ids = previous
            guild_index = self.ticket_staff_role_index.get(previous_guild_id, {})
            for role_id in previous_role_ids:
                tickets = guild_index.get(role_id)
                if tickets is not None:
                    tickets.discard(ticket_id)
                    if not tickets:
                        guild_index.pop(role_id, None)

    def _index_ticket_staff_roles(self, record: TicketRecord) -> None:
        """Index configured staff roles so role updates avoid a ticket scan."""
        ticket_id = record.ticket.id
        if record.ticket.status not in {"open", "claimed", "closed"}:
            self._remove_ticket_staff_role_index(ticket_id)
            return
        guild_id = int(record.ticket.guild_id)
        role_ids = {
            int(role_id)
            for role_id in (record.category.staff_role_ids or record.panel.staff_role_ids)
        }
        self._remove_ticket_staff_role_index(ticket_id)
        self.ticket_staff_roles_by_ticket[ticket_id] = (guild_id, role_ids)
        for role_id in role_ids:
            self.ticket_staff_role_index[guild_id][role_id].add(ticket_id)

    async def _staff_members_for_guild(
        self,
        guild: discord.Guild,
        *,
        additional_members: list[discord.Member] | None = None,
    ) -> dict[int, discord.Member]:
        """Share one member snapshot across all ticket control threads."""
        now = asyncio.get_running_loop().time()
        cached = self.staff_member_cache.get(guild.id)
        if cached is None or now - cached[0] >= STAFF_MEMBER_CACHE_SECONDS:
            lock = self.staff_member_fetch_locks.setdefault(guild.id, asyncio.Lock())
            async with lock:
                now = asyncio.get_running_loop().time()
                cached = self.staff_member_cache.get(guild.id)
                if cached is None or now - cached[0] >= STAFF_MEMBER_CACHE_SECONDS:
                    members = {member.id: member for member in guild.members}
                    cache_is_complete = (
                        guild.member_count is not None
                        and len(members) >= guild.member_count
                    )
                    if (
                        self.bot.settings.discord_members_intent_enabled
                        and not cache_is_complete
                    ):
                        try:
                            async for member in guild.fetch_members(limit=None):
                                members[member.id] = member
                        except discord.HTTPException:
                            logger.warning(
                                "Could not refresh cached guild members for ticket control threads in %s",
                                guild.id,
                            )
                    cached = (now, members)
                    self.staff_member_cache[guild.id] = cached
        members = cached[1]
        for member in additional_members or []:
            members[member.id] = member
        return members

    async def visible_members_for_channel(
        self, channel: discord.TextChannel, *, exclude_member_id: int | None = None
    ) -> list[discord.Member]:
        """Resolve eligible members without scanning every guild member.

        Ticket channels grant access through their explicit role/member
        overwrites. Looking at only those candidates avoids rebuilding a
        50,000-member dictionary every time a staff control view is rendered.
        """
        now = monotonic()
        cached = self.visible_member_cache.get(channel.id)
        if cached is not None and cached[0] > now:
            eligible_members = cached[1]
        else:
            members: dict[int, discord.Member] = {}
            for target in channel.overwrites:
                member = target if isinstance(target, discord.Member) else None
                if member is not None:
                    members[member.id] = member
                elif isinstance(target, discord.Role) and not target.is_default():
                    for role_member in target.members:
                        members[role_member.id] = role_member
            eligible_members = sorted(
                (
                    member
                    for member in members.values()
                    if not member.bot and channel.permissions_for(member).view_channel
                ),
                key=lambda member: (member.display_name.casefold(), member.id),
            )
            self.visible_member_cache[channel.id] = (
                now + VISIBLE_MEMBER_CACHE_SECONDS,
                eligible_members,
            )
        return [
            member
            for member in eligible_members
            if member.id != exclude_member_id
        ]

    async def _exclude_requester_from_staff_thread(
        self,
        record: TicketRecord,
        thread: discord.Thread,
        *,
        force: bool = False,
    ) -> None:
        """Keep a non-staff ticket requester out of the private staff thread."""
        requester = thread.guild.get_member(int(record.ticket.requester_id))
        if requester is None or has_ticket_staff_role(
            requester, record.panel, record.category
        ):
            return
        if not force and requester.id not in {member.id for member in thread.members}:
            return
        try:
            await thread.remove_user(requester)
        except discord.NotFound:
            # The requester was not a thread member, which is already the
            # desired state.
            return
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Could not remove ticket requester %s from staff thread %s",
                requester.id,
                thread.id,
                exc_info=True,
            )

    async def _sync_requester_thread_visibility(
        self,
        channel: discord.TextChannel,
        record: TicketRecord,
        staff_role_ids: list[str],
    ) -> None:
        """Keep the requester from creating or messaging private threads."""
        requester = channel.guild.get_member(int(record.ticket.requester_id))
        if requester is None:
            return
        overwrite = channel.overwrites_for(requester)
        staff_member = requester.guild_permissions.manage_channels or has_ticket_staff_role_ids(
            requester, staff_role_ids
        )
        if staff_member:
            if (
                overwrite.manage_threads is not False
                and overwrite.send_messages_in_threads is not False
            ):
                return
            overwrite.update(
                manage_threads=None,
                send_messages_in_threads=None,
            )
        else:
            if (
                overwrite.manage_threads is False
                and overwrite.send_messages_in_threads is False
            ):
                return
            overwrite.update(
                manage_threads=False,
                send_messages_in_threads=False,
            )
        await channel.set_permissions(
            requester,
            overwrite=overwrite,
            reason="Keep ticket staff controls private from requester",
        )
        self.visible_member_cache.pop(channel.id, None)

    async def ticket_controls_view(
        self,
        ticket_id: uuid.UUID,
        *,
        record: TicketRecord | None = None,
    ) -> TicketControlsView:
        record = record or await self.repository.get_ticket(ticket_id)
        members: list[discord.Member] = []
        if record is not None and record.ticket.channel_id:
            channel = self.bot.get_channel(int(record.ticket.channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(record.ticket.channel_id))
                except discord.HTTPException:
                    channel = None
            if isinstance(channel, discord.TextChannel):
                members = await self.visible_members_for_channel(
                    channel,
                    exclude_member_id=int(record.ticket.requester_id),
                )
        return TicketControlsView(self, ticket_id, members)

    async def cog_load(self) -> None:
        recovered = await self.repository.recover_stale_work(minimum_age=timedelta(minutes=5))
        if recovered["panels"] or recovered["tickets"]:
            logger.warning("Recovered stale ticket work: %s", recovered)
        for record in await self.repository.published_panels():
            if record.panel.message_id:
                self.bot.add_view(
                    TicketPanelView(self, record), message_id=int(record.panel.message_id)
                )
        for ticket in await self.repository.tickets_with_controls():
            record: TicketRecord | None = None
            if ticket.staff_control_message_id:
                record = await self.repository.get_ticket(ticket.id)
                if record is not None:
                    self._index_ticket_staff_roles(record)
            if ticket.control_message_id:
                self.bot.add_view(
                    TicketRequesterView(self, ticket.id),
                    message_id=int(ticket.control_message_id),
                )
            if ticket.staff_control_message_id:
                control_view = await self.ticket_controls_view(ticket.id, record=record)
                self.bot.add_view(
                    control_view,
                    message_id=int(ticket.staff_control_message_id),
                )
                # Registering a persistent view only attaches callbacks locally.  The
                # Discord message may still contain the previous UserSelect, so edit
                # the existing control message immediately with the filtered view.
                if ticket.staff_thread_id:
                    try:
                        staff_channel = self.bot.get_channel(
                            int(ticket.staff_thread_id)
                        )
                        if staff_channel is None:
                            staff_channel = await self.bot.fetch_channel(
                                int(ticket.staff_thread_id)
                            )
                        if isinstance(staff_channel, (discord.TextChannel, discord.Thread)):
                            staff_message = await staff_channel.fetch_message(
                                int(ticket.staff_control_message_id)
                            )
                            await staff_message.edit(view=control_view)
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        logger.debug(
                            "Could not refresh existing ticket control view %s",
                            ticket.id,
                            exc_info=True,
                        )
        self.synchronize_panels.start()
        self.check_inactivity.start()
        self.refresh_existing_ticket_states.start()

    async def cog_unload(self) -> None:
        self.synchronize_panels.cancel()
        self.check_inactivity.cancel()
        self.refresh_existing_ticket_states.cancel()
        for task in self.channel_sync_tasks.values():
            task.cancel()
        for task in self.priority_sync_tasks.values():
            task.cancel()
        self.channel_sync_tasks.clear()
        self.channel_sync_deadlines.clear()
        self.channel_sync_events.clear()
        self.ticket_channel_names.clear()
        self.ticket_channel_cache.clear()
        self.non_ticket_channel_cache.clear()
        self.activity_write_at.clear()
        self.visible_member_cache.clear()
        self.priority_sync_tasks.clear()
        self.pending_priority_syncs.clear()
        self.staff_thread_member_sync_at.clear()
        self.staff_member_cache.clear()
        self.staff_member_fetch_locks.clear()
        self.ticket_staff_role_index.clear()
        self.ticket_staff_roles_by_ticket.clear()
        self.staff_member_sync_locks.clear()
        for task in self.staff_member_sync_tasks:
            task.cancel()
        self.staff_member_sync_tasks.clear()

    async def interaction_ticket(
        self, interaction: discord.Interaction, *, require_staff: bool = False
    ) -> TicketRecord | None:
        if (
            interaction.channel_id is None
            or interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
        ):
            await interaction.response.send_message(
                "This ticket control is invalid.", ephemeral=True
            )
            return None
        ticket_channel_id = (
            interaction.channel.parent_id
            if isinstance(interaction.channel, discord.Thread)
            else interaction.channel_id
        )
        record = await self.repository.get_by_channel(ticket_channel_id)
        if record is None:
            await interaction.response.send_message("This ticket no longer exists.", ephemeral=True)
            return None
        if require_staff and not is_ticket_staff(
            interaction.user, record.panel, record.category
        ):
            await interaction.response.send_message(
                "This control is restricted to ticket staff.", ephemeral=True
            )
            return None
        return record

    async def ticket_state_embed(
        self,
        record: TicketRecord,
        *,
        status_override: str | None = None,
        include_notes: bool = False,
    ) -> discord.Embed:
        ticket = record.ticket
        status = status_override or ticket.status
        assignment = (
            f"Assigned to <@{ticket.assignee_id}>"
            if ticket.assignee_id
            else "Waiting for a staff member"
        )
        embed = themed_embed(
            "ticket",
            title=record.category.label,
            description=f"Opened by <@{ticket.requester_id}>\n{assignment}",
            color=record.panel.color,
            timestamp=ticket.updated_at,
        )
        embed.add_field(
            name="Status",
            value=(
                f"{STATUS_CHANNEL_DOTS.get(status, '⚪')} "
                f"**{status.replace('_', ' ').title()}**"
            ),
        )
        embed.add_field(name="Priority", value=f"**{ticket.priority.title()}**")
        for key, value in ticket.answers.items():
            label = next(
                (
                    str(field["label"])
                    for field in record.category.form_fields
                    if field["key"] == key
                ),
                key,
            )
            embed.add_field(name=label, value=value[:1024] or "Not provided", inline=False)
        if ticket.close_reason and status in {"closed", "archived"}:
            embed.add_field(name="Close reason", value=ticket.close_reason[:1024], inline=False)
        if include_notes:
            notes = await self.repository.notes_for_ticket(ticket.id)
            note_lines = [
                f"<t:{int(note.created_at.timestamp())}:R> <@{note.author_id}>: {note.content[:700]}"
                for note in notes
            ]
            notes_value = "\n".join(note_lines) or "None yet"
            embed.add_field(
                name="Internal notes",
                value=notes_value[:1024],
                inline=False,
            )
        return embed

    async def member_can_view_ticket(
        self, record: TicketRecord, member: discord.Member
    ) -> bool:
        if not record.ticket.channel_id:
            return False
        channel = self.bot.get_channel(int(record.ticket.channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(record.ticket.channel_id))
            except discord.HTTPException:
                return False
        return isinstance(channel, discord.TextChannel) and channel.permissions_for(
            member
        ).view_channel

    async def add_member_to_ticket(
        self, record: TicketRecord, member: discord.Member
    ) -> bool:
        if record.ticket.status not in {"open", "claimed"} or not record.ticket.channel_id:
            return False
        channel = self.bot.get_channel(int(record.ticket.channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(record.ticket.channel_id))
            except discord.HTTPException:
                return False
        if not isinstance(channel, discord.TextChannel):
            return False
        overwrite = channel.overwrites_for(member)
        overwrite.update(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            send_messages_in_threads=True,
        )
        if channel.overwrites_for(member) == overwrite:
            return True
        try:
            await channel.set_permissions(
                member,
                overwrite=overwrite,
                reason="Add member to ticket",
            )
            self.visible_member_cache.pop(channel.id, None)
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Could not add member %s to ticket %s",
                member.id,
                record.ticket.id,
                exc_info=True,
            )
            return False
        return True

    async def add_staff_to_thread(
        self,
        thread: discord.Thread,
        guild: discord.Guild,
        staff_role_ids: list[str],
        *,
        force_member_refresh: bool = False,
        additional_members: list[discord.Member] | None = None,
    ) -> None:
        """Keep role members in the private staff control chat, not the ticket chat."""
        configured_role_ids = set(staff_role_ids)
        now = asyncio.get_running_loop().time()
        if not configured_role_ids:
            bot_id = self.bot.user.id if self.bot.user else None
            for thread_member in thread.members:
                if thread_member.id == bot_id:
                    continue
                try:
                    await thread.remove_user(thread_member)
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning(
                        "Could not remove obsolete ticket staff member %s from control thread %s",
                        thread_member.id,
                        thread.id,
                    )
            self.staff_thread_member_sync_at[thread.id] = now
            return
        last_sync = self.staff_thread_member_sync_at.get(thread.id, 0.0)
        if (
            not force_member_refresh
            and not additional_members
            and now - last_sync < STAFF_THREAD_SYNC_COOLDOWN_SECONDS
        ):
            return
        members = await self._staff_members_for_guild(
            guild,
            additional_members=additional_members,
        )
        member_values = tuple(members.values())
        bot_id = self.bot.user.id if self.bot.user else None
        for thread_member in thread.members:
            member = members.get(thread_member.id)
            # Discord returns ThreadMember objects here, which only expose the
            # member ID. Resolve the full guild Member before checking roles.
            if (
                thread_member.id == bot_id
                or member is None
                or member.bot
                or configured_role_ids.intersection(str(role.id) for role in member.roles)
            ):
                continue
            try:
                await thread.remove_user(thread_member)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not remove ticket staff member %s from control thread %s",
                    thread_member.id,
                    thread.id,
                )
        existing_member_ids = {thread_member.id for thread_member in thread.members}
        for member in member_values:
            if member.bot or not configured_role_ids.intersection(
                str(role.id) for role in member.roles
            ) or member.id in existing_member_ids:
                continue
            try:
                await thread.add_user(member)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not add ticket staff member %s to control thread %s",
                    member.id,
                    thread.id,
                )
        self.staff_thread_member_sync_at[thread.id] = now

    async def ensure_staff_controls(
        self,
        record: TicketRecord,
        *,
        changed_member: discord.Member | None = None,
    ) -> None:
        ticket = record.ticket
        self._index_ticket_staff_roles(record)
        if not ticket.channel_id:
            return
        channel = self.bot.get_channel(int(ticket.channel_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(ticket.channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        bot_member = channel.guild.me
        staff_role_ids = record.category.staff_role_ids or record.panel.staff_role_ids
        await self._sync_requester_thread_visibility(
            channel, record, staff_role_ids
        )
        can_mention_nonmentionable = bool(
            bot_member
            and (
                bot_member.guild_permissions.administrator
                or bot_member.guild_permissions.mention_everyone
            )
        )
        desired_staff_overwrite = ticket_staff_overwrite()
        for role_id in staff_role_ids:
            role = channel.guild.get_role(int(role_id))
            if role is not None and channel.overwrites_for(role) != desired_staff_overwrite:
                await channel.set_permissions(
                    role,
                    overwrite=desired_staff_overwrite,
                    reason="Synchronize ticket manager role access",
                )
        existing_channel: discord.TextChannel | discord.Thread | None = None
        legacy_channels: list[discord.TextChannel | discord.Thread] = []
        staff_thread: discord.Thread | None = None
        thread_was_created = False
        has_existing_location = bool(ticket.staff_thread_id)
        if ticket.staff_thread_id:
            existing_channel = self.bot.get_channel(int(ticket.staff_thread_id))
            if existing_channel is None:
                try:
                    existing_channel = await self.bot.fetch_channel(int(ticket.staff_thread_id))
                except discord.HTTPException:
                    existing_channel = None
            if isinstance(existing_channel, (discord.TextChannel, discord.Thread)):
                if (
                    isinstance(existing_channel, discord.Thread)
                    and existing_channel.parent_id == channel.id
                    and existing_channel.type == discord.ChannelType.private_thread
                ):
                    staff_thread = existing_channel
                    await self._exclude_requester_from_staff_thread(
                        record, staff_thread
                    )
                    if ticket.staff_control_message_id:
                        try:
                            await existing_channel.fetch_message(
                                int(ticket.staff_control_message_id)
                            )
                            await self.add_staff_to_thread(
                                existing_channel,
                                channel.guild,
                                staff_role_ids,
                                additional_members=(
                                    [changed_member] if changed_member else None
                                ),
                            )
                            return
                        except discord.NotFound:
                            pass
                else:
                    legacy_channels.append(existing_channel)
                    if isinstance(existing_channel, discord.Thread):
                        parent = existing_channel.parent
                        if parent is None and existing_channel.parent_id:
                            try:
                                candidate = await self.bot.fetch_channel(
                                    existing_channel.parent_id
                                )
                            except discord.HTTPException:
                                candidate = None
                            parent = (
                                candidate
                                if isinstance(candidate, discord.TextChannel)
                                else None
                            )
                        if (
                            isinstance(parent, discord.TextChannel)
                            and parent.id != channel.id
                        ):
                            legacy_channels.append(parent)
        if staff_thread is None:
            staff_thread = await channel.create_thread(
                name=f"staff-controls-{str(ticket.id)[:8]}",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080,
                invitable=False,
                reason="Create private ticket staff controls",
            )
            thread_was_created = True
            await self._exclude_requester_from_staff_thread(
                record, staff_thread, force=True
            )
        await self.add_staff_to_thread(
            staff_thread,
            channel.guild,
            staff_role_ids,
            force_member_refresh=thread_was_created,
            additional_members=([changed_member] if changed_member else None),
        )
        notified_roles = [
            role
            for role_id in staff_role_ids
            if (role := channel.guild.get_role(int(role_id))) is not None
            and (role.mentionable or can_mention_nonmentionable)
        ]
        message = await staff_thread.send(
            content=" ".join(role.mention for role in notified_roles) or None,
            embed=await self.ticket_state_embed(record, include_notes=True),
            view=await self.ticket_controls_view(ticket.id, record=record),
            allowed_mentions=discord.AllowedMentions(
                users=False, roles=notified_roles, everyone=False
            ),
        )
        stored = (
            await self.repository.replace_staff_controls(
                ticket_id=ticket.id,
                thread_id=staff_thread.id,
                message_id=message.id,
            )
            if has_existing_location
            else await self.repository.set_staff_controls(
                ticket_id=ticket.id,
                thread_id=staff_thread.id,
                message_id=message.id,
            )
        )
        if not stored:
            await message.delete(reason="Duplicate ticket staff controls")
            return
        deleted_ids: set[int] = set()
        for legacy_channel in legacy_channels:
            if legacy_channel.id in deleted_ids or legacy_channel.id in {
                channel.id,
                staff_thread.id,
            }:
                continue
            try:
                await legacy_channel.delete(
                    reason="Restore private ticket staff controls"
                )
                deleted_ids.add(legacy_channel.id)
            except discord.HTTPException:
                logger.warning(
                    "Could not remove old ticket control channel %s",
                    legacy_channel.id,
                )

    async def refresh_ticket_state(
        self,
        ticket_id: uuid.UUID,
        *,
        sync_channel_layout: bool = True,
        staff_member: discord.Member | None = None,
    ) -> None:
        record = await self.repository.get_ticket(ticket_id)
        if record is None or not record.ticket.channel_id:
            return
        try:
            channel = self.bot.get_channel(int(record.ticket.channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(record.ticket.channel_id))
                except discord.NotFound:
                    await self.handle_missing_ticket_channel(ticket_id)
                    return
            if not isinstance(channel, discord.TextChannel):
                return
            if sync_channel_layout:
                self.schedule_ticket_channel_sync(ticket_id, status=record.ticket.status)
            if not record.ticket.control_message_id:
                return
            message = await channel.fetch_message(int(record.ticket.control_message_id))
            active = record.ticket.status in {"open", "claimed"}
            await message.edit(
                embed=await self.ticket_state_embed(record),
                view=TicketRequesterView(self, ticket_id) if active else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.ensure_staff_controls(record, changed_member=staff_member)
            record = await self.repository.get_ticket(ticket_id) or record
            if record.ticket.staff_thread_id and record.ticket.staff_control_message_id:
                staff_channel = self.bot.get_channel(int(record.ticket.staff_thread_id))
                if staff_channel is None:
                    staff_channel = await self.bot.fetch_channel(int(record.ticket.staff_thread_id))
                if isinstance(staff_channel, (discord.TextChannel, discord.Thread)):
                    staff_message = await staff_channel.fetch_message(
                        int(record.ticket.staff_control_message_id)
                    )
                    await staff_message.edit(
                        embed=await self.ticket_state_embed(record, include_notes=True),
                        view=await self.ticket_controls_view(ticket_id),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception("Could not refresh state embed for ticket %s", ticket_id)

    async def handle_missing_ticket_channel(self, ticket_id: uuid.UUID) -> None:
        """Retire a ticket whose Discord channel was deleted outside the bot."""
        self.channel_sync_deadlines.pop(ticket_id, None)
        self.channel_sync_events.pop(ticket_id, None)
        self.pending_priority_syncs.discard(ticket_id)
        changed = await self.repository.mark_deleted(
            ticket_id=ticket_id,
            actor_id=self.bot.user.id if self.bot.user else 0,
            reason="Ticket channel no longer exists in Discord.",
        )
        if changed:
            logger.warning(
                "Marked ticket %s deleted because its Discord channel no longer exists",
                ticket_id,
            )

    def schedule_ticket_channel_sync(self, ticket_id: uuid.UUID, *, status: str) -> None:
        """Run cosmetic room updates without blocking ticket interactions."""
        self.schedule_ticket_priority_sync(ticket_id)
        delay = {
            "open": OPEN_NAME_DEBOUNCE_SECONDS,
            "closed": CLOSED_NAME_DEBOUNCE_SECONDS,
        }.get(status, 0.0)
        self.channel_sync_deadlines[ticket_id] = asyncio.get_running_loop().time() + delay
        self.channel_sync_events[ticket_id].set()
        existing = self.channel_sync_tasks.get(ticket_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self.sync_latest_ticket_channel(ticket_id),
            name=f"ticket-channel-sync:{ticket_id}",
        )
        self.channel_sync_tasks[ticket_id] = task

    async def sync_latest_ticket_channel(self, ticket_id: uuid.UUID) -> None:
        current_task = asyncio.current_task()
        wake_event = self.channel_sync_events[ticket_id]
        try:
            while (deadline := self.channel_sync_deadlines.get(ticket_id)) is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0:
                    wake_event.clear()
                    try:
                        await asyncio.wait_for(wake_event.wait(), timeout=remaining)
                    except TimeoutError:
                        pass
                    if wake_event.is_set():
                        continue
                if self.channel_sync_deadlines.get(ticket_id) != deadline:
                    continue
                self.channel_sync_deadlines.pop(ticket_id, None)
                record = await self.repository.get_ticket(ticket_id)
                if record is None or not record.ticket.channel_id:
                    continue
                channel = self.bot.get_channel(int(record.ticket.channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(record.ticket.channel_id))
                if isinstance(channel, discord.TextChannel):
                    await self.sync_ticket_status_name(record, channel)
        except asyncio.CancelledError:
            raise
        except discord.NotFound:
            await self.handle_missing_ticket_channel(ticket_id)
        except (discord.Forbidden, discord.HTTPException) as error:
            logger.warning(
                "Could not synchronize ticket room %s (%s)",
                ticket_id,
                type(error).__name__,
            )
        finally:
            if self.channel_sync_tasks.get(ticket_id) is current_task:
                self.channel_sync_tasks.pop(ticket_id, None)
            if ticket_id in self.channel_sync_deadlines:
                task = asyncio.create_task(
                    self.sync_latest_ticket_channel(ticket_id),
                    name=f"ticket-channel-sync:{ticket_id}",
                )
                self.channel_sync_tasks[ticket_id] = task
            else:
                self.channel_sync_events.pop(ticket_id, None)

    def schedule_ticket_priority_sync(self, ticket_id: uuid.UUID) -> None:
        self.pending_priority_syncs.add(ticket_id)
        existing = self.priority_sync_tasks.get(ticket_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self.sync_latest_ticket_priority(ticket_id),
            name=f"ticket-priority-sync:{ticket_id}",
        )
        self.priority_sync_tasks[ticket_id] = task

    async def sync_latest_ticket_priority(self, ticket_id: uuid.UUID) -> None:
        current_task = asyncio.current_task()
        try:
            while ticket_id in self.pending_priority_syncs:
                self.pending_priority_syncs.discard(ticket_id)
                record = await self.repository.get_ticket(ticket_id)
                if record is None or not record.ticket.channel_id:
                    continue
                channel = self.bot.get_channel(int(record.ticket.channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(record.ticket.channel_id))
                if isinstance(channel, discord.TextChannel):
                    await self.sync_ticket_priority_order(record, channel)
        except asyncio.CancelledError:
            raise
        except discord.NotFound:
            await self.handle_missing_ticket_channel(ticket_id)
        except (discord.Forbidden, discord.HTTPException) as error:
            logger.warning(
                "Could not order ticket room %s (%s)",
                ticket_id,
                type(error).__name__,
            )
        finally:
            if self.priority_sync_tasks.get(ticket_id) is current_task:
                self.priority_sync_tasks.pop(ticket_id, None)
            self.pending_priority_syncs.discard(ticket_id)

    async def sync_ticket_status_name(
        self, record: TicketRecord, channel: discord.TextChannel
    ) -> None:
        """Keep the room's status dot in sync with the latest ticket state."""
        known_name = self.ticket_channel_names.get(channel.id, channel.name)
        base_name = STATUS_DOT_PREFIX.sub("", known_name).strip()
        if base_name.startswith("ticket-") and record.ticket.ticket_number:
            base_name = f"{record.ticket.ticket_number:03d}-{base_name[len('ticket-'):]}"
        expected_name = status_channel_name(base_name, record.ticket.status)
        if channel.name == expected_name or known_name == expected_name:
            self.ticket_channel_names[channel.id] = expected_name
            return
        updated = await channel.edit(
            name=expected_name,
            reason=f"Ticket status changed to {record.ticket.status}",
        )
        self.ticket_channel_names[channel.id] = updated.name

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        if isinstance(after, discord.TextChannel) and before.name != after.name:
            self.ticket_channel_names[after.id] = after.name

    async def sync_ticket_priority_order(
        self, record: TicketRecord, channel: discord.TextChannel
    ) -> None:
        """Keep active ticket rooms ordered from urgent down to low priority."""
        if record.ticket.status not in {"open", "claimed"}:
            return
        if channel.category_id is None or channel.category is None:
            return
        ordered_ids = await self.repository.active_channel_order(
            guild_id=channel.guild.id,
            open_category_id=channel.category_id,
        )
        available_channels = {
            str(candidate.id): candidate
            for candidate in channel.category.channels
            if isinstance(candidate, discord.TextChannel)
        }
        desired_ids = [channel_id for channel_id in ordered_ids if channel_id in available_channels]
        desired_id_set = set(desired_ids)
        current_ids = [
            str(candidate.id)
            for candidate in channel.category.channels
            if isinstance(candidate, discord.TextChannel)
            and str(candidate.id) in desired_id_set
        ]
        try:
            desired_position = desired_ids.index(str(channel.id))
            current_position = current_ids.index(str(channel.id))
        except ValueError:
            return
        if current_position == desired_position:
            return
        reason = f"Order ticket by {record.ticket.priority} priority"
        if desired_position == 0:
            if len(desired_ids) < 2:
                return
            await channel.move(before=available_channels[desired_ids[1]], reason=reason)
        else:
            await channel.move(
                after=available_channels[desired_ids[desired_position - 1]],
                reason=reason,
            )

    @app_commands.command(
        name="ticket-controls",
        description="Open the staff controls for the ticket in this channel.",
    )
    @app_commands.guild_only()
    async def ticket_controls(self, interaction: discord.Interaction) -> None:
        record = await self.interaction_ticket(interaction, require_staff=True)
        if record is None:
            return
        await interaction.response.send_message(
            "Ticket manager controls.",
            embed=await self.ticket_state_embed(record, include_notes=True),
            view=await self.ticket_controls_view(record.ticket.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def ticket_creation_response(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        try:
            await interaction.edit_original_response(content=message)
        except discord.HTTPException:
            await respond_ephemeral(interaction, message)

    @staticmethod
    def ticket_creation_error_message(error: Exception) -> str:
        if isinstance(error, discord.Forbidden):
            return (
                "I cannot create the ticket because I am missing Discord permissions. "
                "An administrator must check Manage Channels, View Channel, "
                "and message permissions."
            )
        if isinstance(error, discord.HTTPException):
            if error.code in {30013, 30018, 160006, 160009}:
                return (
                    "This server cannot create more ticket channels or staff threads right now. "
                    "Delete or archive old tickets and try again."
                )
            return (
                f"Discord rejected ticket creation (error {error.code}). "
                "No ticket was created; ask an administrator to check the ticket setup."
            )
        if isinstance(error, ValueError) and str(error) == "ticket_category_unavailable":
            return (
                "The configured ticket category is missing or unavailable. "
                "An administrator must update the ticket panel."
            )
        return "The ticket could not be created. No channel was left behind; please try again."

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        panel: TicketPanel,
        category: TicketPanelCategory,
        answers: dict[str, str],
        *,
        subject_override: str | None = None,
        opening_message: str | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.ticket_creation_response(interaction, "Tickets require a server.")
            return
        configuration = await self.repository.panel_category(category.id)
        if configuration is None or configuration[0].id != panel.id:
            await self.ticket_creation_response(
                interaction, "This ticket category is no longer available."
            )
            return
        panel, category = configuration
        subject = (
            subject_override
            or next((value for value in answers.values() if value), category.label)
        )[:256]
        ticket, error = await self.repository.begin_ticket(
            panel=panel,
            category=category,
            requester_id=interaction.user.id,
            subject=subject,
            answers=answers,
        )
        if ticket is None:
            await self.ticket_creation_response(
                interaction,
                "You already have the maximum number of open tickets for this panel."
                if error == "ticket_limit_reached"
                else "Could not reserve a ticket.",
            )
            return
        channel: discord.TextChannel | None = None
        creation_committed = False
        try:
            open_category = interaction.guild.get_channel(int(panel.open_category_id))
            bot_member = interaction.guild.me
            if not isinstance(open_category, discord.CategoryChannel) or bot_member is None:
                raise ValueError("ticket_category_unavailable")
            overwrites = ticket_channel_overwrites(
                interaction.guild,
                interaction.user,
                bot_member,
                category.staff_role_ids or panel.staff_role_ids,
            )
            channel = await interaction.guild.create_text_channel(
                status_channel_name(
                    safe_channel_name(
                        panel.name_template,
                        interaction.user,
                        category.key,
                        ticket.id,
                        ticket.ticket_number,
                    ),
                    "open",
                ),
                category=open_category,
                overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user} ({ticket.id})",
            )
            ticket_record = TicketRecord(ticket=ticket, panel=panel, category=category)
            embed = await self.ticket_state_embed(
                ticket_record,
                status_override="open",
            )
            bot_can_mention_nonmentionable = bool(
                bot_member.guild_permissions.administrator
                or bot_member.guild_permissions.mention_everyone
            )
            notified_roles = [
                role
                for role_id in category.staff_role_ids or panel.staff_role_ids
                if (role := interaction.guild.get_role(int(role_id))) is not None
                and (role.mentionable or bot_can_mention_nonmentionable)
            ]
            opening_content = interaction.user.mention
            configured_opening_message = opening_message or panel.opening_message
            if configured_opening_message:
                opening_content += f"\n{configured_opening_message[:1000]}"
            state_message = await channel.send(
                content=opening_content,
                embed=embed,
                view=TicketRequesterView(self, ticket.id),
                allowed_mentions=discord.AllowedMentions(
                    users=[interaction.user], roles=False, everyone=False
                ),
            )
            staff_thread = await channel.create_thread(
                name=f"staff-controls-{str(ticket.id)[:8]}",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080,
                invitable=False,
                reason="Create private ticket staff controls",
            )
            await self._exclude_requester_from_staff_thread(
                ticket_record,
                staff_thread,
                force=True,
            )
            await self.add_staff_to_thread(
                staff_thread,
                interaction.guild,
                category.staff_role_ids or panel.staff_role_ids,
                force_member_refresh=True,
            )
            staff_controls = await staff_thread.send(
                content=" ".join(role.mention for role in notified_roles) or None,
                embed=embed,
                view=TicketControlsView(
                    self,
                    ticket.id,
                    await self.visible_members_for_channel(
                        channel,
                        exclude_member_id=interaction.user.id,
                    ),
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=False, roles=notified_roles, everyone=False
                ),
            )
            if (
                await self.repository.finish_ticket_creation(
                    ticket.id,
                    channel.id,
                    state_message.id,
                    staff_thread.id,
                    staff_controls.id,
                    [role.id for role in notified_roles],
                )
                is None
            ):
                raise RuntimeError("ticket_creation_state_conflict")
            creation_committed = True
            try:
                await self.refresh_ticket_state(ticket.id)
            except Exception:
                # The Discord channel and ticket row are already valid. A
                # best-effort state refresh must never roll them back.
                logger.exception(
                    "Ticket %s was created but its state refresh failed", ticket.id
                )
            await self.ticket_creation_response(interaction, f"Your ticket is ready: {channel.mention}")
        except Exception as error_object:
            logger.exception("Ticket %s channel creation failed", ticket.id)
            if creation_committed:
                await self.ticket_creation_response(
                    interaction,
                    f"Your ticket is ready: {channel.mention if channel else 'the ticket channel'}",
                )
                return
            if channel:
                try:
                    await channel.delete(reason="Rolling back failed ticket creation")
                except discord.HTTPException:
                    logger.exception("Could not roll back ticket channel %s", channel.id)
            try:
                await self.repository.fail_ticket_creation(
                    ticket.id, type(error_object).__name__
                )
            except Exception:
                logger.exception("Could not persist failure state for ticket %s", ticket.id)
            await self.ticket_creation_response(
                interaction, self.ticket_creation_error_message(error_object)
            )

    async def _transcript(self, channel: discord.TextChannel) -> tuple[str, int]:
        lines = [f"Transcript for #{channel.name} ({channel.id})", ""]
        count = 0
        size = 0
        async for message in channel.history(limit=1000, oldest_first=True):
            line = (
                f"[{message.created_at.astimezone(UTC).isoformat()}] "
                f"{message.author} ({message.author.id}): "
                f"{message.content or '[content unavailable]'}"
            )
            lines.append(line)
            size += len(line)
            for attachment in message.attachments:
                attachment_line = (
                    f"  attachment: {attachment.filename} ({attachment.size} bytes) "
                    f"{attachment.url}"
                )
                lines.append(attachment_line)
                size += len(attachment_line)
            count += 1
            if size > 2_000_000:
                lines.append("[transcript truncated at 2 MB]")
                break
        return "\n".join(lines), count

    async def close_ticket(
        self,
        interaction: discord.Interaction | None,
        ticket_id: uuid.UUID,
        reason: str,
        *,
        record: TicketRecord | None = None,
    ) -> None:
        async with self.ticket_locks[ticket_id]:
            if record is None and interaction:
                record = await self.repository.get_ticket(ticket_id)
            if record is None or record.ticket.id != ticket_id:
                if interaction:
                    await interaction.followup.send("Ticket no longer exists.", ephemeral=True)
                return
            actor_id = (
                interaction.user.id if interaction else (self.bot.user.id if self.bot.user else 0)
            )
            changed = await self.repository.transition(
                ticket_id=ticket_id,
                actor_id=actor_id,
                from_statuses=("open", "claimed"),
                to_status="closed",
                reason=reason,
            )
            if changed is None:
                if interaction:
                    await interaction.followup.send("Ticket is already closed.", ephemeral=True)
                return
            await self.refresh_ticket_state(ticket_id)
            guild = self.bot.get_guild(int(record.ticket.guild_id))
            channel = (
                guild.get_channel(int(record.ticket.channel_id))
                if guild and record.ticket.channel_id
                else None
            )
            if isinstance(channel, discord.TextChannel):
                try:
                    transcript, count = await self._transcript(channel)
                    await self.repository.save_transcript(
                        ticket_id=ticket_id,
                        content=transcript,
                        message_count=count,
                        retention_days=record.panel.transcript_retention_days,
                    )
                    requester = guild.get_member(int(record.ticket.requester_id)) if guild else None
                    if requester:
                        overwrite = channel.overwrites_for(requester)
                        overwrite.update(send_messages=False)
                        await channel.set_permissions(requester, overwrite=overwrite)
                    await channel.send(
                        f"Ticket closed: {reason}",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    logger.exception("Ticket %s closed but Discord cleanup failed", ticket_id)
                    if interaction:
                        await interaction.followup.send(
                            "Ticket closed, but transcript or channel cleanup needs staff review.",
                            ephemeral=True,
                        )
                    return
            if interaction:
                await interaction.followup.send(
                    "Ticket closed and transcript retained.", ephemeral=True
                )

    async def delete_ticket(
        self, interaction: discord.Interaction, ticket_id: uuid.UUID, reason: str
    ) -> None:
        async with self.ticket_locks[ticket_id]:
            record = await self.repository.get_ticket(ticket_id)
            if record is None or record.ticket.status == "deleted":
                await self.ticket_creation_response(interaction, "This ticket is already deleted.")
                return
            if not isinstance(interaction.user, discord.Member) or not is_ticket_staff(
                interaction.user, record.panel, record.category
            ):
                await self.ticket_creation_response(
                    interaction,
                    "Only configured ticket manager roles can delete tickets.",
                )
                return
            guild = interaction.guild or self.bot.get_guild(int(record.ticket.guild_id))
            channel = (
                guild.get_channel(int(record.ticket.channel_id))
                if guild and record.ticket.channel_id
                else None
            )
            if not isinstance(channel, discord.TextChannel):
                await self.ticket_creation_response(
                    interaction, "The ticket channel no longer exists."
                )
                return
            try:
                transcript, count = await self._transcript(channel)
                await self.repository.save_transcript(
                    ticket_id=ticket_id,
                    content=transcript,
                    message_count=count,
                    retention_days=record.panel.transcript_retention_days,
                )
                await channel.delete(reason=f"Ticket deleted: {reason}"[:512])
                await self.repository.mark_deleted(
                    ticket_id=ticket_id,
                    actor_id=interaction.user.id,
                    reason=reason,
                )
            except discord.Forbidden:
                await self.ticket_creation_response(
                    interaction,
                    "I cannot delete this ticket channel because Manage Channels is missing.",
                )
                return
            except discord.HTTPException as error:
                logger.exception("Discord ticket deletion failed for %s", ticket_id)
                await self.ticket_creation_response(
                    interaction, f"Discord could not delete the ticket (error {error.code})."
                )
                return
            await self.ticket_creation_response(
                interaction,
                "Ticket channel deleted. Its transcript and audit record were retained.",
            )

    async def reopen_ticket(self, interaction: discord.Interaction, record: TicketRecord) -> str:
        changed = await self.repository.transition(
            ticket_id=record.ticket.id,
            actor_id=interaction.user.id,
            from_statuses=("closed",),
            to_status="open",
        )
        if not changed:
            return "Only closed tickets can be reopened."
        ticket_channel = (
            interaction.guild.get_channel(int(record.ticket.channel_id))
            if interaction.guild and record.ticket.channel_id
            else None
        )
        if isinstance(ticket_channel, discord.TextChannel) and interaction.guild:
            requester = interaction.guild.get_member(int(record.ticket.requester_id))
            if requester:
                overwrite = ticket_channel.overwrites_for(requester)
                overwrite.update(view_channel=True, send_messages=True)
                await ticket_channel.set_permissions(requester, overwrite=overwrite)
        await self.refresh_ticket_state(record.ticket.id)
        return "Ticket reopened."

    async def archive_ticket(self, interaction: discord.Interaction, record: TicketRecord) -> str:
        if not record.panel.archive_category_id:
            return "No archive category is configured."
        archive: discord.CategoryChannel | None = None
        if interaction.guild:
            candidate = interaction.guild.get_channel(int(record.panel.archive_category_id))
            if not isinstance(candidate, discord.CategoryChannel):
                return "The configured Discord archive category is unavailable."
            archive = candidate
        changed = await self.repository.transition(
            ticket_id=record.ticket.id,
            actor_id=interaction.user.id,
            from_statuses=("closed",),
            to_status="archived",
        )
        if not changed:
            return "Close the ticket before archiving it."
        await self.refresh_ticket_state(record.ticket.id)
        ticket_channel = (
            interaction.guild.get_channel(int(record.ticket.channel_id))
            if interaction.guild and record.ticket.channel_id
            else None
        )
        if isinstance(ticket_channel, discord.TextChannel) and interaction.guild and archive:
            requester = interaction.guild.get_member(int(record.ticket.requester_id))
            if requester:
                overwrite = ticket_channel.overwrites_for(requester)
                overwrite.update(view_channel=False)
                await ticket_channel.set_permissions(requester, overwrite=overwrite)
            await ticket_channel.edit(category=archive, reason="Ticket archived")
        return "Ticket archived."

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
            or not isinstance(message.channel, discord.TextChannel)
        ):
            return
        channel_id = message.channel.id
        now = monotonic()
        cached = self.ticket_channel_cache.get(channel_id)
        if cached is not None and cached[1] > now:
            record = cached[0]
        elif self.non_ticket_channel_cache.get(channel_id, 0.0) > now:
            return
        else:
            record = await self.repository.get_by_channel(channel_id)
            if record is None:
                # Discord channel IDs are never reused, so a negative cache is
                # safe and avoids querying Turso on every public-channel message.
                self.non_ticket_channel_cache[channel_id] = (
                    now + NON_TICKET_CHANNEL_CACHE_SECONDS
                )
                return
            self.ticket_channel_cache[channel_id] = (
                record,
                now + MESSAGE_CHANNEL_CACHE_SECONDS,
            )

        if record.ticket.status not in {"open", "claimed"}:
            return
        staff = isinstance(message.author, discord.Member) and is_ticket_staff(
            message.author, record.panel, record.category
        )
        last_write = self.activity_write_at.get(channel_id, 0.0)
        if (
            now - last_write < ACTIVITY_WRITE_COOLDOWN_SECONDS
            and not (staff and record.ticket.first_staff_response_at is None)
        ):
            return
        if await self.repository.touch_activity(
            channel_id=channel_id,
            actor_id=message.author.id,
            staff=staff,
        ):
            self.activity_write_at[channel_id] = now
            timestamp = datetime.now(UTC)
            record.ticket.last_activity_at = timestamp
            if staff and record.ticket.first_staff_response_at is None:
                record.ticket.first_staff_response_at = timestamp

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Sync private ticket controls when a member gains or loses a staff role."""
        if after.bot:
            return
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        changed_role_ids = before_role_ids.symmetric_difference(after_role_ids)
        if not changed_role_ids:
            return
        cached = self.staff_member_cache.get(after.guild.id)
        if cached is not None:
            cached[1][after.id] = after
        self.visible_member_cache.clear()
        configured_role_ids = self.ticket_staff_role_index.get(after.guild.id, {})
        if not configured_role_ids or not changed_role_ids.intersection(
            configured_role_ids
        ):
            return
        task = asyncio.create_task(
            self.synchronize_member_staff_controls(after, changed_role_ids),
            name=f"ticket-staff-role-sync:{after.guild.id}:{after.id}",
        )
        self.staff_member_sync_tasks.add(task)
        task.add_done_callback(self.staff_member_sync_tasks.discard)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cached = self.staff_member_cache.get(member.guild.id)
        if cached is not None:
            cached[1].pop(member.id, None)
        self.visible_member_cache.clear()

    async def synchronize_member_staff_controls(
        self, member: discord.Member, changed_role_ids: set[int]
    ) -> None:
        lock = self.staff_member_sync_locks.setdefault(member.id, asyncio.Lock())
        try:
            async with lock:
                guild_role_index = self.ticket_staff_role_index.get(member.guild.id, {})
                ticket_ids = {
                    ticket_id
                    for role_id in changed_role_ids
                    for ticket_id in guild_role_index.get(role_id, set())
                }
                for ticket_id in ticket_ids:
                    record = await self.repository.get_ticket(ticket_id)
                    if record is None:
                        self._remove_ticket_staff_role_index(ticket_id)
                        continue
                    configured_role_ids = {
                        int(role_id)
                        for role_id in (
                            record.category.staff_role_ids
                            or record.panel.staff_role_ids
                        )
                    }
                    if not configured_role_ids.intersection(changed_role_ids):
                        continue
                    await self.refresh_ticket_state(
                        ticket_id,
                        sync_channel_layout=False,
                        staff_member=member,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Could not synchronize ticket controls after role update for member %s",
                member.id,
            )

    @tasks.loop(seconds=15.0)
    async def synchronize_panels(self) -> None:
        for _ in range(10):
            record = await self.repository.claim_panel_sync()
            if record is None:
                return
            await self._synchronize_panel(record)

    @synchronize_panels.before_loop
    async def before_panel_sync(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(count=1)
    async def refresh_existing_ticket_states(self) -> None:
        recent_layout_cutoff = datetime.now(UTC) - timedelta(minutes=30)
        for record in await self.repository.published_panels():
            panel = record.panel
            if not panel.message_id:
                continue
            channel = self.bot.get_channel(int(panel.panel_channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                message = await channel.fetch_message(int(panel.message_id))
                await message.edit(
                    embed=self.ticket_panel_embed(record),
                    view=TicketPanelView(self, record),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.debug("Could not refresh published ticket panel %s", panel.id)
        for ticket in await self.repository.tickets_with_controls():
            await self.refresh_ticket_state(ticket.id, sync_channel_layout=True)
            if (
                ticket.status in {"open", "claimed", "closed"}
                and ticket.updated_at >= recent_layout_cutoff
            ):
                self.schedule_ticket_channel_sync(
                    ticket.id, status=ticket.status
                )

    @refresh_existing_ticket_states.before_loop
    async def before_existing_state_refresh(self) -> None:
        await self.bot.wait_until_ready()

    def ticket_panel_embed(
        self,
        record: TicketPanelRecord,
        *,
        title: str | None = None,
        description: str | None = None,
        footer_text: str | None = None,
        category_labels: dict[uuid.UUID, str] | None = None,
        category_descriptions: dict[uuid.UUID, str] | None = None,
    ) -> discord.Embed:
        panel = record.panel
        category_labels = category_labels or {}
        category_descriptions = category_descriptions or {}
        rendered_footer = panel.footer_text if footer_text is None else footer_text
        embed = themed_embed(
            "ticket",
            title=title or panel.title,
            description=description or panel.description,
            color=panel.color,
            emoji=panel.emoji,
            footer_text=rendered_footer,
            thumbnail_url=panel.thumbnail_url,
            image_url=panel.image_url,
        )
        for category in record.categories[:25]:
            icon = f"{category.emoji} " if category.emoji else ""
            embed.add_field(
                name=f"{icon}{category_labels.get(category.id, category.label)}"[:256],
                value=(
                    category_descriptions.get(
                        category.id, category.description or "\u200b"
                    )[:1024]
                    or "\u200b"
                ),
                inline=True,
            )
        return embed

    async def _synchronize_panel(self, record: TicketPanelRecord) -> None:
        panel = record.panel
        try:
            channel = self.bot.get_channel(int(panel.panel_channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(panel.panel_channel_id))
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != int(
                panel.guild_id
            ):
                raise ValueError("ticket_panel_channel_unavailable")
            if panel.status == "deleting":
                if panel.message_id:
                    try:
                        await (await channel.fetch_message(int(panel.message_id))).delete()
                    except discord.NotFound:
                        pass
                await self.repository.finish_panel_delete(panel.id)
                return
            embed = self.ticket_panel_embed(record)
            view = TicketPanelView(self, record)
            if panel.message_id:
                message = await channel.fetch_message(int(panel.message_id))
                await message.edit(
                    embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                message = await channel.send(
                    embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
                )
            await self.repository.mark_panel_published(panel.id, message.id)
            for ticket in await self.repository.tickets_with_controls(panel_id=panel.id):
                await self.refresh_ticket_state(ticket.id, sync_channel_layout=False)
        except discord.NotFound:
            if panel.status == "deleting":
                await self.repository.finish_panel_delete(panel.id)
                return
            await self.repository.mark_panel_failed(panel.id, "not_found")
        except Exception as error:
            logger.exception("Ticket panel %s synchronization failed", panel.id)
            await self.repository.mark_panel_failed(
                panel.id, str(getattr(error, "code", type(error).__name__))
            )

    @tasks.loop(minutes=10.0)
    async def check_inactivity(self) -> None:
        now = datetime.now(UTC)
        for record in await self.repository.active_for_inactivity():
            age = now - record.ticket.last_activity_at
            guild = self.bot.get_guild(int(record.ticket.guild_id))
            channel = (
                guild.get_channel(int(record.ticket.channel_id))
                if guild and record.ticket.channel_id
                else None
            )
            if record.panel.auto_close_hours and age >= timedelta(
                hours=record.panel.auto_close_hours
            ):
                await self.close_ticket(
                    None,
                    record.ticket.id,
                    "Automatically closed after inactivity.",
                    record=record,
                )
            elif (
                record.panel.inactivity_reminder_hours
                and age >= timedelta(hours=record.panel.inactivity_reminder_hours)
                and record.ticket.reminded_at is None
                and isinstance(channel, discord.TextChannel)
                and await self.repository.mark_reminded(record.ticket.id)
            ):
                await channel.send(
                    f"<@{record.ticket.requester_id}> this ticket is inactive "
                    "and may close automatically.",
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False
                    ),
                )
        await self.repository.delete_expired_transcripts()

    @check_inactivity.before_loop
    async def before_inactivity(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketsCog(bot))
