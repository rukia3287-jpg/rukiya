import logging
import discord
from discord.ext import commands, tasks
from discord import app_commands

logger = logging.getLogger(__name__)

class YouTubeCommands(commands.Cog):
    """Cog for YouTube-related commands"""

    def __init__(self, bot):
        self.bot = bot
        self.monitor_task.start()
        logger.info("YouTube Commands cog initialized")

    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        self.monitor_task.cancel()
        self.bot.chat_monitor.stop_monitoring()

    @app_commands.command(name="start", description="Start Rukiya bot for YouTube video")
    @app_commands.describe(video_id="YouTube video ID to monitor")
    async def start_monitoring(self, interaction: discord.Interaction, video_id: str):
        """Start monitoring YouTube chat"""
        await interaction.response.defer()

        if self.bot.chat_monitor.is_running:
            await interaction.followup.send(
                "⚠️ Rukiya bot is already running! Use `/stop` first.",
                ephemeral=True
            )
            return

        # Authenticate with YouTube
        if not self.bot.youtube_service.authenticate():
            await interaction.followup.send(
                "❌ YouTube authentication failed. Check your credentials.",
                ephemeral=True
            )
            return

        # Get video info first
        video_info = self.bot.youtube_service.get_video_info(video_id)
        if not video_info:
            await interaction.followup.send(
                f"❌ Video not found: `{video_id}`",
                ephemeral=True
            )
            return

        # Get live chat ID
        live_chat_id = self.bot.youtube_service.get_live_chat_id(video_id)
        if not live_chat_id:
            await interaction.followup.send(
                f"❌ No active live chat found for video: `{video_id}`\n"
                f"Make sure the video is currently live streaming.",
                ephemeral=True
            )
            return

        # Start monitoring
        self.bot.chat_monitor.start_monitoring(live_chat_id, video_id)

        # Create success embed
        embed = discord.Embed(
            title="🚀 Rukiya Bot Started!",
            color=discord.Color.green()
        )
        embed.add_field(
            name="Video", 
            value=f"[{video_info['snippet']['title'][:50]}...](https://youtube.com/watch?v={video_id})",
            inline=False
        )
        embed.add_field(name="Video ID", value=f"`{video_id}`", inline=True)
        embed.add_field(name="Chat ID", value=f"`{live_chat_id[:20]}...`", inline=True)
        embed.add_field(name="Status", value="🟢 Monitoring", inline=True)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="stop", description="Stop Rukiya bot")
    async def stop_monitoring(self, interaction: discord.Interaction):
        """Stop monitoring YouTube chat"""
        if not self.bot.chat_monitor.is_running:
            await interaction.response.send_message(
                "⚠️ Rukiya bot is not running!",
                ephemeral=True
            )
            return

        self.bot.chat_monitor.stop_monitoring()

        embed = discord.Embed(
            title="🛑 Rukiya Bot Stopped",
            description="Chat monitoring has been stopped.",
            color=discord.Color.red()
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="status", description="Check Rukiya bot status")
    async def check_status(self, interaction: discord.Interaction):
        """Check bot status"""
        status = self.bot.chat_monitor.get_status()

        embed = discord.Embed(
            title="🤖 Rukiya Bot Status",
            color=discord.Color.green() if status["is_running"] else discord.Color.red()
        )

        # Status field
        status_text = "🟢 Running" if status["is_running"] else "🔴 Stopped"
        embed.add_field(name="Status", value=status_text, inline=True)

        # Video info
        if status["video_id"]:
            embed.add_field(name="Video ID", value=f"`{status['video_id']}`", inline=True)
        else:
            embed.add_field(name="Video ID", value="None", inline=True)

        # Chat info
        if status["live_chat_id"]:
            chat_display = f"`{status['live_chat_id'][:20]}...`"
        else:
            chat_display = "None"
        embed.add_field(name="Chat ID", value=chat_display, inline=True)

        # Stats
        embed.add_field(
            name="Messages Processed", 
            value=str(status["processed_count"]), 
            inline=True
        )
        embed.add_field(
            name="AI Cooldown", 
            value=f"{self.bot.config.ai_cooldown}s", 
            inline=True
        )

        if status["ai_cooldown_remaining"] > 0:
            embed.add_field(
                name="Cooldown Remaining", 
                value=f"{status['ai_cooldown_remaining']:.1f}s", 
                inline=True
            )
        else:
            embed.add_field(name="AI Status", value="🟢 Ready", inline=True)

        await interaction.response.send_message(embed=embed)

    @tasks.loop(seconds=10)
    async def monitor_task(self):
        """Background task to monitor chat"""
        if self.bot.chat_monitor.is_running:
            await self.bot.chat_monitor.process_messages()

    @monitor_task.before_loop
    async def before_monitor_task(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(YouTubeCommands(bot))