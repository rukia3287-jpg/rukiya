"""Explicit guild-role delegation for YouTube start, without global operator access."""
from __future__ import annotations

from functools import wraps
import inspect
import logging
from typing import get_type_hints

from discord import app_commands

from services.access import is_operator
from services.discord_ui import defer_response, send_response

logger = logging.getLogger(__name__)


class YouTubeRoleStoreError(RuntimeError):
    pass


class YouTubeStartRoles:
    """Use MemoryService's serialized workers and existing SQLite connection layer.

    Authorization is never reconstructed from memory fallback or chat facts.
    The additive table is initialized lazily, so existing installations need no
    manual migration and the unrelated Brain update can be merged independently.
    """

    def __init__(self, memory):
        self.memory = memory

    def _query(self, guild_id: int, role_id: int | None = None, enabled: bool = True) -> set[int]:
        if type(guild_id) is not int or guild_id <= 0:
            raise ValueError('A Discord server is required.')
        if role_id is not None and (type(role_id) is not int or role_id <= 0):
            raise ValueError('A valid Discord role is required.')
        conn = self.memory._get_connection()
        if conn is None:
            raise YouTubeRoleStoreError('Role permissions could not be read or saved.')
        try:
            with conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS youtube_start_roles (
                    guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL,
                    PRIMARY KEY(guild_id, role_id))''')
                if role_id is not None:
                    if enabled:
                        conn.execute('INSERT OR IGNORE INTO youtube_start_roles VALUES (?,?)', (guild_id, role_id))
                    else:
                        conn.execute('DELETE FROM youtube_start_roles WHERE guild_id=? AND role_id=?', (guild_id, role_id))
                rows = conn.execute('SELECT role_id FROM youtube_start_roles WHERE guild_id=?', (guild_id,)).fetchall()
                return {int(row[0]) for row in rows}
        except Exception as exc:
            raise YouTubeRoleStoreError('Role permissions could not be read or saved.') from exc
        finally:
            conn.close()

    async def roles(self, guild_id: int) -> set[int]:
        return await self.memory.run_async(self._query, guild_id)

    async def set_role(self, guild_id: int, role_id: int, enabled: bool) -> set[int]:
        return await self.memory.run_async(self._query, guild_id, role_id, enabled)


async def can_start_youtube(bot, interaction, *, refresh_member: bool = False) -> bool:
    if await is_operator(bot, interaction.user):
        return True
    guild = getattr(interaction, 'guild', None)
    guild_id = getattr(interaction, 'guild_id', None)
    if type(guild_id) is not int or getattr(guild, 'id', None) != guild_id:
        return False
    member = interaction.user
    if getattr(getattr(member, 'guild', None), 'id', None) != guild_id:
        return False
    try:
        if refresh_member:
            member = await guild.fetch_member(interaction.user.id)
            if getattr(getattr(member, 'guild', None), 'id', None) != guild_id:
                return False
        allowed = await YouTubeStartRoles(bot.memory_service).roles(guild_id)
        return any(getattr(role, 'id', None) in allowed
                   and getattr(getattr(role, 'guild', None), 'id', None) == guild_id
                   for role in getattr(member, 'roles', ()))
    except Exception as exc:
        logger.warning('YouTube start-role authorization unavailable: %s', type(exc).__name__)
        return False


async def _authorize(bot, interaction):
    # Acknowledge before owner lookup/SQLite; both can exceed Discord's window.
    await defer_response(interaction, ephemeral=True, thinking=True)
    return await can_start_youtube(bot, interaction)


def youtube_start_only():
    async def predicate(interaction):
        if not await _authorize(interaction.client, interaction):
            raise app_commands.CheckFailure('Bot owner/operator or an approved YouTube start role is required.')
        return True
    return app_commands.check(predicate)


def youtube_start_callback(function):
    """Guard direct callback use as well as application-command dispatch."""
    @wraps(function)
    async def wrapped(self, interaction, *args, **kwargs):
        if not await _authorize(self.bot, interaction):
            return await send_response(interaction, 'You need the bot owner/operator or a role allowed with /youtube_roles add in this server.', ephemeral=True)
        return await function(self, interaction, *args, **kwargs)
    signature = inspect.signature(function)
    hints = get_type_hints(inspect.unwrap(function))
    wrapped.__signature__ = signature.replace(parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in signature.parameters.values()], return_annotation=hints.get('return', signature.return_annotation))
    return wrapped
