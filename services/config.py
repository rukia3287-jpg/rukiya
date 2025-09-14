import discord
from discord.ext import commands
import psutil
import platform
from datetime import datetime

class UtilityCommands(commands.Cog):
    """Utility commands for the bot"""
    
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="botinfo", aliases=["info"])
    async def system_info(self, ctx):
        """Display bot information and system stats"""
        try:
            # Get system info
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=1)
            
            # Get bot uptime
            uptime = datetime.now() - self.bot.start_time if hasattr(self.bot, 'start_time') else None
            
            embed = discord.Embed(
                title="🤖 Bot Information",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            
            # Bot stats
            embed.add_field(
                name="📊 Bot Stats",
                value=f"Servers: {len(self.bot.guilds)}\n"
                      f"Users: {len(self.bot.users)}\n"
                      f"Commands: {len(self.bot.commands)}",
                inline=True
            )
            
            # System stats
            embed.add_field(
                name="💻 System",
                value=f"OS: {platform.system()}\n"
                      f"Python: {platform.python_version()}\n"
                      f"Discord.py: {discord.__version__}",
                inline=True
            )
            
            # Performance
            embed.add_field(
                name="⚡ Performance",
                value=f"CPU: {cpu_percent}%\n"
                      f"Memory: {memory.percent}%\n"
                      f"Latency: {round(self.bot.latency * 1000)}ms",
                inline=True
            )
            
            if uptime:
                embed.add_field(
                    name="⏰ Uptime",
                    value=f"{uptime.days}d {uptime.seconds//3600}h {(uptime.seconds//60)%60}m",
                    inline=True
                )
            
            embed.set_footer(text=f"Requested by {ctx.author.display_name}")
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"Error getting bot info: {str(e)}")

    @commands.command(name="ping")
    async def latency_check(self, ctx):
        """Check bot latency"""
        latency = round(self.bot.latency * 1000)
        
        # Determine latency quality
        if latency < 100:
            emoji = "🟢"
            quality = "Excellent"
        elif latency < 200:
            emoji = "🟡"
            quality = "Good"
        elif latency < 300:
            emoji = "🟠"
            quality = "Fair"
        else:
            emoji = "🔴"
            quality = "Poor"
        
        embed = discord.Embed(
            title=f"{emoji} Pong!",
            description=f"Latency: **{latency}ms** ({quality})",
            color=discord.Color.green() if latency < 100 else discord.Color.yellow()
        )
        
        await ctx.send(embed=embed)

    @commands.command(name="servers", aliases=["guilds"])
    @commands.is_owner()
    async def server_list(self, ctx):
        """List all servers the bot is in (Owner only)"""
        if not self.bot.guilds:
            await ctx.send("Not connected to any servers.")
            return
        
        embed = discord.Embed(
            title="📋 Server List",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        
        server_info = []
        for guild in self.bot.guilds[:10]:  # Limit to first 10 servers
            member_count = guild.member_count or 0
            server_info.append(f"**{guild.name}** ({member_count} members)")
        
        if len(self.bot.guilds) > 10:
            server_info.append(f"... and {len(self.bot.guilds) - 10} more servers")
        
        embed.description = "\n".join(server_info)
        embed.set_footer(text=f"Total: {len(self.bot.guilds)} servers")
        
        await ctx.send(embed=embed)

    @commands.command(name="uptime")
    async def show_uptime(self, ctx):
        """Show bot uptime"""
        if not hasattr(self.bot, 'start_time'):
            await ctx.send("Uptime tracking not available.")
            return
        
        uptime = datetime.now() - self.bot.start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        embed = discord.Embed(
            title="⏰ Bot Uptime",
            description=f"**{days}** days, **{hours}** hours, **{minutes}** minutes, **{seconds}** seconds",
            color=discord.Color.green()
        )
        
        embed.set_footer(text=f"Started at {self.bot.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        await ctx.send(embed=embed)

    @commands.command(name="invite")
    async def get_invite(self, ctx):
        """Get bot invite link"""
        # Generate invite URL with necessary permissions
        permissions = discord.Permissions(
            read_messages=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            use_slash_commands=True
        )
        
        invite_url = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=permissions
        )
        
        embed = discord.Embed(
            title="🔗 Invite Me!",
            description=f"[Click here to invite me to your server!]({invite_url})",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Permissions",
            value="• Read Messages\n• Send Messages\n• Embed Links\n• Read Message History\n• Use Slash Commands",
            inline=False
        )
        
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(UtilityCommands(bot))
