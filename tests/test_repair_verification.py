"""A repair cannot upgrade failed live verification into a verified answer."""
import unittest
from unittest.mock import AsyncMock

from services.ai_engine.context_compiler import ContextCompiler
from services.ai_engine.critic import Critic
from services.ai_engine.models import AIEngineRequest, AIProviderResult, CriticVerdict, Plan
from services.ai_engine.repair import RepairEngine
from services.config import Config


class RepairVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_price_is_rechecked_after_local_cleanup_and_every_provider_repair(self):
        config = Config(ai_max_repair_attempts=2)
        critic = Critic(config)
        request = AIEngineRequest(text='Steam price today?', author='Viewer')
        plan = Plan(intent='question', search_required=True, freshness_required=True)
        candidate = 'It costs $999 today.'
        initial = critic.evaluate(candidate, request, plan, verified_current_information=False, search_failed=True)
        generate = AsyncMock(return_value=AIProviderResult(text=candidate, provider='test'))
        _, count, report = await RepairEngine(config).execute_repair_loop(
            candidate, initial, request, plan, [], ContextCompiler(config), critic, generate,
            verified_current_information=False, search_failed=True,
        )
        self.assertEqual(report.verdict, CriticVerdict.REPAIR)
        self.assertEqual(count, 2)
        self.assertEqual(generate.await_count, 2)
        for call in generate.call_args_list:
            self.assertIn('verified_current_information=false', str(call.args[0]))

    async def test_repair_accepts_honest_lack_of_live_verification(self):
        config = Config()
        critic = Critic(config)
        request = AIEngineRequest(text='Steam price today?', author='Viewer')
        plan = Plan(intent='question', search_required=True, freshness_required=True)
        candidate = 'It costs $999 today.'
        initial = critic.evaluate(candidate, request, plan, verified_current_information=False, search_failed=True)
        generate = AsyncMock(return_value=AIProviderResult(text="I can't verify the current price right now.", provider='test'))
        _, count, report = await RepairEngine(config).execute_repair_loop(
            candidate, initial, request, plan, [], ContextCompiler(config), critic, generate,
            verified_current_information=False, search_failed=True,
        )
        self.assertEqual(report.verdict, CriticVerdict.PASS)
        self.assertEqual(count, 1)

    def test_current_non_price_claims_are_flagged_without_search_verification(self):
        critic = Critic(Config())
        request = AIEngineRequest(text='Who won today?', author='Viewer')
        plan = Plan(intent='question', search_required=True, freshness_required=True)
        for text in ['Team A won today.', 'The current CEO is Alex.', 'The latest version is 99.']:
            with self.subTest(text=text):
                report = critic.evaluate(text, request, plan, verified_current_information=False, search_failed=True)
                self.assertEqual(report.verdict, CriticVerdict.REPAIR)

    def test_verified_current_response_still_passes(self):
        critic = Critic(Config())
        report = critic.evaluate('Team A won today.', AIEngineRequest(text='Who won?', author='Viewer'), Plan(intent='question', freshness_required=True), verified_current_information=True)
        self.assertEqual(report.verdict, CriticVerdict.PASS)
