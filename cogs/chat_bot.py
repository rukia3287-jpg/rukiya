
from __future__ import annotations
import os
import asyncio
import logging
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_OPENROUTER_BASE = "https://api.openrouter.ai"


class RukiyaCog(commands.Cog):
    """Discord Cog that wires ChatMonitor -> OpenRouter (deepseek_r1)

    Expectations:
      - bot.chat_monitor is set to your ChatMonitor instance before loading this cog.
      - ChatMonitor exposes:
          - subscribe(callback) where callback is async def cb(message, author)
          - send_chat_message(text) -> async wrapper that returns bool
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.enabled = False  # whether to auto-respond in YT chat
        self.model = "deepseek_r1"
        self.openrouter_base = os.environ.get("OPENROUTER_BASE", DEFAULT_OPENROUTER_BASE)
        self.api_key = os.environ.get(OPENROUTER_API_KEY_ENV)
        if not self.api_key:
            logger.warning(
                f"{OPENROUTER_API_KEY_ENV} not set. One-off asks will fail until it's configured."
            )

        # optional configuration
        self.max_tokens = int(os.environ.get("RUKIYA_MAX_TOKENS", "300"))
        self.temperature = float(os.environ.get("RUKIYA_TEMP", "0.8"))
        self.cooldown_seconds = float(os.environ.get("RUKIYA_COOLDOWN", "1.5"))
        self._last_sent_at = 0.0

    # ----------------
    # Discord lifecycle
    # ----------------
    async def cog_load(self) -> None:  # called on cog load in newer discord.py
        # create a shared aiohttp session
        self.session = aiohttp.ClientSession()
        # auto-subscribe if bot has chat_monitor
        cm = getattr(self.bot, "chat_monitor", None)
        if cm:
            cm.subscribe(self.on_yt_message)
            logger.info("RukiyaCog subscribed to bot.chat_monitor")
        else:
            logger.warning("bot.chat_monitor not present; RukiyaCog did not subscribe automatically")

    async def cog_unload(self) -> None:
        # cleanup
        cm = getattr(self.bot, "chat_monitor", None)
        if cm:
            try:
                cm.unsubscribe(self.on_yt_message)
            except Exception:
                pass
        if self.session and not self.session.closed:
            await self.session.close()

    # ----------------
    # YouTube chat handling
    # ----------------
    async def on_yt_message(self, message: str, author: str):
        """Callback subscribed to ChatMonitor. Called for every new chat message."""
        if not self.enabled:
            return

        # Basic cooldown to avoid spamming
        now = asyncio.get_event_loop().time()
        if now - self._last_sent_at < self.cooldown_seconds:
            logger.debug("RukiyaCog cooling down; skipping response")
            return

        # build the prompt/message for OpenRouter
        # We'll send a short system instruction then a user message
        user_prompt = (
            f"User ({author}) wrote: {message}\n\nRespond as Rukiya from Bleach."
            " Keep replies concise (under 120 words), slightly tsundere but kind, and"
            " avoid spoilers about plot. Use casual, natural speech suitable for a livestream chat."
        )

        try:
            reply = await self.generate_reply(user_prompt)
        except Exception as e:
            logger.exception(f"OpenRouter generate_reply failed: {e}")
            return

        if not reply:
            return

        cm = getattr(self.bot, "chat_monitor", None)
        if not cm:
            logger.error("bot.chat_monitor missing when trying to send reply")
            return

        sent = await cm.send_chat_message(reply)
        if sent:
            self._last_sent_at = now
            logger.info("RukiyaCog sent reply to YouTube chat")
        else:
            logger.warning("RukiyaCog failed to send reply via ChatMonitor")

    # ----------------
    # OpenRouter integration
    # ----------------
    async def generate_reply(self, prompt: str) -> Optional[str]:
        """Call OpenRouter chat completions for deepseek_r1 model.

        Returns: text or None
        """
        if not self.api_key:
            raise RuntimeError(f"{OPENROUTER_API_KEY_ENV} not configured")

        if not self.session:
            self.session = aiohttp.ClientSession()

        url = f"{self.openrouter_base}/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Build system prompt to shape Rukiya's behavior. Keep it explicit but brief.
        system_message = (
            "You are roleplaying 'Rukiya', a character inspired by a calm, slightly"
            " tsundere young woman with quick wit and a gentle sense of justice."
            " Speak in short, natural sentences appropriate for a live chat."
            " Never claim to be the user or the platform. Keep replies friendly but"
            " assertive when needed. Do not produce disallowed content."
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        try:
            async with self.session.post(url, json=payload, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"OpenRouter API returned {resp.status}: {text}")
                    return None

                data = await resp.json()

                # Attempt to extract model text. Different routers may use different shapes.
                # We try a few common possibilities.
                # 1) data['choices'][0]['message']['content'] (chat-style)
                # 2) data['output'][0]['content'][0]['text'] (other)
                content = None
                try:
                    content = data.get("choices", [])[0].get("message", {}).get("content")
                except Exception:
                    content = None

                if not content:
                    # fallback attempts
                    try:
                        content = data.get("output", [])[0].get("content", [])[0].get("text")
                    except Exception:
                        content = None

                if not content:
                    logger.error("OpenRouter response missing expected content")
                    return None

                # Post-process: trim and ensure length
                content = content.strip()
                if len(content) > 900:
                    content = content[:900].rsplit(".", 1)[0] + "."

                return content

        except asyncio.TimeoutError:
            logger.warning("OpenRouter request timed out")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error calling OpenRouter: {e}")
            return None

    # ----------------
    # Discord commands to control the cog
    # ----------------
    @commands.group(name="rukiya", invoke_without_command=True)
    async def rukiya_group(self, ctx: commands.Context):
        """Control Rukiya YouTube responder."""
        await ctx.send("Available subcommands: enable, disable, ask")

    @rukiya_group.command(name="enable")
    @commands.is_owner()
    async def cmd_enable(self, ctx: commands.Context):
        self.enabled = True
        await ctx.send("Rukiya auto-responder enabled for YouTube chat.")

    @rukiya_group.command(name="disable")
    @commands.is_owner()
    async def cmd_disable(self, ctx: commands.Context):
        self.enabled = False
        await ctx.send("Rukiya auto-responder disabled.")

    @rukiya_group.command(name="ask")
    @commands.is_owner()
    async def cmd_ask(self, ctx: commands.Context, *, question: str):
        """Ask Rukiya a one-off question and post the answer to YouTube chat."""
        await ctx.send("Asking Rukiya...")
        try:
            reply = await self.generate_reply(question)
        except Exception as e:
            await ctx.send(f"Failed to get reply: {e}")
            return

        if not reply:
            await ctx.send("No reply from model.")
            return

        cm = getattr(self.bot, "chat_monitor", None)
        if not cm:
            await ctx.send("bot.chat_monitor not configured; cannot send to YouTube chat")
            return

        ok = await cm.send_chat_message(reply)
        if ok:
            await ctx.send("Posted reply to YouTube chat.")
        else:
            await ctx.send("Failed to post reply to YouTube chat.")


# Standard setup function for older discord.py loader style
async def setup(bot: commands.Bot):
    await bot.add_cog(RukiyaCog(bot))
