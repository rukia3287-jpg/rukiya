"""cogs/admin_commands.py
Admin diagnostics and control commands for Rukiya V2.

All admin commands are permission-protected (Administrator / Owner).
Strict secret masking ensures API keys, tokens, and secrets are NEVER exposed in Discord.
"""
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.models import ChatMessage

logger = logging.getLogger(__name__)


def mask_secret(secret: Optional[str]) -> str:
    """Mask secret so it is never exposed in Discord."""
    if not secret:
        return "❌ NOT SET"
    clean = str(secret).strip()
    if len(clean) <= 6:
        return "✅ [SET]"
    return f"`{clean[:3]}...{clean[-3:]}` (Valid)"


class AdminCommands(commands.Cog):
    """Admin utilities: AI test, trigger debug, memory inspection, bot status"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_unload(self):
        pass

    # ────────────────────────────────────────────
    # /test_ai  — safe test of AI generation
    # ────────────────────────────────────────────
    @app_commands.command(name="test_ai", description="Test Rukiya's AI response directly (bypasses trigger rules)")
    @app_commands.describe(message="Message to test AI generation with")
    @app_commands.default_permissions(administrator=True)
    async def test_ai(self, interaction: discord.Interaction, message: str):
        """Directly calls AI service or orchestrator with Rukiya persona."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as e:
            logger.exception("Failed to defer /test_ai: %s", e)
            return

        ai = getattr(self.bot, "ai_service", None)
        orch = getattr(self.bot, "orchestrator", None)

        if not ai and not orch:
            await interaction.followup.send(
                "❌ Neither `ai_service` nor `orchestrator` is initialized on the bot.",
                ephemeral=True
            )
            return

        # Check API key configuration safely without leaking
        openrouter_key = getattr(ai, "openrouter_key", None)
        if not openrouter_key:
            await interaction.followup.send(
                "❌ `OPENROUTER_API_KEY` is **not set** in environment variables.\n"
                "Add it in Render → Environment → `OPENROUTER_API_KEY`.",
                ephemeral=True
            )
            return

        try:
            response = None
            if orch:
                response = await orch.process_raw_text(
                    text=message,
                    author=interaction.user.display_name or "tester",
                    platform="discord",
                    user_id=str(interaction.user.id),
                    bypass_trigger=True,
                    bypass_cooldown=True
                )
            elif ai:
                response = await ai._call_openrouter(
                    message,
                    author=interaction.user.display_name or "tester"
                )

            if response:
                embed = discord.Embed(
                    title="🗡️ Rukiya Test Response",
                    color=discord.Color.dark_red()
                )
                embed.add_field(name="📨 Input", value=f"`{message[:1000]}`", inline=False)
                embed.add_field(name="💬 Rukiya says", value=response[:1024], inline=False)
                embed.set_footer(
                    text=f"Model: {getattr(ai, 'model', 'unknown')} | Length: {len(response)} chars | Safe test ✅"
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                embed = discord.Embed(
                    title="❌ No Response from AI Service",
                    color=discord.Color.red()
                )
                embed.add_field(
                    name="Diagnostics",
                    value=(
                        "• Model: `" + getattr(ai, "model", "unknown") + "`\n"
                        "• Key Status: " + mask_secret(openrouter_key) + "\n"
                        "• Check bot logs for any network or HTTP timeout details."
                    ),
                    inline=False
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.exception("test_ai failed")
            await interaction.followup.send(
                f"❌ Exception during AI test: `{type(e).__name__}`. Check bot logs for details.",
                ephemeral=True
            )

    # ────────────────────────────────────────────
    # /test_trigger  — dry run decision engine
    # ────────────────────────────────────────────
    @app_commands.command(name="test_trigger", description="Check if a message would trigger Rukiya")
    @app_commands.describe(message="Message to check against trigger and decision rules")
    @app_commands.default_permissions(administrator=True)
    async def test_trigger(self, interaction: discord.Interaction, message: str):
        """Dry-run DecisionService to inspect eligibility and priority breakdown."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        ai = getattr(self.bot, "ai_service", None)
        orch = getattr(self.bot, "orchestrator", None)
        decision_svc = getattr(orch, "decision_service", None)
        identity_svc = getattr(orch, "identity_service", None)
        config = getattr(self.bot, "config", None) or getattr(ai, "config", None)

        author = interaction.user.display_name or "tester"
        user_id = str(interaction.user.id)

        # Standard checks list
        checks = []
        has_key = bool(getattr(ai, "openrouter_key", None))
        checks.append(("API key set", has_key, "Set `OPENROUTER_API_KEY` in env" if not has_key else ""))

        cooldown_ok = ai.can_respond() if ai else True
        remaining = ai.get_cooldown_remaining() if ai else 0.0
        checks.append(("Cooldown passed", cooldown_ok, f"{remaining:.1f}s remaining" if not cooldown_ok else ""))

        bot_users = getattr(config, "bot_users", set()) if config else set()
        is_bot = any(author.lower() == u.lower() for u in bot_users)
        checks.append((f"Author '{author}' not in bot_users", not is_bot, f"'{author}' is in bot_users list" if is_bot else ""))

        banned = getattr(config, "banned_words", set()) if config else set()
        hit_banned = [w for w in banned if w in message.lower()]
        checks.append(("No banned words", not hit_banned, f"Found banned: {hit_banned}" if hit_banned else ""))

        triggers = getattr(config, "ai_triggers", set()) if config else set()
        hit_triggers = [t for t in triggers if t.lower() in message.lower()]
        checks.append(("Contains trigger word", bool(hit_triggers), f"Matched: {hit_triggers}" if hit_triggers else "No trigger found"))

        # If DecisionService is available, run full decision
        decision_info = ""
        all_pass = all(ok for _, ok, _ in checks)
        if decision_svc and identity_svc:
            chat_msg = ChatMessage(
                platform="discord",
                message_id="test_trigger_msg",
                user_id=user_id,
                username=author,
                display_name=author,
                text=message
            )
            user_obj = identity_svc.resolve(chat_msg)
            dec = decision_svc.decide(chat_msg, user_obj)
            all_pass = dec.should_respond
            decision_info = (
                f"\n\n**V2 Decision Engine Evaluation**:\n"
                f"• Should Respond: `{'Yes' if dec.should_respond else 'No'}`\n"
                f"• Intent: `{dec.intent}`\n"
                f"• Priority: `{dec.priority:.2f}` (Threshold: `{decision_svc.response_threshold:.2f}`)\n"
                f"• Reason: `{dec.reason}`"
            )

        embed = discord.Embed(
            title="🔍 Trigger Check — " + ("✅ Would respond" if all_pass else "❌ Would NOT respond"),
            color=discord.Color.green() if all_pass else discord.Color.orange()
        )
        embed.add_field(name="Message", value=f"`{message[:500]}`", inline=False)

        result_lines = []
        for name, passed, reason in checks:
            icon = "✅" if passed else "❌"
            line = f"{icon} **{name}**"
            if reason:
                line += f"\n　↳ {reason}"
            result_lines.append(line)

        embed.add_field(name="Check Results", value="\n".join(result_lines) + decision_info, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ────────────────────────────────────────────
    # /bot_status  — diagnostic dashboard
    # ────────────────────────────────────────────
    @app_commands.command(name="bot_status", description="Get full bot and V2 service status")
    @app_commands.default_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as e:
            logger.exception("Failed to defer: %s", e)
            return

        cm = getattr(self.bot, "chat_monitor", None)
        ai = getattr(self.bot, "ai_service", None)
        yt = getattr(self.bot, "youtube_service", None)
        orch = getattr(self.bot, "orchestrator", None)
        mem = getattr(self.bot, "memory_service", None)
        rukiya_cog = self.bot.cogs.get("RukiyaCog")
        rukiya_enabled = getattr(rukiya_cog, "enabled", False) if rukiya_cog else False

        embed = discord.Embed(title="🗡️ Rukiya V2 Bot Status", color=discord.Color.dark_red())

        # Core Services
        embed.add_field(name="Orchestrator", value="✅ Active" if orch else "❌ Missing", inline=True)
        embed.add_field(name="Memory Service", value="✅ Active" if mem else "❌ Missing", inline=True)
        embed.add_field(name="AI Service", value="✅ Present" if ai else "❌ Missing", inline=True)
        embed.add_field(name="YouTube Service", value="✅ Present" if yt else "❌ Missing", inline=True)
        embed.add_field(name="Chat Monitor", value="🟢 Running" if (cm and cm.is_running) else "🔴 Stopped", inline=True)
        embed.add_field(name="Auto-responder", value="✅ Enabled" if rukiya_enabled else "❌ Disabled", inline=True)

        if cm and cm.is_running:
            st = cm.get_status()
            embed.add_field(name="Live Video ID", value=f"`{st.get('video_id') or 'N/A'}`", inline=True)
            embed.add_field(name="Processed count", value=str(st.get("processed_count", 0)), inline=True)
            embed.add_field(name="Stream Session", value=f"`{st.get('session_id') or 'N/A'}`", inline=True)

        if ai:
            cooldown_left = ai.get_cooldown_remaining()
            embed.add_field(name="AI Cooldown Left", value=f"{cooldown_left:.1f}s", inline=True)
            embed.add_field(name="Model", value=f"`{getattr(ai, 'model', 'unknown')}`", inline=True)
            key_status = mask_secret(getattr(ai, "openrouter_key", None))
            embed.add_field(name="API Key", value=key_status, inline=True)

        embed.set_footer(text="Rukiya V2 Architecture • All secrets securely masked")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ────────────────────────────────────────────
    # /memory_lookup — inspect user memory
    # ────────────────────────────────────────────
    @app_commands.command(name="memory_lookup", description="Lookup memory stored for a user")
    @app_commands.describe(user_id="Platform canonical ID or Discord user mention")
    @app_commands.default_permissions(administrator=True)
    async def memory_lookup(self, interaction: discord.Interaction, user_id: str):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        mem_svc = getattr(self.bot, "memory_service", None)
        if not mem_svc:
            await interaction.followup.send("❌ Memory service not active.", ephemeral=True)
            return

        # Clean user_id if mention passed
        clean_id = user_id.replace("<@", "").replace(">", "").replace("!", "").strip()
        canonical_id = f"discord:{clean_id}" if ":" not in clean_id else clean_id

        memories = mem_svc.get_all_user_memories(canonical_id)
        user_obj = mem_svc.get_user(canonical_id)

        embed = discord.Embed(title=f"🧠 Memory Lookup: `{canonical_id}`", color=discord.Color.blue())
        if user_obj:
            embed.add_field(name="Display Name", value=user_obj.display_name, inline=True)
            embed.add_field(name="Interactions", value=str(user_obj.interaction_count), inline=True)

        if memories:
            lines = [f"• **{m.key}**: `{m.value}` (conf: {m.confidence:.2f}, uses: {m.usage_count})" for m in memories]
            embed.description = "\n".join(lines)
        else:
            embed.description = "*No persistent facts recorded for this user.*"

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ────────────────────────────────────────────
    # /memory_reset — reset user memory
    # ────────────────────────────────────────────
    @app_commands.command(name="memory_reset", description="Reset persistent memory for a user")
    @app_commands.describe(user_id="Platform canonical ID or Discord user mention")
    @app_commands.default_permissions(administrator=True)
    async def memory_reset(self, interaction: discord.Interaction, user_id: str):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        mem_svc = getattr(self.bot, "memory_service", None)
        if not mem_svc:
            await interaction.followup.send("❌ Memory service not active.", ephemeral=True)
            return

        clean_id = user_id.replace("<@", "").replace(">", "").replace("!", "").strip()
        canonical_id = f"discord:{clean_id}" if ":" not in clean_id else clean_id

        mem_svc.delete_user_memories(canonical_id)
        await interaction.followup.send(f"✅ Reset all persistent memories for `{canonical_id}`.", ephemeral=True)

    # ────────────────────────────────────────────
    # /rate_limit_status — check rate limits
    # ────────────────────────────────────────────
    @app_commands.command(name="rate_limit_status", description="Check rate limiter token budgets")
    @app_commands.default_permissions(administrator=True)
    async def rate_limit_status(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        limiter = getattr(self.bot, "rate_limiter", None)
        if not limiter:
            await interaction.followup.send("❌ Rate limiter not attached.", ephemeral=True)
            return

        embed = discord.Embed(title="⏱️ Rate Limiter Budgets", color=discord.Color.teal())
        embed.add_field(name="Global AI Tokens", value=f"{limiter.get_remaining('global_ai'):.1f}", inline=True)
        embed.add_field(name="YouTube Send Tokens", value=f"{limiter.get_remaining('youtube_send'):.1f}", inline=True)
        embed.add_field(name="Discord AI Tokens", value=f"{limiter.get_remaining('discord_ai'):.1f}", inline=True)
        embed.add_field(name="Idle Chat Tokens", value=f"{limiter.get_remaining('idle_chat'):.1f}", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCommands(bot))
