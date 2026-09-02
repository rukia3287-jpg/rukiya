import unittest
from unittest.mock import AsyncMock, MagicMock
import discord

from cogs.chat_bot import RukiyaCog
from cogs.utility_commands import Utility


class FakeInteraction:
    def __init__(self, user_name="Tester"):
        self.response = MagicMock()
        self.response.defer = AsyncMock()
        self.response.send_message = AsyncMock()
        self.response.is_done = MagicMock(return_value=True)

        self.followup = MagicMock()
        self.followup.send = AsyncMock()

        self.user = MagicMock()
        self.user.display_name = user_name


class SlashCommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.latency = 0.042

        self.chat_cog = RukiyaCog(self.bot)
        self.utility_cog = Utility(self.bot)

    async def test_slash_ask_basic(self):
        interaction = FakeInteraction(user_name="Ichigo")

        ai_mock = MagicMock()
        ai_mock.generate_response = AsyncMock(return_value="Tch. Don't be reckless, Ichigo.")
        self.bot.ai_service = ai_mock

        await self.chat_cog.slash_ask.callback(self.chat_cog, interaction, question="What should I do?")

        interaction.response.defer.assert_called_once_with(thinking=True)
        interaction.followup.send.assert_called_once()
        sent_embed = interaction.followup.send.call_args[1].get("embed")
        self.assertIsNotNone(sent_embed)
        self.assertIn("Tch. Don't be reckless, Ichigo.", sent_embed.description)
        self.assertIn("Ichigo", sent_embed.footer.text)

    async def test_slash_ask_with_yt_posting(self):
        interaction = FakeInteraction(user_name="Orihime")

        ai_mock = MagicMock()
        ai_mock.generate_response = AsyncMock(return_value="Stay safe, Orihime.")
        self.bot.ai_service = ai_mock

        cm_mock = MagicMock()
        cm_mock.is_running = True
        cm_mock.send_chat_message = AsyncMock(return_value=True)
        self.bot.chat_monitor = cm_mock

        await self.chat_cog.slash_ask.callback(self.chat_cog, interaction, question="Are you okay?", post_to_yt=True)

        cm_mock.send_chat_message.assert_called_once_with("Stay safe, Orihime.")
        sent_embed = interaction.followup.send.call_args[1].get("embed")
        self.assertIn("Posted to YouTube live chat", sent_embed.description)

    async def test_slash_say_when_yt_not_running(self):
        interaction = FakeInteraction()
        self.bot.chat_monitor = None

        await self.chat_cog.slash_say.callback(self.chat_cog, interaction, text="Hello YouTube")
        interaction.followup.send.assert_called_once()
        self.assertIn("not currently running", interaction.followup.send.call_args[0][0])

    async def test_slash_say_when_yt_running(self):
        interaction = FakeInteraction()
        cm_mock = MagicMock()
        cm_mock.is_running = True
        cm_mock.send_chat_message = AsyncMock(return_value=True)
        self.bot.chat_monitor = cm_mock

        await self.chat_cog.slash_say.callback(self.chat_cog, interaction, text="Stream starts now!")
        cm_mock.send_chat_message.assert_called_once_with("Stream starts now!")
        self.assertIn("Sent to YouTube live chat", interaction.followup.send.call_args[0][0])

    async def test_slash_auto_reply_actions(self):
        interaction = FakeInteraction()

        # Disable
        await self.chat_cog.slash_auto_reply.callback(self.chat_cog, interaction, action="disable")
        self.assertFalse(self.chat_cog.enabled)

        # Enable
        await self.chat_cog.slash_auto_reply.callback(self.chat_cog, interaction, action="enable")
        self.assertTrue(self.chat_cog.enabled)

        # Status
        await self.chat_cog.slash_auto_reply.callback(self.chat_cog, interaction, action="status")
        interaction.followup.send.assert_called()

    async def test_slash_rukiya_info(self):
        interaction = FakeInteraction()
        await self.chat_cog.slash_rukiya_info.callback(self.chat_cog, interaction)
        sent_embed = interaction.followup.send.call_args[1].get("embed")
        self.assertIsNotNone(sent_embed)
        self.assertIn("Kuchiki Rukiya", sent_embed.title)
        self.assertIn("Sode no Shirayuki", sent_embed.description)

    async def test_slash_ping(self):
        interaction = FakeInteraction()
        await self.utility_cog.ping.callback(self.utility_cog, interaction)
        sent_embed = interaction.followup.send.call_args[1].get("embed")
        self.assertIsNotNone(sent_embed)
        self.assertIn("42 ms", sent_embed.description)

    async def test_slash_uptime(self):
        interaction = FakeInteraction()
        await self.utility_cog.uptime.callback(self.utility_cog, interaction)
        sent_embed = interaction.followup.send.call_args[1].get("embed")
        self.assertIsNotNone(sent_embed)
        self.assertIn("Bot Uptime", sent_embed.title)

    async def test_slash_help(self):
        interaction = FakeInteraction()
        await self.utility_cog.help_command.callback(self.utility_cog, interaction)
        sent_embed = interaction.followup.send.call_args[1].get("embed")
        self.assertIsNotNone(sent_embed)
        self.assertIn("Rukiya Bot — Commands & Guide", sent_embed.title)
        # Check that sections are present
        field_names = [f.name for f in sent_embed.fields]
        self.assertTrue(any("AI & Chat" in name for name in field_names))
        self.assertTrue(any("YouTube Live Chat" in name for name in field_names))
        self.assertTrue(any("Fun & Community" in name for name in field_names))
        self.assertTrue(any("Utilities & Diagnostics" in name for name in field_names))


if __name__ == "__main__":
    unittest.main()
