"""services/orchestrator.py
Rukiya Orchestrator (V2 Core Coordinator).

Orchestrates the entire message lifecycle:
ChatMessage -> Identity -> Input Safety -> Context Builder -> Decision ->
Rate Limiter -> AI Generation -> Output Safety -> Anti-Repetition -> Memory Recording.
"""
from __future__ import annotations

import logging
import time
import asyncio
import re
import uuid
from typing import Any, Dict, Optional

from services.ai_engine.engine import AIEngine
from services.ai_engine.models import AIEngineRequest, RequestPriority
from services.ai_service import AIService
from services.config import Config
from services.decision_service import DecisionService
from services.identity_service import IdentityService
from services.memory_service import MemoryService
from services.models import ChatMessage, GeneratedResponse, ResponseDecision, UserIdentity
from services.rate_limiter import RateLimiter
from services.language import detect_reply_language
from services.safety_service import SERIOUS_INTENT_INSTRUCTIONS, SafetyService
from services.brain import BrainBusy, BrainRuntime

logger = logging.getLogger(__name__)


class RukiyaOrchestrator:
    """Central orchestrator coordinating all V2 services."""

    def __init__(
        self,
        config: Optional[Config] = None,
        identity_service: Optional[IdentityService] = None,
        memory_service: Optional[MemoryService] = None,
        decision_service: Optional[DecisionService] = None,
        safety_service: Optional[SafetyService] = None,
        rate_limiter: Optional[RateLimiter] = None,
        ai_service: Optional[AIService] = None,
        ai_engine: Optional[AIEngine] = None,
    ):
        self.config = config or Config()
        self.memory_service = memory_service or MemoryService(self.config)
        self.identity_service = identity_service or IdentityService(self.memory_service)
        self.safety_service = safety_service or SafetyService(self.config)
        self.decision_service = decision_service or DecisionService(self.config)
        self.rate_limiter = rate_limiter or RateLimiter(self.config)
        self.ai_service = ai_service or AIService(self.config)
        self.ai_engine = ai_engine
        self.brain = BrainRuntime(self.config)

    async def process_message(self, message: ChatMessage, bypass_trigger=False, bypass_cooldown=False):
        from services.scopes import conversation_scope
        scope = conversation_scope(message, self.memory_service.active_session_id)
        started = time.perf_counter()
        outcome = 'declined'
        try:
            if len(message.text) > self.config.brain_prompt_chars:
                outcome = 'prompt_limit'
                return None
            async with self.brain.admit(message):
                async with asyncio.timeout(self.config.brain_timeout):
                    with self.memory_service.session_scope(scope):
                        response = await self._process_message(message, bypass_trigger, bypass_cooldown)
                        outcome = 'fallback' if response and response.is_fallback else 'answered' if response else 'declined'
                        return response
        except BrainBusy:
            outcome = 'busy'
            return None
        except TimeoutError:
            outcome = 'timeout'
            return None
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        finally:
            self.brain.observe(started, outcome)

    async def _process_message(
        self,
        message: ChatMessage,
        bypass_trigger: bool = False,
        bypass_cooldown: bool = False
    ) -> Optional[GeneratedResponse]:
        """
        Full V2 processing pipeline for an incoming ChatMessage.
        """
        logger.info(
            "event=message_received platform=%s chars=%d",
            message.platform, len(message.text)
        )

        # 1. Identity Resolution
        user = await self.memory_service.run_async(self.identity_service.resolve, message)
        preferences = await self.memory_service.run_async(self.memory_service.get_brain_preferences, user.canonical_id)

        # 2. Input Safety Check (Prompt injection defense)
        safety_input = self.safety_service.validate_input(message)
        # Fallback replies follow the viewer's language and script.
        language = detect_reply_language(message.text)
        if not safety_input.allowed:
            logger.warning("event=input_safety_blocked user=%s reason='%s'", user.canonical_id, safety_input.reason)
            return None

        # 3. Retrieve Memory Context
        def read_context():
            return (self.memory_service.get_stream_context(user),
                    self.memory_service.get_relevant_user_memory(user, message.text, limit=5),
                    self.memory_service.get_recent_messages(limit=8, allow_persisted=preferences['history_enabled']))
        session_context, persistent_memory, recent_messages = await self.memory_service.run_async(read_context)
        if not preferences['auto_memory']:
            persistent_memory = [memory for memory in persistent_memory if memory.source == 'user_approved']

        # 4. Decision Engine
        decision = self.decision_service.decide(
            message=message,
            user=user,
            recent_messages=recent_messages,
            bypass_trigger=bypass_trigger,
            bypass_cooldown=bypass_cooldown
        )

        logger.info(
            "event=decision should_respond=%s intent=%s priority=%.2f reason='%s'",
            decision.should_respond, decision.intent, decision.priority, decision.reason
        )

        # Record incoming interaction regardless of response
        await self.memory_service.run_async(self.memory_service.record_interaction, user, message.text, role="user", timestamp=message.timestamp,
                                            persist=preferences['history_enabled'])

        if not decision.should_respond:
            return None

        # 5. Rate Limiting Check
        if not bypass_cooldown:
            # Check global limit
            global_res = self.rate_limiter.allow("global_ai")
            if not global_res.allowed:
                logger.warning("event=rate_limited scope=global_ai wait=%.1fs", global_res.wait_time)
                return None

            # Check per-user limit
            user_res = self.rate_limiter.allow("user_ai", key=user.canonical_id)
            if not user_res.allowed:
                logger.warning("event=rate_limited scope=user_ai user=%s wait=%.1fs", user.canonical_id, user_res.wait_time)
                return None

        # 6 & 7. AI Generation (AI Engine or AIService)
        generated: Optional[GeneratedResponse] = None
        control = await self._memory_control(message, user)
        if control is not None:
            generated = GeneratedResponse(control, model='deterministic')
            preferences = await self.memory_service.run_async(self.memory_service.get_brain_preferences, user.canonical_id)
        # Curated knowledge is an exact scoped retrieval operation, not chat synthesis.
        from services.scopes import persistent_scope
        knowledge_match = re.fullmatch(r'(?:rukiya\s+)?knowledge:\s*(.+)', message.text.strip(), re.I)
        if generated is not None:
            pass
        elif knowledge_match:
            scope = persistent_scope(message) or f'youtube:video:{message.video_id or self.memory_service.active_session_id or "none"}'
            records = await self.memory_service.run_async(self.memory_service.search_brain_knowledge, scope, knowledge_match.group(1))
            text = f"Stored excerpt — {records[0]['title'][:40]}: {records[0]['body'][:160]}" if records else 'No matching local knowledge in this scope.'
            generated = GeneratedResponse(text=text, model='deterministic', used_memory=bool(records))
        elif self.ai_engine is not None:
            try:
                priority_score = float(getattr(decision, "priority", 1.0) or 0.0)
                if priority_score < 0.50:
                    req_pri = RequestPriority.LOW
                elif priority_score < 0.75:
                    req_pri = RequestPriority.MEDIUM
                else:
                    req_pri = RequestPriority.HIGH

                engine_req = AIEngineRequest(
                    text=message.text,
                    author=user.display_name,
                    platform=message.platform,
                    user_id=message.user_id,
                    canonical_id=user.canonical_id,
                    intent=decision.intent,
                    priority=req_pri,
                    persistent_memory=persistent_memory if decision.memory_needed or message.text.casefold().startswith('memory:') else None,
                    stream_memory=session_context if decision.memory_needed else None,
                    recent_messages=recent_messages,
                    reply_to_text=message.reply_to_text,
                    allow_external=bool(preferences['external_allowed']),
                    style=preferences['style'],
                    conversation_scope=self.memory_service._session_id(),
                )
                engine_res = await self.ai_engine.process(engine_req)
                generated = GeneratedResponse(
                    text=engine_res.text,
                    confidence=engine_res.confidence,
                    used_memory=bool((persistent_memory and decision.memory_needed) or (session_context and decision.memory_needed)),
                    latency_ms=engine_res.latency_ms,
                    model=engine_res.provider,
                    is_fallback=engine_res.fallback_used
                )
            except Exception as e:
                logger.exception("AI Engine generation exception: %s", e)
        elif self.config.external_ai_enabled and preferences['external_allowed']:
            prompt_messages = self.ai_service.build_context_messages(
                user_message=message.text,
                author=user.display_name,
                persistent_memory=persistent_memory if decision.memory_needed else None,
                stream_memory=session_context if decision.memory_needed else None,
                recent_messages=recent_messages,
                decision=decision,
                canonical_id=user.canonical_id,
                reply_to_text=message.reply_to_text,
            )
            try:
                generated = await self.ai_service.generate(prompt_messages, author=user.display_name)
            except Exception as e:
                logger.exception("AI generation exception: %s", e)

        # If AI failed or returned None, use safe intent-based fallback
        if not generated or not generated.text.strip():
            fallback_text = self.safety_service.get_fallback(decision.intent, language)
            generated = GeneratedResponse(
                text=fallback_text,
                confidence=0.5,
                used_memory=False,
                latency_ms=0.0,
                is_fallback=True
            )
            logger.info("event=fallback_used intent=%s", decision.intent)

        # 8. Output Safety Validation
        if generated.model == 'deterministic' and len(generated.text) <= 32 and re.fullmatch(r'-?\d+(?:\.\d+)?(?:e[+-]?\d+)?', generated.text, re.I):
            # The prose validator strips markdown hyphens; preserve the sign of
            # bounded numeric skills instead of turning -5 into 5.
            is_safe, validated_text = True, generated.text
        else:
            is_safe, validated_text = self.safety_service.validate_output(generated.text, intent=decision.intent, language=language)
        if not is_safe:
            generated.is_fallback = True
        generated.text = validated_text

        # 9. Anti-Repetition Check
        if generated.model != 'deterministic' and self.decision_service.is_repetitive(generated.text):
            logger.info("event=repetition_detected")
            # Use fallback instead of repeating
            generated.text = self.safety_service.get_fallback(decision.intent, language)
            generated.is_fallback = True

        # 10. Register Response in Decision & Anti-Repeat Tracking
        self.decision_service.record_response(user.canonical_id, generated.text)

        # 11. Record Assistant Message & Extract Facts
        await self.memory_service.run_async(self.memory_service.record_interaction, user, generated.text, role="assistant", persist=preferences['history_enabled'])

        # Extract persistent and stream facts from user's message. Serious messages are
        # skipped: "I am suicidal" must never become a remembered name or preference.
        if preferences['auto_memory'] and decision.intent not in SERIOUS_INTENT_INSTRUCTIONS:
            await self.memory_service.run_async(self.memory_service.extract_and_store_facts, user, message.text)

        # Mark user as welcomed in stream if intent was greeting
        if not user.is_welcomed_in_stream and decision.intent == "greeting":
            user.is_welcomed_in_stream = True
            await self.memory_service.run_async(self.memory_service.save_user, user)

        logger.info(
            "event=ai_response user=%s latency_ms=%.1f chars=%d fallback=%s",
            user.canonical_id, generated.latency_ms, len(generated.text), generated.is_fallback
        )

        return generated

    async def _memory_control(self, message, user):
        """Strict self-owned controls also work for YouTube viewers, without an LLM."""
        text = re.sub(r'^(?:hey\s+)?(?:rukiya|rukia|ruki)[,:]?\s+', '', message.text.strip(), flags=re.I)
        remember = re.fullmatch(r'remember\s+([\w -]{1,64})=(.{1,1000})', text, re.I)
        forget = re.fullmatch(r'forget\s+([\w -]{1,64})', text, re.I)
        privacy = re.fullmatch(r'privacy\s+history=(on|off)\s+automatic=(on|off)\s+external=(on|off)', text, re.I)
        clear = text.casefold() == 'clear my memory'
        if not (remember or forget or privacy or clear):
            return None
        await self.brain.cancel(message.platform, message.user_id or message.username, message.guild_id)
        if self.ai_engine is not None and hasattr(self.ai_engine, 'clear_user_cache'):
            await self.ai_engine.clear_user_cache(user.canonical_id)
        memory = self.memory_service
        if remember:
            await memory.run_async(memory.clear_brain_history, user.canonical_id)
            await memory.run_async(memory.set_user_memory, user.canonical_id, remember.group(1), remember.group(2), source='user_approved')
            saved = await memory.run_async(memory.persistence_available)
            return 'Approved memory saved in your scope.' if saved else 'Memory is temporary until storage recovers.'
        if forget:
            saved = await memory.run_async(memory.forget_brain_fact, user.canonical_id, forget.group(1))
            return 'Memory deleted and conversation context cleared.' if saved else 'Deletion applies now and is queued for storage recovery.'
        if clear:
            saved = await memory.run_async(memory.clear_brain_user, user.canonical_id)
            return 'Your memories, history and feedback were cleared; privacy choices were retained.' if saved else 'Erasure applies now and is queued for storage recovery.'
        prefs = await memory.run_async(memory.get_brain_preferences, user.canonical_id)
        saved = await memory.run_async(memory.set_brain_preferences, user.canonical_id,
                                       history_enabled=privacy.group(1).lower() == 'on',
                                       auto_memory=privacy.group(2).lower() == 'on',
                                       external_allowed=privacy.group(3).lower() == 'on', style=prefs['style'])
        return 'Privacy preferences saved for your scope.' if saved else 'Privacy choices apply in this process; retry after storage recovers.'

    async def process_raw_text(
        self,
        text: str,
        author: str,
        platform: str = "discord",
        user_id: Optional[str] = None,
        bypass_trigger: bool = False,
        bypass_cooldown: bool = False,
        guild_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        video_id: Optional[str] = None,
        reply_to_text: Optional[str] = None,
    ) -> Optional[str]:
        """Convenience method for text-based triggers (e.g. slash /ask or Discord listener)."""
        msg = ChatMessage(
            platform=platform,
            message_id=f"raw_{uuid.uuid4().hex}",
            user_id=user_id or author,
            username=author,
            display_name=author,
            text=text, guild_id=guild_id, channel_id=channel_id, video_id=video_id, reply_to_text=reply_to_text,
        )
        res = await self.process_message(msg, bypass_trigger=bypass_trigger, bypass_cooldown=bypass_cooldown)
        return res.text if res else None

    def get_status(self) -> Dict[str, Any]:
        """Diagnostic state of orchestrator and underlying subsystems."""
        return {
            "orchestrator": "active",
            "memory_fallback_mode": self.memory_service.fallback_mode,
            "active_stream_session": self.memory_service.active_session_id,
            "ai_last_used": self.ai_service.last_used,
            "ai_cooldown_remaining": self.ai_service.get_cooldown_remaining(),
            "global_ai_tokens": self.rate_limiter.get_remaining("global_ai"),
            "model": self.ai_service.model,
            "brain": self.brain.snapshot(),
        }

    async def aclose(self) -> None:
        """Gracefully shut down AI Engine, AI service, and underlying resources."""
        await self.brain.close()
        if self.ai_engine is not None and hasattr(self.ai_engine, "aclose"):
            await self.ai_engine.aclose()
        if hasattr(self.ai_service, "close"):
            await self.ai_service.close()
