import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


class AdminCommands(commands.Cog):
    """Admin utilities: AI test, trigger debug, bot status"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_unload(self):
        pass

    # ────────────────────────────────────────────
    # /test_ai  — bypasses ALL checks, raw API call
    # ────────────────────────────────────────────
    @app_commands.command(name="test_ai", description="Test Rukiya's AI response directly (bypasses all checks)")
    @app_commands.describe(message="Any message — no trigger word needed, cooldown bypassed")
    async def test_ai(self, interaction: discord.Interaction, message: str):
        """
        Directly calls OpenRouter with Rukiya's persona.
        Bypasses trigger words, cooldown, and bot_users checks.
        Use this to confirm your API key and model are working.
        """
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as e:
            logger.exception(f"Failed to defer /test_ai: {e}")
            return

        ai = getattr(self.bot, "ai_service", None)
        if not ai:
            await interaction.followup.send(
                "❌ `ai_service` not initialized on the bot.\n"
                "Check that `main.py` attaches `self.ai_service = AIService(self.config)`.",
                ephemeral=True
            )
            return

        # Check API key explicitly so user gets a clear error
        if not getattr(ai, "openrouter_key", None):
            await interaction.followup.send(
                "❌ `OPENROUTER_API_KEY` is **not set** in your environment variables.\n"
                "Add it in Render → Environment → `OPENROUTER_API_KEY`.",
                ephemeral=True
            )
            return

        try:
            # ✅ Bypass ALL checks — call OpenRouter directly with Rukiya persona
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
                    text=(
                        f"Model: {getattr(ai, 'model', 'unknown')} | "
                        f"Length: {len(response)} chars | "
                        f"All checks bypassed ✅"
                    )
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

            else:
                embed = discord.Embed(
                    title="❌ No Response from OpenRouter",
                    color=discord.Color.red()
                )
                embed.add_field(
                    name="Possible reasons",
                    value=(
                        "• API key is set but **invalid or expired**\n"
                        "• Model name is wrong — current: `" + getattr(ai, "model", "unknown") + "`\n"
                        "• OpenRouter is down or rate limiting you\n"
                        "• Check bot logs for the actual HTTP error"
                    ),
                    inline=False
                )
                embed.add_field(
                    name="API Key (first 8 chars)",
                    value=f"`{ai.openrouter_key[:8]}...`",
                    inline=True
                )
                embed.add_field(
                    name="Endpoint",
                    value=f"`{getattr(ai, 'endpoint', 'unknown')}`",
                    inline=True
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.exception("test_ai failed")
            await interaction.followup.send(
                f"❌ Exception during AI test:\n```\n{e}\n```\nCheck bot logs for full traceback.",
                ephemeral=True
            )

    # ────────────────────────────────────────────
    # /test_trigger  — dry run should_respond()
    # ────────────────────────────────────────────
    @app_commands.command(name="test_trigger", description="Check if a message would trigger Rukiya")
    @app_commands.describe(message="Message to check against trigger rules")
    async def test_trigger(self, interaction: discord.Interaction, message: str):
        """
        Dry-run should_respond() so you can see exactly why a message
        does or does not trigger Rukiya — without actually calling the AI.
        """
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        ai = getattr(self.bot, "ai_service", None)
        if not ai:
            await interaction.followup.send("❌ `ai_service` not available.", ephemeral=True)
            return

        config = getattr(ai, "config", None)
        author = interaction.user.display_name or "tester"
        msg_lower = message.lower()
        author_lower = author.lower()

        checks = []

        # 1. API key
        has_key = bool(getattr(ai, "openrouter_key", None))
        checks.append(("API key set", has_key, "Set `OPENROUTER_API_KEY` in env" if not has_key else ""))

        # 2. Cooldown
        cooldown_ok = ai.can_respond()
        remaining = ai.get_cooldown_remaining()
        checks.append((
            "Cooldown passed",
            cooldown_ok,
            f"{remaining:.1f}s remaining" if not cooldown_ok else ""
        ))

        # 3. Bot user check
        bot_users = getattr(config, "bot_users", set()) if config else set()
        is_bot_user = any(author_lower == u.lower() for u in bot_users)
        checks.append((
            f"Author '{author}' not in bot_users",
            not is_bot_user,
            f"'{author}' is in bot_users list" if is_bot_user else ""
        ))

        # 4. Banned words
        banned = getattr(config, "banned_words", set()) if config else set()
        hit_banned = [w for w in banned if w in msg_lower]
        checks.append((
            "No banned words",
            not hit_banned,
            f"Found banned: {hit_banned}" if hit_banned else ""
        ))

        # 5. Trigger word
        triggers = getattr(config, "ai_triggers", set()) if config else set()
        hit_triggers = [t for t in triggers if t.lower() in msg_lower]
        checks.append((
            "Contains trigger word",
            bool(hit_triggers),
            f"No trigger found.\nAll triggers: {sorted(triggers)}" if not hit_triggers else f"Matched: {hit_triggers}"
        ))

        # Build embed
        all_pass = all(ok for _, ok, _ in checks)
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

        embed.add_field(name="Check Results", value="\n".join(result_lines), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ────────────────────────────────────────────
    # /bot_status
    # ────────────────────────────────────────────
    @app_commands.command(name="bot_status", description="Get full bot status")
    async def status(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as e:
            logger.exception(f"Failed to defer: {e}")
            return

        cm = getattr(self.bot, "chat_monitor", None)
        ai = getattr(self.bot, "ai_service", None)
        yt = getattr(self.bot, "youtube_service", None)
        rukiya_cog = self.bot.cogs.get("RukiyaCog")
        rukiya_enabled = getattr(rukiya_cog, "enabled", False) if rukiya_cog else False

        embed = discord.Embed(title="🗡️ Rukiya Bot Status", color=discord.Color.dark_red())

        # Services
        embed.add_field(name="chat_monitor", value="✅ Present" if cm else "❌ Missing", inline=True)
        embed.add_field(name="ai_service", value="✅ Present" if ai else "❌ Missing", inline=True)
        embed.add_field(name="youtube_service", value="✅ Present" if yt else "❌ Missing", inline=True)

        # Runtime state
        embed.add_field(name="YT Chat Running", value="🟢 Yes" if (cm and cm.is_running) else "🔴 No", inline=True)
        embed.add_field(name="Auto-responder", value="✅ Enabled" if rukiya_enabled else "❌ Disabled", inline=True)

        if cm and cm.is_running:
            st = cm.get_status()
            embed.add_field(name="Video ID", value=st.get("video_id") or "N/A", inline=True)
            embed.add_field(name="Messages processed", value=str(st.get("processed_count", 0)), inline=True)

        # AI info
        if ai:
            cooldown_left = ai.get_cooldown_remaining()
            embed.add_field(name="AI Cooldown Left", value=f"{cooldown_left:.1f}s", inline=True)
            embed.add_field(name="Model", value=f"`{getattr(ai, 'model', 'unknown')}`", inline=True)
            key_preview = (
                ai.openrouter_key[:8] + "..."
                if getattr(ai, "openrouter_key", None)
                else "❌ NOT SET"
            )
            embed.add_field(name="API Key", value=f"`{key_preview}`", inline=True)

        embed.set_footer(text="Use /test_ai to test response | /test_trigger to debug triggers")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCommands(bot))
