"""services/decision_service.py
Decision Engine for Rukiya V2.

Core Principle: DECISION != GENERATION.
Evaluates eligibility, intent, priority, conversation continuity, anti-repetition,
and response budgets without generating text.
"""
from __future__ import annotations

import logging
import math
import random
import re
import string
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from services.config import Config
from services.models import ChatMessage, ResponseDecision, UserIdentity

logger = logging.getLogger(__name__)


class DecisionService:
    """Evaluates incoming messages to decide whether, why, and how to respond."""

    def __init__(self, config: Optional[Config] = None, rng: Optional[random.Random] = None):
        self.config = config or Config()
        self.rng = rng or random.Random()
        self.response_threshold = float(getattr(self.config, "response_threshold", 0.50))
        self.anti_repeat_threshold = float(getattr(self.config, "anti_repeat_threshold", 0.70))

        # Recent response tracking for anti-repetition and frequency control
        self._recent_fingerprints: List[Tuple[Set[str], float]] = []  # (token_set, timestamp)
        self._max_recent_fingerprints = 15
        self._last_responded_timestamps: Dict[str, float] = {}  # canonical_id -> timestamp
        self._recent_message_timestamps: List[float] = []  # sliding window for chat flood detection

    # ─────────────────────────────────────────────────────────────
    # Hard Rules & Triggers
    # ─────────────────────────────────────────────────────────────
    def check_hard_rules(self, message: ChatMessage, user: UserIdentity, bypass_trigger: bool = False) -> Tuple[bool, str]:
        """
        Check hard exclusion rules. Returns (allowed, reason).
        If allowed is False, the message MUST be ignored.
        """
        # 1. Bot users
        bot_users = {u.lower() for u in getattr(self.config, "bot_users", set())}
        if message.username.lower() in bot_users or user.username.lower() in bot_users:
            return False, "author is in bot_users list"

        # 2. Banned words
        msg_lower = message.text.lower()
        banned = {w.lower() for w in getattr(self.config, "banned_words", set())}
        for w in banned:
            if w in msg_lower:
                return False, f"message contains banned word '{w}'"

        return True, ""

    def contains_trigger(self, text: str) -> Tuple[bool, Optional[str]]:
        """Check if message contains a configured trigger word or mention."""
        msg_lower = text.lower()
        triggers = {t.lower() for t in getattr(self.config, "ai_triggers", set())}
        for trigger in triggers:
            if trigger in msg_lower:
                return True, trigger
        return False, None

    # ─────────────────────────────────────────────────────────────
    # Intent Detection
    # ─────────────────────────────────────────────────────────────
    def detect_intent(self, text: str) -> str:
        """Heuristic intent classification."""
        cleaned = text.strip()
        lower = cleaned.lower()

        # Greetings
        greeting_words = ("hi", "hello", "hey", "sup", "yo", "namaste", "konnichiwa", "welcome")
        if any(re.search(rf"\b{w}\b", lower) for w in greeting_words):
            return "greeting"

        # Compliments
        compliment_words = ("cute", "pretty", "beautiful", "kawaii", "best girl", "smart", "love you", "cool")
        if any(w in lower for w in compliment_words):
            return "compliment"

        # Questions
        if "?" in cleaned or any(re.search(rf"\b{w}\b", lower) for w in ("who", "what", "where", "when", "why", "how", "kya", "kyun", "kaun")):
            return "question"

        # Spam / flood signals
        if len(cleaned) > 200 or len(set(cleaned)) < 5:
            return "spam"

        # Help
        if any(w in lower for w in ("help", "stuck", "guide", "madad", "tips")):
            return "help"

        return "chatter"

    # ─────────────────────────────────────────────────────────────
    # Priority & Frequency Scoring
    # ─────────────────────────────────────────────────────────────
    def calculate_conversation_continuity(self, user: UserIdentity) -> float:
        """Decaying score of ongoing conversation continuity: score = exp(-mins / 15)."""
        now = time.time()
        elapsed_seconds = max(0.0, now - user.last_seen)
        elapsed_minutes = elapsed_seconds / 60.0
        return math.exp(-elapsed_minutes / 15.0)

    def evaluate_priority(
        self,
        message: ChatMessage,
        user: UserIdentity,
        intent: str,
        is_direct_mention: bool,
        recent_messages: List[Dict[str, Any]]
    ) -> float:
        """
        priority =
            direct_mention        * 0.30
          + direct_question       * 0.20
          + newcomer_signal       * 0.10
          + conversation_context  * 0.15
          + user_relationship     * 0.10
          + urgency               * 0.10
          + randomness            * 0.05
        """
        direct_mention_score = 1.0 if is_direct_mention else 0.0
        direct_question_score = 1.0 if intent == "question" else 0.0
        newcomer_signal = 1.0 if not user.is_welcomed_in_stream else 0.0
        continuity_score = self.calculate_conversation_continuity(user)

        # User relationship: regular chatters get a slight boost up to 1.0
        relationship_score = min(1.0, user.interaction_count / 10.0)

        # Urgency: help or direct call or newcomer greeting
        urgency_score = 1.0 if (intent == "help") or (intent == "greeting" and not user.is_welcomed_in_stream) else 0.0

        random_score = self.rng.random()

        priority = (
            direct_mention_score * 0.30 +
            direct_question_score * 0.20 +
            newcomer_signal * 0.10 +
            continuity_score * 0.15 +
            relationship_score * 0.10 +
            urgency_score * 0.10 +
            random_score * 0.05
        )
        return min(1.0, max(0.0, priority))

    def evaluate_response_budget(
        self,
        user: UserIdentity,
        is_direct_mention: bool,
        is_question: bool,
        continuity_score: float
    ) -> float:
        """
        Response frequency budget:
        base chance (0.20)
        + direct mention bonus (0.50)
        + question bonus (0.25)
        + active conversation bonus (0.15)
        - recently responded penalty (0.30)
        - chat flood penalty (0.40)
        """
        now = time.time()
        base = 0.20
        mention_bonus = 0.50 if is_direct_mention else 0.0
        question_bonus = 0.25 if is_question else 0.0
        active_bonus = 0.15 if continuity_score > 0.5 else 0.0

        # Recently responded penalty (responded to this user in last 15s)
        last_resp = self._last_responded_timestamps.get(user.canonical_id, 0.0)
        recently_responded = 0.30 if (now - last_resp < 15.0) else 0.0

        # Chat flood penalty (more than 10 messages across all users in last 5s)
        self._recent_message_timestamps = [t for t in self._recent_message_timestamps if now - t < 5.0]
        self._recent_message_timestamps.append(now)
        flood_penalty = 0.40 if len(self._recent_message_timestamps) > 10 else 0.0

        chance = base + mention_bonus + question_bonus + active_bonus - recently_responded - flood_penalty
        return min(1.0, max(0.0, chance))

    # ─────────────────────────────────────────────────────────────
    # Anti-Repetition & Fingerprinting
    # ─────────────────────────────────────────────────────────────
    def get_fingerprint(self, text: str) -> Set[str]:
        """Normalize, lowercase, remove punctuation, collapse whitespace, tokenize."""
        lowered = text.lower()
        no_punct = lowered.translate(str.maketrans("", "", string.punctuation))
        tokens = set(no_punct.split())
        return tokens

    def is_repetitive(self, response_text: str) -> bool:
        """Compute Jaccard similarity against recent responses. Reject if >= anti_repeat_threshold."""
        current_tokens = self.get_fingerprint(response_text)
        if not current_tokens:
            return False

        now = time.time()
        # Keep only recent responses within 120s
        self._recent_fingerprints = [(fp, t) for fp, t in self._recent_fingerprints if now - t < 120.0]

        for prev_tokens, _ in self._recent_fingerprints:
            intersection = current_tokens.intersection(prev_tokens)
            union = current_tokens.union(prev_tokens)
            if union:
                similarity = len(intersection) / len(union)
                if similarity >= self.anti_repeat_threshold:
                    logger.warning("Anti-repetition triggered: similarity %.2f >= %.2f", similarity, self.anti_repeat_threshold)
                    return True
        return False

    def record_response(self, user_canonical_id: str, response_text: str) -> None:
        """Register a response in anti-repetition and frequency tracking."""
        tokens = self.get_fingerprint(response_text)
        now = time.time()
        self._recent_fingerprints.append((tokens, now))
        if len(self._recent_fingerprints) > self._max_recent_fingerprints:
            self._recent_fingerprints = self._recent_fingerprints[-self._max_recent_fingerprints:]
        self._last_responded_timestamps[user_canonical_id] = now

    # ─────────────────────────────────────────────────────────────
    # Main Decision Method
    # ─────────────────────────────────────────────────────────────
    def decide(
        self,
        message: ChatMessage,
        user: UserIdentity,
        recent_messages: Optional[List[Dict[str, Any]]] = None,
        bypass_trigger: bool = False,
        bypass_cooldown: bool = False
    ) -> ResponseDecision:
        """
        Evaluate full decision pipeline for an incoming message.
        Produces ResponseDecision indicating whether to respond, intent, priority, and reason.
        """
        recent = recent_messages or []

        # 1. Hard rules check
        allowed, hard_reason = self.check_hard_rules(message, user, bypass_trigger=bypass_trigger)
        if not allowed:
            return ResponseDecision(
                should_respond=False,
                intent="ignored",
                priority=0.0,
                response_mode="ignore",
                memory_needed=False,
                reason=hard_reason
            )

        # 2. Check triggers & direct mentions
        has_trigger, matched_trigger = self.contains_trigger(message.text)
        is_direct_mention = bool(
            has_trigger or
            (message.channel_id and f"<@{message.channel_id}>" in message.text) or
            bypass_trigger
        )

        # 3. Intent detection
        intent = self.detect_intent(message.text)

        # If spam intent without direct mention -> ignore
        if intent == "spam" and not is_direct_mention:
            return ResponseDecision(
                should_respond=False,
                intent="spam",
                priority=0.0,
                response_mode="boundary",
                memory_needed=False,
                reason="spam detected without mention"
            )

        # 4. Continuity & priority
        continuity = self.calculate_conversation_continuity(user)
        priority = self.evaluate_priority(
            message=message,
            user=user,
            intent=intent,
            is_direct_mention=is_direct_mention,
            recent_messages=recent
        )

        # 5. Budget evaluation
        budget_chance = self.evaluate_response_budget(
            user=user,
            is_direct_mention=is_direct_mention,
            is_question=(intent == "question"),
            continuity_score=continuity
        )

        # 6. Response decision logic
        if bypass_trigger:
            should_respond = True
            reason = "trigger bypassed (direct command or test)"
        elif is_direct_mention:
            # Direct mentions almost always respond unless severely throttled
            should_respond = True
            reason = f"direct mention matched trigger '{matched_trigger}'" if matched_trigger else "direct mention"
        elif not user.is_welcomed_in_stream and intent == "greeting" and priority >= 0.25:
            should_respond = True
            reason = "welcoming new viewer to stream"
        elif continuity > 0.6 and (priority >= self.response_threshold or budget_chance > 0.4):
            should_respond = True
            reason = "active ongoing conversation continuity"
        elif priority >= self.response_threshold and self.rng.random() < budget_chance:
            should_respond = True
            reason = f"priority ({priority:.2f}) passed threshold ({self.response_threshold:.2f}) within budget"
        else:
            should_respond = False
            reason = f"priority ({priority:.2f}) below threshold or budget check failed"

        # Determine if memory retrieval is useful
        memory_needed = bool(
            should_respond and (
                intent in ("question", "greeting", "compliment") or
                is_direct_mention or
                continuity > 0.4
            )
        )

        return ResponseDecision(
            should_respond=should_respond,
            intent=intent,
            priority=priority,
            response_mode="personality" if should_respond else "ignore",
            memory_needed=memory_needed,
            reason=reason,
            extra={
                "continuity": continuity,
                "budget_chance": budget_chance,
                "is_direct_mention": is_direct_mention,
                "matched_trigger": matched_trigger
            }
        )
