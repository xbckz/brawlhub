"""Shared failure-safe Discord interaction primitives."""

import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

import discord

logger = logging.getLogger(__name__)

_CUSTOM_EMOJI_PATTERN = re.compile(r"^<a?:[A-Za-z0-9_~]+:\d{15,25}>$")
_EMOJI_SINGLETONS = {
    0x00A9,
    0x00AE,
    0x203C,
    0x2049,
    0x2122,
    0x2139,
    0x3030,
    0x303D,
    0x3297,
    0x3299,
}


def safe_component_emoji(
    value: str | discord.Emoji | discord.PartialEmoji | None,
    guild: discord.Guild | None = None,
) -> str | discord.Emoji | discord.PartialEmoji | None:
    """Return an emoji Discord can accept in a component, or no emoji."""
    if value is None:
        return None
    if isinstance(value, (discord.Emoji, discord.PartialEmoji)):
        if value.id is None or not value.name or not re.fullmatch(r"[A-Za-z0-9_~]{1,32}", value.name):
            return None
        if guild is not None:
            server_emoji = discord.utils.get(guild.emojis, id=value.id)
            if server_emoji is None:
                return None
            return discord.PartialEmoji(
                name=server_emoji.name,
                id=server_emoji.id,
                animated=server_emoji.animated,
            )
        return discord.PartialEmoji(name=value.name, id=value.id, animated=value.animated)
    normalized = value.strip()
    if not normalized:
        return None
    if _CUSTOM_EMOJI_PATTERN.fullmatch(normalized):
        parsed = discord.PartialEmoji.from_str(normalized)
        if parsed.id is None or not parsed.name:
            return None
        if guild is not None:
            server_emoji = discord.utils.get(guild.emojis, id=parsed.id)
            if server_emoji is None:
                return None
            return discord.PartialEmoji(
                name=server_emoji.name,
                id=server_emoji.id,
                animated=server_emoji.animated,
            )
        return discord.PartialEmoji(name=parsed.name, id=parsed.id, animated=parsed.animated)

    codepoints = [ord(character) for character in normalized]

    def is_base(codepoint: int) -> bool:
        return (
            unicodedata.category(chr(codepoint)) != "Cn"
            and (
                0x1F000 <= codepoint <= 0x1FAFF
                or 0x2600 <= codepoint <= 0x27FF
                or codepoint in _EMOJI_SINGLETONS
            )
        )

    if not any(is_base(codepoint) for codepoint in codepoints):
        return None
    allowed_modifiers = {0x200D, 0x20E3, 0xFE0E, 0xFE0F}
    if all(
        is_base(codepoint)
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or codepoint in allowed_modifiers
        for codepoint in codepoints
    ):
        return normalized
    return None


def normalize_component_emoji(
    value: str | None, guild: discord.Guild | None = None
) -> str | None:
    """Normalize Unicode, custom, or ``:server_emoji:`` input for storage."""
    normalized = (value or "").strip()
    if not normalized:
        return None
    if safe_component_emoji(normalized) is not None:
        return normalized
    if guild is not None and normalized.startswith(":") and normalized.endswith(":"):
        name = normalized[1:-1]
        emoji = discord.utils.get(guild.emojis, name=name)
        if emoji is not None:
            return str(emoji)
    return None


INTERACTION_FAILURE_MESSAGE = "That interaction could not be completed. Please try again."
INTERACTION_EXPIRED_MESSAGE = (
    "That control expired or is no longer available. Refresh it and try again."
)
EMBED_THEMES: dict[str, tuple[int, str]] = {
    "analytics": (0x6366F1, "📊"),
    "announcement": (0x4F8EF7, "📣"),
    "brawl": (0xFF8A3D, "⭐"),
    "giveaway": (0xF5B942, "🎉"),
    "help": (0x7967FF, "✦"),
    "leaderboard": (0xF59E0B, "🏆"),
    "moderation": (0xEF5350, "🛡️"),
    "quiz": (0xEC4899, "🧠"),
    "roles": (0x9B72F2, "🎨"),
    "social": (0x38BDF8, "◉"),
    "ticket": (0x22B8CF, "🎫"),
    "transfer": (0x34C759, "↗"),
    "welcome": (0x45D483, "👋"),
}


def themed_embed(
    theme: str,
    *,
    title: str,
    description: str | None = None,
    color: int | discord.Color | None = None,
    url: str | None = None,
    timestamp: datetime | None = None,
    emoji: str | None = None,
    footer_text: str | None = None,
    thumbnail_url: str | None = None,
    image_url: str | None = None,
) -> discord.Embed:
    """Create a compact, recognizable embed without forcing module copy."""
    default_color, icon = EMBED_THEMES[theme]
    title_emoji = icon if emoji is None else emoji
    rendered_title = f"{title_emoji}  {title}" if title_emoji else title
    embed = discord.Embed(
        title=rendered_title[:256],
        description=description,
        color=color if color is not None else default_color,
        url=url,
        timestamp=timestamp,
    )
    if footer_text:
        embed.set_footer(text=footer_text[:2048])
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    if image_url:
        embed.set_image(url=image_url)
    return embed


async def respond_ephemeral(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await interaction.response.send_message(
                message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
    except discord.HTTPException:
        logger.debug("Could not send interaction failure response", exc_info=True)


async def interaction_rate_allowed(interaction: discord.Interaction) -> bool:
    limiter = getattr(interaction.client, "interaction_limiter", None)
    if limiter is None:
        return True
    result = await limiter.check(
        scope="component",
        user_id=interaction.user.id,
        guild_id=interaction.guild_id,
    )
    if result.allowed:
        return True
    await respond_ephemeral(
        interaction,
        f"Too many interactions. Try again in {result.retry_after} seconds.",
    )
    return False


class SafeView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction_rate_allowed(interaction)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        logger.exception(
            "Discord component failed",
            exc_info=error,
            extra={
                "context": {
                    "interaction_id": interaction.id,
                    "custom_id": getattr(item, "custom_id", None),
                }
            },
        )
        await respond_ephemeral(interaction, INTERACTION_FAILURE_MESSAGE)


class SafeLayoutView(discord.ui.LayoutView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction_rate_allowed(interaction)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        logger.exception(
            "Discord layout component failed",
            exc_info=error,
            extra={
                "context": {
                    "interaction_id": interaction.id,
                    "custom_id": getattr(item, "custom_id", None),
                }
            },
        )
        await respond_ephemeral(interaction, INTERACTION_FAILURE_MESSAGE)


class SafeModal(discord.ui.Modal):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction_rate_allowed(interaction)

    async def on_error(  # type: ignore[override]
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.exception(
            "Discord modal failed",
            exc_info=error,
            extra={"context": {"interaction_id": interaction.id, "title": self.title}},
        )
        await respond_ephemeral(interaction, INTERACTION_FAILURE_MESSAGE)
