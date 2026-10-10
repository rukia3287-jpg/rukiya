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

_TELUGU_SCRIPT = re.compile(r"[ఀ-౿]")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Distinctive words count double; common short words only count in combination,
# so one stray "ho" or "ela" does not switch an English message.
_STRONG_MARKERS = {
    HINGLISH: frozenset({
        "kya", "kaise", "kaisa", "kaisi", "nahi", "nhi", "nahin", "kyun", "kyu", "mujhe", "bahut",
        "bohot", "acha", "accha", "achha", "yaar", "matlab", "kyunki", "lekin", "namaste", "haan",
        "tumhara", "aapka", "mera", "meri", "raha", "rahi",
    }),
    ROMAN_TELUGU: frozenset({
        "unnaru", "unnava", "unnavu", "unnanu", "meeru", "nenu", "nuvvu", "enti", "emiti",
        "bagunnara", "bagunnava", "bagunnanu", "baagunnanu", "cheppandi", "cheppu", "ledu",
        "enduku", "ekkada", "eppudu", "emaindi", "naku", "meeku", "avunu",
    }),
}
_WEAK_MARKERS = {
    HINGLISH: frozenset({
        "hai", "hain", "ho", "aap", "tum", "bhai", "kar", "karo", "sab", "abhi", "kuch", "aur",
        "bhi", "toh", "yeh", "woh", "kab", "kahan", "gaya", "gayi",
    }),
    ROMAN_TELUGU: frozenset({"ela", "chala", "andi", "kada", "kadha", "sare", "mee", "naa"}),
}


def detect_reply_language(text: str) -> str:
    """Return one of SUPPORTED_LANGUAGES for the viewer's message (English when unsure)."""
    if not text:
        return ENGLISH
    if _TELUGU_SCRIPT.search(text):
        return TELUGU
    if _DEVANAGARI.search(text):
        return HINDI

    words = re.findall(r"[a-z']+", text.lower())
    best, best_score = ENGLISH, 0
    for language in (HINGLISH, ROMAN_TELUGU):
        strong = sum(1 for w in words if w in _STRONG_MARKERS[language])
        weak = sum(1 for w in words if w in _WEAK_MARKERS[language])
        if strong == 0 and weak < 2:
            continue
        score = 2 * strong + weak
        if score > best_score:
            best, best_score = language, score
        elif score == best_score:
            best = ENGLISH  # an exact tie between the two is too ambiguous to call
    return best
