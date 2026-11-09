"""Offline Brain integration, ownership, recovery and optional-runtime regressions."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.brain_commands import BrainCommands
from services.ai_engine.engine import AIEngine
from services.ai_engine.cache import InFlightDeduplicator
from services.ai_engine.models import AIEngineRequest, AIProviderResult
from services.ai_engine.provider_registry import ProviderRegistry
from services.ai_engine.providers.local import LocalProvider
from services.ai_engine.providers.openrouter import OpenRouterProvider
from services.ai_engine.context_compiler import ContextCompiler
from services.ai_engine.planner import Planner
from services.brain import BrainBusy, BrainRuntime, LocalSkills, calculate, response_style
from services.config import Config
from services.identity_service import IdentityService
from services.memory_service import MemoryService
from services.models import ChatMessage, ResponseDecision
from services.orchestrator import RukiyaOrchestrator
from services.decision_service import DecisionService
from services.scopes import persistent_scope


class Provider:
    def __init__(self, name, configured=True):
        self.name, self.configured = name, configured
        self.generate = AsyncMock(return_value=AIProviderResult('Use a smaller task queue.', name))
        self.search_grounded = AsyncMock(return_value=AIProviderResult('Verified public result.', name, used_search=True,
                                        citations=[{'url':'https://example.com','title':'Public source'}]))
    def is_configured(self):
        return self.configured


class BrainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'OPENROUTER_API_KEY':'','GEMINI_API_KEY':'','LOCAL_AI_ENABLED':'false'})
        self.env.start()
        self.config = Config(db_path=str(Path(self.tmp.name)/'brain.db'), ai_cooldown=0)
        self.memory = MemoryService(self.config)
        self.identity = IdentityService(self.memory)
        self.registry = ProviderRegistry(self.config)
        self.remote = Provider('openrouter')
        self.gemini = Provider('gemini', configured=False)
        self.registry.register(self.remote)
        self.registry.register(self.gemini)
        self.engine = AIEngine(self.config, registry=self.registry)
        self.decision = DecisionService(self.config)
        self.decision.decide = MagicMock(return_value=ResponseDecision(True, intent='question', memory_needed=True))
        self.orch = RukiyaOrchestrator(config=self.config, memory_service=self.memory,
                                      identity_service=self.identity, decision_service=self.decision, ai_engine=self.engine)

    async def asyncTearDown(self):
        await self.orch.aclose()
        self.env.stop()
        self.tmp.cleanup()

    def message(self, text='explain this', uid='123', guild='1', channel='2', platform='discord', video=None):
        return ChatMessage(platform, str(time.monotonic_ns()), uid, 'Viewer', 'Viewer', text,
                           guild_id=guild, channel_id=channel, video_id=video)

    async def process(self, text, **kwargs):
        return await self.orch.process_message(self.message(text, **kwargs), bypass_trigger=True, bypass_cooldown=True)

    async def test_arithmetic_is_offline_cached_and_not_replaced_as_repetition(self):
        self.remote.configured = False
        for _ in range(2):
            response = await self.process('rukiya calculate (25+5)*4')
            self.assertEqual(response.text, '120')
            self.assertFalse(response.is_fallback)
        self.remote.generate.assert_not_awaited()
        self.assertEqual(self.engine.local_skills.hits, 1)
        self.assertEqual(self.engine.budget_manager.openrouter_count, 0)

    async def test_negative_and_decimal_arithmetic_preserve_the_numeric_result(self):
        for expression, expected in [('2-7','-5'), ('1/4','0.25'), ('0-0','0')]:
            with self.subTest(expression=expression):
                self.assertEqual((await self.process('calculate '+expression)).text, expected)

    async def test_history_and_extraction_are_opt_in(self):
        await self.process('my name is Krishna')
        canonical = self.identity.resolve(self.message()).canonical_id
        self.assertFalse(self.memory.get_all_user_memories(canonical))
        with self.memory._open_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM recent_messages').fetchone()[0], 0)
        await self.process('rukiya privacy history=on automatic=on external=on')
        await self.process('my name is Krishna')
        self.assertTrue(self.memory.get_all_user_memories(canonical))
        with self.memory._open_connection() as conn:
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM recent_messages').fetchone()[0], 0)

    async def test_youtube_self_owned_memory_controls_and_optout(self):
        await self.process('rukiya remember favorite_game=Minecraft', platform='youtube', guild=None, uid='UCfirst')
        response = await self.process('memory: favorite_game', platform='youtube', guild=None, uid='UCfirst')
        self.assertEqual(response.text, 'Minecraft')
        response = await self.process('memory: favorite_game', platform='youtube', guild=None, uid='UCother')
        self.assertNotIn('Minecraft', response.text)
        await self.process('rukiya privacy history=off automatic=off external=off', platform='youtube', guild=None, uid='UCfirst')
        before = self.remote.generate.await_count
        await self.process('explain asyncio', platform='youtube', guild=None, uid='UCfirst')
        self.assertEqual(self.remote.generate.await_count, before)
        await self.process('rukiya clear my memory', platform='youtube', guild=None, uid='UCfirst')
        self.assertFalse(self.memory.get_all_user_memories('youtube:UCfirst'))
        self.assertFalse(self.memory.get_brain_preferences('youtube:UCfirst')['external_allowed'])

    async def test_approved_facts_survive_correction_and_are_guild_isolated(self):
        await self.process('rukiya remember favorite_game=Minecraft')
        await self.process('rukiya remember favorite_game=Valorant')
        self.assertEqual((await self.process('memory: favorite_game')).text, 'Valorant')
        self.assertNotIn('Valorant', (await self.process('memory: favorite_game', guild='99')).text)
        self.assertNotIn('Valorant', (await self.process('memory: favorite_game', uid='99')).text)
        self.assertNotIn('Valorant', (await self.process('memory: favorite_game', guild=None)).text)
        await self.process('rukiya forget favorite_game')
        self.assertNotIn('Valorant', (await self.process('memory: favorite_game')).text)

    async def test_existing_unapproved_facts_do_not_enter_generation(self):
        user = self.identity.resolve(self.message())
        self.memory.set_user_memory(user.canonical_id, 'favorite_game', 'PrivateLegacyGame')
        await self.process('what is my favorite game?')
        self.assertNotIn('PrivateLegacyGame', json.dumps(self.remote.generate.call_args.kwargs['messages']))
        self.memory.set_user_memory(user.canonical_id, 'favorite_game', 'ApprovedGame', source='user_approved')
        await self.process('what is my favorite game?')
        self.assertIn('ApprovedGame', json.dumps(self.remote.generate.call_args.kwargs['messages']))

    async def test_old_database_history_not_loaded_without_optin(self):
        user = self.identity.resolve(self.message())
        self.memory.record_interaction(user, 'legacy private transcript', session_id='discord:guild:1:channel:2')
        self.memory._in_memory_recent_messages.clear()
        await self.process('why that one?')
        self.assertNotIn('legacy private transcript', json.dumps(self.remote.generate.call_args.kwargs['messages']))

    async def test_knowledge_fts_is_scoped_and_requires_no_model(self):
        scope = persistent_scope(self.message())
        self.assertTrue(self.memory.add_brain_knowledge(scope, 'Server guide', 'Use the help command for server rules.'))
        response = await self.process('knowledge: server rules')
        self.assertIn('Server guide', response.text)
        self.remote.generate.assert_not_awaited()
        self.assertNotIn('Server guide', (await self.process('knowledge: server rules', guild='7')).text)
        self.memory.brain_fts = False
        self.assertTrue(self.memory.search_brain_knowledge(scope, 'server rules'))

    async def test_same_conversation_rejects_concurrent_work_other_user_remains_responsive(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return AIProviderResult('A bounded task queue helps.', 'openrouter')
        self.remote.generate.side_effect = slow
        active = asyncio.create_task(self.process('explain asyncio'))
        await entered.wait()
        try:
            self.assertIsNone(await self.process('another question'))
            self.assertEqual((await self.process('2+2', uid='124')).text, '4')
        finally:
            release.set()
            await active
        self.assertFalse(self.orch.brain.active)

    async def test_total_timeout_and_cancellation_release_both_layers(self):
        self.config.brain_timeout = .03
        self.remote.generate.side_effect = lambda *args, **kwargs: None
        async def slow(*args, **kwargs):
            await asyncio.sleep(2)
        self.remote.generate.side_effect = slow
        result = await self.process('explain asyncio')
        self.assertIsNone(result)
        self.assertFalse(self.orch.brain.active)
        self.assertFalse(self.engine._active_tasks)
        self.assertEqual(self.engine.budget_manager.user_counts.get('123', 0), 0)

    async def test_user_cancel_waits_for_active_request(self):
        entered = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.remote.generate.side_effect = slow
        active = asyncio.create_task(self.process('explain asyncio'))
        await entered.wait()
        await self.orch.brain.cancel('discord','123','1')
        self.assertTrue(active.cancelled())
        self.assertFalse(self.orch.brain.active)

    async def test_local_model_first_and_external_fallback(self):
        local = Provider('local')
        self.registry.register(local)
        result = await self.engine.process(AIEngineRequest('hello there', 'viewer', user_id='123'))
        self.assertEqual(result.final_provider, 'local')
        self.assertEqual(self.engine.budget_manager.openrouter_count, 0)
        self.remote.generate.assert_not_awaited()
        local.generate.return_value = AIProviderResult('', 'local', error='offline')
        result = await self.engine.process(AIEngineRequest('hello there', 'viewer', user_id='123'))
        self.assertEqual(result.executed_route, 'local_external_fallback')
        self.remote.generate.assert_awaited_once()

    async def test_local_and_primary_failure_still_reaches_existing_backup(self):
        local = Provider('local')
        local.generate.return_value = AIProviderResult('', 'local', error='offline')
        self.registry.register(local)
        self.remote.generate.return_value = AIProviderResult('', 'openrouter', error='503')
        self.gemini.configured = True
        result = await self.engine.process(AIEngineRequest('hello there','viewer'))
        self.assertEqual(result.final_provider,'gemini')
        self.remote.generate.assert_awaited_once()
        self.gemini.generate.assert_awaited_once()

    async def test_local_failure_never_violates_external_privacy(self):
        local = Provider('local')
        local.generate.return_value = AIProviderResult('', 'local', error='offline')
        self.registry.register(local)
        result = await self.engine.process(AIEngineRequest('hello there', 'viewer', allow_external=False))
        self.assertTrue(result.fallback_used)
        self.remote.generate.assert_not_awaited()
        self.gemini.generate.assert_not_awaited()

    async def test_fresh_information_never_uses_unverified_local_model(self):
        local = Provider('local')
        self.registry.register(local)
        result = await self.engine.process(AIEngineRequest('latest phone price', 'viewer', allow_external=False))
        local.generate.assert_not_awaited()
        self.remote.generate.assert_not_awaited()
        self.assertFalse(result.verified_current_information)

    async def test_search_cache_dedup_and_deletion_are_owner_and_conversation_scoped(self):
        self.gemini.configured = True
        for user, scope in [('one','channel-a'),('one','channel-a'),('two','channel-a'),('one','channel-b')]:
            await self.engine.process(AIEngineRequest('latest phone price', 'Viewer', user_id=user, canonical_id=user, conversation_scope=scope))
        self.assertEqual(self.gemini.search_grounded.await_count, 3)
        await self.engine.clear_user_cache('one')
        await self.engine.process(AIEngineRequest('latest phone price', 'Viewer', user_id='one', canonical_id='one', conversation_scope='channel-a'))
        self.assertEqual(self.gemini.search_grounded.await_count, 4)

    async def test_case_sensitive_platform_ids_never_share_search_cache(self):
        self.gemini.configured = True
        for canonical in ['youtube:UCAb', 'youtube:UCab']:
            await self.engine.process(AIEngineRequest('latest phone price','same name',canonical_id=canonical))
        self.assertEqual(self.gemini.search_grounded.await_count,2)

    async def test_search_cache_preserves_meaningful_query_punctuation(self):
        self.gemini.configured = True
        for text in ['search C++ compiler?', 'search C compiler?']:
            await self.engine.process(AIEngineRequest(text, 'viewer', canonical_id='one'))
        self.assertEqual(self.gemini.search_grounded.await_count, 2)

    async def test_local_repairs_do_not_dispatch_external_generations(self):
        local = Provider('local')
        local.generate.return_value = AIProviderResult('"Use small tasks."', 'local')
        self.registry.register(local)
        result = await self.engine.process(AIEngineRequest('explain asyncio', 'viewer', allow_external=False))
        self.assertIn('Use small tasks', result.text)
        self.remote.generate.assert_not_awaited()

    async def test_clear_during_outage_replays_without_resurrecting_facts(self):
        user = self.identity.resolve(self.message())
        self.memory.set_user_memory(user.canonical_id,'favorite_game','Minecraft',source='user_approved')
        self.memory.set_brain_preferences(user.canonical_id, history_enabled=True, auto_memory=False, external_allowed=False)
        self.memory.record_interaction(user, 'private transcript')
        self.memory.fallback_mode = True
        self.assertFalse(self.memory.clear_brain_user(user.canonical_id))
        self.memory._fallback_since -= self.memory.DB_RETRY_SECONDS + 1
        self.assertTrue(self.memory.persistence_available())
        reopened = MemoryService(self.config)
        self.assertFalse(reopened.get_all_user_memories(user.canonical_id))
        self.assertFalse(reopened.get_recent_messages())
        self.assertFalse(reopened.get_brain_preferences(user.canonical_id)['external_allowed'])

    async def test_individual_delete_during_outage_does_not_remove_other_facts(self):
        user = self.identity.resolve(self.message())
        for key in ['first','second']:
            self.memory.set_user_memory(user.canonical_id,key,key,source='user_approved')
        self.memory.fallback_mode = True
        self.assertFalse(self.memory.forget_brain_fact(user.canonical_id,'first'))
        self.memory._fallback_since -= self.memory.DB_RETRY_SECONDS + 1
        self.memory.persistence_available()
        self.assertEqual([fact.key for fact in MemoryService(self.config).get_all_user_memories(user.canonical_id)], ['second'])

    async def test_preferences_during_outage_recover_and_unknown_privacy_fails_closed(self):
        user = self.identity.resolve(self.message())
        self.memory.fallback_mode = True
        self.assertFalse(self.memory.get_brain_preferences('unknown')['external_allowed'])
        self.assertFalse(self.memory.set_brain_preferences(user.canonical_id, history_enabled=False, auto_memory=False, external_allowed=False))
        self.memory._fallback_since -= self.memory.DB_RETRY_SECONDS + 1
        self.memory.persistence_available()
        reopened = MemoryService(self.config)
        self.assertFalse(reopened.get_brain_preferences(user.canonical_id)['external_allowed'])

    async def test_temporal_bounds_and_sensitive_override(self):
        user = self.identity.resolve(self.message())
        self.memory.record_interaction(user,'old chat')
        with patch('services.memory_service.time.time', return_value=time.time()+100000):
            self.assertFalse(self.memory.get_recent_messages())
        req = AIEngineRequest('I feel awful', 'viewer', intent='sensitive', style='sarcastic')
        self.assertIn('calm', response_style(req))

    async def test_private_commands_cannot_choose_another_users_identity(self):
        cog = BrainCommands(SimpleNamespace(orchestrator=self.orch))
        interaction = SimpleNamespace(id=1, user=SimpleNamespace(id=123,display_name='Alice'),guild_id=1,channel_id=2,
                                      response=SimpleNamespace(is_done=lambda:False, defer=AsyncMock(),send_message=AsyncMock()))
        respond = AsyncMock()
        with patch.dict(cog.remember.callback.__globals__, {'send_response': respond}):
            await cog.remember.callback(cog, interaction, 'game', 'Minecraft')
        canonical = 'scope:discord:guild:1|discord:123'
        self.assertEqual(self.memory.get_all_user_memories(canonical)[0].value,'Minecraft')
        self.assertFalse(self.memory.get_all_user_memories('scope:discord:guild:1|discord:124'))
        self.assertTrue(respond.call_args.kwargs['ephemeral'])

    async def test_knowledge_command_requires_operator_even_on_direct_callback(self):
        bot = SimpleNamespace(orchestrator=self.orch,config=self.config,is_owner=AsyncMock(return_value=False))
        cog = BrainCommands(bot)
        interaction = SimpleNamespace(user=SimpleNamespace(id=999))
        with patch('services.discord_ui.send_response', AsyncMock()) as respond:
            await cog.knowledge.callback(cog, interaction, 'private', 'data')
        self.assertIn('Only the bot owner', respond.call_args.args[1])
        self.assertFalse(self.memory.search_brain_knowledge('discord:guild:1','data'))


class LocalAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_loopback_chunked_protocol_and_lazy_close(self):
        from aiohttp import web
        received=[]
        async def completion(request):
            received.append(await request.json())
            response=web.StreamResponse(headers={'Content-Type':'application/json'})
            await response.prepare(request)
            body=json.dumps({'choices':[{'message':{'content':'Try a smaller queue.'}}]}).encode()
            await response.write(body[:12])
            await asyncio.sleep(.01)
            await response.write(body[12:])
            await response.write_eof()
            return response
        app=web.Application();app.router.add_post('/v1/chat/completions',completion)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        cfg=Config(local_ai_enabled=True,local_ai_model='fake-runtime',local_ai_url=f'http://127.0.0.1:{port}/v1')
        provider=LocalProvider(cfg)
        try:
            self.assertIsNone(provider.session)
            result=await provider.generate([{'role':'user','content':'hello'}])
            self.assertEqual(result.text,'Try a smaller queue.')
            self.assertEqual(received[0]['model'],'fake-runtime')
        finally:
            await provider.aclose();await runner.cleanup()
        self.assertIsNone(provider.session)

    async def test_runtime_redirects_tool_calls_and_oversized_responses_fail_closed(self):
        from aiohttp import web
        async def completion(request):
            mode=(await request.json())['messages'][0]['content']
            if mode=='redirect':
                return web.Response(status=302,headers={'Location':'https://example.com'})
            if mode=='tool':
                return web.json_response({'choices':[{'message':{'content':'hello','tool_calls':[{'name':'send'}]}}]})
            if mode=='large':
                return web.Response(body=b'x'*140000)
            return web.json_response({'choices':[{'message':17}]})
        app=web.Application();app.router.add_post('/v1/chat/completions',completion)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        provider=LocalProvider(Config(local_ai_enabled=True,local_ai_model='test',local_ai_url=f'http://127.0.0.1:{port}/v1'))
        try:
            for mode in ['redirect','tool','large','malformed']:
                with self.subTest(mode=mode):
                    result=await provider.generate([{'role':'user','content':mode}])
                    self.assertTrue(result.error)
                    self.assertFalse(result.text)
        finally:
            await provider.aclose();await runner.cleanup()

    async def test_non_loopback_and_credentials_are_not_treated_as_local(self):
        for url in ['https://example.com/v1','http://127.0.0.1.evil.test/v1','http://user:pass@localhost/v1']:
            with self.subTest(url=url):
                provider=LocalProvider(Config(local_ai_enabled=True,local_ai_model='test',local_ai_url=url))
                self.assertFalse(provider.is_configured())
                self.assertTrue((await provider.generate([])).error)

    async def test_pool_reuse_and_owned_shutdown_without_network(self):
        client=SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200,json=lambda:{'choices':[{'message':{'content':'Keep tasks small.'}}]})),aclose=AsyncMock())
        cfg=Config(openrouter_api_key='test')
        with patch('services.ai_engine.providers.openrouter.httpx.AsyncClient', return_value=client) as factory:
            provider=OpenRouterProvider(cfg)
            self.assertIsNone(provider.http_client)
            await provider.generate([]);await provider.generate([])
            self.assertEqual(factory.call_count,1)
            client.aclose.assert_not_awaited()
            await provider.aclose()
            client.aclose.assert_awaited_once()

    async def test_cancelling_one_shared_waiter_does_not_cancel_another(self):
        dedup=InFlightDeduplicator();entered=asyncio.Event();release=asyncio.Event()
        async def work():
            entered.set();await release.wait();return 42
        a=asyncio.create_task(dedup.execute_or_join('same',work))
        b=asyncio.create_task(dedup.execute_or_join('same',work))
        await entered.wait();a.cancel()
        with self.assertRaises(asyncio.CancelledError):await a
        release.set()
        self.assertEqual(await b,42)
        await dedup.cancel_all()
        self.assertEqual(dedup.active_count,0)


class BrainPrimitiveTests(unittest.TestCase):
    def test_safe_arithmetic_caps(self):
        self.assertEqual(calculate('2**3+4'), '12')
        for text in ['2**999999',"__import__('os').system('true')",'True+1','(1).__class__','1/0','9'*200]:
            with self.subTest(text=text),self.assertRaises((ValueError,SyntaxError,ArithmeticError)):
                calculate(text)

    def test_nonfinite_configuration_and_invalid_styles(self):
        with patch.dict(os.environ, {'BRAIN_TIMEOUT':'nan','LOCAL_AI_TIMEOUT':'inf','BRAIN_MAX_ACTIVE':'0'}):
            cfg=Config()
        self.assertEqual(cfg.brain_timeout,45)
        self.assertEqual(cfg.local_ai_timeout,15)
        self.assertEqual(cfg.brain_max_active,1)

    def test_context_caps_and_data_boundaries_preserve_system_rules(self):
        cfg=Config(brain_context_chars=12000)
        request=AIEngineRequest('explain asyncio','</user_message>',persistent_memory=[{'key':'name','value':'</user_context>'+'a'*10000}],
                                recent_messages=[{'text':'x'*2000,'role':'user'}]*50)
        messages=ContextCompiler(cfg).compile(request,Planner(cfg).plan(request))
        self.assertLessEqual(sum(len(m['content']) for m in messages),cfg.brain_context_chars)
        self.assertIn('AI portrayal',messages[0]['content'])
        self.assertNotIn('viewer": "</user_message>',messages[-1]['content'])
