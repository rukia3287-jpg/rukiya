import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from cogs.chat_bot import RukiyaCog


class FakeUser:
    def __init__(self, id=123, name="User", bot=False):
        self.id = id
        self.name = name
        self.display_name = name
        self.bot = bot


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


class FakeChannel:
    def typing(self):
        return FakeTyping()


class FakeMessage:
    def __init__(self, content, author, mentions=None):
        self.content = content
        self.author = author
        self.mentions = mentions or []
        self.reply = AsyncMock()
        self.channel = FakeChannel()


class DiscordTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot_user = FakeUser(id=999, name="Rukiya", bot=True)
        self.bot.user = self.bot_user

        # Mock get_context so ctx.valid is False (not a command)
        ctx = MagicMock()
        ctx.valid = False
        self.bot.get_context = AsyncMock(return_value=ctx)

        self.cog = RukiyaCog(self.bot)
        self.cog.discord_cooldown_seconds = 0.0  # disable cooldown for testing logic

    async def test_ignore_bot_messages(self):
        bot_sender = FakeUser(id=111, name="OtherBot", bot=True)
        msg = FakeMessage("rukiya hello", author=bot_sender)
        await self.cog.on_message(msg)
        msg.reply.assert_not_called()

    async def test_ignore_non_trigger_messages(self):
        user = FakeUser(id=222, name="Alice", bot=False)
        msg = FakeMessage("hello everyone in chat", author=user)
        await self.cog.on_message(msg)
        msg.reply.assert_not_called()

    async def test_trigger_on_name_call(self):
        user = FakeUser(id=222, name="Alice", bot=False)
        msg = FakeMessage("hey rukiya how are you?", author=user)

        ai_mock = MagicMock()
        ai_mock.generate_response = AsyncMock(return_value="Oi. What do you want?")
        self.bot.ai_service = ai_mock

        await self.cog.on_message(msg)
        msg.reply.assert_called_once_with("Oi. What do you want?", mention_author=False)

    async def test_trigger_on_tag_mention(self):
        user = FakeUser(id=222, name="Alice", bot=False)
        msg = FakeMessage(f"<@{self.bot_user.id}> who is the strongest?", author=user, mentions=[self.bot_user])

        ai_mock = MagicMock()
        ai_mock.generate_response = AsyncMock(return_value="Yamamoto-sōtaichō, obviously.")
        self.bot.ai_service = ai_mock

        await self.cog.on_message(msg)
        msg.reply.assert_called_once_with("Yamamoto-sōtaichō, obviously.", mention_author=False)


if __name__ == "__main__":
    unittest.main()
