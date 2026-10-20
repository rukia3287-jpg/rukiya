"""Consistent, safe command responses and short-lived owned controls."""
from __future__ import annotations

from collections import deque
import logging
import uuid
import discord
from services.access import is_operator

log = logging.getLogger(__name__)
recent_errors = deque(maxlen=30)


async def defer_response(interaction, *, ephemeral=True, thinking=False):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)


async def send_response(interaction, content=None, **kwargs):
    if not interaction.response.is_done():
        return await interaction.response.send_message(content, **kwargs)
    if interaction.response.type in (discord.InteractionResponseType.deferred_channel_message, discord.InteractionResponseType.deferred_message_update):
        kwargs.pop('ephemeral', None)
        return await interaction.edit_original_response(content=content, **kwargs)
    return await interaction.followup.send(content, **kwargs)


def record_error(error):
    reference = uuid.uuid4().hex[:10]
    recent_errors.append({'reference': reference, 'type': type(error).__name__})
    log.error('Command/UI error reference=%s type=%s', reference, type(error).__name__, exc_info=(type(error), error, error.__traceback__))
    return reference


async def handle_error(interaction, error):
    original = getattr(error, 'original', error)
    if isinstance(original, discord.app_commands.CheckFailure):
        message = 'This action requires the bot owner/configured operator, or the required server permissions.'
    elif isinstance(original, discord.Forbidden):
        message = "Discord denied the action. Check the bot's permissions and role position."
    elif isinstance(original, discord.NotFound):
        message = 'That resource or interaction no longer exists. Open a fresh command result.'
    elif isinstance(original, discord.app_commands.CommandOnCooldown):
        message = f'Try again in {original.retry_after:.1f} seconds.'
    else:
        message = 'The action could not finish. An operator can check the error reference in the bot logs.'
    reference = record_error(original)
    try:
        if interaction.response.is_done():
            if interaction.response.type in (discord.InteractionResponseType.deferred_channel_message, discord.InteractionResponseType.deferred_message_update, discord.InteractionResponseType.message_update):
                await interaction.edit_original_response(content='The action could not finish. Private details are below.', embed=None, view=None)
            await interaction.followup.send(f'⚠️ {message}\nReference: `{reference}`', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(f'⚠️ {message}\nReference: `{reference}`', ephemeral=True)
    except discord.HTTPException:
        log.warning('Could not deliver error reference=%s (expired/deleted interaction)', reference)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await handle_error(interaction, error)


class OwnedView(SafeView):
    def __init__(self, owner_id, *, operator=False, timeout=120):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.operator = operator
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or self.operator and not await is_operator(interaction.client, interaction.user):
            await send_response(interaction, 'Open your own command result with the required access.', ephemeral=True)
            return False
        return True

    async def bind(self, interaction):
        try:
            self.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ConfirmView(OwnedView):
    def __init__(self, owner_id, action):
        super().__init__(owner_id, operator=True, timeout=60)
        self.action = action
        self.started = False

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        # Recheck here as well, since old queued component events can outlive a view.
        if self.started or self.is_finished():
            return await send_response(interaction, 'This confirmation has expired or has already been used.', ephemeral=True)
        if not await self.interaction_check(interaction):
            return
        self.started = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content='Processing the confirmed action…', embed=None, view=self)
        await self.action(interaction)

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        if self.started:
            return await send_response(interaction, 'The confirmed action is already running.', ephemeral=True)
        self.started = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content='Cancelled. No changes were made.', embed=None, view=self)


class StatusView(OwnedView):
    def __init__(self, owner_id, callback, *, operator=True):
        super().__init__(owner_id, operator=operator)
        self.callback = callback
        self.busy = False

    @discord.ui.button(label='Refresh status', emoji='🔄', style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        if self.busy:
            return await send_response(interaction, 'A refresh is already running.', ephemeral=True)
        self.busy = True
        try:
            await defer_response(interaction, thinking=False)
            await self.callback(interaction)
            self.stop()  # The replacement panel owns the next timeout.
        finally:
            self.busy = False

    @discord.ui.button(label='Close', style=discord.ButtonStyle.secondary)
    async def close(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
