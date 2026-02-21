import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


class AdminCommands(commands.Cog):
    """Admin utilities: AI test, reload, basic admin helpers"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_unload(self):
        pass

    @app_commands.command(name="test_ai", description="Test Rukiya's AI response (ephemeral)")
    @app_commands.describe(message="Message to test — try tagging her like 'hey rukiya kya kar rahi ho'")
    async def test_ai(self, interaction: discord.Interaction, message: str):
        """Test Rukiya AI response — bypasses cooldown for manual testing."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as e:
            logger.exception(f"Failed to defer /test_ai: {e}")
            return

        ai = getattr(self.bot, "ai_service", None)
        if not ai:
            await interaction.followup.send("❌ AI service not initialized on the bot.", ephemeral=True)
            return

        # Bypass cooldown for testing
        orig_last = getattr(ai, "last_used", 0)
        try:
            ai.last_used = 0  # reset so should_respond passes cooldown check
            response = await ai.generate_response(message, interaction.user.display_name or "tester")
            if response:
                response_text = response[:1024] if len(response) > 1024 else response
                embed = discord.Embed(
                    title="🗡️ Rukiya Test Response",
                    color=discord.Color.dark_red()
                )
                embed.add_field(name="Input", value=f"`{message[:1000]}`", inline=False)
                embed.add_field(name="Rukiya says", value=response_text, inline=False)
                embed.set_footer(text=f"Model: {getattr(ai, 'model', 'unknown')} | Length: {len(response)} chars")
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(
                    "❌ No response generated.\n\n"
                    "**Common causes:**\n"
                    "• Message doesn't contain a trigger word (rukiya, ruki, @rukiya, etc.)\n"
                    "• `OPENROUTER_API_KEY` not set\n"
                    "• Author name matches a bot_users entry",
                    ephemeral=True
                )
        except Exception as e:
            logger.exception("test_ai failed")
            await interaction.followup.send(f"❌ AI test failed: {e}", ephemeral=True)
        finally:
            ai.last_used = orig_last

    @app_commands.command(name="bot_status", description="Get basic bot status")
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
        embed.add_field(name="chat_monitor", value="✅ Present" if cm else "❌ Missing", inline=True)
        embed.add_field(name="ai_service", value="✅ Present" if ai else "❌ Missing", inline=True)
        embed.add_field(name="youtube_service", value="✅ Present" if yt else "❌ Missing", inline=True)
        embed.add_field(name="YT Chat Running", value="🟢 Yes" if (cm and cm.is_running) else "🔴 No", inline=True)
        embed.add_field(name="Auto-responder", value="✅ Enabled" if rukiya_enabled else "❌ Disabled", inline=True)

        if ai:
            cooldown_left = ai.get_cooldown_remaining()
            embed.add_field(name="AI Cooldown Left", value=f"{cooldown_left:.1f}s", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCommands(bot))
