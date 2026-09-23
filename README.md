# 🗡️ Rukiya V2 — Discord & YouTube Livestream AI Bot

Rukiya is an advanced, production-ready AI bot designed for concurrent YouTube livestream chat interaction and Discord server engagement. Inspired by Kuchiki Rukiya from *Bleach*, she features a sharp, composed, livestream-native tsundere persona that is warm beneath teasing, responsive, and safe.

---

## 🏛️ V2 Architecture Overview

Rukiya V2 introduces a major architectural evolution by establishing strict service boundaries and adhering to the core principle:

> **DECISION ≠ GENERATION**
>
> Eligibility, priority scoring, frequency budgeting, and intent classification are decoupled from prose generation. The LLM is never responsible for bot control decisions.

```
                 ┌────────────────────────────────┐
                 │ Discord Events & Slash Commands│
                 │ YouTube Live Chat Poller       │
                 └───────────────┬────────────────┘
                                 │
                 ┌───────────────▼────────────────┐
                 │      Rukiya Orchestrator       │
                 │   (services/orchestrator.py)   │
                 └───────────────┬────────────────┘
                                 │
    ┌────────────────┬───────────┴──────────┬────────────────┐
    ▼                ▼                      ▼                ▼
┌──────────────┐ ┌────────────────┐ ┌──────────────┐ ┌──────────────┐
│   Identity   │ │     Memory     │ │   Decision   │ │    Safety    │
│   Service    │ │    Service     │ │    Engine    │ │   Service    │
│(platform:uid)│ │(Stream+Persist)│ │(Weighted+RNG)│ │(Input+Output)│
└──────────────┘ └────────────────┘ └──────────────┘ └──────────────┘
                                 │
                 ┌───────────────▼────────────────┐
                 │          Rate Limiter          │
                 │   (Token Buckets per scope)    │
                 └───────────────┬────────────────┘
                                 │
                 ┌───────────────▼────────────────┐
                 │       AI Response Layer        │
                 │    (OpenRouter / LLM only)     │
                 └───────────────┬────────────────┘
                                 │
                 ┌───────────────┴────────────────┐
                 ▼                                ▼
┌─────────────────────────────────┐ ┌─────────────────────────────────┐
│         YouTube Service         │ │         Discord Service         │
│     (send_message, bounded)     │ │        (cogs / channels)        │
└─────────────────────────────────┘ └─────────────────────────────────┘
```

### Key Subsystems

1. **Rukiya Orchestrator (`services/orchestrator.py`)**:
   Coordinates message lifecycle: normalization → identity resolution → input safety validation → context retrieval → decision evaluation → rate limiting → AI generation → output safety validation → anti-repetition filter → response transmission → memory recording & fact extraction.

2. **Identity Service (`services/identity_service.py`)**:
   Resolves users to canonical keys (`platform:user_id`, e.g., `youtube:UC...` or `discord:12345...`). Display names are treated as transient attributes and never used as unique identity keys.

3. **Dual-Scope Memory System (`services/memory_service.py`)**:
   - **Temporary Stream Memory**: Facts, ongoing topics, viewer questions specific to the current livestream. Auto-expires when the stream ends (or 24h safety TTL).
   - **Persistent User Memory**: Long-term preferences (`preferred_name`, `favorite_game`, interaction stats). Backed by SQLite with automatic confidence decay:
     $$\text{confidence}_{\text{effective}} = \text{confidence}_{\text{stored}} \cdot e^{-\frac{\text{age\_days}}{\text{decay\_constant}}}$$
     Features deterministic conflict resolution, capacity limits (max 30 persistent facts per user), and automatic in-memory fallback if SQLite encounters I/O errors.

4. **Decision Engine (`services/decision_service.py`)**:
   Deterministic hard rules (filter bots, duplicates, banned words) followed by weighted priority scoring:
   $$\text{priority} = 0.30 \cdot \text{mention} + 0.20 \cdot \text{question} + 0.10 \cdot \text{newcomer} + 0.15 \cdot \text{context} + 0.10 \cdot \text{relationship} + 0.10 \cdot \text{urgency} + 0.05 \cdot \text{randomness}$$
   Includes response frequency budgeting and Jaccard-similarity anti-repetition fingerprinting ($\ge 0.70$ threshold rejection).

5. **Safety Layer & Prompt Injection Defense (`services/safety_service.py`)**:
   - Pre-generation input filtering for prompt injection patterns (`ignore previous instructions`, `DAN mode`, `reveal system prompt`).
   - Post-generation output enforcement (no stage directions like `*smiles*`, max 1 emoji, single sentence limits, no secret/prompt leakage).
   - In-character, deterministic fallbacks based on intent (`"Welcome in, chat."`, `"Give me a second, chat."`).

6. **Token-Bucket Rate Limiter (`services/rate_limiter.py`)**:
   Independent token buckets for `global_ai`, `user_ai`, `youtube_send`, `idle_chat`, and `discord_ai`.

