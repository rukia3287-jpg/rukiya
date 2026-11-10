"""Explicit role delegation; all platform/network operations are mocked."""
import asyncio
import os
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands
from cogs.youtube_commands import YouTubeCommands
from cogs.chat_bot import RukiyaCog
from services.config import Config
from services.memory_service import MemoryService
from services.youtube_access import YouTubeStartRoles, YouTubeRoleStoreError, can_start_youtube


class YouTubeRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(db_path=os.path.join(self.tmp.name,'memory.db'), operator_ids={'7'})
        self.memory = MemoryService(self.config)
        self.bot = NS(config=self.config, memory_service=self.memory, is_owner=AsyncMock(return_value=False),
                      youtube_service=NS(authenticate=Mock(return_value=True), get_live_chat_id=Mock(return_value='live-chat')),
                      chat_monitor=NS(is_running=False, start_monitoring=Mock(return_value=True), stop_monitoring=Mock()))
        self.cog = YouTubeCommands(self.bot)
        self.store = self.cog.start_roles

    def event(self, uid=8, guild_id=10, roles=(50,)):
        guild = NS(id=guild_id) if guild_id else None
        member = NS(id=uid, display_name='Viewer', guild=guild, guild_permissions=NS(administrator=False))
        member.roles = [NS(id=role, guild=guild) for role in roles]
        if guild:
            guild.fetch_member = AsyncMock(return_value=member)
        event = NS(client=self.bot, user=member, guild=guild, guild_id=guild_id, channel_id=20)
        response = NS(type=None)
        response.is_done = lambda: response.type is not None
        async def defer(**kwargs):
            response.type = discord.InteractionResponseType.deferred_channel_message
        async def send(*args, **kwargs):
            response.type = discord.InteractionResponseType.channel_message
        response.defer = AsyncMock(side_effect=defer)
        response.send_message = AsyncMock(side_effect=send)
        event.response = response
        event.edit_original_response = AsyncMock()
        event.followup = NS(send=AsyncMock())
        return event

    async def start(self, event=None, video_id='abc123_-XYZ'):
        event = event or self.event()
        await self.cog.start_monitoring.callback(self.cog,event,video_id=video_id)
        return event

    async def test_default_denial_including_guild_admin(self):
        event = self.event()
        event.user.guild_permissions.administrator = True
        self.assertFalse(await can_start_youtube(self.bot,event))
        with self.assertRaises(discord.app_commands.CheckFailure):
            await self.cog.start_monitoring.checks[0](event)
        await self.start(event)
        self.bot.youtube_service.authenticate.assert_not_called()
        self.bot.chat_monitor.start_monitoring.assert_not_called()

    async def test_multiple_roles_persist_and_remove_independently(self):
        await self.store.set_role(10,50,True)
        await self.store.set_role(10,51,True)
        await self.store.set_role(10,50,True)
        restored = YouTubeStartRoles(MemoryService(self.config))
        self.assertEqual(await restored.roles(10),{50,51})
        self.assertTrue(await can_start_youtube(self.bot,self.event(roles=(999,51))))
        await restored.set_role(10,51,False)
        self.assertFalse(await can_start_youtube(self.bot,self.event(roles=(51,))))
        self.assertTrue(await can_start_youtube(self.bot,self.event(roles=(50,))))

    async def test_role_scope_never_crosses_guild_or_dm(self):
        await self.store.set_role(10,50,True)
        self.assertFalse(await can_start_youtube(self.bot,self.event(guild_id=11)))
        self.assertFalse(await can_start_youtube(self.bot,self.event(guild_id=None)))
        event = self.event()
        event.user.roles = [NS(id=50,guild=NS(id=11))]
        self.assertFalse(await can_start_youtube(self.bot,event))

    async def test_configured_operator_and_owner_keep_access_without_store(self):
        with patch.object(self.memory,'_get_connection',return_value=None):
            self.assertTrue(await can_start_youtube(self.bot,self.event(uid=7,guild_id=None)))
            self.bot.is_owner.return_value = True
            self.assertTrue(await can_start_youtube(self.bot,self.event(uid=99,guild_id=None)))

    async def test_database_failure_never_grants_role_access(self):
        await self.store.set_role(10,50,True)
        with patch.object(self.memory,'_get_connection',return_value=None):
            self.assertFalse(await can_start_youtube(self.bot,self.event()))
            with self.assertRaises(YouTubeRoleStoreError):
                await self.store.set_role(10,50,False)
        self.assertEqual(await self.store.roles(10),{50})

    async def test_only_operators_can_manage_roles_even_on_direct_callback(self):
        role = NS(id=50,guild=NS(id=10))
        for command,kwargs in [(self.cog.add_start_role,{'role':role}),
                               (self.cog.remove_start_role,{'role':role}), (self.cog.list_start_roles,{})]:
            event = self.event()
            event.user.guild_permissions.administrator = True
            with self.assertRaises(discord.app_commands.CheckFailure):
                await command.checks[0](event)
            await command.callback(self.cog,event,**kwargs)
        self.assertEqual(await self.store.roles(10),set())

    async def test_operator_role_controls_acknowledge_and_are_guild_bound(self):
        event = self.event(uid=7)
        role = NS(id=50,guild=NS(id=10))
        await self.cog.add_start_role.callback(self.cog,event,role=role)
        event.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
        self.assertEqual(await self.store.roles(10),{50})
        event = self.event(uid=7)
        await self.cog.add_start_role.callback(self.cog,event,role=NS(id=60,guild=NS(id=11)))
        self.assertEqual(await self.store.roles(10),{50})
        event = self.event(uid=7)
        await self.cog.remove_start_role.callback(self.cog,event,role=role)
        self.assertEqual(await self.store.roles(10),set())

    async def test_role_list_is_private_and_empty_state_is_clear(self):
        event = self.event(uid=7)
        await self.cog.list_start_roles.callback(self.cog,event)
        self.assertIn('No roles configured',event.edit_original_response.call_args.args[0] if event.edit_original_response.call_args.args else event.edit_original_response.call_args.kwargs['content'])
        await self.store.set_role(10,50,True)
        event = self.event(uid=7)
        await self.cog.list_start_roles.callback(self.cog,event)
        kwargs = event.edit_original_response.call_args.kwargs
        self.assertIn('50',kwargs['content'])
        self.assertEqual(kwargs['allowed_mentions'].to_dict(),discord.AllowedMentions.none().to_dict())

    async def test_allowed_role_can_start_by_video_id_without_admin(self):
        await self.store.set_role(10,50,True)
        event = self.event()
        self.assertTrue(await self.cog.start_monitoring.checks[0](event))
        await self.start(event,video_id=' abc123_-XYZ ')
        event.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
        self.bot.youtube_service.get_live_chat_id.assert_called_once_with('abc123_-XYZ')
        event.guild.fetch_member.assert_awaited_once_with(8)
        self.bot.chat_monitor.start_monitoring.assert_called_once_with('live-chat','abc123_-XYZ')
        self.assertIsNotNone(event.edit_original_response.call_args.kwargs['embed'])

    async def test_invalid_id_rejected_before_any_youtube_call(self):
        for value in ('bad','https://youtube.com/watch?v=abc123_-XYZ','x'*100,'abc!23_-XYZ'):
            await self.start(self.event(uid=7),video_id=value)
        self.bot.youtube_service.authenticate.assert_not_called()

    async def test_authentication_and_live_chat_failures_do_not_start(self):
        self.bot.youtube_service.authenticate.return_value = False
        await self.start(self.event(uid=7))
        self.bot.youtube_service.get_live_chat_id.assert_not_called()
        self.bot.youtube_service.authenticate.return_value = True
        self.bot.youtube_service.get_live_chat_id.return_value = None
        await self.start(self.event(uid=7))
        self.bot.chat_monitor.start_monitoring.assert_not_called()

    async def test_existing_stream_is_never_replaced(self):
        self.bot.chat_monitor.is_running = True
        await self.start(self.event(uid=7))
        self.bot.youtube_service.authenticate.assert_not_called()
        self.bot.chat_monitor.start_monitoring.assert_not_called()

    async def test_grant_revoked_during_youtube_lookup_prevents_start(self):
        await self.store.set_role(10,50,True)
        def lookup(video):
            self.store._query(10,50,False)
            return 'live-chat'
        self.bot.youtube_service.get_live_chat_id.side_effect = lookup
        await self.start()
        self.bot.chat_monitor.start_monitoring.assert_not_called()

    async def test_membership_revoked_during_lookup_prevents_start(self):
        await self.store.set_role(10,50,True)
        event = self.event()
        event.guild.fetch_member.return_value = NS(id=8,guild=event.guild,roles=[])
        await self.start(event)
        self.bot.chat_monitor.start_monitoring.assert_not_called()

    async def test_member_refresh_failure_denies_start(self):
        await self.store.set_role(10,50,True)
        event = self.event()
        event.guild.fetch_member.side_effect = RuntimeError('synthetic')
        await self.start(event)
        self.bot.chat_monitor.start_monitoring.assert_not_called()

    async def test_delegation_does_not_grant_other_operator_actions(self):
        await self.store.set_role(10,50,True)
        for command in (self.cog.stop_monitoring,self.cog.yt_status):
            with self.assertRaises(discord.app_commands.CheckFailure):
                await command.checks[0](self.event())
        chat = RukiyaCog(self.bot)
        event = self.event()
        await chat.slash_say.callback(chat,event,text='unapproved')
        self.bot.chat_monitor.start_monitoring.assert_not_called()

    async def test_parallel_start_runs_only_one_auth_and_monitor(self):
        started,finish = threading.Event(),threading.Event()
        def auth():
            started.set(); finish.wait(2); return True
        self.bot.youtube_service.authenticate.side_effect = auth
        first = asyncio.create_task(self.start(self.event(uid=7)))
        await asyncio.to_thread(started.wait,1)
        await self.start(self.event(uid=7))
        finish.set()
        await first
        self.bot.youtube_service.authenticate.assert_called_once()
        self.bot.chat_monitor.start_monitoring.assert_called_once()

    async def test_cancelled_start_drains_worker_and_never_starts_monitor(self):
        started,finish = threading.Event(),threading.Event()
        def auth():
            started.set(); finish.wait(2); return True
        self.bot.youtube_service.authenticate.side_effect = auth
        task = asyncio.create_task(self.start(self.event(uid=7)))
        await asyncio.to_thread(started.wait,1)
        task.cancel(); await asyncio.sleep(.01)
        self.assertTrue(self.cog._control_lock.locked())
        await self.start(self.event(uid=7))
        task.cancel(); finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.cog._control_lock.locked())
        self.bot.youtube_service.authenticate.assert_called_once()
        self.bot.chat_monitor.start_monitoring.assert_not_called()

    async def test_real_async_monitor_entry_is_used(self):
        from services.chat_monitor import ChatMonitor
        monitor = Mock(spec=ChatMonitor)
        monitor.is_running = False
        monitor.start_async = AsyncMock(return_value=True)
        self.bot.chat_monitor = monitor
        await self.start(self.event(uid=7))
        monitor.start_async.assert_awaited_once_with('live-chat','abc123_-XYZ')
        monitor.start_monitoring.assert_not_called()

    async def test_command_registration_includes_role_group_and_no_default_admin_gate(self):
        bot = commands.Bot(command_prefix='!',intents=discord.Intents.none())
        bot.memory_service = self.memory
        try:
            await bot.add_cog(YouTubeCommands(bot))
            group = bot.tree.get_command('youtube_roles')
            self.assertEqual({c.name for c in group.commands},{'add','remove','list'})
            self.assertIsNone(bot.tree.get_command('start').default_permissions)
        finally:
            await bot.close()
