"""tests/test_v2_fallback_language.py
Fallback replies follow the viewer's language and script, and every fallback survives the
output validator unchanged (multi-sentence fallbacks used to be cut down to "Hm.").
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.ai_engine.engine import STATIC_FALLBACKS, AIEngine
from services.ai_engine.models import AIEngineRequest, AIEngineResult, AIProviderResult
from services.ai_engine.provider_registry import ProviderRegistry
from services.ai_engine.providers.base import AIProvider
from services.config import Config
from services.language import ENGLISH, HINDI, HINGLISH, ROMAN_TELUGU, SUPPORTED_LANGUAGES, TELUGU, detect_reply_language
from services.memory_service import MemoryService
from services.models import ChatMessage
from services.orchestrator import RukiyaOrchestrator
from services.safety_service import (
    DEFAULT_VALIDATOR_FALLBACK,
    FALLBACK_INTENTS,
    SafetyService,
    localize_fallback,
    validate_rukiya_response,
)


class LanguageDetectionTests(unittest.TestCase):
    def test_detects_language_and_script(self):
        cases = {
            "ela unnaru?": ROMAN_TELUGU,
            "rukiya meeru ela unnaru": ROMAN_TELUGU,
            "nenu chala bagunnanu, nuvvu?": ROMAN_TELUGU,
            "aap kaise ho": HINGLISH,
            "kya scene hai bhai": HINGLISH,
            "bhai this boss is hard yaar": HINGLISH,
            "नमस्ते, कैसे हो?": HINDI,
            "ఎలా ఉన్నారు?": TELUGU,
            "hello how are you": ENGLISH,
            "hi": ENGLISH,
            "I love this game": ENGLISH,
            "": ENGLISH,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_reply_language(text), expected)


class LocalizedFallbackTests(unittest.TestCase):
    def _all_english_fallbacks(self):
        return set(FALLBACK_INTENTS.values()) | set(STATIC_FALLBACKS.values()) | {DEFAULT_VALIDATOR_FALLBACK}

    def test_every_fallback_has_every_translation(self):
        for english in self._all_english_fallbacks():
            for language in SUPPORTED_LANGUAGES:
                with self.subTest(fallback=english, language=language):
                    text = localize_fallback(english, language)
                    self.assertTrue(text.strip())
                    if language != ENGLISH:
                        self.assertNotEqual(text, english)

    def test_every_fallback_in_every_language_survives_the_validator(self):
        for english in self._all_english_fallbacks():
            for language in SUPPORTED_LANGUAGES:
                text = localize_fallback(english, language)
                with self.subTest(text=text):
                    self.assertEqual(validate_rukiya_response(text, fallback="SENTINEL"), text)

    def test_romanized_fallbacks_stay_in_latin_script(self):
        for english in self._all_english_fallbacks():
            for language in (HINGLISH, ROMAN_TELUGU):
                with self.subTest(fallback=english, language=language):
                    self.assertTrue(localize_fallback(english, language).isascii())

    def test_unknown_text_or_language_falls_back_to_english(self):
        self.assertEqual(localize_fallback("Some new line.", HINGLISH), "Some new line.")
        self.assertEqual(localize_fallback(FALLBACK_INTENTS["default"], "klingon"), FALLBACK_INTENTS["default"])

    def test_safety_service_localizes_by_language(self):
        safety = SafetyService(Config())
        self.assertEqual(safety.get_fallback("greeting", HINGLISH), localize_fallback(FALLBACK_INTENTS["greeting"], HINGLISH))
        self.assertEqual(safety.get_fallback("greeting"), FALLBACK_INTENTS["greeting"])


class StubProvider(AIProvider):
    def __init__(self, name):
        self.name = name
        self.generate_mock = AsyncMock(return_value=AIProviderResult(text="", provider=name, error="503"))
        self.search_mock = AsyncMock()

    def is_configured(self):
        return True

    async def generate(self, messages, **kwargs):
        return await self.generate_mock(messages, **kwargs)

    async def search_grounded(self, query, **kwargs):
        return await self.search_mock(query, **kwargs)

    async def health_check(self):
        return True


class FallbackRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_fallback_follows_roman_telugu(self):
        config = Config()
        registry = ProviderRegistry(config)
        registry.register(StubProvider("openrouter"))
        registry.register(StubProvider("gemini"))
        engine = AIEngine(config=config, registry=registry)

        result = await engine.process(AIEngineRequest(text="rukiya meeru ela unnaru", author="v", user_id="v", intent="chatter"))

        self.assertEqual(result.text, localize_fallback(STATIC_FALLBACKS["default"], ROMAN_TELUGU))

    async def test_orchestrator_fallback_follows_hinglish_and_is_not_truncated(self):
        db_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, db_dir, True)
        config = Config(db_path=os.path.join(db_dir, "lang.db"))
        engine = MagicMock()
        # The engine's own greeting fallback must reach chat whole, not cut to "Hm."
        engine.process = AsyncMock(return_value=AIEngineResult(
            text=localize_fallback(STATIC_FALLBACKS["greeting"], HINGLISH), provider="static_fallback", fallback_used=True,
        ))
        orchestrator = RukiyaOrchestrator(config=config, memory_service=MemoryService(config), ai_engine=engine)
        message = ChatMessage(platform="discord", message_id="m1", user_id="123456789012345678",
                              username="v", display_name="V", text="rukiya namaste, aap kaise ho")

        response = await orchestrator.process_message(message, bypass_trigger=True, bypass_cooldown=True)

        self.assertEqual(response.text, localize_fallback(STATIC_FALLBACKS["greeting"], HINGLISH))

    async def test_orchestrator_uses_localized_safety_fallback_when_generation_is_empty(self):
        db_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, db_dir, True)
        config = Config(db_path=os.path.join(db_dir, "lang2.db"))
        engine = MagicMock()
        engine.process = AsyncMock(return_value=AIEngineResult(text="", provider="none"))
        orchestrator = RukiyaOrchestrator(config=config, memory_service=MemoryService(config), ai_engine=engine)
        message = ChatMessage(platform="discord", message_id="m2", user_id="223456789012345678",
                              username="v", display_name="V", text="rukiya ela unnaru")

        response = await orchestrator.process_message(message, bypass_trigger=True, bypass_cooldown=True)

        intent = orchestrator.decision_service.detect_intent(message.text)
        self.assertEqual(response.text, localize_fallback(FALLBACK_INTENTS.get(intent, FALLBACK_INTENTS["default"]), ROMAN_TELUGU))


if __name__ == "__main__":
    unittest.main()
