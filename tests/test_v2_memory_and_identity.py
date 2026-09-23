import os
import tempfile
import unittest
from services.config import Config
from services.models import ChatMessage
from services.identity_service import IdentityService
from services.memory_service import MemoryService


class TestIdentityService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_identity.db")
        self.config = Config(db_path=self.db_path)
        self.memory = MemoryService(self.config)
        self.identity = IdentityService(self.memory)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_stable_platform_user_id(self):
        msg = ChatMessage(
            platform="youtube",
            message_id="m1",
            user_id="UC_123456",
            username="gamer123",
            display_name="AwesomeGamer",
            text="Hello"
        )
        resolved = self.identity.resolve(msg)
        self.assertEqual(resolved.canonical_id, "youtube:UC_123456")
        self.assertEqual(resolved.display_name, "AwesomeGamer")

    def test_duplicate_display_names_remain_separate(self):
        msg1 = ChatMessage(
            platform="youtube",
            message_id="m1",
            user_id="UC_A",
            username="alex_yt",
            display_name="Alex",
            text="Hello from YT"
        )
        msg2 = ChatMessage(
            platform="discord",
            message_id="m2",
            user_id="987654321",
            username="alex_dc",
            display_name="Alex",
            text="Hello from Discord"
        )
        user1 = self.identity.resolve(msg1)
        user2 = self.identity.resolve(msg2)
        self.assertNotEqual(user1.canonical_id, user2.canonical_id)
        self.assertEqual(user1.canonical_id, "youtube:UC_A")
        self.assertEqual(user2.canonical_id, "discord:987654321")

    def test_explicit_link_identities(self):
        self.identity.link_identities("discord:111", "youtube:UC_222")
        msg = ChatMessage(
            platform="youtube",
            message_id="m3",
            user_id="UC_222",
            username="yt_user",
            display_name="LinkedUser",
            text="Linked"
        )
        resolved = self.identity.resolve(msg)
        self.assertEqual(resolved.canonical_id, "discord:111")


class TestMemoryService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_memory.db")
        self.config = Config(db_path=self.db_path, max_memory_per_user=5, max_stream_memory=5)
        self.memory = MemoryService(self.config)
        self.identity = IdentityService(self.memory)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_explicit_memory_stored_and_retrieved(self):
        msg = ChatMessage(
            platform="youtube",
            message_id="m1",
            user_id="UC_krishna",
            username="krishna",
            display_name="Krishna",
            text="My name is Krishna and I mainly play Genshin."
        )
        user = self.identity.resolve(msg)
        extracted = self.memory.extract_and_store_facts(user, msg.text)
        self.assertGreaterEqual(len(extracted), 1)

        keys = [e.key for e in extracted]
        self.assertIn("preferred_name", keys)

        # Retrieve relevant memory
        relevant = self.memory.get_relevant_user_memory(user, "What game should I play?")
        rel_keys = [m.key for m in relevant]
        self.assertTrue("favorite_game" in rel_keys or "preferred_name" in rel_keys)

    def test_low_value_chatter_ignored(self):
        msg = ChatMessage(
            platform="youtube",
            message_id="m2",
            user_id="UC_random",
            username="viewer",
            display_name="Viewer",
            text="lol I died"
        )
        user = self.identity.resolve(msg)
        extracted = self.memory.extract_and_store_facts(user, msg.text)
        self.assertEqual(len(extracted), 0)

    def test_memory_conflict_resolution(self):
        msg = ChatMessage(platform="youtube", message_id="m1", user_id="UC_gamer", username="gamer", display_name="Gamer", text="init")
        user = self.identity.resolve(msg)

        # First statement
        self.memory.set_user_memory(user.canonical_id, "favorite_game", "Genshin", confidence=0.8)
        # Conflicting statement
        self.memory.set_user_memory(user.canonical_id, "favorite_game", "Wuthering Waves", confidence=0.9)

        all_mems = self.memory.get_all_user_memories(user.canonical_id)
        fav = next(m for m in all_mems if m.key == "favorite_game")
        self.assertEqual(fav.value, "Wuthering Waves")
        self.assertEqual(fav.usage_count, 2)

    def test_memory_limits_enforced(self):
        msg = ChatMessage(platform="youtube", message_id="m1", user_id="UC_limit", username="limit", display_name="Limit", text="limit")
        user = self.identity.resolve(msg)

        # Insert 8 facts (limit is 5)
        for i in range(8):
            self.memory.set_user_memory(user.canonical_id, f"fact_{i}", f"val_{i}", confidence=0.5 + (i * 0.05))

        all_mems = self.memory.get_all_user_memories(user.canonical_id)
        self.assertLessEqual(len(all_mems), 5)

    def test_stream_memory_expiration_on_end_session(self):
        session = self.memory.start_stream_session("video_abc")
        msg = ChatMessage(platform="youtube", message_id="m1", user_id="UC_streamer", username="streamer", display_name="Streamer", text="test")
        user = self.identity.resolve(msg)

        self.memory.set_stream_fact(user.canonical_id, "topic", "Boss fight discussion")
        facts = self.memory.get_stream_context(user)
        self.assertEqual(len(facts), 1)

        self.memory.end_stream_session(session.session_id)
        facts_after = self.memory.get_stream_context(user, session_id=session.session_id)
        self.assertEqual(len(facts_after), 0)

    def test_sqlite_fallback_graceful_degradation(self):
        # Force fallback mode
        self.memory.fallback_mode = True
        msg = ChatMessage(platform="youtube", message_id="m1", user_id="UC_fb", username="fb", display_name="FB", text="My name is Fallback")
        user = self.identity.resolve(msg)
        self.memory.set_user_memory(user.canonical_id, "key1", "val1")
        mems = self.memory.get_all_user_memories(user.canonical_id)
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0].value, "val1")


if __name__ == "__main__":
    unittest.main()
