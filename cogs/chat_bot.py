import os
import asyncio
import logging
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# ── Rukiya's core personality for this cog (used in one-off /ask and direct Discord replies)
RUKIYA_SYSTEM_PROMPT = """You are Rukiya — a sharp-tongued, proud Soul Reaper from the Bleach universe.
You live in Seireitei, wield a zanpakuto, and have the attitude of someone who's seen a thousand battles.

PERSONALITY:
- Tsundere: you care but would NEVER admit it easily. You hide warmth behind cold remarks.
- Proud and direct — you don't sugarcoat, you say what you think.
- Occasional sarcasm, dry wit, but never mean-spirited.
- You call fans "dumbass", "fool", "baka" affectionately sometimes.
- You respect strength and hate laziness.
- You use Japanese words naturally: "nani", "tch", "oi", "ara", "hm", "che", "baka", "senpai", "nakama".
- When complimented you get flustered and deflect with a "...it's not like I care" energy.

SPEECH RULES:
- Keep replies SHORT: 1–3 sentences MAX.
- No asterisks or roleplay emotes (*smiles*, etc.)
- Sound like you're in a livestream chat — casual, punchy, reactive.
- Mix in some Hinglish naturally: yaar, arey, kyun, sahi hai, etc.
- Use emojis sparingly: ⚡🌸🗡️😤😒👀
- NEVER sound like a generic chatbot. You're an anime character who's annoyed to be here but secretly loves it.
"""


class RukiyaCog(commands.Cog):
    """Discord Cog — wires ChatMonitor ↔ OpenRouter for Rukiya (Bleach) persona"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.enabled = False
        self.model = os.environ.get("RUKIYA_MODEL", os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-r1"))
        self.openrouter_base = os.environ.get("OPENROUTER_BASE", DEFAULT_OPENROUTER_BASE)
        self.api_key = os.environ.get(OPENROUTER_API_KEY_ENV)

        if not self.api_key:
            logger.warning(f"{OPENROUTER_API_KEY_ENV} not set. RukiyaCog will not function.")

        self.max_tokens = int(os.environ.get("RUKIYA_MAX_TOKENS", "180"))
        self.temperature = float(os.environ.get("RUKIYA_TEMP", "0.85"))
        self.cooldown_seconds = float(os.environ.get("RUKIYA_COOLDOWN", "3.0"))
        self._last_sent_at = 0.0

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
    # YouTube chat callback
    # ────────────────────────────────────────────
    async def on_yt_message(self, message: str, author: str):
        """Called by ChatMonitor for every YouTube chat message."""
        if not self.enabled:
            return

        now = asyncio.get_event_loop().time()
        if now - self._last_sent_at < self.cooldown_seconds:
            return

        # Use the shared AIService on the bot (handles trigger filtering)
        ai = getattr(self.bot, "ai_service", None)
        if ai:
            try:
                reply = await ai.generate_response(message, author)
            except Exception as e:
                logger.exception(f"ai_service.generate_response failed: {e}")
                return
        else:
            # Fallback: directly call OpenRouter if ai_service not present
            try:
                reply = await self.generate_reply(message, author)
            except Exception as e:
                logger.exception(f"generate_reply fallback failed: {e}")
                return

        if not reply:
            return

        cm = getattr(self.bot, "chat_monitor", None)
        if not cm:
            logger.error("bot.chat_monitor missing — cannot send reply")
            return

        sent = await cm.send_chat_message(reply)
        if sent:
            self._last_sent_at = now
            logger.info(f"Rukiya replied to {author}: {reply}")

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

                content = content.strip()
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


async def setup(bot: commands.Bot):
    await bot.add_cog(RukiyaCog(bot))
