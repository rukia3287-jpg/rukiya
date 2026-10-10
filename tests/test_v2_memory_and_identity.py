import os
import tempfile
import time
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

    def test_resolve_does_not_advance_last_seen_before_decision(self):
        first = ChatMessage(
            platform="youtube",
            message_id="m_last_seen_1",
            user_id="UC_last_seen",
            username="viewer",
            display_name="Viewer",
            text="hello",
            timestamp=1000.0,
        )
        user = self.identity.resolve(first)
        # Mirrors the orchestrator, which records the user turn with the message's own timestamp.
        self.memory.record_interaction(user, first.text, role="user", timestamp=first.timestamp)
        previous_last_seen = user.last_seen

        second = ChatMessage(
            platform="youtube",
            message_id="m_last_seen_2",
            user_id="UC_last_seen",
            username="viewer",
            display_name="Viewer",
            text="follow up",
            timestamp=1900.0,
        )
        resolved = self.identity.resolve(second)

        self.assertEqual(resolved.canonical_id, user.canonical_id)
        self.assertEqual(resolved.last_seen, previous_last_seen)
        self.assertEqual(resolved.last_seen, 1000.0)

    def test_record_interaction_never_moves_last_seen_backwards(self):
        msg = ChatMessage(
            platform="youtube",
            message_id="m_monotonic",
            user_id="UC_monotonic",
            username="viewer",
            display_name="Viewer",
            text="hello",
            timestamp=5000.0,
        )
        user = self.identity.resolve(msg)
        self.memory.record_interaction(user, "late duplicate", role="user", timestamp=4000.0)

        self.assertEqual(user.last_seen, 5000.0)
        self.assertEqual(self.memory.get_user(user.canonical_id).last_seen, 5000.0)

    def test_future_or_millisecond_timestamp_cannot_freeze_last_seen(self):
        msg = ChatMessage(
            platform="youtube", message_id="m_future", user_id="UC_future",
            username="viewer", display_name="Viewer", text="hello", timestamp=1000.0,
        )
        user = self.identity.resolve(msg)
        before = time.time()
        self.memory.record_interaction(user, "hello", role="user", timestamp=1.7e12)  # milliseconds by mistake

        self.assertLessEqual(user.last_seen, time.time())
        self.assertGreaterEqual(user.last_seen, before)

    def test_assistant_turn_does_not_change_viewer_last_seen(self):
        msg = ChatMessage(
            platform="youtube", message_id="m_reply", user_id="UC_reply",
            username="viewer", display_name="Viewer", text="hello", timestamp=1000.0,
        )
        user = self.identity.resolve(msg)
        self.memory.record_interaction(user, msg.text, role="user", timestamp=msg.timestamp)
        self.memory.record_interaction(user, "Hm. Welcome in.", role="assistant")

        self.assertEqual(user.last_seen, 1000.0)
        self.assertEqual(user.interaction_count, 1)

    def test_recent_history_keeps_recording_order_across_clocks(self):
        msg = ChatMessage(
            platform="youtube", message_id="m_order", user_id="UC_order",
            username="viewer", display_name="Viewer", text="first", timestamp=1000.0,
        )
        user = self.identity.resolve(msg)
        self.memory.record_interaction(user, "first", role="user", timestamp=1000.0)
        self.memory.record_interaction(user, "reply one", role="assistant")
        self.memory.record_interaction(user, "second", role="user", timestamp=2000.0)
        self.memory.record_interaction(user, "reply two", role="assistant")

        # A fresh instance reads history from SQLite, which orders rows by timestamp.
        history = MemoryService(self.config).get_recent_messages(limit=8)
        self.assertEqual([m["text"] for m in history], ["first", "reply one", "second", "reply two"])

    def test_explicit_link_identities(self):
        self.identity.link_identities(DISCORD_A, YOUTUBE_A)
        resolved = self.identity.resolve(_yt_message(YOUTUBE_A_RAW))
        self.assertEqual(resolved.canonical_id, DISCORD_A)


DISCORD_A = "discord:123456789012345678"
DISCORD_B = "discord:223456789012345678"
YOUTUBE_A_RAW = "UCabcdefghijklmnopqrstuv"
YOUTUBE_B_RAW = "UCbbcdefghijklmnopqrstuv"
YOUTUBE_A = f"youtube:{YOUTUBE_A_RAW}"
YOUTUBE_B = f"youtube:{YOUTUBE_B_RAW}"


def _yt_message(channel_id, display_name="Viewer"):
    return ChatMessage(
        platform="youtube",
        message_id=f"m_{channel_id}",
        user_id=channel_id,
        username=display_name,
        display_name=display_name,
        text="hello",
    )


class TestIdentityLinkPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_links.db")
        self.config = Config(db_path=self.db_path)
        self.memory = MemoryService(self.config)
        self.identity = IdentityService(self.memory)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass

    def _fresh_identity(self):
        """A new process: fresh services against the same database file."""
        return IdentityService(MemoryService(Config(db_path=self.db_path)))

    def test_link_survives_restart(self):
        self.assertTrue(self.identity.link_identities(DISCORD_A, YOUTUBE_A))

        restarted = self._fresh_identity()
        self.assertEqual(restarted.resolve(_yt_message(YOUTUBE_A_RAW)).canonical_id, DISCORD_A)

    def test_invalid_links_are_rejected(self):
        invalid_pairs = [
            (DISCORD_A, DISCORD_A),                # self link
            (DISCORD_A, DISCORD_B),                # same platform
            (DISCORD_A, "youtube:dn_viewer"),      # display-name fallback key
            (DISCORD_A, "youtube:Viewer"),         # display name, not a channel ID
            (DISCORD_A, "youtube:unknown"),
            ("discord:not-a-snowflake", YOUTUBE_A),
            ("twitch:123456789012345678", YOUTUBE_A),
            ("", YOUTUBE_A),
            (DISCORD_A, f"{YOUTUBE_A} "),
        ]
        for primary, secondary in invalid_pairs:
            with self.subTest(primary=primary, secondary=secondary):
                with self.assertRaises(ValueError):
                    self.identity.link_identities(primary, secondary)
        self.assertEqual(self.memory.load_identity_links(), {})

    def test_conflicting_link_is_rejected_and_original_kept(self):
        self.identity.link_identities(DISCORD_A, YOUTUBE_A)
        with self.assertRaises(ValueError):
            self.identity.link_identities(DISCORD_B, YOUTUBE_A)

        self.assertEqual(self.identity.resolve(_yt_message(YOUTUBE_A_RAW)).canonical_id, DISCORD_A)
        self.assertEqual(self._fresh_identity().resolve(_yt_message(YOUTUBE_A_RAW)).canonical_id, DISCORD_A)

    def test_relinking_the_same_pair_is_idempotent(self):
        self.assertTrue(self.identity.link_identities(DISCORD_A, YOUTUBE_A))
        self.assertTrue(self.identity.link_identities(DISCORD_A, YOUTUBE_A))
        self.assertEqual(self.memory.load_identity_links(), {YOUTUBE_A: DISCORD_A})

    def test_chained_links_are_rejected(self):
        self.identity.link_identities(DISCORD_A, YOUTUBE_A)
        # A linked secondary cannot become a primary, and a primary cannot become a secondary.
        with self.assertRaises(ValueError):
            self.identity.link_identities(YOUTUBE_A, DISCORD_B)
        with self.assertRaises(ValueError):
            self.identity.link_identities(YOUTUBE_B, DISCORD_A)

    def test_unlink_is_persisted(self):
        self.identity.link_identities(DISCORD_A, YOUTUBE_A)
        self.assertTrue(self.identity.unlink_identity(YOUTUBE_A))

        self.assertEqual(self.identity.resolve(_yt_message(YOUTUBE_A_RAW)).canonical_id, YOUTUBE_A)
        self.assertEqual(self._fresh_identity().resolve(_yt_message(YOUTUBE_A_RAW)).canonical_id, YOUTUBE_A)
        with self.assertRaises(KeyError):
            self.identity.unlink_identity(YOUTUBE_A)

    def test_link_in_fallback_mode_is_reported_as_not_persisted(self):
        self.memory.fallback_mode = True

        self.assertFalse(self.identity.link_identities(DISCORD_A, YOUTUBE_A))
        # Still effective for this process...
        self.assertEqual(self.identity.resolve(_yt_message(YOUTUBE_A_RAW)).canonical_id, DISCORD_A)
        # ...but never claimed to be durable.
        self.assertEqual(self._fresh_identity().resolve(_yt_message(YOUTUBE_A_RAW)).canonical_id, YOUTUBE_A)

    def test_link_does_not_join_viewers_who_share_a_display_name(self):
        self.identity.link_identities(DISCORD_A, YOUTUBE_A)

        linked = self.identity.resolve(_yt_message(YOUTUBE_A_RAW, display_name="Alex"))
        namesake = self.identity.resolve(_yt_message(YOUTUBE_B_RAW, display_name="Alex"))

        self.assertEqual(linked.canonical_id, DISCORD_A)
        self.assertEqual(namesake.canonical_id, YOUTUBE_B)

    def test_link_preserves_existing_memory_associations(self):
        self.memory.set_user_memory(YOUTUBE_A, "favorite_game", "Genshin")
        self.memory.set_user_memory(DISCORD_A, "preferred_name", "Ichigo")

        self.identity.link_identities(DISCORD_A, YOUTUBE_A)
        self.assertEqual([m.key for m in self.memory.get_all_user_memories(YOUTUBE_A)], ["favorite_game"])
        self.assertEqual([m.key for m in self.memory.get_all_user_memories(DISCORD_A)], ["preferred_name"])

        # Unlinking restores the YouTube identity with its own memories intact.
        self.identity.unlink_identity(YOUTUBE_A)
        resolved = self.identity.resolve(_yt_message(YOUTUBE_A_RAW))
        self.assertEqual([m.key for m in self.memory.get_all_user_memories(resolved.canonical_id)], ["favorite_game"])


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
