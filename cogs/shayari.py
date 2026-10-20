from services.discord_ui import defer_response, send_response
import logging
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from services.access import operator_only, operator_callback

logger = logging.getLogger(__name__)

DEFAULT_SHAYARIS = [
    "Zindagi ek safar hai, muqaddar ka asar hai.",
    "Aankhon mein sapne, dil mein umeed jagti hai.",
    "Chandni raat mein, teri yaad saath mein.",
    "Dil ki baat, zuban pe aa gayi aaj.",
    "Khushiyon ki baarish ho, gham door jaye.",
]

class Shayari(commands.Cog):
    """Send shayari to YouTube chat or Discord"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="shayari_send", description="Send a shayari to YouTube chat")
    @app_commands.describe(index="Index of shayari (optional)")
    @operator_only()
    @operator_callback
    async def shayari_send(self, interaction: discord.Interaction, index: Optional[int] = None):
        try:
            await defer_response(interaction, thinking=True)
        except Exception as e:
            logger.exception(f"Failed to defer: {e}")
            return

        # Select shayari
        try:
            if index is not None and 0 <= index < len(DEFAULT_SHAYARIS):
                sh = DEFAULT_SHAYARIS[index]
            else:
                sh = random.choice(DEFAULT_SHAYARIS)
        except Exception:
            sh = DEFAULT_SHAYARIS[0]

        # Try YouTube chat first
        cm = getattr(self.bot, "chat_monitor", None)
        if cm and cm.is_running:
            try:
                sent = await cm.send_chat_message(sh)
                if sent:
                    await send_response(interaction, "✅ Shayari sent to YouTube chat.", ephemeral=True)
                    return
            except Exception as e:
                logger.exception(f"Failed to send to YouTube: {e}")

        # Fallback to Discord
        try:
            await send_response(interaction, f"📜 Shayari: {sh}")
        except Exception as e:
            logger.exception(f"Failed to send to Discord: {e}")

    @app_commands.command(name="shayari_list", description="List available shayaris")
    async def shayari_list(self, interaction: discord.Interaction):
        try:
            await defer_response(interaction, ephemeral=True)
        except Exception:
            pass

        desc = "\n".join([f"{i}. {s}" for i, s in enumerate(DEFAULT_SHAYARIS)])
        embed = discord.Embed(title="Shayari List", description=desc, color=discord.Color.blurple())
        await send_response(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Shayari(bot))
