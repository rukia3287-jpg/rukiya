"""Load every cog and serialize registered commands without calling Discord APIs."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from services.config import Config
from services.discord_ui import handle_error


class BotWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_extensions_load_and_commands_serialize(self):
        from main import RukiyaBot
        with tempfile.TemporaryDirectory() as directory:
            config = Config(db_path=os.path.join(directory, 'memory.db'))
            with patch('main.Config', return_value=config), patch.dict(os.environ, {'DISCORD_MEMBER_WELCOMES_ENABLED': 'false'}):
                bot = RukiyaBot()
            bot.tree.sync = AsyncMock(return_value=[])
            try:
                await bot.setup_hook()
                self.assertFalse(bot.failed_cogs)
                self.assertIs(bot.tree.on_error, handle_error)
                self.assertFalse(bot.intents.members)
                payloads = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
                self.assertGreaterEqual(len(payloads), 19)
                names = {command['name'] for command in payloads}
                self.assertTrue({'help', 'ask', 'start', 'bot_status', 'memory_reset'}.issubset(names))
                self.assertIsNone(bot.command_sync_error)
            finally:
                await bot.close()

    async def test_members_intent_is_explicitly_opt_in_and_sync_failure_is_visible(self):
        from main import RukiyaBot
        with tempfile.TemporaryDirectory() as directory:
            config = Config(db_path=os.path.join(directory, 'memory.db'))
            with patch('main.Config', return_value=config), patch.dict(os.environ, {'DISCORD_MEMBER_WELCOMES_ENABLED': 'true'}):
                bot = RukiyaBot()
            bot.tree.sync = AsyncMock(side_effect=RuntimeError('synthetic sync outage'))
            try:
                with self.assertLogs('main', level='ERROR'):
                    await bot.setup_hook()
                self.assertTrue(bot.intents.members)
                self.assertEqual(bot.command_sync_error, 'RuntimeError')
                self.assertFalse(bot.failed_cogs)
            finally:
                await bot.close()
