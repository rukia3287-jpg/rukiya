"""services/memory_service.py
Lightweight SQLite-backed dual-scope memory service with in-memory fallback.

Scopes:
1. Stream Memory (Temporary): Per-stream facts, context, questions; auto-expires at stream end or TTL.
2. User Memory (Persistent): Per-user facts, preferences, relationships; confidence decay, conflict resolution.

Gracefully degrades to in-memory dictionary storage if SQLite encounters any operational errors.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from services.config import Config
from services.models import MemoryEntry, StreamSession, UserIdentity

logger = logging.getLogger(__name__)


class MemoryService:
    """Manages stream and persistent user memories with SQLite and resilient fallback."""

    # While in fallback, SQLite is retried at most this often. On recovery, exactly what
    # changed during the outage is replayed: deletions first, then users, user facts,
    # stream facts and identity links, merged so stored data never moves backwards.
    # Recent chat messages are not written back (they only feed short prompt context).
    DB_RETRY_SECONDS = 30.0

    def __init__(self, config: Optional[Config] = None, clock=None):
        self.config = config or Config()
        self.db_path = getattr(self.config, "db_path", "rukiya_memory.db")
        self.decay_constant = float(getattr(self.config, "memory_decay_days", 30.0))
        self.max_memory_per_user = int(getattr(self.config, "max_memory_per_user", 30))
        self.max_stream_memory = int(getattr(self.config, "max_stream_memory", 20))
        self.max_context_messages = int(getattr(self.config, "max_context_messages", 8))

        self._clock = clock or time.monotonic  # injectable for tests
        self._fallback_mode = False
        self._fallback_since = 0.0
        self._in_memory_users: Dict[str, UserIdentity] = {}
        self._in_memory_memories: Dict[str, Dict[str, MemoryEntry]] = {}  # canonical_id -> {key: MemoryEntry}
        self._in_memory_sessions: Dict[str, StreamSession] = {}  # session_id -> StreamSession
        self._in_memory_stream_memory: Dict[str, Dict[str, MemoryEntry]] = {}  # session_id:canonical_id -> {key: MemoryEntry}
        self._in_memory_recent_messages: Dict[str, List[Dict[str, Any]]] = {}  # session_id -> list of message dicts
        self._in_memory_identity_links: Dict[str, str] = {}  # secondary canonical_id -> primary canonical_id

        # Changes that could not reach SQLite, replayed on recovery.
        self._pending_users: set = set()  # canonical_ids
        self._pending_memories: Dict[str, set] = {}  # canonical_id -> keys
        self._pending_memory_resets: Dict[str, float] = {}  # canonical_id -> reset time
        self._pending_stream_facts: Dict[Tuple[str, str, str], float] = {}  # (session, canonical_id, key) -> expires_at
        self._pending_links: set = set()  # secondary ids to insert
        self._pending_link_deletes: set = set()  # secondary ids to delete

        self.active_session_id: Optional[str] = None
        self._init_db()

    @property
    def fallback_mode(self) -> bool:
        return self._fallback_mode

    @fallback_mode.setter
    def fallback_mode(self, value: bool) -> None:
        if value and not self._fallback_mode:
            self._fallback_since = self._clock()
            logger.warning("MemoryService using in-memory fallback; SQLite retry in %.0fs", self.DB_RETRY_SECONDS)
        self._fallback_mode = bool(value)

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        if self._fallback_mode and not self._try_recover():
            return None
        return self._open_connection()

    def _try_recover(self) -> bool:
        """Retry SQLite once the retry interval has passed; on success write back fallback state."""
        if self._clock() - self._fallback_since < self.DB_RETRY_SECONDS:
            return False
        self._fallback_since = self._clock()  # a failed attempt waits a full interval again
        self._fallback_mode = False
        self._init_db()  # recreates any missing tables; re-enters fallback on failure
        if self._fallback_mode or not self._write_back_fallback_state():
            self._fallback_since = self._clock()
            logger.warning("SQLite still unavailable; staying in in-memory fallback")
            return False
        logger.warning("MemoryService recovered SQLite persistence and wrote back in-memory state")
        return True

    def persistence_available(self) -> bool:
        """True if SQLite is in use. While in fallback this also attempts a (rate-limited) recovery."""
        if self._fallback_mode:
            self._try_recover()
        return not self._fallback_mode

    def _write_back_fallback_state(self) -> bool:
        conn = self._open_connection()
        if not conn:
            return False

        def run(sql: str, params: tuple, what: str) -> None:
            # A row that cannot be written (bad data) is skipped and logged; it must not
            # keep the whole service in fallback. I/O errors still abort the recovery.
            try:
                conn.execute(sql, params)
            except (sqlite3.IntegrityError, sqlite3.InterfaceError, sqlite3.ProgrammingError, ValueError, TypeError) as e:
                logger.error("Skipping unwritable %s during recovery: %s", what, e)

        try:
            with conn:
                # 1. Deletions first, so later writes are not removed again.
                for secondary_id in sorted(self._pending_link_deletes):
                    run("DELETE FROM identity_links WHERE secondary_id=?", (secondary_id,), "identity link removal")
                for canonical_id, reset_at in self._pending_memory_resets.items():
                    run("DELETE FROM memories WHERE canonical_id=? AND updated_at <= ?", (canonical_id, reset_at), "memory reset")

                # 2. Users changed during the outage. A profile built while SQLite was
                # unreachable may be blank, so counters only grow and stored metadata
                # and first_seen are kept.
                for canonical_id in sorted(self._pending_users):
                    user = self._in_memory_users.get(canonical_id)
                    if not user:
                        continue
                    meta = dict(user.metadata)
                    meta["welcomed_in_stream"] = user.is_welcomed_in_stream
                    run("""
                        INSERT INTO users (canonical_id, platform, user_id, username, display_name, first_seen, last_seen, interaction_count, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(canonical_id) DO UPDATE SET
                            username=excluded.username,
                            display_name=excluded.display_name,
                            last_seen=MAX(users.last_seen, excluded.last_seen),
                            interaction_count=MAX(users.interaction_count, excluded.interaction_count)
                    """, (
                        user.canonical_id, user.platform, user.user_id, user.username, user.display_name,
                        user.first_seen, user.last_seen, user.interaction_count, json.dumps(meta)
                    ), "user")

                # 3. User facts changed during the outage, merged like a normal update.
                for canonical_id, keys in self._pending_memories.items():
                    cached = self._in_memory_memories.get(canonical_id, {})
                    for key in sorted(keys):
                        entry = cached.get(key)
                        if not entry:
                            continue
                        run("""
                            INSERT INTO memories (canonical_id, key, value, confidence, source, created_at, updated_at, last_used_at, usage_count)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(canonical_id, key) DO UPDATE SET
                                value=CASE WHEN excluded.updated_at > memories.updated_at THEN excluded.value ELSE memories.value END,
                                source=CASE WHEN excluded.updated_at > memories.updated_at THEN excluded.source ELSE memories.source END,
                                confidence=MAX(memories.confidence, excluded.confidence),
                                updated_at=MAX(memories.updated_at, excluded.updated_at),
                                last_used_at=MAX(memories.last_used_at, excluded.last_used_at),
                                usage_count=MAX(memories.usage_count, excluded.usage_count)
                        """, (
                            canonical_id, entry.key, entry.value, entry.confidence, entry.source,
                            entry.created_at, entry.updated_at, entry.last_used_at, entry.usage_count
                        ), "user fact")
                    # Keep the per-user cap: drop the oldest rows beyond it.
                    run("""
                        DELETE FROM memories WHERE canonical_id=? AND key NOT IN (
                            SELECT key FROM memories WHERE canonical_id=? ORDER BY updated_at DESC LIMIT ?
                        )
                    """, (canonical_id, canonical_id, self.max_memory_per_user), "memory cap")

                # 4. Stream facts, so the current stream's context survives recovery.
                now = time.time()
                for (session_id, canonical_id, key), expires_at in self._pending_stream_facts.items():
                    entry = self._in_memory_stream_memory.get(f"{session_id}:{canonical_id}", {}).get(key)
                    if not entry or expires_at <= now:
                        continue
                    run("""
                        INSERT INTO stream_memory (session_id, canonical_id, key, value, confidence, created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, canonical_id, key) DO UPDATE SET
                            value=excluded.value,
                            confidence=excluded.confidence,
                            expires_at=excluded.expires_at
                    """, (session_id, canonical_id, key, entry.value, entry.confidence, entry.created_at, expires_at), "stream fact")

                # 5. Identity links added during the outage; never replace an existing link.
                for secondary_id in sorted(self._pending_links):
                    primary_id = self._in_memory_identity_links.get(secondary_id)
                    if not primary_id:
                        continue
                    run(
                        "INSERT INTO identity_links (secondary_id, primary_id, created_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(secondary_id) DO NOTHING",
                        (secondary_id, primary_id, time.time()),
                        "identity link",
                    )
        except Exception as e:
            logger.error("Error writing back in-memory state to DB: %s", e)
            self.fallback_mode = True
            return False
        finally:
            conn.close()

        self._pending_users.clear()
        self._pending_memories.clear()
        self._pending_memory_resets.clear()
        self._pending_stream_facts.clear()
        self._pending_links.clear()
        self._pending_link_deletes.clear()
        return True

    def _open_connection(self) -> Optional[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        except Exception as e:
            logger.warning("SQLite connection error (%s); falling back to in-memory storage", e)
            self.fallback_mode = True
            return None

    def _init_db(self) -> None:
        """Create tables and indexes if they don't exist."""
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        canonical_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        username TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        first_seen REAL NOT NULL,
                        last_seen REAL NOT NULL,
                        interaction_count INTEGER DEFAULT 0,
                        metadata TEXT DEFAULT '{}'
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        canonical_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        source TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_used_at REAL NOT NULL,
                        usage_count INTEGER DEFAULT 1,
                        UNIQUE(canonical_id, key)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS stream_sessions (
                        session_id TEXT PRIMARY KEY,
                        video_id TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        ended_at REAL,
                        is_active INTEGER DEFAULT 1,
                        metadata TEXT DEFAULT '{}'
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS stream_memory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        canonical_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        UNIQUE(session_id, canonical_id, key)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS recent_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        canonical_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        author TEXT NOT NULL,
                        text TEXT NOT NULL,
                        timestamp REAL NOT NULL
                    )
                """)
                # Explicit, verified cross-platform identity links. Additive table, so
                # existing databases pick it up on the next start without a migration step.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS identity_links (
                        secondary_id TEXT PRIMARY KEY,
                        primary_id TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                # Indexes
                conn.execute("CREATE INDEX IF NOT EXISTS idx_users_platform_user ON users(platform, user_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_links_primary ON identity_links(primary_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user_key ON memories(canonical_id, key)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_memory_session ON stream_memory(session_id, expires_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_recent_session ON recent_messages(session_id, timestamp)")
            logger.info("MemoryService SQLite tables initialized at %s", self.db_path)
        except Exception as e:
            logger.warning("Failed to initialize SQLite database (%s); switching to in-memory fallback", e)
            self.fallback_mode = True
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────────
    # Stream Session Management
    # ─────────────────────────────────────────────────────────────
    def start_stream_session(self, video_id: str, session_id: Optional[str] = None) -> StreamSession:
        sid = session_id or f"stream_{video_id}_{int(time.time())}"
        session = StreamSession(
            session_id=sid,
            video_id=video_id,
            started_at=time.time(),
            is_active=True
        )
        self.active_session_id = sid

        conn = self._get_connection()
        if conn:
            try:
                with conn:
                    # Deactivate prior active sessions for this video
                    conn.execute("UPDATE stream_sessions SET is_active=0, ended_at=? WHERE video_id=? AND is_active=1", (time.time(), video_id))
                    conn.execute("""
                        INSERT OR REPLACE INTO stream_sessions (session_id, video_id, started_at, is_active, metadata)
                        VALUES (?, ?, ?, 1, ?)
                    """, (sid, video_id, session.started_at, json.dumps(session.metadata)))
            except Exception as e:
                logger.error("Error saving stream session to DB: %s", e)
                self.fallback_mode = True
                self._in_memory_sessions[sid] = session
            finally:
                conn.close()
        else:
            self._in_memory_sessions[sid] = session

        logger.info("Started stream session %s for video %s", sid, video_id)
        return session

    def end_stream_session(self, session_id: Optional[str] = None) -> None:
        sid = session_id or self.active_session_id
        if not sid:
            return
        now = time.time()
        if sid in self._in_memory_sessions:
            s = self._in_memory_sessions[sid]
            s.is_active = False
            s.ended_at = now

        conn = self._get_connection()
        if conn:
            try:
                with conn:
                    conn.execute("UPDATE stream_sessions SET is_active=0, ended_at=? WHERE session_id=?", (now, sid))
                    # Expire all stream memory for this session
                    conn.execute("DELETE FROM stream_memory WHERE session_id=?", (sid,))
            except Exception as e:
                logger.error("Error ending stream session in DB: %s", e)
            finally:
                conn.close()

        # Clean in-memory stream memory
        keys_to_delete = [k for k in self._in_memory_stream_memory if k.startswith(f"{sid}:")]
        for k in keys_to_delete:
            self._in_memory_stream_memory.pop(k, None)

        if self.active_session_id == sid:
            self.active_session_id = None
        logger.info("Ended stream session %s", sid)

    # ─────────────────────────────────────────────────────────────
    # User Profile Operations
    # ─────────────────────────────────────────────────────────────
    def get_user(self, canonical_id: str) -> Optional[UserIdentity]:
        conn = self._get_connection()
        if not conn:
            return self._in_memory_users.get(canonical_id)
        try:
            row = conn.execute("SELECT * FROM users WHERE canonical_id=?", (canonical_id,)).fetchone()
            if row:
                metadata = {}
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except Exception:
                    pass
                return UserIdentity(
                    canonical_id=row["canonical_id"],
                    platform=row["platform"],
                    user_id=row["user_id"],
                    username=row["username"],
                    display_name=row["display_name"],
                    first_seen=float(row["first_seen"]),
                    last_seen=float(row["last_seen"]),
                    interaction_count=int(row["interaction_count"]),
                    is_welcomed_in_stream=bool(metadata.get("welcomed_in_stream", False)),
                    metadata=metadata
                )
            return None
        except Exception as e:
            logger.error("Error reading user from DB: %s", e)
            return self._in_memory_users.get(canonical_id)
        finally:
            conn.close()

    @staticmethod
    def _upsert_user(conn: sqlite3.Connection, user: UserIdentity) -> None:
        meta = dict(user.metadata)
        meta["welcomed_in_stream"] = user.is_welcomed_in_stream
        conn.execute("""
            INSERT INTO users (canonical_id, platform, user_id, username, display_name, first_seen, last_seen, interaction_count, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_id) DO UPDATE SET
                username=excluded.username,
                display_name=excluded.display_name,
                last_seen=excluded.last_seen,
                interaction_count=excluded.interaction_count,
                metadata=excluded.metadata
        """, (
            user.canonical_id, user.platform, user.user_id, user.username,
            user.display_name, user.first_seen, user.last_seen,
            user.interaction_count, json.dumps(meta)
        ))

    def save_user(self, user: UserIdentity) -> None:
        self._in_memory_users[user.canonical_id] = user
        conn = self._get_connection()
        if not conn:
            self._pending_users.add(user.canonical_id)
            return
        try:
            with conn:
                self._upsert_user(conn, user)
        except Exception as e:
            logger.error("Error saving user to DB: %s", e)
            self._pending_users.add(user.canonical_id)
            self.fallback_mode = True
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────────
    # Identity Link Operations
    # ─────────────────────────────────────────────────────────────
    def load_identity_links(self) -> Dict[str, str]:
        """Return every stored link as {secondary canonical_id: primary canonical_id}."""
        conn = self._get_connection()
        if not conn:
            return dict(self._in_memory_identity_links)
        try:
            rows = conn.execute("SELECT secondary_id, primary_id FROM identity_links").fetchall()
            return {row["secondary_id"]: row["primary_id"] for row in rows}
        except Exception as e:
            logger.error("Error loading identity links from DB: %s", e)
            return dict(self._in_memory_identity_links)
        finally:
            conn.close()

    def save_identity_link(self, secondary_id: str, primary_id: str) -> bool:
        """Store a link. Returns True only if it was written to SQLite.

        Raises ValueError if the database already links secondary_id to a different
        primary (for example, written by another process since this one loaded).
        """
        self._in_memory_identity_links[secondary_id] = primary_id
        self._pending_link_deletes.discard(secondary_id)
        conn = self._get_connection()
        if not conn:
            logger.warning("Identity link held in memory only; database unavailable")
            self._pending_links.add(secondary_id)
            return False
        try:
            with conn:
                conn.execute(
                    "INSERT INTO identity_links (secondary_id, primary_id, created_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(secondary_id) DO NOTHING",
                    (secondary_id, primary_id, time.time()),
                )
                row = conn.execute(
                    "SELECT primary_id FROM identity_links WHERE secondary_id=?", (secondary_id,)
                ).fetchone()
            if row and row["primary_id"] != primary_id:
                self._in_memory_identity_links[secondary_id] = row["primary_id"]
                raise ValueError(f"{secondary_id} is already linked to {row['primary_id']}")
            return True
        except ValueError:
            raise
        except Exception as e:
            logger.error("Error saving identity link to DB: %s", e)
            self._pending_links.add(secondary_id)
            return False
        finally:
            conn.close()

    def delete_identity_link(self, secondary_id: str) -> bool:
        """Remove a link. Returns True only if the removal was written to SQLite."""
        self._in_memory_identity_links.pop(secondary_id, None)
        self._pending_links.discard(secondary_id)
        conn = self._get_connection()
        if not conn:
            logger.warning("Identity link removal held in memory only; database unavailable")
            self._pending_link_deletes.add(secondary_id)
            return False
        try:
            with conn:
                conn.execute("DELETE FROM identity_links WHERE secondary_id=?", (secondary_id,))
            return True
        except Exception as e:
            logger.error("Error deleting identity link from DB: %s", e)
            self._pending_link_deletes.add(secondary_id)
            return False
        finally:
            conn.close()

    def record_interaction(
        self,
        user: UserIdentity,
        text: str,
        role: str = "user",
        session_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        now = time.time()
        if role == "user":
            # last_seen is the viewer's own latest activity, on the message clock that
            # IdentityService uses for first_seen. Clamped to now so a future/millisecond
            # timestamp cannot freeze it, and it never moves backwards. Assistant turns
            # leave it alone. History rows below keep wall-clock order.
            seen_at = now if timestamp is None else min(timestamp, now)
            user.last_seen = max(user.last_seen, seen_at)
            user.interaction_count += 1
        self.save_user(user)

        sid = session_id or self.active_session_id or "default_session"

        # Record recent message
        msg_record = {
            "session_id": sid,
            "canonical_id": user.canonical_id,
            "role": role,
            "author": user.display_name,
            "text": text,
            "timestamp": now
        }
        self._in_memory_recent_messages.setdefault(sid, []).append(msg_record)
        if len(self._in_memory_recent_messages[sid]) > self.max_context_messages * 2:
            self._in_memory_recent_messages[sid] = self._in_memory_recent_messages[sid][-self.max_context_messages:]

        conn = self._get_connection()
        if conn:
            try:
                with conn:
                    conn.execute("""
                        INSERT INTO recent_messages (session_id, canonical_id, role, author, text, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (sid, user.canonical_id, role, user.display_name, text, now))
                    # Prune old messages for this session
                    conn.execute("""
                        DELETE FROM recent_messages WHERE id NOT IN (
                            SELECT id FROM recent_messages WHERE session_id=? ORDER BY timestamp DESC LIMIT ?
                        ) AND session_id=?
                    """, (sid, self.max_context_messages * 3, sid))
            except Exception as e:
                logger.error("Error saving recent message: %s", e)
            finally:
                conn.close()

    # ─────────────────────────────────────────────────────────────
    # Persistent Memory Operations
    # ─────────────────────────────────────────────────────────────
    def get_effective_confidence(self, entry: MemoryEntry) -> float:
        """Calculate confidence decayed by age in days."""
        age_days = (time.time() - entry.updated_at) / 86400.0
        if age_days <= 0:
            return entry.confidence
        return entry.confidence * math.exp(-age_days / self.decay_constant)

    def set_user_memory(self, canonical_id: str, key: str, value: str, confidence: float = 0.9, source: str = "explicit_statement") -> MemoryEntry:
        now = time.time()
        user_memories = self._in_memory_memories.setdefault(canonical_id, {})
        entry = user_memories.get(key)

        if entry:
            # Conflict resolution: update value, update timestamp, increment usage
            entry.value = value
            entry.confidence = max(entry.confidence, confidence)
            entry.source = source
            entry.updated_at = now
            entry.last_used_at = now
            entry.usage_count += 1
        else:
            entry = MemoryEntry(
                key=key,
                value=value,
                confidence=confidence,
                source=source,
                created_at=now,
                updated_at=now,
                last_used_at=now,
                usage_count=1
            )
            # Enforce max memory limit per user
            if len(user_memories) >= self.max_memory_per_user:
                # Remove lowest effective confidence memory
                lowest_key = min(user_memories, key=lambda k: self.get_effective_confidence(user_memories[k]))
                user_memories.pop(lowest_key, None)
            user_memories[key] = entry

        conn = self._get_connection()
        if conn:
            try:
                with conn:
                    # Enforce capacity in DB
                    count_row = conn.execute("SELECT COUNT(*) as cnt FROM memories WHERE canonical_id=?", (canonical_id,)).fetchone()
                    if count_row and count_row["cnt"] >= self.max_memory_per_user:
                        conn.execute("""
                            DELETE FROM memories WHERE canonical_id=? AND key NOT IN (
                                SELECT key FROM memories WHERE canonical_id=? ORDER BY updated_at DESC LIMIT ?
                            )
                        """, (canonical_id, canonical_id, self.max_memory_per_user - 1))

                    conn.execute("""
                        INSERT INTO memories (canonical_id, key, value, confidence, source, created_at, updated_at, last_used_at, usage_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(canonical_id, key) DO UPDATE SET
                            value=excluded.value,
                            confidence=MAX(memories.confidence, excluded.confidence),
                            source=excluded.source,
                            updated_at=excluded.updated_at,
                            last_used_at=excluded.last_used_at,
                            usage_count=memories.usage_count + 1
                    """, (
                        canonical_id, entry.key, entry.value, entry.confidence,
                        entry.source, entry.created_at, entry.updated_at,
                        entry.last_used_at, entry.usage_count
                    ))
            except Exception as e:
                logger.error("Error saving memory to DB: %s", e)
                self._pending_memories.setdefault(canonical_id, set()).add(entry.key)
                self.fallback_mode = True
            finally:
                conn.close()
        else:
            self._pending_memories.setdefault(canonical_id, set()).add(entry.key)

        return entry

    def get_all_user_memories(self, canonical_id: str) -> List[MemoryEntry]:
        memories: List[MemoryEntry] = []
        conn = self._get_connection()
        if not conn:
            return list(self._in_memory_memories.get(canonical_id, {}).values())
        try:
            rows = conn.execute("SELECT * FROM memories WHERE canonical_id=? ORDER BY updated_at DESC", (canonical_id,)).fetchall()
            for r in rows:
                entry = MemoryEntry(
                    key=r["key"],
                    value=r["value"],
                    confidence=float(r["confidence"]),
                    source=r["source"],
                    created_at=float(r["created_at"]),
                    updated_at=float(r["updated_at"]),
                    last_used_at=float(r["last_used_at"]),
                    usage_count=int(r["usage_count"])
                )
                memories.append(entry)
            # Sync with in-memory
            self._in_memory_memories[canonical_id] = {m.key: m for m in memories}
            return memories
        except Exception as e:
            logger.error("Error reading memories from DB: %s", e)
            return list(self._in_memory_memories.get(canonical_id, {}).values())
        finally:
            conn.close()

    def get_relevant_user_memory(self, user: UserIdentity, query_text: str, limit: int = 5) -> List[MemoryEntry]:
        """Score memories by semantic keyword match, confidence, recency, usage, and identity relevance."""
        all_memories = self.get_all_user_memories(user.canonical_id)
        if not all_memories:
            return []

        tokens = set(re.findall(r"\w+", query_text.lower()))
        scored: List[Tuple[float, MemoryEntry]] = []

        now = time.time()
        for m in all_memories:
            effective_conf = self.get_effective_confidence(m)
            # Prune stale and very low confidence facts
            if effective_conf < 0.2:
                continue

            # 1. Semantic keyword match (key and value)
            mem_tokens = set(re.findall(r"\w+", f"{m.key} {m.value}".lower()))
            overlap = len(tokens.intersection(mem_tokens))
            kw_score = min(1.0, overlap / max(1, len(mem_tokens)))

            # 2. Confidence score
            conf_score = min(1.0, effective_conf)

            # 3. Recency score (decayed over 7 days)
            age_days = (now - m.updated_at) / 86400.0
            recency_score = math.exp(-age_days / 7.0)

            # 4. Usage frequency
            usage_score = min(1.0, m.usage_count / 10.0)

            # 5. Identity relevance (e.g. preferred_name is always slightly relevant)
            identity_score = 1.0 if m.key in ("preferred_name", "favorite_game") else 0.5

            total_score = (
                kw_score * 0.35 +
                conf_score * 0.25 +
                recency_score * 0.20 +
                usage_score * 0.10 +
                identity_score * 0.10
            )

            # Only return items that have keyword match OR are highly confident core identity facts
            if overlap > 0 or (m.key in ("preferred_name", "favorite_game") and conf_score > 0.8):
                scored.append((total_score, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_entries = [m for _, m in scored[:limit]]

        # Update last_used_at for returned memories
        for m in top_entries:
            m.last_used_at = now

        return top_entries

    def delete_user_memories(self, canonical_id: str) -> bool:
        """Reset all persistent memory for a user.

        Returns True if SQLite was updated. Otherwise the reset applies in memory now and
        is replayed when the database recovers.
        """
        self._in_memory_memories.pop(canonical_id, None)
        self._pending_memories.pop(canonical_id, None)
        conn = self._get_connection()
        if conn:
            try:
                with conn:
                    conn.execute("DELETE FROM memories WHERE canonical_id=?", (canonical_id,))
                return True
            except Exception as e:
                logger.error("Error deleting user memories: %s", e)
            finally:
                conn.close()
        self._pending_memory_resets[canonical_id] = time.time()
        return False

    # ─────────────────────────────────────────────────────────────
    # Stream Memory (Temporary)
    # ─────────────────────────────────────────────────────────────
    def set_stream_fact(self, canonical_id: str, key: str, value: str, confidence: float = 0.85, ttl_hours: float = 24.0, session_id: Optional[str] = None) -> MemoryEntry:
        now = time.time()
        sid = session_id or self.active_session_id or "default_session"
        mem_key = f"{sid}:{canonical_id}"
        session_mems = self._in_memory_stream_memory.setdefault(mem_key, {})

        if len(session_mems) >= self.max_stream_memory:
            oldest_key = min(session_mems, key=lambda k: session_mems[k].created_at)
            session_mems.pop(oldest_key, None)

        entry = MemoryEntry(
            key=key,
            value=value,
            confidence=confidence,
            source="stream_chatter",
            created_at=now,
            updated_at=now,
            last_used_at=now,
            usage_count=1,
            session_id=sid
        )
        session_mems[key] = entry
        expires_at = now + (ttl_hours * 3600.0)

        conn = self._get_connection()
        if conn:
            try:
                with conn:
                    conn.execute("""
                        INSERT INTO stream_memory (session_id, canonical_id, key, value, confidence, created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, canonical_id, key) DO UPDATE SET
                            value=excluded.value,
                            confidence=excluded.confidence,
                            expires_at=excluded.expires_at
                    """, (sid, canonical_id, key, value, confidence, now, expires_at))
            except Exception as e:
                logger.error("Error saving stream memory: %s", e)
                self._pending_stream_facts[(sid, canonical_id, key)] = expires_at
            finally:
                conn.close()
        else:
            self._pending_stream_facts[(sid, canonical_id, key)] = expires_at

        return entry

    def get_stream_context(self, user: UserIdentity, session_id: Optional[str] = None) -> List[MemoryEntry]:
        """Fetch active unexpired temporary stream facts for this user."""
        sid = session_id or self.active_session_id or "default_session"
        now = time.time()
        mem_key = f"{sid}:{user.canonical_id}"

        # Clean expired
        in_mem = self._in_memory_stream_memory.get(mem_key, {})
        valid_in_mem = [m for m in in_mem.values()]

        conn = self._get_connection()
        if not conn:
            return valid_in_mem

        try:
            rows = conn.execute("""
                SELECT * FROM stream_memory
                WHERE session_id=? AND canonical_id=? AND expires_at > ?
                ORDER BY created_at DESC LIMIT ?
            """, (sid, user.canonical_id, now, self.max_stream_memory)).fetchall()
            results = []
            for r in rows:
                results.append(MemoryEntry(
                    key=r["key"],
                    value=r["value"],
                    confidence=float(r["confidence"]),
                    source="stream_chatter",
                    created_at=float(r["created_at"]),
                    session_id=r["session_id"]
                ))
            return results
        except Exception as e:
            logger.error("Error fetching stream context: %s", e)
            return valid_in_mem
        finally:
            conn.close()

    def get_recent_messages(self, session_id: Optional[str] = None, limit: int = 8) -> List[Dict[str, Any]]:
        """Retrieve recent conversation history for prompt context."""
        sid = session_id or self.active_session_id or "default_session"
        in_mem = self._in_memory_recent_messages.get(sid, [])
        if in_mem:
            return in_mem[-limit:]

        conn = self._get_connection()
        if not conn:
            return []
        try:
            rows = conn.execute("""
                SELECT role, author, text, timestamp FROM recent_messages
                WHERE session_id=? ORDER BY timestamp DESC LIMIT ?
            """, (sid, limit)).fetchall()
            return [{"role": r["role"], "author": r["author"], "text": r["text"], "timestamp": float(r["timestamp"])} for r in reversed(rows)]
        except Exception as e:
            logger.error("Error retrieving recent messages: %s", e)
            return []
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────────
    # Fact Extraction & Scoring Algorithm
    # ─────────────────────────────────────────────────────────────
    def evaluate_memory_score(self, explicitness: float, repetition: float, future_usefulness: float, identity_relevance: float, stability: float) -> float:
        """
        memory_score = explicitness * 0.30 + repetition * 0.20 + future_usefulness * 0.25 + identity_relevance * 0.15 + stability * 0.10
        """
        score = (
            explicitness * 0.30 +
            repetition * 0.20 +
            future_usefulness * 0.25 +
            identity_relevance * 0.15 +
            stability * 0.10
        )
        return min(1.0, max(0.0, score))

    def extract_and_store_facts(self, user: UserIdentity, text: str) -> List[MemoryEntry]:
        """
        Heuristic extraction of high-value persistent and stream facts.
        Filters out low-value chatter (e.g. 'lol I died') and saves high-value items.
        """
        extracted: List[MemoryEntry] = []
        cleaned = text.strip()
        lower = cleaned.lower()

        # Reject pure reaction/short chatter
        if len(cleaned) < 5 or lower in ("lol i died", "gg", "hi", "hello", "lmao", "rip", "bruh", "f in chat"):
            return extracted

        # 1. Preferred Name Extraction
        name_patterns = [
            r"\bmy name is\s+([A-Za-z0-9_\u0900-\u097F]{2,20})\b",
            r"\bcall me\s+([A-Za-z0-9_\u0900-\u097F]{2,20})\b",
            r"\bi am\s+([A-Za-z0-9_\u0900-\u097F]{2,20})\b",
            r"\bnaam\s+([A-Za-z0-9_\u0900-\u097F]{2,20})\s+hai\b",
            r"\bmujhe\s+([A-Za-z0-9_\u0900-\u097F]{2,20})\s+bulao\b"
        ]
        for pat in name_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                name_candidate = m.group(1).strip()
                if name_candidate.lower() not in ("rukiya", "rukia", "here", "back", "tired", "playing", "ready", "dead"):
                    score = self.evaluate_memory_score(
                        explicitness=0.95, repetition=0.5, future_usefulness=0.90, identity_relevance=1.0, stability=0.90
                    )
                    if score >= 0.50:
                        entry = self.set_user_memory(
                            user.canonical_id,
                            key="preferred_name",
                            value=name_candidate,
                            confidence=0.92,
                            source="explicit_statement"
                        )
                        extracted.append(entry)
                        break

        # 2. Favorite Game / Main Game Extraction
        game_patterns = [
            r"\b(?:i mainly play|i play|mostly play|favorite game is|favourite game is|fav game is)\s+([A-Za-z0-9\s:]{2,30})",
            r"\b(?:maining|i main)\s+([A-Za-z0-9\s:]{2,25})"
        ]
        for pat in game_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                game_candidate = m.group(1).strip().split(".")[0].split(",")[0].strip()
                # Exclude nonsense words
                if len(game_candidate) >= 3 and game_candidate.lower() not in ("a game", "this", "something", "nothing"):
                    score = self.evaluate_memory_score(
                        explicitness=0.85, repetition=0.4, future_usefulness=0.85, identity_relevance=0.80, stability=0.75
                    )
                    if score >= 0.50:
                        entry = self.set_user_memory(
                            user.canonical_id,
                            key="favorite_game",
                            value=game_candidate,
                            confidence=0.88,
                            source="explicit_statement"
                        )
                        extracted.append(entry)
                        break

        # 3. Stream-level Topic/Question Context (Temporary)
        if "?" in text and len(cleaned) > 15:
            # Store viewer's recent question in stream memory
            score = self.evaluate_memory_score(
                explicitness=0.70, repetition=0.2, future_usefulness=0.50, identity_relevance=0.30, stability=0.20
            )
            if score >= 0.40:
                s_entry = self.set_stream_fact(
                    user.canonical_id,
                    key="recent_question",
                    value=cleaned[:100],
                    confidence=0.75,
                    ttl_hours=6.0
                )
                extracted.append(s_entry)

        return extracted
