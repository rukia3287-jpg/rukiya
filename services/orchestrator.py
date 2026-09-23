"""services/orchestrator.py
Rukiya Orchestrator (V2 Core Coordinator).

Orchestrates the entire message lifecycle:
ChatMessage -> Identity -> Input Safety -> Context Builder -> Decision ->
Rate Limiter -> AI Generation -> Output Safety -> Anti-Repetition -> Memory Recording.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from services.ai_engine.engine import AIEngine
from services.ai_engine.models import AIEngineRequest
from services.ai_service import AIService
from services.config import Config
from services.decision_service import DecisionService
from services.identity_service import IdentityService
from services.memory_service import MemoryService
from services.models import ChatMessage, GeneratedResponse, ResponseDecision, UserIdentity
from services.rate_limiter import RateLimiter
from services.safety_service import SafetyService

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

    async def process_message(
        self,
        message: ChatMessage,
        bypass_trigger: bool = False,
        bypass_cooldown: bool = False
    ) -> Optional[GeneratedResponse]:
        """
        Full V2 processing pipeline for an incoming ChatMessage.
        """
        logger.info(
            "event=message_received platform=%s author=%s text='%s'",
            message.platform, message.author_repr, message.text[:60]
        )

        # 1. Identity Resolution
        user = self.identity_service.resolve(message)

        # 2. Input Safety Check (Prompt injection defense)
        safety_input = self.safety_service.validate_input(message)
        if not safety_input.allowed:
            logger.warning("event=input_safety_blocked user=%s reason='%s'", user.canonical_id, safety_input.reason)
            return None

        # 3. Retrieve Memory Context
        session_context = self.memory_service.get_stream_context(user)
        persistent_memory = self.memory_service.get_relevant_user_memory(user, message.text, limit=5)
        recent_messages = self.memory_service.get_recent_messages(limit=8)

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
        self.memory_service.record_interaction(user, message.text, role="user")

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
        if self.ai_engine is not None:
            try:
                engine_req = AIEngineRequest(
                    text=message.text,
                    author=user.display_name,
                    platform=message.platform,
                    user_id=message.user_id,
                    canonical_id=user.canonical_id,
                    intent=decision.intent,
                    persistent_memory=persistent_memory if decision.memory_needed else None,
                    stream_memory=session_context if decision.memory_needed else None,
                    recent_messages=recent_messages,
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
        else:
            prompt_messages = self.ai_service.build_context_messages(
                user_message=message.text,
                author=user.display_name,
                persistent_memory=persistent_memory if decision.memory_needed else None,
                stream_memory=session_context if decision.memory_needed else None,
                recent_messages=recent_messages,
                decision=decision
            )
            try:
                generated = await self.ai_service.generate(prompt_messages, author=user.display_name)
            except Exception as e:
                logger.exception("AI generation exception: %s", e)

        # If AI failed or returned None, use safe intent-based fallback
        if not generated or not generated.text.strip():
            fallback_text = self.safety_service.get_fallback(decision.intent)
            generated = GeneratedResponse(
                text=fallback_text,
                confidence=0.5,
                used_memory=False,
                latency_ms=0.0,
                is_fallback=True
            )
            logger.info("event=fallback_used intent=%s text='%s'", decision.intent, fallback_text)

        # 8. Output Safety Validation
        is_safe, validated_text = self.safety_service.validate_output(generated.text, intent=decision.intent)
        generated.text = validated_text

        # 9. Anti-Repetition Check
        if self.decision_service.is_repetitive(generated.text):
            logger.info("event=repetition_detected rejecting text='%s'", generated.text)
            # Use fallback instead of repeating
            generated.text = self.safety_service.get_fallback(decision.intent)
            generated.is_fallback = True

        # 10. Register Response in Decision & Anti-Repeat Tracking
        self.decision_service.record_response(user.canonical_id, generated.text)

        # 11. Record Assistant Message & Extract Facts
        self.memory_service.record_interaction(user, generated.text, role="assistant")

        # Extract persistent and stream facts from user's message
        self.memory_service.extract_and_store_facts(user, message.text)

        # Mark user as welcomed in stream if intent was greeting
        if not user.is_welcomed_in_stream and decision.intent == "greeting":
            user.is_welcomed_in_stream = True
            self.memory_service.save_user(user)

        logger.info(
            "event=ai_response user=%s latency_ms=%.1f chars=%d fallback=%s text='%s'",
            user.canonical_id, generated.latency_ms, len(generated.text), generated.is_fallback, generated.text
        )

        return generated

    async def process_raw_text(
        self,
        text: str,
        author: str,
        platform: str = "discord",
        user_id: Optional[str] = None,
        bypass_trigger: bool = False,
        bypass_cooldown: bool = False
    ) -> Optional[str]:
        """Convenience method for text-based triggers (e.g. slash /ask or Discord listener)."""
        msg = ChatMessage(
            platform=platform,
            message_id=f"raw_{int(time.time()*1000)}",
            user_id=user_id or author,
            username=author,
            display_name=author,
            text=text
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
            "model": self.ai_service.model
        }
