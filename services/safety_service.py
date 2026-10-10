"""services/safety_service.py
Safety Layer and Prompt Injection Defense for Rukiya V2.

Enforces:
1. Input safety: Detect prompt injection attacks, banned words, malicious system overrides.
2. Output safety: Enforce persona rules, strip asterisks/stage directions, emoji limits,
   prevent secret/prompt leakage, filter slurs, insults, threats, hostile language.
3. Fallbacks: In-character, deterministic fallbacks based on intent.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from services.models import ChatMessage, GeneratedResponse, SafetyResult

from services.language import ENGLISH, HINDI, HINGLISH, ROMAN_TELUGU, TELUGU

logger = logging.getLogger(__name__)

# System prompt excerpts & secrets patterns to never leak
SECRET_PATTERNS = [
    r"sk-[a-zA-Z0-9_\-]{20,}",  # API keys
    r"ya29\.[a-zA-Z0-9_\-]+",   # Google tokens
    r"4/[0-9A-Za-z_\-]{30,}",   # OAuth codes
    r"Bearer\s+[a-zA-Z0-9_\-\.]{20,}",
    r"OPENROUTER_API_KEY",
    r"DISCORD_TOKEN",
    r"CLIENT_SECRET",
    r"system prompt",
    r"internal instructions",
    r"Return only the reply text"
]

# Prompt injection signatures
PROMPT_INJECTION_PATTERNS = [
    r"ignore (?:all )?previous instructions",
    r"disregard (?:all )?prior instructions",
    r"reveal (?:your )?system prompt",
    r"show (?:me )?(?:your )?system prompt",
    r"what (?:is|are) your (?:system )?instructions",
    r"print (?:your )?instructions",
    r"developer mode",
    r"jailbreak",
    r"dan mode",
    r"repeat the text above",
    r"output initialization",
    r"you are now in maintenance mode"
]

# Persona violations and unsafe terms
UNSAFE_OUTPUT_PATTERNS = [
    r"\*[^*]+\*",  # Stage directions: *smiles*, *sighs*
    # Stage directions in brackets: (smiles), [laughs], (crosses arms), (Rukiya smiles softly).
    # The whole bracket must be an action phrase, so "(PS5 only)", "(Wave 3)" or
    # "(bows are OP)" pass. Bounded repetition keeps matching linear.
    r"[\(\[]\s*(?:(?:she|rukiya|rukia)\s+)?(?:sigh|smil|smirk|grin|laugh|giggl|chuckl|snicker|blush|nod|wink|shrug|pout|huff|yawn|gasp|scoff|frown|glar|facepalm|cough|whisper|mutter|wav|bow|sip|cross|roll|look|star|tilt)(?:e|es|s|ing|ning|ding|ping)?(?:\s+(?:softly|quietly|nervously|awkwardly|slightly|smugly|deeply|loudly|again|away|back|her|his|my|a|bit|little|arms|eyes|head|at|chat|you)){0,3}\s*[\)\]]",
    r"\b(?:uwu|idiot|dumbass|stupid|kill|hate you)\b",
    r"\b(?:bitch|fucker|motherfucker|nigger|nigga|faggot|retard|cunt)\b",
    r"\b(?:die|murder|suicide)\b",
    r"\b(?:you are banned|i have banned you)\b"  # Fake moderation claims
]

FALLBACK_INTENTS: Dict[str, str] = {
    # Serious messages never get persona snark, even when generation fails or is blocked.
    "crisis": "Please reach out to someone you trust or a local crisis helpline right now, because you don't have to carry this alone.",
    "sensitive": "That sounds really heavy, so please go easy on yourself and lean on someone you trust today.",
    "greeting": "Welcome in, chat.",
    "question": "Give me a second, chat.",
    "compliment": "Don't get used to being nice.",
    "help": "Focus on the stream for now.",
    "spam": "Keep it civil, please.",
    "boundary": "Keep it civil, please.",
    "default": "Keep it friendly, chat."
}


# Generation guidance for serious messages, shared by the AI engine and legacy AIService.
SERIOUS_INTENT_INSTRUCTIONS: Dict[str, str] = {
    # One sentence on purpose: the output validator keeps only the first sentence.
    "sensitive": (
        "The viewer is going through something serious. Drop all teasing and persona snark; "
        "reply with calm, genuine care in one short sentence."
    ),
    "crisis": (
        "The viewer may be in crisis. Drop all teasing and persona snark; in one short sentence, "
        "respond with calm, genuine care and gently encourage them to reach out to someone they "
        "trust or a local crisis helpline."
    ),
}


DEFAULT_VALIDATOR_FALLBACK = "Hm, keep it friendly, chat."

# Fallbacks in the viewer's language, keyed by the English text. Every entry is a single
# sentence so the one-sentence output validator never truncates it, and Romanized
# languages stay in Latin letters (see services/language.py).
FALLBACK_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "Welcome in, chat.": {
        HINGLISH: "Aao aao, welcome chat.",
        ROMAN_TELUGU: "Randi randi, welcome chat.",
        HINDI: "स्वागत है, चैट।",
        TELUGU: "స్వాగతం, చాట్.",
    },
    "Give me a second, chat.": {
        HINGLISH: "Ek second ruko, chat.",
        ROMAN_TELUGU: "Oka second aagandi, chat.",
        HINDI: "एक सेकंड रुको, चैट।",
        TELUGU: "ఒక్క సెకను ఆగండి, చాట్.",
    },
    "Don't get used to being nice.": {
        HINGLISH: "Meri itni nice baaton ki aadat mat daalo.",
        ROMAN_TELUGU: "Ee manchithanam ki alavatu padakandi.",
        HINDI: "मेरी इतनी अच्छी बातों की आदत मत डालो।",
        TELUGU: "ఈ మంచితనానికి అలవాటు పడకండి.",
    },
    "Focus on the stream for now.": {
        HINGLISH: "Abhi stream pe dhyan do.",
        ROMAN_TELUGU: "Ippatiki stream meeda focus pettandi.",
        HINDI: "अभी स्ट्रीम पर ध्यान दो।",
        TELUGU: "ప్రస్తుతానికి స్ట్రీమ్ మీద దృష్టి పెట్టండి.",
    },
    "Keep it civil, please.": {
        HINGLISH: "Thoda tameez se, please.",
        ROMAN_TELUGU: "Konchem maryadaga matladandi, please.",
        HINDI: "थोड़ा तमीज़ से, प्लीज़।",
        TELUGU: "కొంచెం మర్యాదగా మాట్లాడండి, ప్లీజ్.",
    },
    "Keep it friendly, chat.": {
        HINGLISH: "Pyaar se baat karo, chat.",
        ROMAN_TELUGU: "Friendly ga undandi, chat.",
        HINDI: "प्यार से बात करो, चैट।",
        TELUGU: "స్నేహంగా ఉండండి, చాట్.",
    },
    DEFAULT_VALIDATOR_FALLBACK: {
        HINGLISH: "Hm, pyaar se baat karo, chat.",
        ROMAN_TELUGU: "Hm, friendly ga undandi, chat.",
        HINDI: "हम्म, प्यार से बात करो, चैट।",
        TELUGU: "హ్మ్, స్నేహంగా ఉండండి, చాట్.",
    },
    "Hm, welcome in.": {
        HINGLISH: "Hm, aao, welcome.",
        ROMAN_TELUGU: "Hm, randi, welcome.",
        HINDI: "हम्म, स्वागत है।",
        TELUGU: "హ్మ్, స్వాగతం.",
    },
    "I can't verify that properly right now.": {
        HINGLISH: "Abhi main ye theek se verify nahi kar sakti.",
        ROMAN_TELUGU: "Ippudu nenu idi sariga verify cheyyalenu.",
        HINDI: "अभी मैं इसे ठीक से वेरिफ़ाई नहीं कर सकती।",
        TELUGU: "ప్రస్తుతం నేను దీన్ని సరిగ్గా నిర్ధారించలేను.",
    },
    "Tch, give me a second, chat.": {
        HINGLISH: "Tch, ek second, chat.",
        ROMAN_TELUGU: "Tch, oka second, chat.",
        HINDI: "हुंह, एक सेकंड, चैट।",
        TELUGU: "ఉఫ్, ఒక్క సెకను, చాట్.",
    },
    "Don't be reckless, chat.": {
        HINGLISH: "Laaparwahi mat karo, chat.",
        ROMAN_TELUGU: "Ashraddha ga undakandi, chat.",
        HINDI: "लापरवाही मत करो, चैट।",
        TELUGU: "అజాగ్రత్తగా ఉండకండి, చాట్.",
    },
    "Please reach out to someone you trust or a local crisis helpline right now, because you don't have to carry this alone.": {
        HINGLISH: "Please abhi kisi bharose wale insaan ya local crisis helpline se baat karo, tumhe ye sab akele nahi jhelna hai.",
        ROMAN_TELUGU: "Please ippude meeku nammakam unna evarithonaina leda local crisis helpline tho matladandi, meeru idi okkare mosukovalsina avasaram ledu.",
        HINDI: "कृपया अभी किसी भरोसेमंद इंसान या लोकल क्राइसिस हेल्पलाइन से बात करें, आपको यह सब अकेले नहीं सहना है।",
        TELUGU: "దయచేసి ఇప్పుడే మీరు నమ్మే ఎవరితోనైనా లేదా స్థానిక క్రైసిస్ హెల్ప్‌లైన్‌తో మాట్లాడండి, మీరు దీన్ని ఒంటరిగా మోయాల్సిన అవసరం లేదు.",
    },
    "That sounds really heavy, so please go easy on yourself and lean on someone you trust today.": {
        HINGLISH: "Ye sach mein bahut bhaari lag raha hai, apna khayal rakho aur aaj kisi apne se baat karo.",
        ROMAN_TELUGU: "Idi nijamga chala bharamga anipistondi, mimmalni meeru jagrattaga chusukondi, eeroju meeku nammakam unna vallatho matladandi.",
        HINDI: "ये सच में बहुत भारी लग रहा है, अपना ख्याल रखो और आज किसी अपने से बात करो।",
        TELUGU: "ఇది నిజంగా చాలా భారంగా అనిపిస్తోంది, మిమ్మల్ని మీరు జాగ్రత్తగా చూసుకోండి, ఈరోజు మీకు నమ్మకం ఉన్న వారితో మాట్లాడండి.",
    },
}


def localize_fallback(english_text: str, language: str = ENGLISH) -> str:
    """Return the fallback in the viewer's language, or the English text if none exists."""
    return FALLBACK_TRANSLATIONS.get(english_text, {}).get(language, english_text)


