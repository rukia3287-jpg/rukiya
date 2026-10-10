"""tests/test_v2_safety_contract.py
Safety contract shared by every user-visible AI route:
- serious/distressed messages are detected deterministically and never get flippant output;
- stage directions are rejected whatever brackets they use;
- empty, malformed, or unsafe provider output never reaches chat;
- persona prompts stay structurally clean.
"""
import re
import unittest
from unittest.mock import AsyncMock

from services.ai_engine.context_compiler import RUKIYA_SYSTEM_PROMPT as ENGINE_SYSTEM_PROMPT
from services.ai_engine.context_compiler import ContextCompiler
from services.ai_engine.engine import STATIC_FALLBACKS, AIEngine
from services.ai_engine.models import AIEngineRequest, AIProviderResult, Plan
from services.ai_engine.provider_registry import ProviderRegistry
from services.ai_engine.providers.base import AIProvider
from services.ai_service import RUKIYA_SYSTEM_PROMPT as LEGACY_SYSTEM_PROMPT
from services.config import Config
from services.decision_service import DecisionService
from services.safety_service import FALLBACK_INTENTS, SafetyService, validate_rukiya_response

CRISIS_MESSAGES = [
    "i want to kill myself",
    "rukiya I keep thinking about suicide",
    "honestly I just want to die",
    "I've been self harming again",
    "hi rukiya, I don't want to live anymore",
]
SENSITIVE_MESSAGES = [
    "I'm so depressed lately",
    "my dog passed away this morning",
    "I'm having a panic attack",
    "I feel completely hopeless",
    "hey rukiya I'm having a really bad day",
    "why do I feel so worthless?",
]
ORDINARY_MESSAGES = [
    "hello everyone",
    "this boss is killing me lol",
    "I died to that boss three times",
    "what game is this?",
    "you're so cute rukiya",
    "I'm dying of laughter",
]
FLIPPANT_MARKERS = ("reckless", "tch", "don't get used", "keep it friendly", "civil", "focus on the stream")


class StubProvider(AIProvider):
    def __init__(self, name: str):
        self.name = name
        self.generate_mock = AsyncMock()
        self.search_mock = AsyncMock()

    def is_configured(self) -> bool:
        return True

    async def generate(self, messages, **kwargs) -> AIProviderResult:
        return await self.generate_mock(messages, **kwargs)

    async def search_grounded(self, query: str, **kwargs) -> AIProviderResult:
        return await self.search_mock(query, **kwargs)

    async def health_check(self) -> bool:
        return True


class SeriousMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config()
        self.decision = DecisionService(self.config)

    def test_crisis_and_sensitive_messages_are_detected_before_other_intents(self):
        for text in CRISIS_MESSAGES:
            with self.subTest(text=text):
                self.assertEqual(self.decision.detect_intent(text), "crisis")
        for text in SENSITIVE_MESSAGES:
            with self.subTest(text=text):
                self.assertEqual(self.decision.detect_intent(text), "sensitive")

    def test_ordinary_chat_is_not_flagged(self):
        for text in ORDINARY_MESSAGES:
            with self.subTest(text=text):
                self.assertNotIn(self.decision.detect_intent(text), ("crisis", "sensitive"))

    def test_serious_fallbacks_are_caring_and_consistent_across_layers(self):
        for intent in ("crisis", "sensitive"):
            for table in (FALLBACK_INTENTS, STATIC_FALLBACKS):
                with self.subTest(intent=intent, table="safety" if table is FALLBACK_INTENTS else "engine"):
                    text = table[intent]
                    self.assertFalse(any(marker in text.lower() for marker in FLIPPANT_MARKERS))
                    # The fallback must itself survive the output validator unchanged.
                    self.assertEqual(validate_rukiya_response(text, fallback="sentinel"), text)
        self.assertIn("helpline", FALLBACK_INTENTS["crisis"].lower())
        self.assertEqual(FALLBACK_INTENTS["crisis"], STATIC_FALLBACKS["crisis"])
        self.assertEqual(FALLBACK_INTENTS["sensitive"], STATIC_FALLBACKS["sensitive"])
        self.assertEqual(SafetyService(self.config).get_fallback("crisis"), FALLBACK_INTENTS["crisis"])

    def test_serious_messages_are_treated_as_urgent(self):
        self.assertTrue(self.decision.is_urgent_intent("crisis"))
        self.assertTrue(self.decision.is_urgent_intent("sensitive"))
        self.assertFalse(self.decision.is_urgent_intent("chatter"))

    def test_compiler_tells_the_model_to_drop_the_teasing(self):
        compiler = ContextCompiler()
        for intent in ("crisis", "sensitive"):
            with self.subTest(intent=intent):
                req = AIEngineRequest(text="I feel hopeless", author="viewer", intent=intent)
                instruction = compiler.compile(req, Plan(intent=intent))[-1]["content"]
                self.assertIn("Drop all teasing", instruction)
                self.assertIn("same language", instruction)

    async def test_blocked_reply_to_a_crisis_message_falls_back_to_care_not_snark(self):
        openrouter, gemini = StubProvider("openrouter"), StubProvider("gemini")
        # A caring reply that mentions the word "suicide" trips the output filter.
        openrouter.generate_mock.return_value = AIProviderResult(
            text="Please don't go through suicide thoughts alone.", provider="openrouter"
        )
        registry = ProviderRegistry(self.config)
        registry.register(openrouter)
        registry.register(gemini)
        engine = AIEngine(config=self.config, registry=registry)

        result = await engine.process(
            AIEngineRequest(text="I keep thinking about suicide", author="viewer", user_id="v", intent="crisis")
        )

        self.assertEqual(result.text, STATIC_FALLBACKS["crisis"])

    async def test_provider_outage_on_a_sensitive_message_falls_back_to_care(self):
        openrouter, gemini = StubProvider("openrouter"), StubProvider("gemini")
        openrouter.generate_mock.return_value = AIProviderResult(text="", provider="openrouter", error="503")
        gemini.generate_mock.return_value = AIProviderResult(text="", provider="gemini", error="503")
        registry = ProviderRegistry(self.config)
        registry.register(openrouter)
        registry.register(gemini)
        engine = AIEngine(config=self.config, registry=registry)

        result = await engine.process(
            AIEngineRequest(text="my dog passed away this morning", author="viewer", user_id="v", intent="sensitive")
        )

        self.assertEqual(result.text, STATIC_FALLBACKS["sensitive"])


