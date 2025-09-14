import logging
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

class Events(commands.Cog):
    """Cog for handling Discord events"""

    def __init__(self, bot):
        self.bot = bot
        logger.info("Events cog initialized")

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        """Called when bot joins a new guild"""
        logger.info(f"Joined new guild: {guild.name} (ID: {guild.id})")

        # Try to find a suitable channel to send welcome message
        channel = None

        # Look for common channel names
        for channel_name in ['general', 'bot-commands', 'commands', 'welcome']:
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if channel and channel.permissions_for(guild.me).send_messages:
                break

        # If no suitable named channel, try the first channel we can send to
        if not channel:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break

        # Send welcome message
        if channel:
            embed = discord.Embed(
                title="🎉 Thanks for adding Rukiya Bot!",
                description=(
                    "I'm here to help you monitor and interact with YouTube live chats!\n\n"
                    "**Quick Start:**\n"
                    "• Use `/help` to see all available commands\n"
                    "• Use `/start <video_id>` to begin monitoring a YouTube live chat\n"
                    "• Use `/status` to check if everything is working\n\n"
                    "Need help? Use `/help` for detailed command information!"
                ),
                color=discord.Color.green()
            )

            embed.add_field(
                name="🔧 Setup Required",
                value=(
                    "Make sure you have:\n"
                    "• YouTube API credentials configured\n"
                    "• Gemini AI API key for responses\n"
                    "• Proper permissions for the bot"
                ),
                inline=False
            )

            embed.set_footer(text="Use /help to get started!")

            try:
                await channel.send(embed=embed)
                logger.info(f"Sent welcome message to {guild.name}")
            except Exception as e:
                logger.error(f"Failed to send welcome message to {guild.name}: {e}")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        """Called when bot leaves a guild"""
        logger.info(f"Left guild: {guild.name} (ID: {guild.id})")

        # Stop monitoring if this guild was using the bot
        if self.bot.chat_monitor.is_running:
            logger.info("Stopping chat monitoring due to guild leave")
            self.bot.chat_monitor.stop_monitoring()

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Handle command errors"""
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore command not found errors

        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use this command!")
            return

        if isinstance(error, commands.BotMissingPermissions):
            await ctx.send("❌ I don't have the necessary permissions to execute this command!")
            return

        # Log unexpected errors
        logger.error(f"Command error in {ctx.command}: {error}", exc_info=error)

        try:
            await ctx.send("❌ An unexpected error occurred. Please try again later.")
        except:
            pass  # Channel might be deleted or we might not have permissions

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error):
        """Handle application command errors"""
        if isinstance(error, discord.app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command!", 
                ephemeral=True
            )
            return

        if isinstance(error, discord.app_commands.BotMissingPermissions):
            await interaction.response.send_message(
                "❌ I don't have the necessary permissions to execute this command!", 
                ephemeral=True
            )
            return

        # Log unexpected errors
        logger.error(f"App command error in {interaction.command}: {error}", exc_info=error)

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred. Please try again later.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ An unexpected error occurred. Please try again later.",
                    ephemeral=True
                )
        except:
            pass  # Interaction might have expired

    @commands.Cog.listener()
    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f"Bot ready event fired")

        # Set bot presence
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="YouTube chats | /help"
        )
        await self.bot.change_presence(
            status=discord.Status.online,
            activity=activity
        )

    @commands.Cog.listener()
    async def on_disconnect(self):
        """Called when bot disconnects"""
        logger.warning("Bot disconnected from Discord")

        # Stop monitoring to prevent issues
        if self.bot.chat_monitor.is_running:
            logger.info("Stopping chat monitoring due to disconnect")
            self.bot.chat_monitor.stop_monitoring()

    @commands.Cog.listener()
    async def on_resumed(self):
        """Called when bot resumes connection"""
        logger.info("Bot resumed connection to Discord")

async def setup(bot):
    await bot.add_cog(Events(bot))