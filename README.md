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
                 │           AI Engine            │
                 │ (OpenRouter + Gemini, budgeted)│
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
   Resolves users to canonical keys (`platform:user_id`): `discord:<snowflake>` for Discord and `youtube:<channel ID>` for YouTube, taken from the live-chat `authorDetails.channelId`. Display names are transient attributes and never identity keys; two viewers with the same name stay separate. Explicit cross-platform links are persisted (see [Identity & Cross-Platform Links](#-identity--cross-platform-links)).

3. **Dual-Scope Memory System (`services/memory_service.py`)**:
   - **Temporary Stream Memory**: Facts, ongoing topics, viewer questions specific to the current livestream. Deleted when the stream ends; each stream fact also carries a TTL (6h for automatically extracted facts, 24h default otherwise).
   - **Persistent User Memory**: Long-term preferences (`preferred_name`, `favorite_game`, interaction stats). Backed by SQLite with automatic confidence decay:
     $$\text{confidence}_{\text{effective}} = \text{confidence}_{\text{stored}} \cdot e^{-\frac{\text{age\_days}}{\text{decay\_constant}}}$$
     Features deterministic conflict resolution, capacity limits (max 30 persistent facts per user), and an in-memory fallback if SQLite fails. While in fallback, SQLite is retried every 30 seconds; on recovery the users, facts and identity links saved during the outage are written back (stream-scoped data stays in memory). Messages classified as serious (see Safety) are never mined for profile facts.

4. **Decision Engine (`services/decision_service.py`)**:
   Deterministic hard rules (filter configured bot users and banned words; duplicate YouTube messages are dropped earlier by the chat monitor's LRU) followed by intent detection and weighted priority scoring:
   $$\text{priority} = 0.30 \cdot \text{mention} + 0.20 \cdot \text{question} + 0.10 \cdot \text{newcomer} + 0.15 \cdot \text{context} + 0.10 \cdot \text{relationship} + 0.10 \cdot \text{urgency} + 0.05 \cdot \text{randomness}$$
   Includes response frequency budgeting and Jaccard-similarity anti-repetition fingerprinting ($\ge 0.70$ threshold rejection). Distressed messages are classified as `crisis` (self-harm, suicidal phrasing) or `sensitive` (grief, depression, panic, a bad day) before any other intent, with first-person patterns that ignore gaming hyperbole such as "kill myself laughing"; both count as urgent.

5. **Safety Layer & Prompt Injection Defense (`services/safety_service.py`)**:
   - Pre-generation input filtering for prompt injection patterns (`ignore previous instructions`, `DAN mode`, `reveal system prompt`). Viewer messages, remembered facts, recent chat and web evidence are passed to the model only inside blocks the system prompt declares untrusted.
   - Post-generation output enforcement (no stage directions such as `*smiles*`, `(smiles)` or `[laughs]`, max 1 emoji, single sentence limit, no secret/prompt leakage).
   - In-character, deterministic fallbacks based on intent (`"Welcome in, chat."`, `"Give me a second, chat."`). Serious messages get caring fallbacks instead (the crisis one points to someone they trust or a local crisis helpline), and the model is told to drop all teasing for them.
   - Discord and YouTube share this contract: when the orchestrator declines to answer (blocked input, hard rule, decision), Discord stays silent (`/ask` shows a neutral ephemeral notice) rather than retrying the text through a raw model call.

6. **Token-Bucket Rate Limiter (`services/rate_limiter.py`)**:
   Independent token buckets for `global_ai`, `user_ai`, `youtube_send`, `idle_chat`, and `discord_ai`. YouTube messages use `global_ai`/`user_ai`; Discord mentions (which also have a per-user cooldown) and `/ask` use `discord_ai` (`RATE_LIMIT_DISCORD_CAPACITY`). Idle per-user buckets are pruned, so state stays bounded.

7. **YouTube Chat Monitor (`services/chat_monitor.py`)**:
   Quota-aware background polling task with bounded LRU message deduplication (max 5000 entries), explicit stream session tracking, quota exhaustion shutdown, and exponential backoff on transient network errors.

---

## ⚙️ Environment Variables & Configuration

Configure these variables in your `.env` file or hosting environment (e.g. Render):

| Variable | Description | Default | Required |
|---|---|---|:---:|
| `DISCORD_TOKEN` | Discord bot token | - | **Yes** |
| `OPENROUTER_API_KEY` | OpenRouter API key | - | Yes, unless Gemini-only |
| `OPENROUTER_MODEL` | OpenRouter model | `deepseek/deepseek-r1` | No |
| `OPENROUTER_ENDPOINT` | OpenRouter chat completions URL (AI engine) | `https://openrouter.ai/api/v1/chat/completions` | No |
| `GEMINI_API_KEY` | Google Gemini API key | - | For search / Gemini |
| `GEMINI_MODEL` | Gemini model with search grounding | `gemini-3.5-flash-lite` | No |
| `GEMINI_SEARCH_ENABLED` | Enable Google Search grounding | `true` | No |
| `GEMINI_SEARCH_DAILY_LIMIT` | Daily cap on dispatched grounded searches | `450` | No |
| `GEMINI_SEARCH_CACHE_TTL` | Search result cache TTL (seconds) | `300` | No |
| `CLIENT_SECRET_JSON` | Google OAuth client secret (JSON string), read by the YouTube service | - | For YouTube |
| `TOKEN_JSON` | Google OAuth authorized-user token (JSON string), read by the YouTube service | - | For YouTube |
| `DB_PATH` | SQLite memory database path | `rukiya_memory.db` | No |
| `PORT` | Health server port (read by `main.py`) | `8080` | No |
| `POLL_INTERVAL` | YouTube chat polling interval (seconds) | `10` | No |
| `SEND_COOLDOWN` | Minimum seconds between YouTube sends | `1.5` | No |
| `MAX_MESSAGE_LENGTH` | Character limit for AI responses | `250` | No |
| `MAX_MEMORY_PER_USER` | Max persistent facts per user | `30` | No |
| `MAX_STREAM_MEMORY` | Max temporary facts per user/stream | `20` | No |
| `MAX_CONTEXT_MESSAGES` | Recent messages included in prompt context | `8` | No |
| `MEMORY_DECAY_DAYS` | Memory confidence decay constant (days) | `30.0` | No |
| `RESPONSE_THRESHOLD` | Priority threshold to respond | `0.50` | No |
| `ANTI_REPEAT_THRESHOLD` | Jaccard similarity threshold to reject repeated output | `0.70` | No |
| `PROCESSED_MESSAGES_MAX` | Deduplicated YouTube message IDs kept in memory | `5000` | No |
| `IDLE_CHAT_ENABLED` | Automated idle livestream messages | `true` | No |
| `IDLE_CHAT_INTERVAL` | Seconds between idle livestream messages | `180` | No |
| `RATE_LIMIT_GLOBAL_CAPACITY` | `global_ai` bucket burst size | `10` | No |
| `RATE_LIMIT_USER_CAPACITY` | `user_ai` bucket burst size | `2` | No |
| `RATE_LIMIT_IDLE_CAPACITY` | `idle_chat` bucket burst size | `1` | No |
| `RATE_LIMIT_DISCORD_CAPACITY` | `discord_ai` bucket burst size (Discord mentions and `/ask`) | `5` | No |
| `RUKIYA_TEMP` / `AI_TEMPERATURE` | Generation temperature (`RUKIYA_TEMP` wins) | `0.85` | No |
| `RUKIYA_MAX_COMPLETION_TOKENS` | Max completion tokens for the AI engine (clamped 64-2000) | `320` | No |
| `RUKIYA_REASONING_ENABLED` | Request provider reasoning | `true` | No |
| `RUKIYA_REASONING_EFFORT` | Reasoning effort | `medium` | No |
| `OPENROUTER_TIMEOUT` / `GEMINI_TIMEOUT` / `SEARCH_TIMEOUT` | Provider timeouts (seconds) | `20` / `20` / `15` | No |
| `AI_MAX_REPAIR_ATTEMPTS` | Max self-critic repair generations | `2` | No |
| `MAX_AI_CONCURRENCY` | Max concurrent provider generations | `3` | No |
| `MAX_SEARCH_CONCURRENCY` | Max concurrent searches | `2` | No |
| `AI_COOLDOWN` | Minimum seconds between responses, **legacy `AIService` path only** (the AI engine path uses the rate limiter) | `5` | No |
| `RUKIYA_AUTO_REPLY` | YouTube auto-reply enabled at startup | `true` | No |
| `RUKIYA_COOLDOWN` / `RUKIYA_DISCORD_COOLDOWN` | YouTube reply cooldown / per-user Discord cooldown (seconds) | `3.0` / `2.0` | No |
| `RUKIYA_MODEL` / `RUKIYA_MAX_TOKENS` / `OPENROUTER_BASE` | Model, token limit and base URL for the cog's direct OpenRouter call (legacy fallback and owner `!rukiya ask`) | `OPENROUTER_MODEL` / `180` / `https://openrouter.ai/api/v1` | No |

Invalid numeric values are ignored per variable (a warning names the variable and the default is kept), so one typo no longer resets unrelated settings.
`AI_MAX_PLANNER_STEPS`, `CHAT_CHECK_INTERVAL`, `CRITIC_TIMEOUT`, `REPAIR_TIMEOUT`, `BOT_NAME` and `YOUTUBE_VIDEO_ID` are parsed but currently not used by any code path (`/start` takes the video ID as an argument).

---

### 🧠 Rukiya Advanced AI Engine (OpenRouter + Gemini + Google Search Grounding)

Rukiya integrates a production-hardened multi-provider control layer:
- **OpenRouter**: Primary provider for conversation, personality responses, memory-driven chat, and final Rukiya-style persona rendering.
- **Gemini (`gemini-3.5-flash-lite`)**: Grounded generation and Google Search integration via the official `google-genai` SDK and `google_search` grounding tool.
- **Independent Capabilities**: `gemini_generation` and `gemini_search` maintain separated circuit breakers, health decay, and capability states (`AVAILABLE`, `RATE_LIMITED`, `QUOTA_EXHAUSTED`, etc.). A search outage never disables generation.
- **Fail-Fast 429 & Negative Caching**: HTTP 429 rate limits fail fast without repeated immediate retries and are negatively cached to prevent provider hammering; transient server errors such as 503 may retry with bounded backoff.
- **Accurate Fallback Telemetry**: When search is degraded and OpenRouter generates the answer, telemetry transparently records `planned_route="hybrid"`, `executed_route="degraded_hybrid"`, `fallback_used=True`, `fallback_reason="gemini_search_rate_limited"`, and `verified_current_information=False`.
- **Anti-Hallucination Boundaries**: When real-time verification fails, the prompt compiler injects explicit non-fabrication directives and the Self-Critic enforces transparent admission rather than unverified live price/news claims.
- **In-flight Deduplication & Search Caching**: Identical concurrent queries join an active in-flight task to eliminate redundant API calls.
- **Circuit Breakers & Daily Budgeting**: Adaptive cooldowns (60s for 429, 300s for quota, 30s for server error) with graceful shutdown hooks (`aclose()`).
- **Live Diagnostic**: Run `python scripts/verify_gemini_search.py` to independently test live provider generation and search capabilities.

### 💰 AI Budget Semantics (`services/ai_engine/budget.py`)

All counters reset together at the **UTC** day boundary.

| Counter | Counts | Does not count |
|---|---|---|
| `search_count` (cap `GEMINI_SEARCH_DAILY_LIMIT`) | Grounded searches **dispatched** to Gemini, successful or not | Cache hits, in-flight dedup joins, negative-cache hits |
| `openrouter_count` + `gemini_count` (internal cap `global_daily_limit`, default 5000) | Provider operations dispatched: generations including backups and repair calls (success or failure), plus searches | Calls refused because a cap was reached; calls to unconfigured providers |
| `user_counts` (internal cap `user_daily_limit`, default 100) | **One unit per viewer request that a provider served**, however many operations it took | Requests answered by a static fallback (the reserved unit is refunded, also on cancellation) |

Every cap is reserved with a check-and-increment that runs with no `await` between the check and the dispatch, so concurrent requests cannot overshoot a cap. When the global cap is the reason nothing answered, telemetry reports `fallback_reason="budget_exhausted"`.

---

## 🔗 Identity & Cross-Platform Links

- **Canonical IDs** are `discord:<snowflake>` and `youtube:<channel ID>`. Without a channel ID (legacy callers), YouTube falls back to the username key used before.
- **Explicit links** (`IdentityService.link_identities(primary, secondary)`) map a secondary account to a primary one and are stored in the SQLite `identity_links` table (created automatically, additive migration), so they survive restarts.
- **Rules**: only stable account IDs can be linked (Discord snowflakes, `UC…` channel IDs; never display names or fallback keys); links must be cross-platform; no chains; a secondary already linked elsewhere must be unlinked first (re-linking the same pair is idempotent); a different link written by another process is reported as a conflict, never overwritten; stored rows are re-validated on load.
- **Persistence reporting**: `link_identities()` / `unlink_identity()` return `True` only when SQLite was updated, `False` when the change applies to the running process only (database unavailable). A failed unlink takes effect immediately and can be retried.
- **Memory**: linking never moves or merges memories. The secondary keeps its own rows, which become reachable again after unlinking.
- No Discord command creates links yet. Exposing one (e.g. owner-only) is a product decision left to the owner.

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

The web server will start on port `8080` (or `$PORT`) exposing `GET /` and `GET /health`. Both always return HTTP 200 (the host's liveness probe) with a JSON body that reports degradation:

```json
{"status": "ok", "discord_ready": true, "failed_cogs": [], "memory_fallback_mode": false, "youtube_monitoring": false}
```

`status` becomes `"degraded"` when a cog failed to load (the full traceback is in the log) or the memory service is in fallback.

---

## 🧪 Testing

The repository includes a broad unit and regression test suite that does not require real API credentials or production OAuth tokens.

Run exactly what CI runs (Python 3.11):
```bash
python -m compileall -q .
python -m unittest discover -s tests -p "test_*.py" -v
```

No test makes a live provider call; providers are stubbed. Test modules should not insert stubs into `sys.modules` at import time: discovery imports every module before running any test, so a stub leaks into the whole suite (a guard test catches a stubbed `google` package).

Run specific test modules:
```bash
# Goal regressions (Quota handling, safety contracts, sleep intervals)
python -m unittest tests/test_goal_regressions.py

# Discord trigger tests
python -m unittest tests/test_discord_triggers.py

# Slash command tests
python -m unittest tests/test_slash_commands.py

# V2 Decision Engine
python -m unittest tests/test_v2_decision.py

# V2 Dual-Scope Memory & Identity
python -m unittest tests/test_v2_memory_and_identity.py

# V2 Safety & Rate Limiting
python -m unittest tests/test_v2_safety_and_ratelimit.py

# V2 Orchestrator Pipeline
python -m unittest tests/test_v2_orchestrator.py

# V2 Invariants & Security (Prompt injection, leak prevention)
python -m unittest tests/test_v2_invariants_and_security.py

# Safety contract (serious messages, stage directions, malformed provider output)
python -m unittest tests/test_v2_safety_contract.py

# AI budget semantics (caps, concurrency, rollover)
python -m unittest tests/test_ai_engine_budget_semantics.py

# Runtime resilience (config parsing, bounded state, DB recovery, health, shutdown)
python -m unittest tests/test_v2_runtime_limits.py tests/test_v2_memory_recovery.py tests/test_v2_runtime_status.py
```

### Reading provider errors

Each AI engine request logs one `event=ai_engine_result` line with `planned_route`, `executed_route`, `provider`, `fallback`, and `fallback_reason`. Common reasons:

| `fallback_reason` | Meaning |
|---|---|
| `gemini_search_rate_limited` / `gemini_search_quota_exhausted` | Search failed fast (HTTP 429 / quota or the daily search cap); the answer was generated without fresh data and the model was told not to invent current facts |
| `openrouter_failed`, `gemini_generation_failed`, `all_providers_failed` | Provider calls failed; see the preceding `event=provider_failure` line for category and status code |
| `budget_exhausted` | A daily AI cap was reached; no provider was called |
| `static_fallback` | No provider was available at routing time (both unconfigured, circuit-open, or over budget) |
| `exception` | Unexpected error inside the engine; the log has the traceback |
| `critic_rejected`, `critic_repair_exhausted`, `output_safety_rejected` | The reply failed the critic or output validator and a safe fallback was sent |

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
| `/ask <question> [post_to_yt]` | Everyone* | Ask Rukiya a question; posting to YT requires Administrator |
| `/say <text>` | Administrator | Send a raw message directly to active YouTube live chat |
| `/auto_reply [action]` | Administrator | Check status, enable, or disable YouTube auto-responder |
| `/rukiya_info` | Everyone | View Rukiya's character profile and system settings |
| `/start <video_id>` | Administrator | Start monitoring YouTube live chat for a livestream |
| `/stop` | Administrator | Stop monitoring YouTube live chat |
| `/yt_status` | Everyone | Check live status of YouTube chat monitor |
| `/ping` | Everyone | Check bot websocket latency |
| `/uptime` | Everyone | View bot uptime duration |
| `/help` | Everyone | Comprehensive command guide |
| `/welcome_send [text]` | Administrator | Send a welcome message to YouTube live chat (Discord fallback, mentions disabled) |
| `/shayari_send [index]` | Everyone | Post a predefined shayari to YouTube live chat (bounded by the `youtube_send` bucket) |
| `/shayari_list` | Everyone | List the predefined shayaris |
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
- **Behavior**: HTTP 429 fails fast and is classified as a rate-limit event so the engine can switch capability/routes immediately. HTTP 503 remains retryable with bounded exponential backoff.

### 2. YouTube Quota Exceeded (`quotaExceeded`)
- **Symptom**: `YouTube quota exhausted; monitoring stops without retry`
- **Behavior**: Rukiya detects quota exhaustion and immediately halts polling without hammering the API. Discord functionality remains completely unaffected.

### 3. YouTube OAuth Issues
- **Symptom**: `invalid_grant: Token has been expired or revoked.`
- **Fix**: Re-authorize OAuth credentials and update the `TOKEN_JSON` environment variable.

### 4. Database Resilience
- If SQLite encounters a disk or permission issue, `MemoryService` logs a warning and switches to in-memory fallback so chat and Discord keep working. `/health` reports `memory_fallback_mode: true`.
- SQLite is retried every 30 seconds. On recovery, users, facts and identity links saved during the outage are written back and the log says `MemoryService recovered SQLite persistence`.

---

## ⚠️ Known Limitations

- Memories stored before YouTube identities switched to channel IDs are keyed by display name and are not migrated (doing it by name would be the unsafe merge this avoids). Returning YouTube viewers start with a fresh profile once.
- No command creates identity links yet (see Identity & Cross-Platform Links).
- Deletes made while the database is unavailable (memory reset, unlink) are not replayed on recovery; unlink reports `persisted=False` so it can be retried.
- Serious-message detection is pattern-based (English and some Roman Hindi). It cannot catch every phrasing, and crisis fallbacks are English-only with a non-region-specific helpline mention.
- `/shayari_send` lets any member post a predefined shayari to YouTube chat; restricting it would change public command behavior and is left to the owner.
- Live Discord/YouTube behavior is verified by unit tests with stubs, not by an automated end-to-end run against real platforms.


> Permission note: `/ask` is available to everyone, but `post_to_yt=true`, `/say`, `/start`, `/stop`, and `/auto_reply` require server Administrator permissions.
