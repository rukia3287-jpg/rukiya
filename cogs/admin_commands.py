# cogs/admin_commands.py
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

class AdminCommands(commands.Cog):
    """Admin utilities: AI test, reload (if needed), basic admin helpers"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_unload(self):
        # cleanup if needed
        pass

    @app_commands.command(name="test_ai", description="Test AI response generation (ephemeral)")
    @app_commands.describe(message="Message to test AI response with")
    async def test_ai(self, interaction: discord.Interaction, message: str):
        """Test AI response generation using bot.ai_service"""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            logger.exception("Failed to defer interaction for /test_ai; continuing best-effort")

        ai = getattr(self.bot, "ai_service", None)
        if not ai:
            await interaction.followup.send("❌ AI service not initialized on the bot.", ephemeral=True)
            return

        # Bypass cooldown temporarily for manual test (don't abuse this)
        orig_last = getattr(ai, "last_used", 0)
        try:
            ai.last_used = 0
            response = await ai.generate_response(message, interaction.user.display_name or "tester")
            if response:
                embed = discord.Embed(title="🤖 AI Test Response", color=discord.Color.blue())
                embed.add_field(name="Input", value=f"`{message}`", inline=False)
                embed.add_field(name="Output", value=response, inline=False)
                embed.set_footer(text=f"Length: {len(response)} chars")
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("❌ AI did not generate a response. (Maybe triggers/cooldown or rate-limit.)", ephemeral=True)
        except Exception as e:
            logger.exception("test_ai failed")
            await interaction.followup.send(f"❌ AI test failed: {e}", ephemeral=True)
        finally:
            ai.last_used = orig_last

    @app_commands.command(name="bot_status", description="Get basic bot status")
    async def bot_status(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        cm = getattr(self.bot, "chat_monitor", None)
        ai = getattr(self.bot, "ai_service", None)
        yt = getattr(self.bot, "youtube_service", None)

        status = {
            "chat_monitor": "present" if cm else "missing",
            "ai_service": "present" if ai else "missing",
            "youtube_service": "present" if yt else "missing",
        }

        embed = discord.Embed(title="Bot Status", color=discord.Color.green())
        for k, v in status.items():
            embed.add_field(name=k, value=v, inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCommands(bot))
