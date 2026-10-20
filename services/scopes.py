"""Scope identifiers are derived from adapter metadata, never from prompt text."""
from __future__ import annotations
from services.models import ChatMessage


def persistent_scope(message: ChatMessage):
    if message.platform.lower() != 'discord':
        return None  # YouTube viewer memory stays on its existing stable platform ID.
    if message.guild_id:
        return f'discord:guild:{message.guild_id}'
    if message.channel_id:
        return f'discord:dm:{message.user_id or message.username}'
    return None  # Legacy Python callers retain their existing identity namespace.


def scoped_identity(canonical_id, scope):
    return f'scope:{scope}|{canonical_id}' if scope else canonical_id


def conversation_scope(message: ChatMessage, active_stream=None):
    if message.platform.lower() == 'youtube':
        return active_stream or f'youtube:video:{message.video_id or message.channel_id or "unscoped"}'
    if message.platform.lower() == 'discord':
        if message.guild_id:
            return f'discord:guild:{message.guild_id}:channel:{message.channel_id or "user-" + str(message.user_id)}'
        return f'discord:dm:{message.channel_id or message.user_id or message.username}'
    return f'{message.platform}:channel:{message.channel_id or message.user_id or "unscoped"}'
