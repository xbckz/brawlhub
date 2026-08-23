"""Selected-admin Discord commands for configuring the ticket subsystem."""

import io
import re
import uuid
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from bhbot.bot import TicketBot
from bhbot.db.models import TicketPanelCategory
from bhbot.db.ticket_repository import TicketCategoryConfig, TicketPanelRecord, TicketRepository
from bhbot.ui import (
    SafeModal,
    SafeView,
    normalize_component_emoji,
    respond_ephemeral,
    safe_component_emoji,
    themed_embed,
)

ticket_commands = app_commands.Group(
    name="ticket", description="Configure and inspect the ticket system."
)


async def selected_admin(interaction: discord.Interaction) -> bool:
    if (
        interaction.guild is not None
        and isinstance(interaction.user, discord.Member)
        and await cast(TicketBot, interaction.client).is_selected_admin(interaction.user)
    ):
        return True
    raise app_commands.CheckFailure("Only selected ticket administrators can use this command.")


def ticket_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise app_commands.AppCommandError("Ticket ID must be a valid UUID.") from error


def category_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")
    return normalized[:50] or "support"


def category_config(
    *,
    key: str,
    label: str,
    description: str | None,
    emoji: str | None,
    staff_role_ids: list[int],
    form_fields: list[dict[str, Any]] | None = None,
) -> TicketCategoryConfig:
    return {
        "key": category_key(key),
        "label": label[:100],
        "description": description[:100] if description else None,
        "emoji": emoji[:100] if emoji else None,
        "staff_role_ids": staff_role_ids,
        "form_fields": form_fields or [],
    }


def panel_values(panel: Any) -> dict[str, Any]:
    return {
        "panel_channel_id": panel.panel_channel_id,
        "open_category_id": panel.open_category_id,
        "archive_category_id": panel.archive_category_id,
        "title": panel.title,
        "description": panel.description,
        "opening_message": panel.opening_message,
        "color": panel.color,
        "emoji": panel.emoji,
        "footer_text": panel.footer_text,
        "select_placeholder": panel.select_placeholder,
        "thumbnail_url": panel.thumbnail_url,
        "image_url": panel.image_url,
        "staff_role_ids": panel.staff_role_ids,
        "notify_staff_on_create": panel.notify_staff_on_create,
        "name_template": panel.name_template,
        "max_open_per_user": panel.max_open_per_user,
        "duplicate_scope": panel.duplicate_scope,
        "inactivity_reminder_hours": panel.inactivity_reminder_hours,
        "auto_close_hours": panel.auto_close_hours,
        "transcript_retention_days": panel.transcript_retention_days,
    }


def category_configs(categories: list[TicketPanelCategory]) -> list[TicketCategoryConfig]:
    return [
        category_config(
            key=item.key,
            label=item.label,
            description=item.description,
            emoji=item.emoji,
            staff_role_ids=[int(value) for value in item.staff_role_ids],
            form_fields=item.form_fields,
        )
        for item in categories
    ]


def parse_role_ids(raw: str) -> list[int]:
    values: list[int] = []
    for value in raw.replace("<@&", "").replace(">", "").split(","):
        value = value.strip()
        if not value:
            continue
        if not value.isdigit():
            raise ValueError(f"Invalid role ID: {value}")
        values.append(int(value))
    return list(dict.fromkeys(values))


def parse_color(raw: str) -> int:
    value = raw.strip().removeprefix("#")
    if len(value) != 6 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError("Color must be a six-digit hexadecimal value such as #22B8CF.")
    return int(value, 16)


def parse_optional_int(raw: str, label: str, *, minimum: int = 0, maximum: int = 3650) -> int | None:
    value = raw.strip()
    if not value:
        return None
    if not value.isdigit() or not minimum <= int(value) <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}, or blank.")
    return int(value)


async def ticket_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return []
    bot = cast(TicketBot, interaction.client)
    if not await bot.is_selected_admin(interaction.user):
        return []
    records = await TicketRepository(bot.database).history(guild_id=interaction.guild.id, limit=100)
    needle = current.casefold().strip()
    return [
        app_commands.Choice(
            name=f"{record.ticket.subject} • {str(record.ticket.id)[:8]}"[:100],
            value=str(record.ticket.id),
        )
        for record in records
        if not needle or needle in record.ticket.subject.casefold() or needle in str(record.ticket.id).casefold()
    ][:25]


class PanelEditorButton(discord.ui.Button[Any]):
    def __init__(self, editor: "PanelEditorView", label: str, action: str, row: int, *, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(
            label=label,
            style=style,
            row=row,
            custom_id=f"ticket-editor:{action}:{editor.panel_id}",
        )
        self.editor = editor
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.editor.handle_action(self.action, interaction)


class PanelCategorySelect(discord.ui.Select[Any]):
    def __init__(self, editor: "PanelEditorView") -> None:
        self.editor = editor
        options = self.options_for(editor.record)
        if not options:
            options = [discord.SelectOption(label="No categories", value="__none__")]
        super().__init__(
            placeholder="Select a category to edit or remove",
            min_values=1,
            max_values=1,
            row=3,
            custom_id=f"ticket-editor-category:{editor.panel_id}",
            options=options,
            disabled=not editor.record.categories,
        )

    @staticmethod
    def options_for(record: TicketPanelRecord) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(
                label=item.label[:100],
                value=str(item.id),
                description=(item.description or "No description")[:100],
                emoji=safe_component_emoji(item.emoji),
            )
            for item in record.categories[:25]
        ]

    async def callback(self, interaction: discord.Interaction) -> None:
        self.editor.selected_category_id = uuid.UUID(self.values[0])
        await interaction.response.edit_message(embed=self.editor.editor_embed(), view=self.editor)


