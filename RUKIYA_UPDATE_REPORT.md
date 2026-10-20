# Rukiya audit and update report

Repository: `rukia3287-jpg/rukiya`  
Baseline reviewed: `00ff253152a239d38520eb84ff7f65ef7703734c`  
Date: 2026-10-20

## Findings and implemented fixes

| Problem observed in source or tests | Result after this update |
| --- | --- |
| Recent chat could share `default_session` or the active YouTube session with Discord | Discord history is scoped by guild/channel or DM; YouTube history uses its monitored stream/video. Request-local scope survives concurrent async processing. |
| Discord persistent facts used a global platform ID across guilds | Facts and profiles are scoped to the guild, or privately to the DM user. YouTube retains its stable channel-ID namespace. |
| Any server administrator could control the one global YouTube account and inspect arbitrary memory | Application owner and explicitly configured `OPERATOR_IDS` control global commands; guild administrator alone grants no global access. Checks apply to slash dispatch, direct callbacks, prefix commands and owned operator buttons. |
| Public shayari posting and automated welcome bridging could unexpectedly send to YouTube | Manual YouTube posting requires an operator. Member-join crossposting additionally requires the guild in `WELCOME_YOUTUBE_GUILD_IDS`. |
| A Cog error listener was not wired to the slash command tree | `bot.tree.on_error` handles actual dispatch failures; component views use the same handler. Private notices carry a reference matching the traceback and recent operator dashboard entry. |
| Commands left deferred results pending or could acknowledge twice during refresh | Shared response helpers defer once and finish/edit the original result. Slash outputs are private by default. |
| Memory reset executed immediately | A private 60-second Confirm/Cancel preview identifies the exact target. Confirmation rechecks the original operator, rejects replay and revocation, and deletes only persistent facts. |
| Help and diagnostics were static; ping could raise before gateway latency was available | Help has an owned category selector; bot and YouTube status panels have owned refresh/close controls. Ping shows “Connecting…” while latency is unknown. |
| SQLite work could stall Discord's event loop | Orchestrator memory work, diagnostic reads, health checks, admin memory operations and production monitor session lifecycle run in worker threads. Memory state is serialized; repeated cancellation waits for an ongoing write before another worker enters. |
| AI repair silently restored default verification flags | Local cleanup and every model repair keep the search-failure and verification flags. A failed live-price check cannot become a passing answer just because repair ran. |
| Critic only checked unverified prices | Added checks for common unsupported current factual claims, while retaining honest inability-to-verify responses and bounded repairs. This is a heuristic safeguard, not a guarantee of factual correctness. |
| Prefix `!rukiya ask` skipped the main pipeline | It now uses the scoped orchestrator when available, preserves pipeline refusals, and reports failures without echoing exception details. |
| Routine logs included full replies and configured secrets could appear in tracebacks | Orchestrator INFO logs and successful YouTube reply logs use metadata instead of chat text. Output formatters redact configured API tokens and secret fields inside OAuth JSON, including traceback text. |
| Command sync failure was only a log entry | `/bot_status` and `/health` report the failure; health body becomes degraded. The liveness endpoint still returns HTTP 200. |
| Member welcomes lacked the Discord members intent | Intent is available through explicit opt-in, with status guidance. Default remains off so existing installations can start without a newly required privileged intent. |

## Setup and compatibility

1. The Discord application's owner retains access automatically. Set `OPERATOR_IDS=123456789012345678,234567890123456789` to grant additional trusted users global bot controls. These are user IDs, not role IDs. An operator can inspect any stored scope; grant access accordingly.
2. Use `/help`, `/bot_status`, `/yt_status`, and `/rate_limit_status` to inspect the running bot. Normal chat, `/ask` without crossposting, `/rukiya_info`, `/ping`, `/uptime`, `/help`, and `/shayari_list` remain available to everyone. Slash replies are private; ordinary channel mention replies remain in their channel.
3. For automatic member welcomes, enable **Server Members Intent** in the Discord Developer Portal and set `DISCORD_MEMBER_WELCOMES_ENABLED=true`. Message-content intent is also required for text/name triggers. For optional member-join forwarding to YouTube, set `WELCOME_YOUTUBE_GUILD_IDS` to a comma-separated list of guild IDs. It defaults to empty.
4. New Discord identities look like `scope:discord:guild:GUILD_ID|discord:USER_ID` or `scope:discord:dm:USER_ID|discord:USER_ID`. Conversation history additionally separates guild channels. Explicit identity links resolve inside the current Discord scope; they do not import global YouTube facts into a guild or DM.
5. Legacy rows are retained without automatic copying: old global Discord history/facts might contain private data from another context. `/memory_lookup user_id:discord:USER_ID scope:legacy` can inspect those rows privately. `/memory_reset` offers the same selector and requires confirmation. A full scoped canonical ID can target another stored scope explicitly. A reset removes persistent facts only, retaining profiles and conversation history; it is not a full personal-data erasure command.
6. Python callers that omit all Discord guild/channel metadata retain the legacy persistent identity behavior for compatibility. Production Discord adapters now supply it. Custom adapters must supply `guild_id` and `channel_id` to receive the isolation guarantees.
7. SQLite schema and existing rows are preserved. Back up the database before a production upgrade. `DB_PATH` should point to durable storage. The existing free Render blueprint has no persistent disk: local database files can disappear on replacement/redeploy. This update does not provision a paid service or change hosting. In-process fallback and pending writes also disappear if the process stops before storage recovers.

## Validation

- Original suite before changes: **215 tests passed**.
- Updated suite: **250 tests passed**, including 35 new regressions covering actual cog loading and command serialization, global authorization, confirmation replay/revocation, private error handling, UI refresh, memory isolation and concurrent worker cancellation, monitor lifecycle, and verification throughout AI repair.
- `python -m compileall -q main.py cogs services tests`.
- `python -m unittest discover -s tests -p 'test_*.py' -v`.
- `git diff --check`.
- Tests use temporary file databases and mocked provider/Discord/YouTube transports. No live account credentials or paid requests were used. GitHub CI must pass before merge.

## Remaining limits and live verification

- No source review can establish that every external integration works live. After deployment, check `/bot_status`, one private `/ask`, `/test_ai`, then an operator `/start` and a YouTube send on an active stream. API keys, OAuth validity, quotas, bot roles and Developer Portal intent settings remain external requirements.
- Existing AI route selection, provider budgets, fail-fast rate-limit handling, language matching, evidence processing and safety fallbacks remain in place. These changes improve trust boundaries rather than replacing the existing AI engine. Models and web evidence can still be wrong.
- Diagnostic error references are bounded and process-local (last 30 central command/UI errors). Known failures that commands handle directly retain their specific messages/logging. This is not durable alert delivery or an automatic notification subscription.
- Existing pattern-based language/serious-intent detection and global anti-repetition can still misclassify or affect a response. No command creates identity links or performs complete account erasure.
- `DB_PATH=:memory:` is unsuitable for this connection-per-operation SQLite service; use a writable file path. Some legacy tests intentionally exercise this path's dictionary fallback. Durable production storage requires a file on persistent storage.