7. **YouTube Chat Monitor (`services/chat_monitor.py`)**:
   Quota-aware background polling task with bounded LRU message deduplication (max 5000 entries), explicit stream session tracking, quota exhaustion shutdown, and exponential backoff on transient network errors.

---

## ⚙️ Environment Variables & Configuration

Configure these variables in your `.env` file or hosting environment (e.g. Render):

| Variable | Description | Default | Required |
|---|---|---|:---:|
| `DISCORD_TOKEN` | Discord Bot Token | - | **Yes** |
| `OPENROUTER_API_KEY` | OpenRouter API Key | - | **Yes** |
| `OPENROUTER_MODEL` | LLM Model Name | `deepseek/deepseek-r1` | No |
| `CLIENT_SECRET_JSON` | Google OAuth Client Secret (JSON string) | - | For YouTube |
| `TOKEN_JSON` | Google OAuth Authorized User Token (JSON string) | - | For YouTube |
| `YOUTUBE_VIDEO_ID` | Default YouTube Live Video ID | `""` | No |
| `DB_PATH` | Path to SQLite memory database | `rukiya_memory.db` | No |
| `POLL_INTERVAL` | YouTube chat polling interval (seconds) | `10` | No |
| `AI_COOLDOWN` | Minimum seconds between AI responses | `5` | No |
| `MAX_MESSAGE_LENGTH` | Character limit for AI responses | `250` | No |
| `MAX_MEMORY_PER_USER` | Max persistent facts per user | `30` | No |
| `MAX_STREAM_MEMORY` | Max temporary facts per user/stream | `20` | No |
| `MAX_CONTEXT_MESSAGES` | Number of recent messages in prompt context | `8` | No |
| `MEMORY_DECAY_DAYS` | Memory confidence decay constant (days) | `30.0` | No |
| `RESPONSE_THRESHOLD` | Priority threshold to respond | `0.50` | No |
| `ANTI_REPEAT_THRESHOLD`| Jaccard similarity threshold to reject repeated output | `0.70` | No |
| `PROCESSED_MESSAGES_MAX`| Max deduplicated messages kept in memory | `5000` | No |
| `IDLE_CHAT_ENABLED` | Enable automated idle livestream messages | `true` | No |
| `IDLE_CHAT_INTERVAL` | Seconds between idle livestream messages | `180` | No |
| `GEMINI_API_KEY` | Google Gemini API Key | - | For Search/Gemini |
| `GEMINI_MODEL` | Gemini Model with Search Grounding | `gemini-3.5-flash-lite` | No |
| `GEMINI_SEARCH_ENABLED` | Enable Google Search Grounding | `true` | No |
| `GEMINI_SEARCH_DAILY_LIMIT`| Daily Search Grounding Safety Allowance | `450` | No |
| `GEMINI_SEARCH_CACHE_TTL`| Search Grounding Cache TTL (seconds) | `300` | No |
| `AI_MAX_REPAIR_ATTEMPTS` | Max Self-Critic Repair Iterations | `2` | No |
| `AI_MAX_PLANNER_STEPS` | Max Steps for Query Planning | `4` | No |
| `MAX_AI_CONCURRENCY` | Max Concurrent LLM Provider Requests | `3` | No |
| `MAX_SEARCH_CONCURRENCY` | Max Concurrent Search Provider Requests | `2` | No |

---

### 🧠 Rukiya Advanced AI Engine (OpenRouter + Gemini + Google Search Grounding)

Rukiya integrates a production-hardened multi-provider control layer:
- **OpenRouter**: Primary provider for conversation, personality responses, memory-driven chat, and final Rukiya-style persona rendering.
- **Gemini (`gemini-3.5-flash-lite`)**: Grounded generation and Google Search integration via the official `google-genai` SDK and `google_search` grounding tool.
- **Independent Capabilities**: `gemini_generation` and `gemini_search` maintain separated circuit breakers, health decay, and capability states (`AVAILABLE`, `RATE_LIMITED`, `QUOTA_EXHAUSTED`, etc.). A search outage never disables generation.
- **Fail-Fast 429 & Negative Caching**: HTTP 429 rate limit or quota errors fail fast in milliseconds (no 15s timeout wait) and are negatively cached to prevent provider hammer.
- **Accurate Fallback Telemetry**: When search is degraded and OpenRouter generates the answer, telemetry transparently records `planned_route="hybrid"`, `executed_route="degraded_hybrid"`, `fallback_used=True`, `fallback_reason="gemini_search_rate_limited"`, and `verified_current_information=False`.
- **Anti-Hallucination Boundaries**: When real-time verification fails, the prompt compiler injects explicit non-fabrication directives and the Self-Critic enforces transparent admission rather than unverified live price/news claims.
- **In-flight Deduplication & Search Caching**: Identical concurrent queries join an active in-flight task to eliminate redundant API calls.
- **Circuit Breakers & Daily Budgeting**: Adaptive cooldowns (60s for 429, 300s for quota, 30s for server error) with graceful shutdown hooks (`aclose()`).
- **Live Diagnostic**: Run `python scripts/verify_gemini_search.py` to independently test live provider generation and search capabilities.
---

