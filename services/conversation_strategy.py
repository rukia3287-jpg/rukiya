"""Safe, application-owned conversation strategy instructions for Rukiya."""
from __future__ import annotations

from typing import Any, Mapping


MODE_INSTRUCTIONS = {
    "answer": "Answer the viewer's actual question or point first; avoid filler.",
    "banter": "Join the playful tone naturally, with clever restraint and no insults.",
    "acknowledge": "Give a brief, natural acknowledgement; do not turn it into a speech.",
    "follow_up": "Respond to the current point, then ask at most one short follow-up only if it genuinely helps.",
    "join_conversation": "Make one useful or playful contribution tied to the recent conversation; do not pretend the message was addressed directly to you.",
}
TONE_INSTRUCTIONS = {
    "witty": "Use dry, observant wit sparingly; never insult the viewer.",
    "warm": "Be naturally warm and welcoming without exaggerated praise.",
    "calm": "Be calm, caring, and straightforward; avoid teasing.",
    "neutral": "Use a balanced, conversational tone.",
    "firm": "Be concise and firm without hostility.",
}
LENGTH_INSTRUCTIONS = {
    "short": "Prefer one compact sentence.",
    "normal": "Usually use one or two short sentences.",
    "detailed": "Add detail only when it materially improves the answer; live chat still favors concise replies.",
}


def strategy_instruction(strategy: Mapping[str, Any] | None) -> str:
    """Translate validated strategy enums into fixed prompt instructions.

    Do not put arbitrary model-produced text into the generation prompt.
    The values are mapped through local allowlists before they reach a provider.
    """
    if not isinstance(strategy, Mapping):
        return ""

    parts = []
    mode = strategy.get("conversation_mode")
    tone = strategy.get("tone")
    reply_length = strategy.get("reply_length")

    if mode in MODE_INSTRUCTIONS:
        parts.append(MODE_INSTRUCTIONS[mode])
    if tone in TONE_INSTRUCTIONS:
        parts.append(TONE_INSTRUCTIONS[tone])
    if reply_length in LENGTH_INSTRUCTIONS:
        parts.append(LENGTH_INSTRUCTIONS[reply_length])

    return "ADAPTIVE CONVERSATION STRATEGY: " + " ".join(parts) if parts else ""
