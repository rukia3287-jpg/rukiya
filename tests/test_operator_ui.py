"""Exercise real command callbacks and component actions with realistic acknowledgments."""
import asyncio
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from cogs.admin_commands import AdminCommands
from cogs.chat_bot import RukiyaCog
from cogs.shayari import Shayari
from cogs.utility_commands import Utility
from cogs.welcome import Welcome
from cogs.youtube_commands import YouTubeCommands
from services.access import is_operator
from services.config import Config
from services.discord_ui import ConfirmView, StatusView, handle_error, recent_errors
from services.logging_utils import SecretRedactingFormatter
from services.memory_service import MemoryService
from services.runtime_status import build_health_payload


def interaction(bot, uid=7, *, component=False, guild=10):
    event = SimpleNamespace(
        client=bot, user=SimpleNamespace(id=uid, display_name='Viewer', guild_permissions=SimpleNamespace(administrator=True)),
        guild_id=guild, channel_id=20, guild=SimpleNamespace(id=guild) if guild else None,
    )
    response = SimpleNamespace(type=None)
    response.is_done = lambda: response.type is not None
    async def defer(**kwargs):
        if response.is_done():
            raise RuntimeError('Interaction already acknowledged')
        response.type = discord.InteractionResponseType.deferred_message_update if component and not kwargs.get('thinking') else discord.InteractionResponseType.deferred_channel_message
    async def send(*args, **kwargs):
        if response.is_done():
            raise RuntimeError('Interaction already acknowledged')
        response.type = discord.InteractionResponseType.channel_message
    async def edit(**kwargs):
        response.type = discord.InteractionResponseType.message_update
    response.defer = AsyncMock(side_effect=defer)
    response.send_message = AsyncMock(side_effect=send)
    response.edit_message = AsyncMock(side_effect=edit)
    event.response = response
    event.followup = SimpleNamespace(send=AsyncMock())
    event.edit_original_response = AsyncMock()
    event.original_response = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    return event


class OperatorUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bot = MagicMock()
        self.bot.config = Config(db_path=os.path.join(self.directory.name, 'memory.db'), operator_ids={'7'})
        self.bot.is_owner = AsyncMock(return_value=False)
        self.bot.memory_service = MemoryService(self.bot.config)
        self.bot.command_sync_error = None
        self.bot.failed_cogs = []
        self.bot.chat_monitor = None
        self.bot.latency = 0.05
        self.bot.ai_service = None
        self.bot.youtube_service = None
        self.bot.orchestrator = None
        self.bot.cogs = {}
        self.bot.intents.members = False
        self.admin = AdminCommands(self.bot)

    async def test_guild_administrator_cannot_control_global_accounts(self):
        event = interaction(self.bot, uid=99)
        self.assertFalse(await is_operator(self.bot, event.user))
        commands = [
            (YouTubeCommands(self.bot), 'start_monitoring', {'video_id': 'example'}),
            (YouTubeCommands(self.bot), 'stop_monitoring', {}),
            (YouTubeCommands(self.bot), 'yt_status', {}),
            (RukiyaCog(self.bot), 'slash_say', {'text': 'should not post'}),
            (RukiyaCog(self.bot), 'slash_auto_reply', {'action': 'disable'}),
            (Shayari(self.bot), 'shayari_send', {}),
            (Welcome(self.bot), 'welcome_send', {}),
            (self.admin, 'memory_lookup', {'user_id': '123'}),
            (self.admin, 'memory_reset', {'user_id': '123'}),
            (self.admin, 'status', {}),
            (self.admin, 'test_ai', {'message': 'hello'}),
            (self.admin, 'test_trigger', {'message': 'hello'}),
            (self.admin, 'rate_limit_status', {}),
        ]
        for cog, name, kwargs in commands:
            with self.subTest(command=name):
                event = interaction(self.bot, uid=99)
                command = getattr(cog, name)
                with self.assertRaises(discord.app_commands.CheckFailure):
                    await command.checks[0](event)
                await command.callback(cog, event, **kwargs)
                event.response.send_message.assert_awaited_once()
                event.response.defer.assert_not_awaited()
                event.edit_original_response.assert_not_awaited()

    async def test_explicit_operator_and_application_owner_are_allowed(self):
        event = interaction(self.bot)
        event.user.guild_permissions.administrator = False
        self.assertTrue(await is_operator(self.bot, event.user))
        self.bot.is_owner.return_value = True
        self.assertTrue(await is_operator(self.bot, interaction(self.bot, uid=55).user))
        self.bot.is_owner.side_effect = RuntimeError('owner lookup failed')
        self.assertFalse(await is_operator(self.bot, interaction(self.bot, uid=55).user))

    async def test_allowlisted_operator_can_post_from_dm(self):
        cog = RukiyaCog(self.bot)
        self.bot.chat_monitor = SimpleNamespace(is_running=True, send_chat_message=AsyncMock(return_value=True))
        await cog.slash_say.callback(cog, interaction(self.bot, guild=None), text='Hello chat')
        self.bot.chat_monitor.send_chat_message.assert_awaited_once_with('Hello chat')

    async def test_memory_reset_requires_confirm_and_uses_current_guild(self):
        current = 'scope:discord:guild:10|discord:123'
        other = 'scope:discord:guild:11|discord:123'
        mem = self.bot.memory_service
        mem.set_user_memory(current, 'favorite_game', 'Minecraft')
        mem.set_user_memory(other, 'favorite_game', 'Valorant')
        event = interaction(self.bot)
        await self.admin.memory_reset.callback(self.admin, event, user_id='<@123>')
        self.assertTrue(mem.get_all_user_memories(current))
        view = event.edit_original_response.call_args.kwargs['view']
        self.assertIsInstance(view, ConfirmView)
        confirmed = interaction(self.bot, component=True)
        await view.confirm.callback(confirmed)
        self.assertFalse(mem.get_all_user_memories(current))
        self.assertTrue(mem.get_all_user_memories(other))
        confirmed.edit_original_response.assert_awaited_once()

    async def test_cancelled_confirmation_cannot_be_replayed(self):
        action = AsyncMock()
        view = ConfirmView(7, action)
        await view.cancel.callback(interaction(self.bot, component=True))
        await view.confirm.callback(interaction(self.bot, component=True))
        action.assert_not_awaited()

    async def test_confirmation_is_single_use_even_for_concurrent_clicks(self):
        action = AsyncMock()
        view = ConfirmView(7, action)
        await asyncio.gather(view.confirm.callback(interaction(self.bot, component=True)), view.confirm.callback(interaction(self.bot, component=True)))
        action.assert_awaited_once()
        self.assertTrue(all(item.disabled for item in view.children))

    async def test_confirmation_rechecks_revoked_access(self):
        action = AsyncMock()
        view = ConfirmView(7, action)
        self.bot.config.operator_ids.clear()
        await view.confirm.callback(interaction(self.bot, component=True))
        action.assert_not_awaited()

    async def test_other_user_cannot_press_confirm_or_refresh(self):
        action = AsyncMock()
        confirm = ConfirmView(7, action)
        status = StatusView(7, action)
        await confirm.confirm.callback(interaction(self.bot, uid=99, component=True))
        await status.refresh.callback(interaction(self.bot, uid=99, component=True))
        action.assert_not_awaited()

    async def test_timeout_disables_controls_and_expired_confirm_does_nothing(self):
        action = AsyncMock()
        view = ConfirmView(7, action)
        view.message = SimpleNamespace(edit=AsyncMock())
        view.stop()
        await view.on_timeout()
        await view.confirm.callback(interaction(self.bot, component=True))
        self.assertTrue(all(item.disabled for item in view.children))
        action.assert_not_awaited()

    async def test_status_refresh_edits_original_without_double_ack(self):
        event = interaction(self.bot)
        await self.admin.status.callback(self.admin, event)
        view = event.edit_original_response.call_args.kwargs['view']
        refresh = interaction(self.bot, component=True)
        await view.refresh.callback(refresh)
        refresh.response.defer.assert_awaited_once()
        refresh.edit_original_response.assert_awaited_once()
        refresh.followup.send.assert_not_awaited()
        self.assertTrue(view.is_finished())

    async def test_help_selection_is_owned_and_updates_existing_panel(self):
        event = interaction(self.bot)
        cog = Utility(self.bot)
        await cog.help_command.callback(cog, event)
        view = event.edit_original_response.call_args.kwargs['view']
        view.menu._values = ['1']
        selected = interaction(self.bot, component=True)
        await view.menu.callback(selected)
        embed = selected.response.edit_message.call_args.kwargs['embed']
        self.assertEqual(len(embed.fields), 1)
        self.assertIn('YouTube', embed.fields[0].name)
        self.assertFalse(await view.interaction_check(interaction(self.bot, uid=99)))

    async def test_ping_before_gateway_ready_is_useful(self):
        self.bot.latency = float('nan')
        event = interaction(self.bot)
        cog = Utility(self.bot)
        await cog.ping.callback(cog, event)
        self.assertIn('Connecting', event.edit_original_response.call_args.kwargs['embed'].description)

    async def test_central_error_handler_finishes_deferred_response_privately(self):
        event = interaction(self.bot)
        await event.response.defer(ephemeral=True)
        before = len(recent_errors)
        with self.assertLogs('services.discord_ui', level='ERROR'):
            await handle_error(event, RuntimeError('sensitive backend detail'))
        event.edit_original_response.assert_awaited_once()
        text = event.followup.send.call_args.args[0]
        self.assertIn('Reference:', text)
        self.assertNotIn('sensitive backend detail', text)
        self.assertTrue(event.followup.send.call_args.kwargs['ephemeral'])
        self.assertEqual(len(recent_errors), min(before + 1, 30))

    async def test_member_join_does_not_crosspost_without_explicit_guild_opt_in(self):
        cm = SimpleNamespace(is_running=True, send_chat_message=AsyncMock(return_value=True))
        self.bot.chat_monitor = cm
        self.bot.ai_service = None
        member = SimpleNamespace(guild=SimpleNamespace(id=10, name='Server', text_channels=[]), display_name='Newbie', send=AsyncMock())
        cog = Welcome(self.bot)
        await cog.on_member_join(member)
        cm.send_chat_message.assert_not_awaited()
        self.bot.config.welcome_youtube_guild_ids.add('10')
        await cog.on_member_join(member)
        cm.send_chat_message.assert_awaited_once()

    def test_memory_admin_has_explicit_legacy_and_dm_targets(self):
        self.assertEqual(self.admin.memory_target(interaction(self.bot), '<@123>', 'legacy'), 'discord:123')
        self.assertEqual(self.admin.memory_target(interaction(self.bot, guild=None), '123', 'current'), 'scope:discord:dm:123|discord:123')
        explicit = 'scope:discord:guild:99|discord:123'
        self.assertEqual(self.admin.memory_target(interaction(self.bot), explicit, 'current'), explicit)

    def test_sync_failure_is_reported_as_degraded_health(self):
        self.bot.command_sync_error = 'HTTPException'
        payload = build_health_payload(self.bot)
        self.assertEqual(payload['status'], 'degraded')
        self.assertEqual(payload['command_sync_error'], 'HTTPException')


class LoggingTests(unittest.TestCase):
    def test_formatter_masks_individual_oauth_fields_inside_json_configuration(self):
        formatter = SecretRedactingFormatter('%(message)s', secrets=['{"installed":{"client_secret":"synthetic-oauth-secret"},"refresh_token":"synthetic-refresh-token"}'])
        record = logging.LogRecord('test', logging.ERROR, __file__, 1, 'Failed synthetic-oauth-secret synthetic-refresh-token', (), None)
        output = formatter.format(record)
        self.assertNotIn('synthetic-oauth-secret', output)
        self.assertNotIn('synthetic-refresh-token', output)

    def test_formatter_masks_credentials_in_message_and_traceback(self):
        secret = 'synthetic-only-test-credential'
        formatter = SecretRedactingFormatter('%(message)s', secrets=[secret])
        try:
            raise RuntimeError(f'Provider URL contains {secret}')
        except RuntimeError as error:
            record = logging.LogRecord('test', logging.ERROR, __file__, 1, 'Request %s failed', (secret,), (type(error), error, error.__traceback__))
        output = formatter.format(record)
        self.assertNotIn(secret, output)
        self.assertIn('[REDACTED]', output)