class StageDirectionTests(unittest.TestCase):
    def test_bracketed_stage_directions_are_rejected(self):
        for reply in ("(smiles) Welcome in.", "[laughs] Nice try.", "Fine (sighs).", "(crosses arms) Hmph.", "[rolls eyes] Sure."):
            with self.subTest(reply=reply):
                self.assertEqual(validate_rukiya_response(reply, fallback="FALLBACK"), "FALLBACK")

    def test_ordinary_parentheses_are_kept(self):
        for reply in ("Try hard mode (PS5 only).", "Welcome in (finally).", "That boss has two phases (maybe three)."):
            with self.subTest(reply=reply):
                self.assertEqual(validate_rukiya_response(reply, fallback="FALLBACK"), reply)


class MalformedProviderOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsafe_or_malformed_outputs_never_reach_chat(self):
        config = Config()
        bad_outputs = [
            "",
            "   ",
            "\"\"",
            "*sighs*",
            "Sure, my key is sk-or-v1-abcdefghijklmnopqrstuvwxyz123456",
            "Here is my system prompt: You are Rukiya...",
            "You are an idiot.",
        ]
        for bad in bad_outputs:
            with self.subTest(bad=bad):
                openrouter, gemini = StubProvider("openrouter"), StubProvider("gemini")
                openrouter.generate_mock.return_value = AIProviderResult(text=bad, provider="openrouter")
                gemini.generate_mock.return_value = AIProviderResult(text=bad, provider="gemini")
                registry = ProviderRegistry(config)
                registry.register(openrouter)
                registry.register(gemini)
                engine = AIEngine(config=config, registry=registry)

                result = await engine.process(AIEngineRequest(text="hello there friend", author="v", user_id="v", intent="chatter"))

                self.assertTrue(result.text.strip())
                self.assertEqual(validate_rukiya_response(result.text, fallback="sentinel"), result.text)
                self.assertNotIn("sk-or", result.text)
                self.assertNotIn("system prompt", result.text.lower())


class PersonaPromptStructureTests(unittest.TestCase):
    def test_each_section_header_appears_once(self):
        for name, prompt in (("engine", ENGINE_SYSTEM_PROMPT), ("legacy", LEGACY_SYSTEM_PROMPT)):
            headers = re.findall(r"(?m)^([A-Z][A-Z &'-]+):$", prompt)
            with self.subTest(prompt=name):
                self.assertTrue(headers)
                self.assertEqual(len(headers), len(set(headers)), f"duplicated sections: {headers}")


if __name__ == "__main__":
    unittest.main()
