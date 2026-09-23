"""scripts/verify_gemini_search.py
Diagnostic tool for live API verification of OpenRouter, Gemini Generation, and Gemini Search Grounding.
Never prints or leaks API keys.
"""
import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from services.config import Config
from services.ai_engine.providers.gemini import GeminiProvider
from services.ai_engine.providers.openrouter import OpenRouterProvider
from services.ai_engine.errors import classify_provider_error


async def main():
    print("=" * 60)
    print("RUKIYA ADVANCED AI ENGINE — LIVE API DIAGNOSTIC")
    print("=" * 60)

    config = Config()

    # 1. OpenRouter Verification
    print("\n[1/3] Testing OpenRouter Generation...")
    or_key = os.getenv("OPENROUTER_API_KEY", "")
    if not or_key:
        print("  -> SKIPPED (OPENROUTER_API_KEY not configured in environment)")
        or_status = "NOT_CONFIGURED"
    else:
        masked = or_key[:4] + "..." + or_key[-4:] if len(or_key) > 8 else "***"
        print(f"  -> Key detected: {masked} | Model: {config.openrouter_model}")
        or_provider = OpenRouterProvider(config)
        messages = [
            {"role": "system", "content": "You are Rukiya. Answer in one short sentence."},
            {"role": "user", "content": "Hello, are you ready for stream?"}
        ]
        res = await or_provider.generate(messages, max_tokens=60)
        if res.text and not res.error:
            print(f"  -> SUCCESS! Response ({res.latency_ms:.0f}ms): {res.text.strip()}")
            or_status = "READY"
        else:
            print(f"  -> FAILED: {res.error}")
            or_status = "FAILED"

    # 2. Gemini Generation Verification
    print("\n[2/3] Testing Gemini Generation (gemini_generation capability)...")
    gem_key = os.getenv("GEMINI_API_KEY", "")
    if not gem_key:
        print("  -> SKIPPED (GEMINI_API_KEY not configured in environment)")
        gem_gen_status = "NOT_CONFIGURED"
        gem_search_status = "NOT_CONFIGURED"
    else:
        masked = gem_key[:4] + "..." + gem_key[-4:] if len(gem_key) > 8 else "***"
        print(f"  -> Key detected: {masked} | Model: {config.gemini_model}")
        gem_provider = GeminiProvider(config)

        gen_msgs = [
            {"role": "system", "content": "Answer in 5 words."},
            {"role": "user", "content": "What color is the sky?"}
        ]
        res_gen = await gem_provider.generate(gen_msgs, max_tokens=30)
        if res_gen.text and not res_gen.error:
            print(f"  -> SUCCESS! Response ({res_gen.latency_ms:.0f}ms): {res_gen.text.strip()}")
            gem_gen_status = "READY"
        else:
            err = classify_provider_error(res_gen.error or "Unknown", provider="gemini", capability="generation")
            print(f"  -> FAILED: {res_gen.error} (category={err.category.value})")
            gem_gen_status = "FAILED"

        # 3. Gemini Search Grounding Verification
        print("\n[3/3] Testing Gemini Search Grounding (gemini_search capability)...")
        if not getattr(config, "gemini_search_enabled", True):
            print("  -> SKIPPED (GEMINI_SEARCH_ENABLED is false)")
            gem_search_status = "DISABLED"
        else:
            query = "latest Genshin Impact version 2026"
            print(f"  -> Executing search grounding query: '{query}'")
            res_search = await gem_provider.search_grounded(query, max_tokens=150)
            if res_search.text and not res_search.error:
                citations_cnt = len(res_search.citations or [])
                print(f"  -> SUCCESS! Grounded search returned {citations_cnt} citations ({res_search.latency_ms:.0f}ms)")
                print(f"  -> Snippet: {res_search.text[:120].strip()}...")
                gem_search_status = "AVAILABLE"
            else:
                err = classify_provider_error(res_search.error or "Unknown", provider="gemini", capability="search")
                print(f"  -> SEARCH FAILED fast: {res_search.error}")
                print(f"  -> Classified Category: {err.category.value} (status_code={err.status_code})")
                if err.status_code == 429:
                    gem_search_status = "RATE_LIMITED"
                else:
                    gem_search_status = "UNAVAILABLE"

    print("\n" + "=" * 60)
    print("DIAGNOSTIC SUMMARY:")
    print(f"  OpenRouter Generation: {or_status}")
    print(f"  Gemini Generation:     {gem_gen_status}")
    print(f"  Gemini Search:         {gem_search_status}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