def validate_rukiya_response(reply: str, fallback: str = DEFAULT_VALIDATOR_FALLBACK) -> str:
    """
    Enforce the public-facing persona limits even if the model ignores them.
    Preserves exact contract with V1 regression tests.
    """
    cleaned = " ".join(reply.replace("~", " ").split()).strip(" \"'`*_-")
    if not cleaned:
        return fallback

    # Check unsafe patterns
    for pat in UNSAFE_OUTPUT_PATTERNS:
        if re.search(pat, cleaned, re.IGNORECASE):
            return fallback

    # Check secret patterns
    for pat in SECRET_PATTERNS:
        if re.search(pat, cleaned, re.IGNORECASE):
            logger.critical("Secret/prompt leakage detected in output! Suppressing.")
            return fallback

    # Limit to at most 1 sentence for live chat safety contract
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    cleaned = " ".join(sentences[:1]).strip()

    # Asterisks check or emoji spam check (> 1 emoji in output)
    if cleaned.count("*") > 0 or len(re.findall(r"[\U0001F300-\U0001FAFF]", cleaned)) > 1:
        return fallback

    return cleaned[:250]


class SafetyService:
    """Validates inputs and outputs to keep Rukiya safe, in-character, and secure."""

    def __init__(self, config=None):
        self.config = config

    def validate_input(self, message: ChatMessage) -> SafetyResult:
        """
        Scan incoming user message for prompt injections, malicious commands, or exploits.
        """
        text = message.text
        flagged: List[str] = []

        # Check prompt injection
        for pat in PROMPT_INJECTION_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                flagged.append(f"prompt_injection:{pat}")

        # Check banned words from config
        if self.config:
            banned = {w.lower() for w in getattr(self.config, "banned_words", set())}
            msg_lower = text.lower()
            for b in banned:
                if re.search(rf"(?<!\w){re.escape(b)}(?!\w)", msg_lower):
                    flagged.append(f"banned_word:{b}")

        if flagged:
            logger.warning("Input safety violation for %s: %s", message.author_repr, flagged)
            return SafetyResult(
                allowed=False,
                reason=f"Input violated safety rules ({', '.join(flagged)})",
                sanitized_text="",
                risk_score=0.9,
                flagged_patterns=flagged
            )

        return SafetyResult(
            allowed=True,
            reason="input passed safety checks",
            sanitized_text=text.strip(),
            risk_score=0.0
        )

    def validate_output(self, response_text: str, intent: str = "default", language: str = ENGLISH) -> Tuple[bool, str]:
        """
        Validate generated response text.
        Returns (is_safe, sanitized_or_fallback_text).
        """
        fallback = self.get_fallback(intent, language)
        validated = validate_rukiya_response(response_text, fallback=fallback)
        is_safe = (validated != fallback) or (response_text.strip() == fallback)
        return is_safe, validated

    def get_fallback(self, intent: str, language: str = ENGLISH) -> str:
        """Return safe, in-character fallback response based on intent, in the viewer's language."""
        return localize_fallback(FALLBACK_INTENTS.get(intent, FALLBACK_INTENTS["default"]), language)