## 💻 Local Development & Installation

### Prerequisites
- Python 3.10+ (Python 3.11+ recommended)
- Git

### 1. Clone & Setup Environment
```bash
git clone https://github.com/rukia3287-jpg/rukiya.git
cd rukiya
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment
Create a `.env` file in the project root:
```env
DISCORD_TOKEN=your_discord_bot_token_here
OPENROUTER_API_KEY=your_openrouter_api_key_here
OPENROUTER_MODEL=deepseek/deepseek-r1
```

### 3. Run the Bot
```bash
python main.py
```

The web server will start on port `8080` (or `$PORT`) exposing:
- `GET /` → `Bot is running!`
- `GET /health` → `Bot is running!` (HTTP 200)

---

## 🧪 Testing

The repository features comprehensive automated test coverage (54 unit and regression tests) that do not require real API credentials or production OAuth tokens:

Run the entire test suite:
```bash
python -m pytest
```

Run specific test modules:
```bash
# Goal regressions (Quota handling, safety contracts, sleep intervals)
python -m pytest tests/test_goal_regressions.py

# Discord trigger tests
python -m pytest tests/test_discord_triggers.py

# Slash command tests
python -m pytest tests/test_slash_commands.py

# V2 Decision Engine
python -m pytest tests/test_v2_decision.py

# V2 Dual-Scope Memory & Identity
python -m pytest tests/test_v2_memory_and_identity.py

# V2 Safety & Rate Limiting
python -m pytest tests/test_v2_safety_and_ratelimit.py

# V2 Orchestrator Pipeline
python -m pytest tests/test_v2_orchestrator.py

# V2 Invariants & Security (Prompt injection, leak prevention)
python -m pytest tests/test_v2_invariants_and_security.py
```

---

## 🚀 Deployment (Render)

The bot is fully configured for deployment on [Render](https://render.com) using `render.yaml`.

1. Connect your repository on Render as a Web Service.
2. Ensure the following environment variables are configured in the Render Dashboard:
   - `DISCORD_TOKEN`
   - `OPENROUTER_API_KEY`
   - `CLIENT_SECRET_JSON` (Google OAuth client secret JSON)
   - `TOKEN_JSON` (Google OAuth token JSON)
3. Render uses:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
   - **Health Check Path**: `/health`

---

## 🛡️ Discord Slash Commands

| Command | Permission | Description |
|---|---|---|
| `/ask <question> [post_to_yt]` | Everyone | Ask Rukiya a question (optionally mirror to YT chat) |
| `/say <text>` | Everyone | Send a raw message directly to active YouTube live chat |
| `/auto_reply [action]` | Everyone | Check status, enable, or disable YouTube auto-responder |
| `/rukiya_info` | Everyone | View Rukiya's character profile and system settings |
| `/start <video_id>` | Everyone | Start monitoring YouTube live chat for a livestream |
| `/stop` | Everyone | Stop monitoring YouTube live chat |
| `/yt_status` | Everyone | Check live status of YouTube chat monitor |
| `/ping` | Everyone | Check bot websocket latency |
| `/uptime` | Everyone | View bot uptime duration |
| `/help` | Everyone | Comprehensive command guide |
| `/test_ai <message>` | Admin Only | Safe direct test of AI generation with secret masking |
| `/test_trigger <message>` | Admin Only | Dry-run decision engine breakdown for a message |
| `/bot_status` | Admin Only | Full diagnostic dashboard of all V2 subsystems |
| `/memory_lookup <user>` | Admin Only | Inspect stored persistent facts for a user |
| `/memory_reset <user>` | Admin Only | Reset all stored persistent facts for a user |
| `/rate_limit_status` | Admin Only | View real-time token bucket capacities |

---

## 🔧 Troubleshooting

### 1. OpenRouter API Errors
- **Symptom**: `OPENROUTER_API_KEY not set. AIService disabled.`
- **Fix**: Verify `OPENROUTER_API_KEY` is present in your `.env` or Render environment.
- **Symptom**: HTTP 429 / 503 from OpenRouter.
- **Fix**: The orchestrator automatically uses safe in-character fallbacks (`"Give me a second, chat."`) and applies exponential backoff up to 3 retries without crashing the bot.

### 2. YouTube Quota Exceeded (`quotaExceeded`)
- **Symptom**: `YouTube quota exhausted; monitoring stops without retry`
- **Behavior**: Rukiya detects quota exhaustion and immediately halts polling without hammering the API. Discord functionality remains completely unaffected.

### 3. YouTube OAuth Issues
- **Symptom**: `invalid_grant: Token has been expired or revoked.`
- **Fix**: Re-authorize OAuth credentials and update the `TOKEN_JSON` environment variable.

### 4. Database Resilience
- If SQLite encounters any disk or permission issues, `MemoryService` logs a warning and automatically switches to in-memory fallback mode so chat and Discord interactions continue seamlessly.
