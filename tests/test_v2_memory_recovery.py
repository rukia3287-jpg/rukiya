"""tests/test_v2_memory_recovery.py
MemoryService leaves in-memory fallback once SQLite works again and writes back what it
stored during the outage, instead of staying in fallback until the process restarts.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

from services.config import Config
from services.memory_service import MemoryService
from services.models import UserIdentity


def _user(canonical_id="youtube:UCrecoveryrecoveryrecov"):
    platform, uid = canonical_id.split(":", 1)
    return UserIdentity(
        canonical_id=canonical_id, platform=platform, user_id=uid,
        username="viewer", display_name="Viewer", first_seen=1.0, last_seen=2.0,
    )


class MemoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.db_path = os.path.join(self.temp_dir, "recovery.db")
        self.now = 1000.0
        self.memory = self._service(self.db_path)

    def _service(self, db_path):
        return MemoryService(Config(db_path=db_path), clock=lambda: self.now)

    def _advance_past_retry(self):
        self.now += MemoryService.DB_RETRY_SECONDS + 1

    def test_stays_in_fallback_until_the_retry_interval(self):
        self.memory.fallback_mode = True
        self.now += 5
        self.memory.save_user(_user())

        self.assertTrue(self.memory.fallback_mode)
        self.assertIsNone(MemoryService(Config(db_path=self.db_path)).get_user(_user().canonical_id))

    def test_recovers_and_writes_back_state_saved_during_the_outage(self):
        user = _user()
        self.memory.fallback_mode = True
        self.memory.save_user(user)
        self.memory.set_user_memory(user.canonical_id, "favorite_game", "Genshin")
        self.assertFalse(self.memory.save_identity_link(user.canonical_id, "discord:123456789012345678"))

        self._advance_past_retry()
        self.memory.get_user(user.canonical_id)  # any storage access retries SQLite

        self.assertFalse(self.memory.fallback_mode)
        fresh = MemoryService(Config(db_path=self.db_path))
        self.assertIsNotNone(fresh.get_user(user.canonical_id))
        self.assertEqual([m.value for m in fresh.get_all_user_memories(user.canonical_id)], ["Genshin"])
        self.assertEqual(fresh.load_identity_links(), {user.canonical_id: "discord:123456789012345678"})

    def test_recovers_after_failing_at_startup(self):
        missing_dir = os.path.join(self.temp_dir, "not-created-yet")
        db_path = os.path.join(missing_dir, "late.db")
        memory = self._service(db_path)
        self.assertTrue(memory.fallback_mode)
        memory.save_user(_user())

        os.makedirs(missing_dir)
        self._advance_past_retry()
        memory.get_user(_user().canonical_id)

        self.assertFalse(memory.fallback_mode)
        self.assertIsNotNone(MemoryService(Config(db_path=db_path)).get_user(_user().canonical_id))

    def test_still_broken_database_keeps_fallback_and_waits_again(self):
        missing_dir = os.path.join(self.temp_dir, "never-created")
        memory = self._service(os.path.join(missing_dir, "x.db"))
        self._advance_past_retry()
        memory.get_user("youtube:anyone")
        self.assertTrue(memory.fallback_mode)
        # The failed attempt restarts the wait; no retry storm on every call.
        self.assertEqual(memory._fallback_since, self.now)

    def test_write_back_never_overwrites_newer_database_rows(self):
        user = _user()
        self.memory.save_user(user)
        self.memory.set_user_memory(user.canonical_id, "favorite_game", "Genshin")
        # The cached copy is now older than the row in SQLite.
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                "UPDATE memories SET value='Valorant', updated_at=updated_at + 100 WHERE canonical_id=?",
                (user.canonical_id,),
            )
        conn.close()

        self.memory.fallback_mode = True
        self._advance_past_retry()
        self.memory.get_user(user.canonical_id)

        self.assertFalse(self.memory.fallback_mode)
        fresh = MemoryService(Config(db_path=self.db_path))
        self.assertEqual([m.value for m in fresh.get_all_user_memories(user.canonical_id)], ["Valorant"])

    def _db(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        with conn:
            rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows

    def test_blank_outage_profile_never_clobbers_a_regular(self):
        regular = _user()
        regular.interaction_count = 50
        regular.metadata = {"note": "kept"}
        self.memory.save_user(regular)

        # A fresh process enters fallback before it ever read this user from SQLite,
        # so resolve() builds a blank profile for them.
        restarted = self._service(self.db_path)
        restarted.fallback_mode = True
        blank = _user()
        blank.interaction_count = 1
        blank.last_seen = 9999.0
        restarted.save_user(blank)

        self._advance_past_retry()
        restarted.get_user(regular.canonical_id)

        self.assertFalse(restarted.fallback_mode)
        count, last_seen, metadata = self._db(
            "SELECT interaction_count, last_seen, metadata FROM users WHERE canonical_id=?", (regular.canonical_id,)
        )[0]
        self.assertEqual(count, 50)
        self.assertEqual(last_seen, 9999.0)
        self.assertIn("kept", metadata)

    def test_users_untouched_during_the_outage_are_not_written_back(self):
        user = _user()
        self.memory.save_user(user)  # cached copy with interaction_count=0
        self._db("UPDATE users SET interaction_count=9 WHERE canonical_id=?", (user.canonical_id,))

        self.memory.fallback_mode = True
        self._advance_past_retry()
        self.memory.get_user(user.canonical_id)

        self.assertEqual(self._db("SELECT interaction_count FROM users")[0][0], 9)

    def test_memory_reset_during_the_outage_is_replayed_on_recovery(self):
        user = _user()
        self.memory.set_user_memory(user.canonical_id, "favorite_game", "Genshin")
        self.memory.fallback_mode = True

        self.assertFalse(self.memory.delete_user_memories(user.canonical_id))
        self.now += 1
        self.memory.set_user_memory(user.canonical_id, "preferred_name", "Ichigo")  # after the reset

        self._advance_past_retry()
        self.memory.get_user(user.canonical_id)

        self.assertFalse(self.memory.fallback_mode)
        keys = [m.key for m in MemoryService(Config(db_path=self.db_path)).get_all_user_memories(user.canonical_id)]
        self.assertEqual(keys, ["preferred_name"])

    def test_write_back_merges_confidence_and_usage_like_a_normal_update(self):
        user = _user()
        for _ in range(3):
            self.memory.set_user_memory(user.canonical_id, "favorite_game", "Genshin", confidence=0.9)

        restarted = self._service(self.db_path)
        restarted.fallback_mode = True
        restarted.set_user_memory(user.canonical_id, "favorite_game", "Valorant", confidence=0.5)

        self._advance_past_retry()
        restarted.get_user(user.canonical_id)

        value, confidence, usage = self._db(
            "SELECT value, confidence, usage_count FROM memories WHERE canonical_id=?", (user.canonical_id,)
        )[0]
        self.assertEqual(value, "Valorant")
        self.assertEqual(confidence, 0.9)
        self.assertEqual(usage, 3)

    def test_write_back_respects_the_per_user_fact_cap(self):
        memory = MemoryService(Config(db_path=self.db_path, max_memory_per_user=3), clock=lambda: self.now)
        user = _user()
        for i in range(3):
            memory.set_user_memory(user.canonical_id, f"old_{i}", "x")

        restarted = MemoryService(Config(db_path=self.db_path, max_memory_per_user=3), clock=lambda: self.now)
        restarted.fallback_mode = True
        restarted.set_user_memory(user.canonical_id, "new_a", "y")
        restarted.set_user_memory(user.canonical_id, "new_b", "y")

        self._advance_past_retry()
        restarted.get_user(user.canonical_id)

        self.assertLessEqual(self._db("SELECT COUNT(*) FROM memories WHERE canonical_id=?", (user.canonical_id,))[0][0], 3)

    def test_stream_facts_saved_during_the_outage_stay_visible(self):
        user = _user()
        self.memory.start_stream_session("video1")
        self.memory.fallback_mode = True
        self.memory.set_stream_fact(user.canonical_id, "topic", "boss fight")

        self._advance_past_retry()
        facts = self.memory.get_stream_context(user)

        self.assertFalse(self.memory.fallback_mode)
        self.assertEqual([f.value for f in facts], ["boss fight"])

    def test_identity_link_removed_during_the_outage_is_replayed(self):
        link = (_user().canonical_id, "discord:123456789012345678")
        self.assertTrue(self.memory.save_identity_link(*link))
        self.memory.fallback_mode = True
        self.assertFalse(self.memory.delete_identity_link(link[0]))

        self._advance_past_retry()
        self.memory.get_user(link[0])

        self.assertEqual(MemoryService(Config(db_path=self.db_path)).load_identity_links(), {})

    def test_one_unwritable_row_does_not_pin_fallback(self):
        user = _user()
        self.memory.fallback_mode = True
        self.memory.set_user_memory(user.canonical_id, "good", "ok")
        self.memory.set_user_memory(user.canonical_id, "bad", "will be corrupted")
        self.memory._in_memory_memories[user.canonical_id]["bad"].value = None  # violates NOT NULL

        self._advance_past_retry()
        self.memory.get_user(user.canonical_id)

        self.assertFalse(self.memory.fallback_mode)
        self.assertEqual(self._db("SELECT key FROM memories"), [("good",)])

    def test_persistence_probe_retries_when_due(self):
        self.memory.fallback_mode = True
        self.assertFalse(self.memory.persistence_available())  # not due yet
        self._advance_past_retry()
        self.assertTrue(self.memory.persistence_available())


if __name__ == "__main__":
    unittest.main()
