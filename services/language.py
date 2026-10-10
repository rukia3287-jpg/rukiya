"""services/language.py
Deterministic detection of the viewer's reply language, used to pick fallback replies
that match how the viewer wrote: same language, and Romanized text stays Romanized.
"""
from __future__ import annotations

import re

ENGLISH = "en"
HINGLISH = "hinglish"          # Hindi written in Latin letters
ROMAN_TELUGU = "roman_telugu"  # Telugu written in Latin letters
HINDI = "hindi"                # Devanagari script
TELUGU = "telugu"              # Telugu script
SUPPORTED_LANGUAGES = (ENGLISH, HINGLISH, ROMAN_TELUGU, HINDI, TELUGU)

_TELUGU_SCRIPT = re.compile(r"[\u0C00-\u0C7F]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

# Distinctive words count double; common short words (and words that are also names,
# like "Raha" or "Mera") only count in combination. Each word counts once, and the
# score must beat the number of plain English function words, so "ho ho ho merry
# christmas" or "Raha is in the chat" stay English.
_STRONG_MARKERS = {
    HINGLISH: frozenset({
        "kya", "kaise", "kaisa", "kaisi", "nahi", "nhi", "nahin", "kyun", "kyu", "mujhe", "bahut",
        "bohot", "acha", "accha", "achha", "yaar", "matlab", "kyunki", "lekin", "namaste", "haan",
        "tumhara", "aapka", "kaun", "kidhar", "kitna", "theek", "hoon", "sahi",
    }),
    ROMAN_TELUGU: frozenset({
        "unnaru", "unnava", "unnavu", "unnav", "unnanu", "meeru", "nenu", "nuvvu", "enti", "emiti",
        "bagunnara", "bagunnava", "bagunnanu", "baagunnanu", "bagundi", "undi", "cheppandi", "cheppu",
        "ledu", "enduku", "ekkada", "eppudu", "emaindi", "ayindi", "chestunnav", "chestunnaru",
        "tinnava", "evaru", "naku", "meeku", "avunu",
    }),
}
_WEAK_MARKERS = {
    HINGLISH: frozenset({
        "hai", "hain", "ho", "hu", "aap", "tum", "bhai", "kar", "karo", "sab", "abhi", "kuch", "aur",
        "bhi", "toh", "yeh", "ye", "woh", "kab", "kahan", "kaha", "se", "gaya", "gayi", "raha", "rahi",
        "mera", "meri",
    }),
    ROMAN_TELUGU: frozenset({"ela", "chala", "andi", "kada", "kadha", "sare", "mee", "naa", "inka", "kuda", "em", "ra", "ga", "unna"}),
}
_ENGLISH_FUNCTION_WORDS = frozenset({
    "the", "is", "are", "am", "and", "a", "an", "i", "you", "to", "of", "in", "it", "this", "that",
    "was", "for", "on", "with", "my", "what", "how", "be", "have", "has", "do", "not", "from", "here",
})


def detect_reply_language(text: str) -> str:
    """Return one of SUPPORTED_LANGUAGES for the viewer's message (English when unsure)."""
    if not text:
        return ENGLISH
    if _TELUGU_SCRIPT.search(text):
        return TELUGU
    if _DEVANAGARI.search(text):
        return HINDI

    words = set(re.findall(r"[a-z']+", text.lower()))
    english = len(words & _ENGLISH_FUNCTION_WORDS)
    best, best_score = ENGLISH, 0
    for language in (HINGLISH, ROMAN_TELUGU):
        strong = len(words & _STRONG_MARKERS[language])
        weak = len(words & _WEAK_MARKERS[language])
        if strong == 0 and weak < 2:
            continue
        score = 2 * strong + weak
        if score <= english:
            continue
        if score > best_score:
            best, best_score = language, score
        elif score == best_score:
            best = ENGLISH  # an exact tie between the two is too ambiguous to call
    return best
