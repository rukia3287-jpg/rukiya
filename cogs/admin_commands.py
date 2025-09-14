import logging
import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)

class AdminCommands(commands.Cog):
    """Cog for admin-only commands"""

    def __init__(self, bot):
        self.bot = bot
        logger.info("Admin Commands cog initialized")

    async def cog_check(self, ctx):
        """Check if user has admin permissions"""
        if ctx.interaction:
            return ctx.interaction.user.guild_permissions.administrator
        return ctx.author.guild_permissions.administrator

    @app_commands.command(name="reload", description="Reload a specific cog")
    @app_commands.describe(cog="Name of the cog to reload")
    async def reload_cog(self, interaction: discord.Interaction, cog: str):
        """Reload a specific cog"""
        await interaction.response.defer(ephemeral=True)

        try:
            await self.bot.reload_extension(f"cogs.{cog}")
            await interaction.followup.send(f"✅ Successfully reloaded `{cog}` cog!")
            logger.info(f"Cog reloaded: {cog} by {interaction.user}")
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to reload `{cog}`: {str(e)}")
            logger.error(f"Failed to reload cog {cog}: {e}")

    @app_commands.command(name="config", description="View/modify bot configuration")
    @app_commands.describe(
        setting="Configuration setting to view/modify",
        value="New value (leave empty to view current value)"
    )
    async def config_command(self, interaction: discord.Interaction, setting: str, value: str = None):
        """View or modify bot configuration"""
        await interaction.response.defer(ephemeral=True)

        valid_settings = {
            'ai_cooldown': int,
            'max_message_length': int,
            'chat_check_interval': int
        }

        if setting not in valid_settings:
            await interaction.followup.send(
                f"❌ Invalid setting. Valid settings: {', '.join(valid_settings.keys())}"
            )
            return

        # View current value
        if value is None:
            current_value = getattr(self.bot.config, setting)
            await interaction.followup.send(
                f"📋 Current value of `{setting}`: `{current_value}`"
            )
            return

        # Modify value
        try:
            # Convert to appropriate type
            new_value = valid_settings[setting](value)
            setattr(self.bot.config, setting, new_value)

            await interaction.followup.send(
                f"✅ Updated `{setting}` from `{getattr(self.bot.config, setting)}` to `{new_value}`"
            )
            logger.info(f"Config updated: {setting} = {new_value} by {interaction.user}")

        except ValueError:
            await interaction.followup.send(
                f"❌ Invalid value for `{setting}`. Expected type: `{valid_settings[setting].__name__}`"
            )

    @app_commands.command(name="logs", description="Get recent bot logs")
    @app_commands.describe(lines="Number of log lines to retrieve (default: 20)")
    async def get_logs(self, interaction: discord.Interaction, lines: int = 20):
        """Get recent bot logs"""
        await interaction.response.defer(ephemeral=True)

        try:
            lines = min(lines, 50)  # Limit to 50 lines

            with open('rukiya_bot.log', 'r') as f:
                log_lines = f.readlines()

            recent_logs = ''.join(log_lines[-lines:])

            if len(recent_logs) > 1900:  # Discord message limit
                recent_logs = recent_logs[-1900:]
                recent_logs = "...\n" + recent_logs[recent_logs.find('\n') + 1:]

            await interaction.followup.send(f"```\n{recent_logs}\n```")

        except FileNotFoundError:
            await interaction.followup.send("❌ Log file not found")
        except Exception as e:
            await interaction.followup.send(f"❌ Error reading logs: {str(e)}")

    @app_commands.command(name="force_stop", description="Force stop all bot operations")
    async def force_stop(self, interaction: discord.Interaction):
        """Force stop all bot operations"""
        await interaction.response.defer(ephemeral=True)

        # Stop chat monitoring
        if self.bot.chat_monitor.is_running:
            self.bot.chat_monitor.stop_monitoring()
            status = "✅ Stopped chat monitoring"
        else:
            status = "ℹ️ Chat monitoring was not running"

        embed = discord.Embed(
            title="🚨 Force Stop Executed",
            description=status,
            color=discord.Color.orange()
        )

        await interaction.followup.send(embed=embed)
        logger.warning(f"Force stop executed by {interaction.user}")

    @app_commands.command(name="test_ai", description="Test AI response generation")
    @app_commands.describe(message="Message to test AI response with")
    async def test_ai(self, interaction: discord.Interaction, message: str):
        """Test AI response generation"""
        await interaction.response.defer(ephemeral=True)

        if not self.bot.ai_service.model:
            await interaction.followup.send("❌ AI service not initialized")
            return

        # Temporarily bypass cooldown for testing
        original_last_used = self.bot.ai_service.last_used
        self.bot.ai_service.last_used = 0

        try:
            response = self.bot.ai_service.generate_response(message, interaction.user.name)

            if response:
                embed = discord.Embed(
                    title="🤖 AI Test Response",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Input", value=f"`{message}`", inline=False)
                embed.add_field(name="Output", value=f"`{response}`", inline=False)
                embed.add_field(name="Length", value=f"{len(response)} chars", inline=True)

                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ AI did not generate a response (check triggers)")

        except Exception as e:
            await interaction.followup.send(f"❌ AI test failed: {str(e)}")

        finally:
            # Restore original last_used time
            self.bot.ai_service.last_used = original_last_used

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))