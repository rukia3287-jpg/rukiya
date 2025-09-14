import logging
import discord
from discord.ext import commands
from discord import app_commands
import time
import psutil
import os

logger = logging.getLogger(__name__)

class UtilityCommands(commands.Cog):
    """Cog for utility commands"""

    def __init__(self, bot):
        self.bot = bot
        self.start_time = time.time()
        logger.info("Utility Commands cog initialized")

    @app_commands.command(name="ping", description="Check bot latency")
    async def ping(self, interaction: discord.Interaction):
        """Check bot latency"""
        start_time = time.time()
        await interaction.response.defer()
        end_time = time.time()

        # Calculate latencies
        api_latency = (end_time - start_time) * 1000
        websocket_latency = self.bot.latency * 1000

        embed = discord.Embed(
            title="🏓 Pong!",
            color=discord.Color.green()
        )
        embed.add_field(
            name="API Latency", 
            value=f"{api_latency:.2f}ms", 
            inline=True
        )
        embed.add_field(
            name="WebSocket Latency", 
            value=f"{websocket_latency:.2f}ms", 
            inline=True
        )

        # Add status indicator
        if websocket_latency < 100:
            embed.add_field(name="Status", value="🟢 Excellent", inline=True)
        elif websocket_latency < 200:
            embed.add_field(name="Status", value="🟡 Good", inline=True)
        else:
            embed.add_field(name="Status", value="🔴 Poor", inline=True)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="uptime", description="Check bot uptime")
    async def uptime(self, interaction: discord.Interaction):
        """Check bot uptime"""
        uptime_seconds = int(time.time() - self.start_time)

        # Calculate time components
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        # Format uptime string
        uptime_parts = []
        if days > 0:
            uptime_parts.append(f"{days}d")
        if hours > 0:
            uptime_parts.append(f"{hours}h")
        if minutes > 0:
            uptime_parts.append(f"{minutes}m")
        uptime_parts.append(f"{seconds}s")

        uptime_str = " ".join(uptime_parts)

        embed = discord.Embed(
            title="⏱️ Bot Uptime",
            description=f"**{uptime_str}**",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Started", 
            value=f"<t:{int(self.start_time)}:R>", 
            inline=True
        )
        embed.add_field(
            name="Total Seconds", 
            value=f"{uptime_seconds:,}", 
            inline=True
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="info", description="Get bot information")
    async def bot_info(self, interaction: discord.Interaction):
        """Get bot information"""
        # Get system info
        process = psutil.Process(os.getpid())
        memory_usage = process.memory_info().rss / 1024 / 1024  # MB
        cpu_usage = process.cpu_percent()

        embed = discord.Embed(
            title="🤖 Rukiya Bot Information",
            color=discord.Color.purple()
        )

        # Bot stats
        embed.add_field(
            name="📊 Statistics", 
            value=(
                f"**Guilds:** {len(self.bot.guilds)}\n"
                f"**Users:** {len(self.bot.users)}\n"
                f"**Commands:** {len(self.bot.tree.get_commands())}"
            ), 
            inline=True
        )

        # System stats
        embed.add_field(
            name="💻 System", 
            value=(
                f"**Memory:** {memory_usage:.1f} MB\n"
                f"**CPU:** {cpu_usage}%\n"
                f"**Python:** {sys.version.split()[0]}"
            ), 
            inline=True
        )

        # Status
        status = "🟢 Running" if self.bot.chat_monitor.is_running else "🔴 Idle"
        embed.add_field(
            name="📡 Status", 
            value=(
                f"**Chat Monitor:** {status}\n"
                f"**AI Service:** {'🟢 Active' if self.bot.ai_service.model else '🔴 Inactive'}\n"
                f"**YouTube API:** {'🟢 Ready' if self.bot.youtube_service.youtube else '🔴 Not Auth'}"
            ), 
            inline=True
        )

        # Add footer
        embed.set_footer(
            text=f"Requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Get help information")
    async def help_command(self, interaction: discord.Interaction):
        """Get help information"""
        embed = discord.Embed(
            title="📚 Rukiya Bot Help",
            description="Here are all available commands:",
            color=discord.Color.gold()
        )

        # YouTube commands
        embed.add_field(
            name="🎥 YouTube Commands",
            value=(
                "`/start <video_id>` - Start monitoring YouTube chat\n"
                "`/stop` - Stop chat monitoring\n"
                "`/status` - Check monitoring status"
            ),
            inline=False
        )

        # Utility commands
        embed.add_field(
            name="🛠️ Utility Commands",
            value=(
                "`/ping` - Check bot latency\n"
                "`/uptime` - Check bot uptime\n"
                "`/info` - Get bot information\n"
                "`/help` - Show this help message"
            ),
            inline=False
        )

        # Admin commands (only show if user is admin)
        if interaction.user.guild_permissions.administrator:
            embed.add_field(
                name="⚙️ Admin Commands",
                value=(
                    "`/reload <cog>` - Reload a cog\n"
                    "`/config <setting> [value]` - View/modify config\n"
                    "`/logs [lines]` - Get recent logs\n"
                    "`/force_stop` - Force stop all operations\n"
                    "`/test_ai <message>` - Test AI response"
                ),
                inline=False
            )

        # Add usage tips
        embed.add_field(
            name="💡 Tips",
            value=(
                "• Use `/start` with a YouTube video ID to begin monitoring\n"
                "• The bot responds to messages containing 'rukiya' or similar triggers\n"
                "• Check `/status` to see if everything is working correctly"
            ),
            inline=False
        )

        embed.set_footer(text="Rukiya Bot - Your YouTube Chat Assistant")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="invite", description="Get bot invite link")
    async def invite(self, interaction: discord.Interaction):
        """Get bot invite link"""
        # Generate invite URL with necessary permissions
        permissions = discord.Permissions(
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            use_slash_commands=True
        )

        invite_url = discord.utils.oauth_url(
            self.bot.user.id, 
            permissions=permissions,
            scopes=['bot', 'applications.commands']
        )

        embed = discord.Embed(
            title="🔗 Invite Rukiya Bot",
            description=f"[Click here to invite me to your server!]({invite_url})",
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Required Permissions",
            value=(
                "• Send Messages\n"
                "• Embed Links\n"
                "• Read Message History\n"
                "• Use Slash Commands"
            ),
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(UtilityCommands(bot))