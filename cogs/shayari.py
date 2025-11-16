# cogs/shayari.py
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

# Minimal set of example shayaris - extend as needed
DEFAULT_SHAYARIS = [
    "Zindagi ek safar hai, muqaddar ka asar hai.",
    "Aankhon mein sapne, dil mein umeed jagti hai.",
    "Chandni raat mein, teri yaad saath mein.",
]

class Shayari(commands.Cog):
    """Send shayari to YouTube chat or Discord channel"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="shayari_send", description="Send a shayari to the active YouTube chat (or to Discord if no chat)")
    @app_commands.describe(index="Index of shayari from the list (optional)")
    async def shayari_send(self, interaction: discord.Interaction, index: Optional[int] = None):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True)
        except Exception:
            pass

        sh = None
        try:
            if index is not None and 0 <= index < len(DEFAULT_SHAYARIS):
                sh = DEFAULT_SHAYARIS[index]
            else:
                import random
                sh = random.choice(DEFAULT_SHAYARIS)
        except Exception:
            sh = DEFAULT_SHAYARIS[0]

        # If youtube chat is active, send there
        cm = getattr(self.bot, "chat_monitor", None)
        if cm and cm.is_running:
            try:
                sent = await cm.send_chat_message(sh)
                if sent:
                    await interaction.followup.send("✅ Shayari sent to YouTube chat.", ephemeral=True)
                    return
                else:
                    logger.warning("send_chat_message returned False")
            except Exception as e:
                logger.exception("Failed to send shayari to YouTube chat: %s", e)

        # fallback: send in the channel where command was used
        try:
            await interaction.followup.send(f"📜 Shayari: {sh}")
        except Exception as e:
            logger.exception("Failed to send shayari to Discord: %s", e)

    @app_commands.command(name="shayari_list", description="List available shayaris")
    async def shayari_list(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        desc = "\n".join([f"{i}. {s[:120]}" for i, s in enumerate(DEFAULT_SHAYARIS)])
        embed = discord.Embed(title="Shayari List", description=desc, color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Shayari(bot))
