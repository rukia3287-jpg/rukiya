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


if __name__ == "__main__":
    unittest.main()
