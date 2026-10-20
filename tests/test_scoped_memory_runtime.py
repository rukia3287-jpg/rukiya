"""Database-backed isolation and event-loop responsiveness regressions."""
import asyncio
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from services.chat_monitor import ChatMonitor
from services.config import Config
from services.decision_service import DecisionService
from services.identity_service import IdentityService
from services.memory_service import MemoryService
from services.models import ChatMessage, ResponseDecision
from services.orchestrator import RukiyaOrchestrator
from services.scopes import conversation_scope


def message(*, guild='10', channel='20', platform='discord', text='hello', uid='123'):
    return ChatMessage(platform=platform, message_id=text, user_id=uid, username='Viewer', display_name='Viewer', text=text, guild_id=guild, channel_id=channel)


class ScopedMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(db_path=os.path.join(self.directory.name, 'memory.db'), ai_cooldown=0)
        self.mem = MemoryService(self.config)
        self.identity = IdentityService(self.mem)
        decision = DecisionService(self.config)
        decision.decide = MagicMock(return_value=ResponseDecision(should_respond=True, intent='question', reason='test', memory_needed=True))
        self.requests = []
        async def generate(request):
            self.requests.append(request)
            return SimpleNamespace(text='Good to see you here.', confidence=0.9, latency_ms=0.1, provider='test', fallback_used=False)
        self.engine = SimpleNamespace(process=AsyncMock(side_effect=generate))
        self.orch = RukiyaOrchestrator(config=self.config, identity_service=self.identity, memory_service=self.mem, decision_service=decision, ai_engine=self.engine)

    async def process(self, msg):
        return await self.orch.process_message(msg, bypass_trigger=True, bypass_cooldown=True)

    def test_guild_persistent_facts_do_not_leak_to_other_guild_or_dm(self):
        guild_a = self.identity.resolve(message())
        guild_b = self.identity.resolve(message(guild='11'))
        dm = self.identity.resolve(message(guild=None))
        self.mem.set_user_memory(guild_a.canonical_id, 'favorite_game', 'Minecraft')
        self.assertNotEqual(guild_a.canonical_id, guild_b.canonical_id)
        self.assertFalse(self.mem.get_all_user_memories(guild_b.canonical_id))
        self.assertFalse(self.mem.get_all_user_memories(dm.canonical_id))
        same_guild_other_channel = self.identity.resolve(message(channel='21'))
        self.assertEqual(guild_a.canonical_id, same_guild_other_channel.canonical_id)

    def test_dm_facts_are_private_to_each_user(self):
        one = self.identity.resolve(message(guild=None))
        two = self.identity.resolve(message(guild=None, uid='124', channel='21'))
        self.mem.set_user_memory(one.canonical_id, 'name', 'Alice')
        self.assertNotEqual(one.canonical_id, two.canonical_id)
        self.assertFalse(self.mem.get_all_user_memories(two.canonical_id))

    def test_legacy_rows_are_retained_but_not_loaded_into_new_scope(self):
        legacy = self.identity.resolve(message(guild=None, channel=None))
        self.mem.set_user_memory(legacy.canonical_id, 'name', 'Old secret name')
        self.mem.record_interaction(legacy, 'legacy private history', session_id='default_session')
        scoped = self.identity.resolve(message())
        self.assertEqual(legacy.canonical_id, 'discord:123')
        self.assertFalse(self.mem.get_all_user_memories(scoped.canonical_id))
        self.assertTrue(self.mem.get_all_user_memories(legacy.canonical_id))
        reopened = MemoryService(self.config)
        self.assertTrue(reopened.get_all_user_memories(legacy.canonical_id))

    async def test_same_guild_channel_history_is_separate_and_relevant_history_survives(self):
        await self.process(message(text='channel A private discussion'))
        await self.process(message(channel='21', text='channel B request'))
        self.assertFalse(self.requests[-1].recent_messages)
        await self.process(message(text='follow-up in channel A'))
        history = ' '.join(row['text'] for row in self.requests[-1].recent_messages)
        self.assertIn('channel A private discussion', history)
        self.assertNotIn('channel B request', history)

    async def test_discord_cannot_read_the_active_youtube_session_history(self):
        self.mem.start_stream_session('video1', session_id='stream_live')
        await self.process(message(platform='youtube', guild=None, channel='chat', text='YouTube-only discussion'))
        await self.process(message(text='Discord-only discussion'))
        self.assertFalse(self.requests[-1].recent_messages)
        youtube_history = self.mem.get_recent_messages(session_id='stream_live')
        self.assertIn('YouTube-only discussion', str(youtube_history))
        self.assertNotIn('Discord-only discussion', str(youtube_history))
        youtube_user = self.identity.resolve(message(platform='youtube', guild=None, uid='UC_viewer'))
        self.assertEqual(youtube_user.canonical_id, 'youtube:UC_viewer')

    async def test_simultaneous_requests_keep_context_and_written_history_separate(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        count = 0
        async def generate(request):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
            return SimpleNamespace(text=f'Reply for {request.text}.', confidence=0.9, latency_ms=0.1, provider='test', fallback_used=False)
        self.engine.process.side_effect = generate
        a = asyncio.create_task(self.process(message(text='alpha')))
        b = asyncio.create_task(self.process(message(channel='21', text='beta')))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            release.set()
        await asyncio.gather(a, b)
        first = str(self.mem.get_recent_messages(session_id='discord:guild:10:channel:20'))
        second = str(self.mem.get_recent_messages(session_id='discord:guild:10:channel:21'))
        self.assertIn('alpha', first)
        self.assertNotIn('beta', first)
        self.assertIn('beta', second)
        self.assertNotIn('alpha', second)
        self.assertEqual(self.mem._session_id(), 'default_session')

    async def test_slow_database_worker_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        def slow_database():
            entered.set()
            release.wait(timeout=3)
            return 42
        task = asyncio.create_task(self.mem.run_async(slow_database))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertEqual(await task, 42)

    async def test_repeated_cancellation_waits_for_worker_before_next_write(self):
        entered, release = threading.Event(), threading.Event()
        order = []
        def write():
            entered.set()
            release.wait(timeout=3)
            order.append('first commit')
        first = asyncio.create_task(self.mem.run_async(write))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(self.mem.run_async(lambda: order.append('second commit')))
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertEqual(order, ['first commit', 'second commit'])

    async def test_worker_failure_does_not_deadlock_subsequent_requests(self):
        def fail():
            raise ValueError('synthetic database failure')
        with self.assertRaises(ValueError):
            await self.mem.run_async(fail)
        self.assertEqual(await self.mem.run_async(lambda: 'recovered'), 'recovered')

    async def test_async_monitor_lifecycle_serializes_start_and_ends_stream(self):
        monitor = ChatMonitor(SimpleNamespace(), SimpleNamespace(), orchestrator=self.orch)
        release = asyncio.Event()
        async def loop():
            try:
                await release.wait()
            finally:
                monitor._stopping = False
        monitor._monitor_loop = loop
        started = await asyncio.gather(monitor.start_async('chat', 'video'), monitor.start_async('chat', 'video'))
        self.assertEqual(started, [True, False])
        session = monitor.stream_session_id
        task = monitor._monitor_task
        await asyncio.sleep(0)
        await monitor.stop_async()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(monitor.is_running)
        self.assertIsNone(self.mem.active_session_id)
        self.assertIsNotNone(session)

    def test_youtube_video_history_is_separate_without_active_monitor(self):
        a = message(platform='youtube', guild=None, channel='chat1')
        b = message(platform='youtube', guild=None, channel='chat2')
        self.assertNotEqual(conversation_scope(a), conversation_scope(b))