class PanelEmojiSelect(discord.ui.Select[Any]):
    def __init__(self, picker: "PanelEmojiPickerView") -> None:
        self.picker = picker
        emojis = sorted(picker.emojis, key=lambda emoji: (emoji.name.casefold(), emoji.id))
        current = discord.PartialEmoji.from_str(picker.current_emoji or "")
        current_id = current.id
        if current_id is not None and all(emoji.id != current_id for emoji in emojis):
            current_emoji = discord.utils.get(picker.emojis, id=current_id)
            if current_emoji is not None:
                emojis.insert(0, current_emoji)
        options = [discord.SelectOption(label="No emoji", value="__none__")]
        options.extend(
            discord.SelectOption(
                label=emoji.name[:100],
                value=str(emoji.id),
                emoji=emoji,
            )
            for emoji in emojis[:24]
        )
        super().__init__(
            placeholder="Choose a server emoji",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"ticket-editor-emoji:{picker.editor.panel_id}:{picker.target}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        emoji = None
        if selected != "__none__":
            emoji = discord.utils.get(self.picker.emojis, id=int(selected))
            if emoji is None:
                await respond_ephemeral(interaction, "That server emoji is no longer available.")
                return
        await interaction.response.defer(ephemeral=True)
        updated = await self.picker.editor.set_emoji(
            self.picker.target,
            str(emoji) if emoji is not None else None,
        )
        if updated is None:
            await interaction.followup.send(
                "The panel changed while you were editing it. Reopen the editor.",
                ephemeral=True,
            )
            return
        if self.picker.editor.message is not None:
            await self.picker.editor.refresh_message(self.picker.editor.message)
        target_label = "Panel" if self.picker.target == "panel" else "Category"
        await interaction.followup.send(
            f"{target_label} emoji updated and the editor refreshed.",
            ephemeral=True,
        )


class PanelEmojiPickerView(SafeView):
    def __init__(
        self,
        editor: "PanelEditorView",
        emojis: list[discord.Emoji],
        target: str,
        current_emoji: str | None,
    ) -> None:
        super().__init__(timeout=600)
        self.editor = editor
        self.emojis = emojis
        self.target = target
        self.current_emoji = current_emoji
        self.add_item(PanelEmojiSelect(self))


class PanelEditorView(SafeView):
    def __init__(self, cog: "TicketAdminCog", record: TicketPanelRecord, actor_id: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.record = record
        self.panel_id = record.panel.id
        self.actor_id = actor_id
        self.selected_category_id: uuid.UUID | None = None
        self.message: discord.Message | None = None
        self.preview_message: discord.Message | None = None
        self.add_item(PanelEditorButton(self, "Preview", "preview", 0))
        self.add_item(PanelEditorButton(self, "Appearance", "appearance", 0))
        self.add_item(PanelEditorButton(self, "Messages", "messages", 0))
        self.add_item(PanelEditorButton(self, "Media", "media", 0))
        self.add_item(PanelEditorButton(self, "Publish", "publish", 0, style=discord.ButtonStyle.success))
        self.add_item(PanelEditorButton(self, "Add category", "add-category", 1, style=discord.ButtonStyle.success))
        self.add_item(PanelEditorButton(self, "Edit category", "edit-category", 1))
        self.add_item(PanelEditorButton(self, "Remove category", "remove-category", 1, style=discord.ButtonStyle.danger))
        self.add_item(PanelEditorButton(self, "Limits", "limits", 1))
        self.add_item(PanelEditorButton(self, "Routes", "routes", 1))
        self.add_item(PanelEditorButton(self, "Panel emoji", "panel-emoji", 2))
        self.add_item(PanelEditorButton(self, "Edit category emoji", "category-emoji", 2))
        self.category_select = PanelCategorySelect(self)
        self.add_item(self.category_select)

    def editor_embed(self) -> discord.Embed:
        panel = self.record.panel
        selected = next(
            (item.label for item in self.record.categories if item.id == self.selected_category_id),
            "none",
        )
        embed = themed_embed(
            "ticket",
            title=f"Panel editor: {panel.title}",
            description="Use the controls below to edit this panel. Changes are saved and queued automatically; Preview shows the exact public layout.",
            color=panel.color,
            emoji=panel.emoji,
            footer_text=panel.footer_text,
            thumbnail_url=panel.thumbnail_url,
            image_url=panel.image_url,
        )
        embed.add_field(name="Panel ID", value=f"`{panel.id}`", inline=False)
        embed.add_field(
            name=f"Categories ({len(self.record.categories)}/25)",
            value="\n".join(
                f"{item.emoji or '•'} **{item.label}** — {item.description or 'No description'}"
                for item in self.record.categories[:25]
            )[:1024]
            or "No categories configured.",
            inline=False,
        )
        embed.add_field(name="Selected category", value=selected, inline=True)
        embed.add_field(
            name="Ticket opening message",
            value=panel.opening_message or "(only the requester mention will be sent)",
            inline=False,
        )
        return embed

    def editor_content(self) -> str:
        return (
            f"Panel editor — {self.record.panel.status}. "
            "Every saved change refreshes this editor and any open preview."
        )

    async def current_record(self) -> TicketPanelRecord | None:
        return await self.cog.repository.get_panel(
            guild_id=int(self.record.panel.guild_id), panel_id=self.panel_id
        )

    async def persist(
        self,
        changes: dict[str, Any],
        categories: list[TicketCategoryConfig] | None = None,
    ) -> TicketPanelRecord | None:
        current = await self.current_record()
        if current is None:
            return None
        values = panel_values(current.panel)
        values.update(changes)
        return await self.cog.repository.update_panel(
            guild_id=int(current.panel.guild_id),
            panel_id=current.panel.id,
            actor_id=self.actor_id,
            requested_status="queued",
            values=values,
            categories=categories or category_configs(current.categories),
            expected_revision=current.panel.revision,
        )

    async def set_emoji(
        self, target: str, emoji: str | None
    ) -> TicketPanelRecord | None:
        current = await self.current_record()
        if current is None:
            return None
        if target == "panel":
            return await self.persist({"emoji": emoji or ""})
        selected = next(
            (item for item in current.categories if item.id == self.selected_category_id),
            None,
        )
        if selected is None:
            return None
        categories = category_configs(current.categories)
        category_index = next(
            index for index, item in enumerate(current.categories) if item.id == selected.id
        )
        categories[category_index]["emoji"] = emoji
        return await self.persist({}, categories)

    async def refresh_message(self, message: discord.Message) -> None:
        record = await self.current_record()
        if record is None:
            return
        selected_key = next(
            (item.key for item in self.record.categories if item.id == self.selected_category_id),
            None,
        )
        self.record = record
        refreshed = PanelEditorView(self.cog, record, self.actor_id)
        refreshed.message = message
        refreshed.selected_category_id = next(
            (item.id for item in record.categories if item.key == selected_key),
            None,
        )
        refreshed.preview_message = self.preview_message
        await message.edit(
            content=refreshed.editor_content(),
            embed=refreshed.editor_embed(),
            view=refreshed,
        )
        if refreshed.preview_message is not None:
            ticket_cog = self.cog.bot.get_cog("TicketsCog")
            if ticket_cog is not None:
                from bhbot.cogs.tickets import TicketPanelView

                try:
                    await refreshed.preview_message.edit(
                        content="Preview (updated automatically; the dropdown is disabled):",
                        embed=ticket_cog.ticket_panel_embed(record),
                        view=TicketPanelView(ticket_cog, record, timeout=600, disabled=True),
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    refreshed.preview_message = None

    async def send_preview(self, interaction: discord.Interaction) -> None:
        ticket_cog = self.cog.bot.get_cog("TicketsCog")
        if ticket_cog is None:
            await respond_ephemeral(interaction, "The ticket runtime is still starting. Try again in a moment.")
            return
        from bhbot.cogs.tickets import TicketPanelView

        await interaction.response.send_message(
            content="Preview (the dropdown is disabled here):",
            embed=ticket_cog.ticket_panel_embed(self.record),
            view=TicketPanelView(
                ticket_cog,
                self.record,
                timeout=600,
                disabled=True,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.preview_message = await interaction.original_response()

    async def handle_action(self, action: str, interaction: discord.Interaction) -> None:
        message = interaction.message
        if action == "preview":
            await self.send_preview(interaction)
            return
        if action == "publish":
            if not self.record.categories:
                await respond_ephemeral(
                    interaction,
                    "Add at least one category before publishing this panel.",
                )
                return
            changed = await self.cog.repository.queue_panel(
                guild_id=int(self.record.panel.guild_id), panel_id=self.panel_id,
                actor_id=interaction.user.id,
            )
            if message is not None:
                await interaction.response.defer(ephemeral=True)
                if changed:
                    await self.refresh_message(message)
                await interaction.followup.send(
                    "Panel synchronization queued." if changed else "The panel is currently busy or no longer exists.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Panel synchronization queued." if changed else "The panel is currently busy or no longer exists.",
                    ephemeral=True,
                )
            return
        if message is None:
            await respond_ephemeral(interaction, "This editor message is no longer available. Run /ticket panels again.")
            return
        if action == "appearance":
            await interaction.response.send_modal(PanelAppearanceModal(self, message))
        elif action in {"panel-emoji", "category-emoji"}:
            if action == "category-emoji":
                selected = next(
                    (item for item in self.record.categories if item.id == self.selected_category_id),
                    None,
                )
                if selected is None:
                    await respond_ephemeral(interaction, "Select a category first.")
                    return
                target = "category"
                current_emoji = selected.emoji
                prompt = "Choose a server emoji for the selected category."
            else:
                target = "panel"
                current_emoji = self.record.panel.emoji
                prompt = "Choose a server emoji for the panel title."
            if interaction.guild is None:
                await respond_ephemeral(interaction, "This control can only be used in a server.")
                return
            await interaction.response.defer(ephemeral=True)
            try:
                emojis = await interaction.guild.fetch_emojis()
            except discord.HTTPException:
                emojis = list(interaction.guild.emojis)
            if not emojis:
                prompt += "\nNo custom server emojis are available to the bot."
            await interaction.followup.send(
                prompt,
                view=PanelEmojiPickerView(self, emojis, target, current_emoji),
                ephemeral=True,
            )
        elif action == "messages":
            await interaction.response.send_modal(PanelMessagesModal(self, message))
        elif action == "media":
            await interaction.response.send_modal(PanelMediaModal(self, message))
        elif action == "limits":
            await interaction.response.send_modal(PanelLimitsModal(self, message))
        elif action == "routes":
            await interaction.response.send_modal(PanelRoutesModal(self, message))
        elif action == "add-category":
            await interaction.response.send_modal(PanelCategoryModal(self, message))
        elif action == "edit-category":
            selected = next(
                (item for item in self.record.categories if item.id == self.selected_category_id),
                None,
            )
            if selected is None:
                await respond_ephemeral(interaction, "Select a category first.")
                return
            await interaction.response.send_modal(PanelCategoryModal(self, message, selected))
        elif action == "remove-category":
            selected = next(
                (item for item in self.record.categories if item.id == self.selected_category_id),
                None,
            )
            if selected is None:
                await respond_ephemeral(interaction, "Select a category first.")
                return
            if len(self.record.categories) <= 1:
                await respond_ephemeral(interaction, "A panel must keep at least one category.")
                return
            categories = category_configs(
                [item for item in self.record.categories if item.id != selected.id]
            )
            updated = await self.persist({}, categories)
            if updated is None:
                await respond_ephemeral(interaction, "The panel changed while you were editing it. Reopen the editor.")
                return
            await interaction.response.defer(ephemeral=True)
            await self.refresh_message(message)
            await interaction.followup.send("Category removed and the full editor preview was refreshed.", ephemeral=True)


class PanelEditorModal(SafeModal):
    def __init__(self, editor: PanelEditorView, message: discord.Message, *, title: str) -> None:
        super().__init__(title=title, timeout=600)
        self.editor = editor
        self.message = message

    async def apply_changes(
        self,
        interaction: discord.Interaction,
        changes: dict[str, Any],
        categories: list[TicketCategoryConfig] | None = None,
    ) -> None:
        try:
            updated = await self.editor.persist(changes, categories)
        except ValueError as error:
            await respond_ephemeral(interaction, str(error))
            return
        if updated is None:
            await respond_ephemeral(interaction, "The panel changed while you were editing it. Reopen the editor.")
            return
        try:
            await interaction.response.defer(ephemeral=True)
            await self.editor.refresh_message(self.message)
            await interaction.followup.send("Panel changes saved and the full editor preview was refreshed.", ephemeral=True)
        except discord.HTTPException:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Panel changes were saved, but the editor message could not be refreshed.",
                    ephemeral=True,
                )
            else:
                await respond_ephemeral(
                    interaction, "Panel changes were saved, but the editor message could not be refreshed."
                )


class PanelAppearanceModal(PanelEditorModal):
    def __init__(self, editor: PanelEditorView, message: discord.Message) -> None:
        super().__init__(editor, message, title="Edit panel appearance")
        panel = editor.record.panel
        self.title_input = discord.ui.TextInput(label="Title", default=panel.title, max_length=256)
        self.description_input = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, default=panel.description[:4000], max_length=4000
        )
        self.color_input = discord.ui.TextInput(label="Color (#RRGGBB)", default=f"#{panel.color:06X}", max_length=7)
        self.emoji_input = discord.ui.TextInput(label="Title emoji (Unicode/server emoji)", default=panel.emoji, required=False, max_length=100)
        self.footer_input = discord.ui.TextInput(label="Footer (optional)", default=panel.footer_text or "", required=False, max_length=2048)
        for item in (self.title_input, self.description_input, self.color_input, self.emoji_input, self.footer_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            color = parse_color(self.color_input.value)
        except ValueError as error:
            await respond_ephemeral(interaction, str(error))
            return
        await self.apply_changes(
            interaction,
            {
                "title": self.title_input.value.strip()[:256],
                "description": self.description_input.value[:4096],
                "color": color,
                "emoji": normalize_component_emoji(self.emoji_input.value, interaction.guild) or "",
                "footer_text": self.footer_input.value.strip()[:2048] or None,
            },
        )


class PanelMessagesModal(PanelEditorModal):
    def __init__(self, editor: PanelEditorView, message: discord.Message) -> None:
        super().__init__(editor, message, title="Edit ticket messages")
        panel = editor.record.panel
        self.placeholder_input = discord.ui.TextInput(label="Category selector placeholder", default=panel.select_placeholder, max_length=150)
        self.opening_input = discord.ui.TextInput(
            label="Ticket opening message", style=discord.TextStyle.paragraph,
            default=panel.opening_message, required=False, max_length=1000,
        )
        self.template_input = discord.ui.TextInput(label="Channel template ({number}, {category})", default=panel.name_template, max_length=100)
        self.notify_input = discord.ui.TextInput(
            label="Notify staff on create? yes/no", default="yes" if panel.notify_staff_on_create else "no", max_length=3
        )
        for item in (self.placeholder_input, self.opening_input, self.template_input, self.notify_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        notify = self.notify_input.value.strip().casefold()
        if notify not in {"yes", "no"}:
            await respond_ephemeral(interaction, "Notify staff must be `yes` or `no`.")
            return
        await self.apply_changes(
            interaction,
            {
                "select_placeholder": self.placeholder_input.value.strip()[:150],
                "opening_message": self.opening_input.value[:1000],
                "name_template": self.template_input.value.strip()[:100],
                "notify_staff_on_create": notify == "yes",
            },
        )


class PanelMediaModal(PanelEditorModal):
    def __init__(self, editor: PanelEditorView, message: discord.Message) -> None:
        super().__init__(editor, message, title="Edit panel media")
        panel = editor.record.panel
        self.thumbnail_input = discord.ui.TextInput(label="Thumbnail URL (optional)", default=panel.thumbnail_url or "", required=False, max_length=2048)
        self.image_input = discord.ui.TextInput(label="Large image URL (optional)", default=panel.image_url or "", required=False, max_length=2048)
        self.add_item(self.thumbnail_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.apply_changes(
            interaction,
            {
                "thumbnail_url": self.thumbnail_input.value.strip() or None,
                "image_url": self.image_input.value.strip() or None,
            },
        )


class PanelLimitsModal(PanelEditorModal):
    def __init__(self, editor: PanelEditorView, message: discord.Message) -> None:
        super().__init__(editor, message, title="Edit ticket limits")
        panel = editor.record.panel
        self.max_open_input = discord.ui.TextInput(label="Max open tickets per user", default=str(panel.max_open_per_user), max_length=3)
        self.scope_input = discord.ui.TextInput(
            label="Duplicate scope: category or server",
            default=panel.duplicate_scope if panel.duplicate_scope in {"category", "guild"} else "category",
            max_length=8,
        )
        self.reminder_input = discord.ui.TextInput(label="Reminder hours (blank disables)", default=str(panel.inactivity_reminder_hours or ""), required=False, max_length=4)
        self.close_input = discord.ui.TextInput(label="Auto-close hours (blank disables)", default=str(panel.auto_close_hours or ""), required=False, max_length=4)
        self.retention_input = discord.ui.TextInput(label="Transcript retention days", default=str(panel.transcript_retention_days), max_length=4)
        for item in (self.max_open_input, self.scope_input, self.reminder_input, self.close_input, self.retention_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            max_open = parse_optional_int(self.max_open_input.value, "Max open tickets", minimum=1, maximum=100)
            reminder = parse_optional_int(self.reminder_input.value, "Reminder hours", maximum=8760)
            auto_close = parse_optional_int(self.close_input.value, "Auto-close hours", maximum=8760)
            retention = parse_optional_int(self.retention_input.value, "Transcript retention days", minimum=1, maximum=3650)
        except ValueError as error:
            await respond_ephemeral(interaction, str(error))
            return
        scope = self.scope_input.value.strip().casefold()
        if scope == "server":
            scope = "guild"
        if scope not in {"category", "guild"} or max_open is None or retention is None:
            await respond_ephemeral(interaction, "Scope must be `category` or `server`, and required numeric values must be valid.")
            return
        await self.apply_changes(
            interaction,
            {
                "max_open_per_user": max_open,
                "duplicate_scope": scope,
                "inactivity_reminder_hours": reminder,
                "auto_close_hours": auto_close,
                "transcript_retention_days": retention,
            },
        )


class PanelRoutesModal(PanelEditorModal):
    def __init__(self, editor: PanelEditorView, message: discord.Message) -> None:
        super().__init__(editor, message, title="Edit panel locations and staff")
        panel = editor.record.panel
        self.panel_channel_input = discord.ui.TextInput(label="Panel channel ID", default=panel.panel_channel_id, max_length=20)
        self.open_category_input = discord.ui.TextInput(label="Open category ID", default=panel.open_category_id, max_length=20)
        self.archive_category_input = discord.ui.TextInput(label="Archive category ID (blank = none)", default=panel.archive_category_id or "", required=False, max_length=20)
        self.staff_roles_input = discord.ui.TextInput(label="Staff role IDs (comma-separated)", default=", ".join(panel.staff_role_ids), max_length=1000)
        for item in (self.panel_channel_input, self.open_category_input, self.archive_category_input, self.staff_roles_input):
            self.add_item(item)

    @staticmethod
    def snowflake(raw: str, label: str, *, required: bool = True) -> int | None:
        value = raw.strip()
        if not value and not required:
            return None
        match = re.fullmatch(r"(?:<#|<@&)?(\d+)>?", value)
        if match is None:
            raise ValueError(f"{label} must be a Discord ID or mention.")
        return int(match.group(1))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond_ephemeral(interaction, "This editor can only be used in a server.")
            return
        try:
            panel_channel_id = self.snowflake(self.panel_channel_input.value, "Panel channel")
            open_category_id = self.snowflake(self.open_category_input.value, "Open category")
            archive_category_id = self.snowflake(self.archive_category_input.value, "Archive category", required=False)
            role_ids = parse_role_ids(self.staff_roles_input.value)
        except ValueError as error:
            await respond_ephemeral(interaction, str(error))
            return
        panel_channel = interaction.guild.get_channel(panel_channel_id or 0)
        open_category = interaction.guild.get_channel(open_category_id or 0)
        archive_category = interaction.guild.get_channel(archive_category_id or 0) if archive_category_id else None
        if not isinstance(panel_channel, discord.TextChannel):
            await respond_ephemeral(interaction, "Panel channel must be a text channel in this server.")
            return
        if not isinstance(open_category, discord.CategoryChannel):
            await respond_ephemeral(interaction, "Open category must be a category channel in this server.")
            return
        if archive_category_id and not isinstance(archive_category, discord.CategoryChannel):
            await respond_ephemeral(interaction, "Archive category must be a category channel in this server.")
            return
        if any(interaction.guild.get_role(role_id) is None for role_id in role_ids):
            await respond_ephemeral(interaction, "Every staff role ID must belong to this server.")
            return
        await self.apply_changes(
            interaction,
            {
                "panel_channel_id": panel_channel_id,
                "open_category_id": open_category_id,
                "archive_category_id": archive_category_id,
                "staff_role_ids": role_ids,
            },
        )


class PanelCategoryModal(PanelEditorModal):
    def __init__(self, editor: PanelEditorView, message: discord.Message, existing: TicketPanelCategory | None = None) -> None:
        super().__init__(editor, message, title="Edit ticket category" if existing else "Add ticket category")
        self.existing = existing
        self.label_input = discord.ui.TextInput(label="Category label", default=existing.label if existing else "", max_length=100)
        self.description_input = discord.ui.TextInput(label="Category description", style=discord.TextStyle.paragraph, default=existing.description if existing and existing.description else "", required=False, max_length=100)
        self.emoji_input = discord.ui.TextInput(label="Category emoji (Unicode/server emoji)", default=existing.emoji if existing and existing.emoji else "", required=False, max_length=100)
        self.roles_input = discord.ui.TextInput(label="Staff role IDs (comma-separated)", default=", ".join(existing.staff_role_ids) if existing else ", ".join(editor.record.panel.staff_role_ids), required=False, max_length=1000)
        existing_question = ""
        if existing and existing.form_fields:
            existing_question = str(existing.form_fields[0].get("label", ""))
        self.question_input = discord.ui.TextInput(
            label="Ticket question (one line, optional)",
            default=existing_question[:45],
            required=False,
            max_length=45,
        )
        for item in (self.label_input, self.description_input, self.emoji_input, self.roles_input, self.question_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        label = self.label_input.value.strip()
        if not label:
            await respond_ephemeral(interaction, "Category label cannot be empty.")
            return
        try:
            roles = parse_role_ids(self.roles_input.value) or [int(value) for value in self.editor.record.panel.staff_role_ids]
        except ValueError as error:
            await respond_ephemeral(interaction, str(error))
            return
        question = self.question_input.value.strip()[:45]
        form_fields = [
            {
                "key": category_key(question),
                "label": question,
                "style": "paragraph",
                "required": True,
                "minimum_length": 0,
                "maximum_length": 2000,
                "placeholder": None,
            }
        ] if question else []
        key = category_key(label)
        current = await self.editor.current_record()
        if current is None:
            await respond_ephemeral(interaction, "The panel no longer exists.")
            return
        if any(item.key == key and (self.existing is None or item.id != self.existing.id) for item in current.categories):
            await respond_ephemeral(interaction, "That category key already exists.")
            return
        categories = category_configs(current.categories)
        normalized_emoji = normalize_component_emoji(self.emoji_input.value, interaction.guild)
        replacement = category_config(
            key=key,
            label=label,
            description=self.description_input.value.strip() or None,
            emoji=normalized_emoji,
            staff_role_ids=roles,
            form_fields=form_fields,
        )
        if self.existing is None:
            if len(categories) >= 25:
                await respond_ephemeral(interaction, "Discord panels support a maximum of 25 categories.")
                return
            categories.append(replacement)
        else:
            index = next((index for index, item in enumerate(current.categories) if item.id == self.existing.id), None)
            if index is None:
                await respond_ephemeral(interaction, "That category is no longer available. Reopen the editor.")
                return
            categories[index] = replacement
        await self.apply_changes(interaction, {}, categories)


class PanelManagerSelect(discord.ui.Select[Any]):
    def __init__(self, manager: "PanelManagerView") -> None:
        self.manager = manager
        options = [
            discord.SelectOption(
                label=record.panel.title[:100],
                value=str(record.panel.id),
                description=f"{record.panel.status} • {len(record.categories)} categories"[:100],
            )
            for record in manager.records[:25]
        ]
        if not options:
            options = [discord.SelectOption(label="No panels", value="__none__")]
        super().__init__(
            placeholder="Select a ticket panel",
            min_values=1,
            max_values=1,
            row=0,
            custom_id="ticket-panel-manager-select",
            options=options,
            disabled=not manager.records,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.manager.selected_panel_id = uuid.UUID(self.values[0])
        await interaction.response.edit_message(embed=self.manager.manager_embed(), view=self.manager)


class PanelManagerButton(discord.ui.Button[Any]):
    def __init__(self, manager: "PanelManagerView", label: str, action: str, *, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(
            label=label,
            style=style,
            row=1,
            custom_id=f"ticket-panel-manager:{action}",
        )
        self.manager = manager
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.manager.handle_action(self.action, interaction)


class PanelCreateContinueButton(discord.ui.Button[Any]):
    def __init__(self, setup: "PanelCreateSetupView") -> None:
        super().__init__(
            label="Continue",
            style=discord.ButtonStyle.success,
            row=4,
            custom_id="ticket-panel-create-continue",
        )
        self.setup = setup

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.setup.continue_to_details(interaction)


class PanelCreateChannelSelect(discord.ui.ChannelSelect[Any]):
    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()


class PanelCreateRoleSelect(discord.ui.RoleSelect[Any]):
    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()


class PanelCreateSetupView(SafeView):
    def __init__(self, cog: "TicketAdminCog") -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.panel_channel = PanelCreateChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Select the panel channel",
            min_values=1,
            max_values=1,
            row=0,
            custom_id="ticket-panel-create-channel",
        )
        self.open_category = PanelCreateChannelSelect(
            channel_types=[discord.ChannelType.category],
            placeholder="Select the ticket category",
            min_values=1,
            max_values=1,
            row=1,
            custom_id="ticket-panel-create-open-category",
        )
        self.archive_category = PanelCreateChannelSelect(
            channel_types=[discord.ChannelType.category],
            placeholder="Select an archive category (optional)",
            min_values=0,
            max_values=1,
            row=2,
            custom_id="ticket-panel-create-archive-category",
        )
        self.staff_role = PanelCreateRoleSelect(
            placeholder="Select the staff role",
            min_values=1,
            max_values=1,
            row=3,
            custom_id="ticket-panel-create-staff-role",
        )
        self.add_item(self.panel_channel)
        self.add_item(self.open_category)
        self.add_item(self.archive_category)
        self.add_item(self.staff_role)
        self.add_item(PanelCreateContinueButton(self))

    async def continue_to_details(self, interaction: discord.Interaction) -> None:
        if not self.panel_channel.values or not self.open_category.values or not self.staff_role.values:
            await respond_ephemeral(
                interaction,
                "Select the panel channel, ticket category, and staff role first.",
            )
            return
        await interaction.response.send_modal(PanelCreateDetailsModal(self))


class PanelCreateDetailsModal(SafeModal):
    def __init__(self, setup: PanelCreateSetupView) -> None:
        super().__init__(title="Create ticket panel", timeout=600)
        self.setup = setup
        self.title_input = discord.ui.TextInput(
            label="Panel title",
            default="Support tickets",
            max_length=100,
        )
        self.description_input = discord.ui.TextInput(
            label="Panel description",
            style=discord.TextStyle.paragraph,
            default="Choose a category below to open a private support ticket.",
            max_length=1000,
        )
        self.opening_input = discord.ui.TextInput(
            label="Ticket opening message",
            style=discord.TextStyle.paragraph,
            default="Thanks for opening a ticket. A member of our team will be with you shortly.",
            max_length=1000,
        )
        for item in (self.title_input, self.description_input, self.opening_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond_ephemeral(interaction, "This control can only be used in a server.")
            return
        panel_channel_id = int(getattr(self.setup.panel_channel.values[0], "id", 0))
        open_category_id = int(getattr(self.setup.open_category.values[0], "id", 0))
        archive_category_id = (
            int(getattr(self.setup.archive_category.values[0], "id", 0))
            if self.setup.archive_category.values
            else None
        )
        staff_role_id = int(getattr(self.setup.staff_role.values[0], "id", 0))
        panel_channel = interaction.guild.get_channel(panel_channel_id)
        open_category = interaction.guild.get_channel(open_category_id)
        archive_category = (
            interaction.guild.get_channel(archive_category_id)
            if archive_category_id
            else None
        )
        staff_role = interaction.guild.get_role(staff_role_id)
        if not isinstance(panel_channel, discord.TextChannel):
            await respond_ephemeral(interaction, "The panel channel must be a text channel.")
            return
        if not isinstance(open_category, discord.CategoryChannel):
            await respond_ephemeral(interaction, "The ticket category must be a category channel.")
            return
        if archive_category is not None and not isinstance(archive_category, discord.CategoryChannel):
            await respond_ephemeral(interaction, "The archive category must be a category channel.")
            return
        if not isinstance(staff_role, discord.Role) or staff_role.is_default():
            await respond_ephemeral(interaction, "Select a normal staff role, not @everyone.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        record = await self.setup.cog.repository.create_panel(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            panel_channel_id=panel_channel_id,
            open_category_id=open_category_id,
            archive_category_id=archive_category_id,
            title=self.title_input.value.strip()[:256] or "Support tickets",
            description=self.description_input.value.strip()[:4096],
            opening_message=self.opening_input.value[:1000],
            color=0x22B8CF,
            emoji="🎫",
            footer_text="SUPPORT • Choose a category below",
            select_placeholder="Choose a support category",
            thumbnail_url=None,
            image_url=None,
            staff_role_ids=[staff_role_id],
            notify_staff_on_create=True,
            name_template="{number}-{category}-{username}",
            max_open_per_user=1,
            duplicate_scope="category",
            inactivity_reminder_hours=None,
            auto_close_hours=None,
            transcript_retention_days=30,
            status="draft",
            categories=[],
        )
        editor = PanelEditorView(self.setup.cog, record, interaction.user.id)
        editor.message = await interaction.followup.send(
            "Panel created as a draft. Use **Add category** in the editor before publishing.",
            embed=editor.editor_embed(),
            view=editor,
            ephemeral=True,
            wait=True,
        )


class PanelManagerView(SafeView):
    def __init__(self, cog: "TicketAdminCog", records: list[TicketPanelRecord], actor_id: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.records = records
        self.actor_id = actor_id
        self.selected_panel_id: uuid.UUID | None = None
        self.add_item(PanelManagerSelect(self))
        self.add_item(PanelManagerButton(self, "Add panel", "add", style=discord.ButtonStyle.success))
        self.add_item(PanelManagerButton(self, "Edit", "edit"))
        self.add_item(PanelManagerButton(self, "Preview", "preview"))
        self.add_item(PanelManagerButton(self, "Publish", "publish", style=discord.ButtonStyle.success))
        self.add_item(PanelManagerButton(self, "Delete", "delete", style=discord.ButtonStyle.danger))

    def manager_embed(self) -> discord.Embed:
        selected = next(
            (record for record in self.records if record.panel.id == self.selected_panel_id),
            None,
        )
        embed = themed_embed(
            "ticket",
            title="Ticket panel manager",
            description="Select a panel below, then choose Edit, Preview, Publish, or Delete.",
        )
        embed.add_field(
            name=f"Available panels ({len(self.records)})",
            value="\n".join(
                f"• **{record.panel.title}** — {record.panel.status} — {len(record.categories)} categories"
                for record in self.records[:25]
            )[:1024]
            or "No panels exist yet. Click **Add panel** below.",
            inline=False,
        )
        if selected is not None:
            embed.add_field(
                name="Selected",
                value=f"**{selected.panel.title}** ({selected.panel.status})",
                inline=False,
            )
        return embed

    async def handle_action(self, action: str, interaction: discord.Interaction) -> None:
        if action == "add":
            await interaction.response.send_message(
                "Choose where the panel and tickets should be created, then continue.",
                view=PanelCreateSetupView(self.cog),
                ephemeral=True,
            )
            return
        if interaction.guild is None or self.selected_panel_id is None:
            await respond_ephemeral(interaction, "Select a panel first.")
            return
        record = await self.cog.repository.get_panel(
            guild_id=interaction.guild.id, panel_id=self.selected_panel_id
        )
        if record is None:
            await respond_ephemeral(interaction, "That panel no longer exists. Refresh the panel manager.")
            return
        editor = PanelEditorView(self.cog, record, interaction.user.id)
        if action == "edit":
            await interaction.response.send_message(embed=editor.editor_embed(), view=editor, ephemeral=True)
            editor.message = await interaction.original_response()
        elif action == "preview":
            await editor.send_preview(interaction)
        elif action == "publish":
            if not record.categories:
                await respond_ephemeral(
                    interaction,
                    "Add at least one category before publishing this panel.",
                )
                return
            changed = await self.cog.repository.queue_panel(
                guild_id=interaction.guild.id, panel_id=record.panel.id, actor_id=interaction.user.id
            )
            await interaction.response.send_message(
                "Panel synchronization queued." if changed else "That panel is currently busy.",
                ephemeral=True,
            )
        elif action == "delete":
            changed = await self.cog.repository.request_panel_delete(
                guild_id=interaction.guild.id, panel_id=record.panel.id, actor_id=interaction.user.id
            )
            await interaction.response.send_message(
                "Panel deletion queued." if changed else "That panel is currently busy.",
                ephemeral=True,
            )


class TicketAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = cast(TicketBot, bot)
        self.repository = TicketRepository(self.bot.database)

    @ticket_commands.command(name="panels", description="Open the visual ticket panel manager.")
    @app_commands.guild_only()
    @app_commands.check(selected_admin)
    async def panels(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        records = await self.repository.list_panels(guild_id=interaction.guild.id)
        view = PanelManagerView(self, records, interaction.user.id)
        await interaction.response.send_message(embed=view.manager_embed(), view=view, ephemeral=True)
        return
        if not records:
            await interaction.response.send_message("No ticket panels are configured.", ephemeral=True)
            return
        lines = [
            f"`{record.panel.id}` • **{record.panel.title}** • {record.panel.status} • "
            f"<{record.panel.panel_channel_id}> • {len(record.categories)} categories"
            for record in records[:25]
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @ticket_commands.command(name="history", description="Show recent tickets.")
    @app_commands.guild_only()
    @app_commands.check(selected_admin)
    @app_commands.describe(open_only="Only show open and claimed tickets.")
    async def history(self, interaction: discord.Interaction, open_only: bool = True) -> None:
        assert interaction.guild is not None
        records = await self.repository.history(guild_id=interaction.guild.id, open_only=open_only, limit=25)
        if not records:
            await interaction.response.send_message("No tickets found.", ephemeral=True)
            return
        lines = [
            f"`{record.ticket.id}` • <#{record.ticket.channel_id}> • "
            f"{record.category.label} • **{record.ticket.status}** • <@{record.ticket.requester_id}>"
            for record in records
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @ticket_commands.command(name="transcript", description="Download a retained ticket transcript.")
    @app_commands.guild_only()
    @app_commands.check(selected_admin)
    @app_commands.describe(ticket="Ticket to download; choose from the available tickets.")
    @app_commands.autocomplete(ticket=ticket_autocomplete)
    async def transcript(self, interaction: discord.Interaction, ticket: str) -> None:
        assert interaction.guild is not None
        stored = await self.repository.get_transcript(
            guild_id=interaction.guild.id, ticket_id=ticket_id(ticket)
        )
        if stored is None:
            await interaction.response.send_message("No retained transcript was found.", ephemeral=True)
            return
        file = discord.File(io.BytesIO(stored.content.encode("utf-8")), filename=f"ticket-{ticket[:8]}.txt")
        await interaction.response.send_message(file=file, ephemeral=True)

    @ticket_commands.command(name="admin-add", description="Grant ticket administration to a server member.")
    @app_commands.guild_only()
    @app_commands.check(selected_admin)
    @app_commands.describe(member="Member who should be allowed to manage tickets.")
    async def admin_add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild is not None
        if member.bot:
            await interaction.response.send_message("Bots cannot be ticket administrators.", ephemeral=True)
            return
        changed = await self.bot.ticket_admin_repository.add(
            guild_id=interaction.guild.id,
            user_id=member.id,
            actor_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"{member.mention} can now manage tickets." if changed else f"{member.mention} is already a ticket administrator.",
            ephemeral=True,
        )

    @ticket_commands.command(name="admin-remove", description="Revoke ticket administration from a server member.")
    @app_commands.guild_only()
    @app_commands.check(selected_admin)
    @app_commands.describe(member="Member whose ticket administration should be revoked.")
    async def admin_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild is not None
        if member.id in self.bot.settings.ticket_admin_ids():
            await interaction.response.send_message(
                "The bootstrap administrator cannot be removed from configuration.", ephemeral=True
            )
            return
        changed = await self.bot.ticket_admin_repository.remove(
            guild_id=interaction.guild.id, user_id=member.id
        )
        await interaction.response.send_message(
            f"{member.mention} can no longer manage tickets." if changed else f"{member.mention} is not a server ticket administrator.",
            ephemeral=True,
        )

    @ticket_commands.command(name="admins", description="Show the configured ticket administrators.")
    @app_commands.guild_only()
    @app_commands.check(selected_admin)
    async def admins(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        managed_users = await self.bot.ticket_admin_repository.list_admins(
            guild_id=interaction.guild.id
        )
        users = ", ".join(
            f"<@{value}>" for value in sorted(set(self.bot.settings.ticket_admin_ids()) | set(managed_users))
        ) or "none"
        roles = ", ".join(f"<@&{value}>" for value in sorted(self.bot.settings.ticket_admin_role_id_set())) or "none"
        await interaction.response.send_message(
            f"Selected admin users: {users}\nSelected admin roles: {roles}", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    cog = TicketAdminCog(bot)
    await bot.add_cog(cog)
    # This group is declared outside the Cog class so the decorators can keep
    # the readable ``@ticket_commands.command`` form. Bind every callback to
    # the live Cog instance before registering it; otherwise discord.py calls
    # the unbound methods without ``self`` and reports a misleading
    # CommandSignatureMismatch for every ticket-admin command.
    ticket_commands.binding = cog
    for command in ticket_commands.commands:
        command.binding = cog
    bot.tree.add_command(ticket_commands, override=True)
