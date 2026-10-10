import os
import re
import time
import asyncio
import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from services.ai_service import RUKIYA_SYSTEM_PROMPT as SAFE_RUKIYA_SYSTEM_PROMPT, validate_rukiya_response

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Rukiya persona is centralized in services.ai_service so Discord and YouTube
# use the same intelligent, context-aware behavior rules.
RUKIYA_SYSTEM_PROMPT = SAFE_RUKIYA_SYSTEM_PROMPT
class RukiyaCog(commands.Cog):
    """Discord Cog — wires ChatMonitor ↔ OpenRouter for Rukiya (Bleach) persona"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.enabled = os.environ.get("RUKIYA_AUTO_REPLY", "true").lower() in ("true", "1", "yes")
        self.model = os.environ.get("RUKIYA_MODEL", os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-r1"))
        self.openrouter_base = os.environ.get("OPENROUTER_BASE", DEFAULT_OPENROUTER_BASE)
        self.api_key = os.environ.get(OPENROUTER_API_KEY_ENV)

        if not self.api_key:
            logger.warning(f"{OPENROUTER_API_KEY_ENV} not set. RukiyaCog will not function.")

        try:
            self.max_tokens = max(16, int(os.environ.get("RUKIYA_MAX_TOKENS", "180")))
        except (TypeError, ValueError):
            self.max_tokens = 180

        try:
            self.temperature = min(
                2.0,
                max(0.0, float(os.environ.get("RUKIYA_TEMP", os.environ.get("AI_TEMPERATURE", "0.85"))))
            )
        except (TypeError, ValueError):
            self.temperature = 0.85

        try:
            self.cooldown_seconds = max(0.0, float(os.environ.get("RUKIYA_COOLDOWN", "3.0")))
        except (TypeError, ValueError):
            self.cooldown_seconds = 3.0
        self._last_sent_at = 0.0
        # Discord cooldown is per-user. YouTube/auto-reply activity is separate.
        self._last_discord_reply_at = {}
        self.discord_cooldown_seconds = float(os.environ.get("RUKIYA_DISCORD_COOLDOWN", "2.0"))
        self._name_pattern = re.compile(r"\b(rukiya|rukia|ruki)\b", re.IGNORECASE)

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        cm = getattr(self.bot, "chat_monitor", None)
        if cm:
            cm.subscribe(self.on_yt_message)
            logger.info("RukiyaCog subscribed to bot.chat_monitor")
        else:
            logger.warning("bot.chat_monitor not present — YouTube auto-reply disabled")

    async def cog_unload(self) -> None:
        cm = getattr(self.bot, "chat_monitor", None)
        if cm:
            try:
                cm.unsubscribe(self.on_yt_message)
            except Exception:
                pass
        if self.session and not self.session.closed:
            await self.session.close()

    # ────────────────────────────────────────────
    # Discord chat listener (reply when tagged or called by name)
    # ────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Reply when Rukiya is tagged or called by name in Discord."""
        # Never reply to bots (prevents recursive loops)
        if message.author.bot:
            return

        # Don't intercept command invocations
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        # Check if the bot is mentioned or tagged
        bot_user = self.bot.user
        is_tagged = False
        if bot_user and (bot_user in message.mentions or f"<@{bot_user.id}>" in message.content or f"<@!{bot_user.id}>" in message.content):
            is_tagged = True

        # Check if called by name
        is_named = bool(self._name_pattern.search(message.content))

        if not (is_tagged or is_named):
            return

        # Cooldown is per Discord user, so one user's recent reply cannot
        # silence another user's explicit call to Rukiya.
        now = asyncio.get_event_loop().time()
        user_id = message.author.id
        last_user_reply = self._last_discord_reply_at.get(user_id, 0.0)
        if now - last_user_reply < self.discord_cooldown_seconds:
            return

        # Clean prompt by stripping bot mention tag
        cleaned_text = message.content
        if bot_user:
            cleaned_text = cleaned_text.replace(f"<@{bot_user.id}>", "").replace(f"<@!{bot_user.id}>", "")
        cleaned_text = cleaned_text.strip()
        if not cleaned_text:
            cleaned_text = "oi"

        try:
            async with message.channel.typing():
                from services.orchestrator import RukiyaOrchestrator
                orch = getattr(self.bot, "orchestrator", None)
                ai = getattr(self.bot, "ai_service", None)
                reply = None
                if isinstance(orch, RukiyaOrchestrator):
                    reply = await orch.process_raw_text(
                        cleaned_text,
                        author=message.author.display_name,
                        platform="discord",
                        user_id=str(message.author.id),
                        bypass_trigger=True,
                        bypass_cooldown=True
                    )
                elif ai and hasattr(ai, "generate_response"):
                    reply = await ai.generate_response(
                        cleaned_text,
                        message.author.display_name,
                        bypass_trigger=True,
                        bypass_cooldown=True
                    )
                if not reply:
                    reply = await self.generate_reply(cleaned_text, author=message.author.display_name)

                if reply:
                    self._last_discord_reply_at[user_id] = asyncio.get_event_loop().time()
                    await message.reply(reply, mention_author=False)
        except Exception as e:
            logger.exception(f"Failed to reply to Discord message: {e}")

    # ────────────────────────────────────────────
    # YouTube chat callback
    # ────────────────────────────────────────────
    async def on_yt_message(self, message: str, author: str, author_details: Optional[dict] = None):
        """Called by ChatMonitor for every YouTube chat message."""
        if not self.enabled:
            return

        now = asyncio.get_event_loop().time()
        if now - self._last_sent_at < self.cooldown_seconds:
            return

        try:
            from services.orchestrator import RukiyaOrchestrator
            orch = getattr(self.bot, "orchestrator", None)
            ai = getattr(self.bot, "ai_service", None)
            reply = None

            if isinstance(orch, RukiyaOrchestrator):
                from services.models import ChatMessage
                details = author_details or {}
                # The channel ID is the stable identity; display names are neither unique
                # nor immutable. Without one (legacy two-argument delivery) resolve() falls
                # back to the username key, which is the previous behavior.
                channel_id = str(details.get("channelId") or "").strip() or None
                chat_msg = ChatMessage(
                    platform="youtube",
                    message_id=f"yt_{int(time.time() * 1000)}",
                    user_id=channel_id,
                    username=author,
                    display_name=author,
                    text=message,
                    is_owner=bool(details.get("isChatOwner")),
                    is_moderator=bool(details.get("isChatModerator")),
                )
                res = await orch.process_message(chat_msg)
                reply = res.text if res else None
            elif ai and hasattr(ai, "generate_response"):
                reply = await ai.generate_response(message, author)
            else:
                reply = await self.generate_reply(message, author)

            if not reply:
                logger.debug("No YouTube reply generated for %s: %s", author, message[:120])
                return

            cm = getattr(self.bot, "chat_monitor", None)
            if not cm:
                logger.error("bot.chat_monitor missing — cannot send reply")
                return

            sent = await cm.send_chat_message(reply)
            if sent:
                self._last_sent_at = asyncio.get_event_loop().time()
                logger.info("Rukiya replied to %s: %s", author, reply)

        except Exception:
            logger.exception(
                "YouTube auto-reply failed for author=%s message=%r",
                author,
                message[:120],
            )

    # ────────────────────────────────────────────
    # OpenRouter call (used by direct commands)
    # ────────────────────────────────────────────
    async def generate_reply(self, message: str, author: str = "viewer") -> Optional[str]:
        """Direct OpenRouter call with Rukiya Bleach persona."""
        if not self.api_key:
            raise RuntimeError(f"{OPENROUTER_API_KEY_ENV} not configured")

        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

        url = f"{self.openrouter_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/yourusername/your-bot",
            "X-Title": "Rukiya Discord Bot",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": RUKIYA_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"[Stream viewer '{author}' says]: {message}\n\n"
                        "Reply as Rukiya — short, punchy, in-character. 1-3 sentences max."
                    ),
                },
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        try:
            async with self.session.post(url, json=payload, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"OpenRouter returned {resp.status}: {text}")
                    return None

                data = await resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    logger.error("OpenRouter response missing content")
                    return None

                content = validate_rukiya_response(content.strip())
                # Hard cap
                if len(content) > 300:
                    last = max(content.rfind("."), content.rfind("!"), content.rfind("?"))
                    content = content[:last + 1] if last > 0 else content[:300] + "..."

                return content

        except asyncio.TimeoutError:
            logger.warning("OpenRouter request timed out")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error calling OpenRouter: {e}")
            return None

    # ────────────────────────────────────────────
    # Prefix commands  (!rukiya enable / disable / ask / say)
    # ────────────────────────────────────────────
    @commands.group(name="rukiya", invoke_without_command=True)
    @commands.is_owner()
    async def rukiya_group(self, ctx: commands.Context):
        """Control Rukiya YouTube responder. Subcommands: enable, disable, ask, say"""
        embed = discord.Embed(
            title="🗡️ Rukiya Controls",
            description=(
                "`!rukiya enable` — start auto-responding to YouTube chat\n"
                "`!rukiya disable` — stop auto-responding\n"
                "`!rukiya ask <message>` — ask Rukiya something (sends to YT chat)\n"
                "`!rukiya say <message>` — send a raw message to YT chat\n"
                "`!rukiya status` — current status"
            ),
            color=discord.Color.dark_red(),
        )
        await ctx.send(embed=embed)

    @rukiya_group.command(name="enable")
    @commands.is_owner()
    async def cmd_enable(self, ctx: commands.Context):
        self.enabled = True
        await ctx.send("⚡ Rukiya auto-responder **enabled**. She'll reply to YouTube chat when triggered.")

    @rukiya_group.command(name="disable")
    @commands.is_owner()
    async def cmd_disable(self, ctx: commands.Context):
        self.enabled = False
        await ctx.send("🛑 Rukiya auto-responder **disabled**.")

    @rukiya_group.command(name="status")
    @commands.is_owner()
    async def cmd_status(self, ctx: commands.Context):
        cm = getattr(self.bot, "chat_monitor", None)
        yt_running = cm.is_running if cm else False
        embed = discord.Embed(title="🗡️ Rukiya Status", color=discord.Color.dark_red())
        embed.add_field(name="Auto-responder", value="✅ Enabled" if self.enabled else "❌ Disabled", inline=True)
        embed.add_field(name="YT Chat", value="🟢 Running" if yt_running else "🔴 Stopped", inline=True)
        embed.add_field(name="Model", value=f"`{self.model}`", inline=False)
        embed.add_field(name="Cooldown", value=f"{self.cooldown_seconds}s", inline=True)
        await ctx.send(embed=embed)

    @rukiya_group.command(name="ask")
    @commands.is_owner()
    async def cmd_ask(self, ctx: commands.Context, *, question: str):
        """Ask Rukiya something and post her reply to YouTube chat."""
        async with ctx.typing():
            try:
                reply = await self.generate_reply(question, author=ctx.author.display_name)
            except Exception as e:
                await ctx.send(f"❌ Failed to get reply: {e}")
                return

        if not reply:
            await ctx.send("❌ No reply from model.")
            return

        # Show reply in Discord too
        embed = discord.Embed(
            title="🗡️ Rukiya says...",
            description=reply,
            color=discord.Color.dark_red()
        )
        await ctx.send(embed=embed)

        cm = getattr(self.bot, "chat_monitor", None)
        if not cm or not cm.is_running:
            await ctx.send("⚠️ YT chat not running — reply shown in Discord only.")
            return

        ok = await cm.send_chat_message(reply)
        if ok:
            await ctx.send("✅ Posted to YouTube chat.")
        else:
            await ctx.send("❌ Failed to post to YouTube chat.")

    @rukiya_group.command(name="say")
    @commands.is_owner()
    async def cmd_say(self, ctx: commands.Context, *, text: str):
        """Send a raw message to YouTube chat as Rukiya."""
        cm = getattr(self.bot, "chat_monitor", None)
        if not cm or not cm.is_running:
            await ctx.send("❌ YouTube chat is not running. Use `/start` first.")
            return

        ok = await cm.send_chat_message(text)
        if ok:
            await ctx.send(f"✅ Sent to YT chat: `{text[:80]}`")
        else:
            await ctx.send("❌ Failed to send to YouTube chat.")

    # ────────────────────────────────────────────
    # Slash Commands
    # ────────────────────────────────────────────
    @app_commands.command(name="ask", description="Ask Rukiya a question directly")
    @app_commands.describe(
        question="What do you want to ask Rukiya?",
        post_to_yt="Also post Rukiya's reply to YouTube live chat if active (default: False)"
    )
    async def slash_ask(self, interaction: discord.Interaction, question: str, post_to_yt: bool = False):
        """Ask Rukiya a question directly via slash command."""
        if post_to_yt and (
            not interaction.guild
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "❌ Only server administrators can post an AI reply to YouTube chat.",
                ephemeral=True,
            )
            return

        try:
            await interaction.response.defer(thinking=True)
        except Exception:
            pass

        from services.orchestrator import RukiyaOrchestrator
        orch = getattr(self.bot, "orchestrator", None)
        ai = getattr(self.bot, "ai_service", None)
        reply = None
        if isinstance(orch, RukiyaOrchestrator):
            reply = await orch.process_raw_text(
                question,
                author=interaction.user.display_name,
                platform="discord",
                user_id=str(interaction.user.id),
                bypass_trigger=True,
                bypass_cooldown=True
            )
        elif ai and hasattr(ai, "generate_response"):
            reply = await ai.generate_response(
                question,
                interaction.user.display_name,
                bypass_trigger=True,
                bypass_cooldown=True
            )
        if not reply:
            reply = await self.generate_reply(question, author=interaction.user.display_name)

        if not reply:
            await interaction.followup.send("❌ No response from model. Please check logs or API key.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🗡️ Rukiya says...",
            description=reply,
            color=discord.Color.dark_red()
        )
        embed.set_footer(text=f"Asked by {interaction.user.display_name}")

        if post_to_yt:
            cm = getattr(self.bot, "chat_monitor", None)
            if cm and cm.is_running:
                ok = await cm.send_chat_message(reply)
                yt_status_msg = "\n\n*(✅ Posted to YouTube live chat)*" if ok else "\n\n*(❌ Failed to post to YouTube live chat)*"
            else:
                yt_status_msg = "\n\n*(⚠️ YouTube live chat is not active)*"
            embed.description += yt_status_msg

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="say", description="Send a message to YouTube live chat as Rukiya")
    @app_commands.describe(text="Message text to post in YouTube live chat")
    @app_commands.checks.has_permissions(administrator=True)
    async def slash_say(self, interaction: discord.Interaction, text: str):
        """Send a message directly to YouTube live chat."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        cm = getattr(self.bot, "chat_monitor", None)
        if not cm or not cm.is_running:
            await interaction.followup.send("❌ YouTube live chat is not currently running. Start monitoring with `/start <video_id>` first.", ephemeral=True)
            return

        ok = await cm.send_chat_message(text)
        if ok:
            await interaction.followup.send(f"✅ Sent to YouTube live chat: `{text[:100]}`", ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to send message to YouTube live chat.", ephemeral=True)

    @app_commands.command(name="auto_reply", description="Check or toggle YouTube chat auto-replying")
    @app_commands.describe(action="Action to perform: status, enable, or disable")
    @app_commands.choices(action=[
        app_commands.Choice(name="Status (View current setting)", value="status"),
        app_commands.Choice(name="Enable (Auto-respond to triggers)", value="enable"),
        app_commands.Choice(name="Disable (Stop auto-responding)", value="disable"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def slash_auto_reply(self, interaction: discord.Interaction, action: str = "status"):
        """Manage auto-reply setting for YouTube live chat."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        if action == "enable":
            self.enabled = True
            await interaction.followup.send("⚡ Rukiya auto-reply **enabled** for YouTube chat.", ephemeral=True)
        elif action == "disable":
            self.enabled = False
            await interaction.followup.send("🛑 Rukiya auto-reply **disabled** for YouTube chat.", ephemeral=True)
        else:
            status_text = "✅ **Enabled**" if self.enabled else "❌ **Disabled**"
            await interaction.followup.send(f"🗡️ YouTube chat auto-reply is currently: {status_text}", ephemeral=True)

    @app_commands.command(name="rukiya_info", description="View Rukiya's character profile and system details")
    async def slash_rukiya_info(self, interaction: discord.Interaction):
        """Display information about Rukiya's character and active configuration."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        embed = discord.Embed(
            title="🗡️ Kuchiki Rukiya (朽木 ルキア)",
            description=(
                "Soul Reaper of the Gotei 13, Lieutenant of the Thirteenth Division.\n"
                "Wielder of **Sode no Shirayuki** (袖白雪, *Sleeved White Snow*), the most beautiful Zanpakutō in the Soul Society.\n\n"
                "**Personality**: Proud, sharp-tongued, tsundere, battle-hardened, with a secret soft side."
            ),
            color=discord.Color.dark_red()
        )
        embed.add_field(name="AI Model", value=f"`{self.model}`", inline=True)
        embed.add_field(name="Auto-Reply", value="✅ Active" if self.enabled else "❌ Inactive", inline=True)
        embed.add_field(name="Discord Cooldown", value=f"{self.discord_cooldown_seconds}s", inline=True)
        embed.add_field(name="Chat Cooldown", value=f"{self.cooldown_seconds}s", inline=True)
        embed.set_footer(text="Call my name or tag me anytime in chat! ❄️")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RukiyaCog(bot))

