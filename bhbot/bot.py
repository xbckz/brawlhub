"""Discord bot construction and lifecycle for the ticket-only deployment."""

import asyncio
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from bhbot.config import Settings, get_settings
from bhbot.db.engine import Database
from bhbot.db.guild_repository import GuildRepository
from bhbot.db.ticket_admin_repository import TicketAdminRepository
from bhbot.logging import configure_logging
from bhbot.ui import INTERACTION_EXPIRED_MESSAGE, respond_ephemeral

logger = logging.getLogger(__name__)


class TicketCommandTree(app_commands.CommandTree):
    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        logger.exception(
            "Application command failed",
            exc_info=error,
            extra={
                "context": {
                    "interaction_id": interaction.id,
                    "command": interaction.command.qualified_name if interaction.command else None,
                }
            },
        )
        if isinstance(original, app_commands.CheckFailure) and str(original):
            message = str(original)
        elif isinstance(original, app_commands.NoPrivateMessage):
            message = "That command can only be used inside a server."
        elif isinstance(original, app_commands.MissingPermissions):
            message = "You do not have the permissions required for that action."
        elif isinstance(original, app_commands.BotMissingPermissions):
            message = "I am missing a Discord permission required for that action."
        else:
            message = "Something went wrong while handling that action."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class TicketBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.members = settings.discord_members_intent_enabled
        super().__init__(
            command_prefix=commands.when_mentioned,
            help_command=None,
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            tree_cls=TicketCommandTree,
        )
        self.settings = settings
        self.database = database
        self.guild_repository = GuildRepository(database)
        self.ticket_admin_repository = TicketAdminRepository(database)
        self._interaction_watchdogs: set[asyncio.Task[None]] = set()
        self._closing = False

    async def is_selected_admin(self, member: discord.Member) -> bool:
        if member.id in self.settings.ticket_admin_ids():
            return True
        if self.settings.ticket_admin_role_id_set().intersection(
            {role.id for role in member.roles}
        ):
            return True
        return await self.ticket_admin_repository.is_admin(
            guild_id=member.guild.id, user_id=member.id
        )

    async def _expire_unhandled_interaction(self, interaction: discord.Interaction) -> None:
        await asyncio.sleep(2.25)
        if not interaction.response.is_done():
            await respond_ephemeral(interaction, INTERACTION_EXPIRED_MESSAGE)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type not in {
            discord.InteractionType.component,
            discord.InteractionType.modal_submit,
        }:
            return
        task = asyncio.create_task(self._expire_unhandled_interaction(interaction))
        self._interaction_watchdogs.add(task)
        task.add_done_callback(self._interaction_watchdogs.discard)

    async def setup_hook(self) -> None:
        if not await self.database.ping():
            raise RuntimeError("Could not connect to the ticket database")
        await self.database.initialize()
        await self.load_extension("bhbot.cogs.admin")
        await self.load_extension("bhbot.cogs.tickets")
        if self.settings.discord_development_guild_id:
            guild = discord.Object(id=self.settings.discord_development_guild_id)
            # Development commands must be the only visible version in the
            # test guild. A previous global registration can otherwise remain
            # selectable by Discord with an older option signature.
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            # Remove stale global registrations while developing. Production
            # deployments leave DISCORD_DEVELOPMENT_GUILD_ID unset and sync
            # the current command tree globally below.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            scope = f"development guild {guild.id}"
        else:
            synced = await self.tree.sync()
            scope = "global"
        logger.info("Synchronized %d ticket commands (%s)", len(synced), scope)

    async def on_ready(self) -> None:
        for guild in self.guilds:
            await self.guild_repository.mark_installed(guild.id, guild.name)
        if self.user is not None:
            logger.info("Ticket bot ready as %s (%s)", self.user, self.user.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.guild_repository.mark_installed(guild.id, guild.name)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.guild_repository.mark_removed(guild.id)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        pending = list(self._interaction_watchdogs)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._interaction_watchdogs.clear()
        await super().close()


async def _run(settings: Settings) -> None:
    database = Database(settings.require_database_url(), settings.require_database_auth_token())
    bot = TicketBot(settings, database)
    # On Windows, aiodns can fail to contact the configured DNS servers even
    # when normal system DNS works. Use aiohttp's threaded resolver instead.
    bot.http.connector = aiohttp.TCPConnector(
        limit=0, resolver=aiohttp.ThreadedResolver()
    )
    try:
        await bot.start(settings.require_bot_token(), reconnect=True)
    finally:
        await bot.close()
        await database.close()


def run_bot() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, production=settings.environment == "production")
    settings.validate_bot_runtime()
    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        logger.info("Discord ticket bot stopped")
