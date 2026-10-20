"""Global account actions belong to the bot owner and explicit operators."""
from __future__ import annotations

import inspect
from discord import app_commands
from discord.ext import commands


def _user_id(user) -> str:
    value = getattr(user, 'id', None)
    return str(value) if isinstance(value, int) or isinstance(value, str) and value.isdigit() else ''


async def is_operator(bot, user) -> bool:
    uid = _user_id(user)
    configured = getattr(getattr(bot, 'config', None), 'operator_ids', ())
    if uid and isinstance(configured, (set, frozenset, tuple, list)) and uid in {str(x) for x in configured}:
        return True
    check = getattr(bot, 'is_owner', None)
    if callable(check):
        try:
            result = check(user)
            if inspect.isawaitable(result):
                return await result is True
        except Exception:
            return False
    return False


def operator_only():
    async def predicate(interaction):
        if not await is_operator(interaction.client, interaction.user):
            raise app_commands.CheckFailure('Bot owner or configured operator access is required.')
        return True
    return app_commands.check(predicate)


def prefix_operator():
    async def predicate(ctx):
        return await is_operator(ctx.bot, ctx.author)
    return commands.check(predicate)


def operator_callback(function):
    """Also guard direct UI/callback invocation, not just command dispatch."""
    from functools import wraps
    from typing import get_type_hints
    @wraps(function)
    async def wrapped(self, interaction, *args, **kwargs):
        if not await is_operator(self.bot, interaction.user):
            from services.discord_ui import send_response
            return await send_response(interaction, 'Only the bot owner or configured operators can use this action.', ephemeral=True)
        return await function(self, interaction, *args, **kwargs)
    signature = inspect.signature(function)
    hints = get_type_hints(inspect.unwrap(function))
    wrapped.__signature__ = signature.replace(parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in signature.parameters.values()], return_annotation=hints.get('return', signature.return_annotation))
    return wrapped
