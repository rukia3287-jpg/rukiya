"""Additive Brain tables in MemoryService's existing SQLite database.

These operations run under MemoryService.run_async's serialized worker lock.
No message text or private answer is placed in the shared knowledge index.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

from services.brain import STYLES


class BrainMemoryMixin:
    def _init_brain_tables(self, conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS brain_preferences (
                canonical_id TEXT PRIMARY KEY, preferences TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS brain_feedback (
                id INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL,
                helpful INTEGER NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS brain_knowledge (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, title TEXT NOT NULL,
                body TEXT NOT NULL, expires_at REAL NOT NULL,
                UNIQUE(scope,title)
            );
            CREATE INDEX IF NOT EXISTS brain_knowledge_scope ON brain_knowledge(scope,expires_at);
        """)
        try:
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS brain_knowledge_fts USING fts5(
                    title, body, content='brain_knowledge', content_rowid='id'
                );
                CREATE TRIGGER IF NOT EXISTS brain_knowledge_insert AFTER INSERT ON brain_knowledge BEGIN
                    INSERT INTO brain_knowledge_fts(rowid,title,body) VALUES(new.id,new.title,new.body);
                END;
                CREATE TRIGGER IF NOT EXISTS brain_knowledge_delete AFTER DELETE ON brain_knowledge BEGIN
                    INSERT INTO brain_knowledge_fts(brain_knowledge_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body);
                END;
                CREATE TRIGGER IF NOT EXISTS brain_knowledge_update AFTER UPDATE ON brain_knowledge BEGIN
                    INSERT INTO brain_knowledge_fts(brain_knowledge_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body);
                    INSERT INTO brain_knowledge_fts(rowid,title,body) VALUES(new.id,new.title,new.body);
                END;
            """)
            self.brain_fts = True
        except sqlite3.OperationalError:
            self.brain_fts = False

    def _replay_brain_state(self, conn):
        # Run before the existing recovery writer, which replays post-deletion facts.
        for (kind, canonical, key), cutoff in self._pending_brain_deletes.items():
            if kind in {'all', 'history'}:
                conn.execute("DELETE FROM recent_messages WHERE canonical_id=? AND timestamp<=?", (canonical, cutoff))
            if kind == 'all':
                conn.execute("DELETE FROM stream_memory WHERE canonical_id=? AND created_at<=?", (canonical, cutoff))
                conn.execute("DELETE FROM brain_feedback WHERE canonical_id=? AND created_at<=?", (canonical, cutoff))
            if kind == 'fact':
                conn.execute("DELETE FROM memories WHERE canonical_id=? AND key=? AND updated_at<=?", (canonical, key, cutoff))
        for canonical in self._pending_brain_preferences:
            conn.execute("INSERT INTO brain_preferences VALUES(?,?) ON CONFLICT(canonical_id) DO UPDATE SET preferences=excluded.preferences",
                         (canonical, json.dumps(self._brain_preferences[canonical])))

    def get_brain_preferences(self, canonical):
        cached = self._brain_preferences.get(canonical)
        if canonical in self._pending_brain_preferences:
            return dict(cached)
        defaults = {'history_enabled': bool(self.config.memory_history_enabled),
                    'auto_memory': bool(self.config.memory_auto_extract),
                    'external_allowed': True, 'style': 'adaptive'}
        conn = self._get_connection()
        if conn:
            try:
                row = conn.execute("SELECT preferences FROM brain_preferences WHERE canonical_id=?", (canonical,)).fetchone()
                prefs = {**defaults, **json.loads(row['preferences'])} if row else defaults
                self._brain_preferences[canonical] = prefs
                if len(self._brain_preferences) > 2000:
                    for oldest in list(self._brain_preferences):
                        if oldest != canonical and oldest not in self._pending_brain_preferences:
                            self._brain_preferences.pop(oldest, None)
                            break
                return dict(prefs)
            except (sqlite3.Error, ValueError, TypeError):
                pass
            finally:
                conn.close()
        # Unknown consent during a storage outage must not authorize egress.
        return dict(cached) if cached else {**defaults, 'external_allowed': False, 'auto_memory': False, 'history_enabled': False}

    def set_brain_preferences(self, canonical, *, history_enabled, auto_memory, external_allowed, style='adaptive'):
        if style not in STYLES:
            return False
        if len(self._pending_brain_preferences) >= 1000 and canonical not in self._pending_brain_preferences:
            raise RuntimeError('Privacy settings capacity reached; change was not applied')
        prefs = {'history_enabled': bool(history_enabled), 'auto_memory': bool(auto_memory),
                 'external_allowed': bool(external_allowed), 'style': style}
        self._brain_preferences[canonical] = prefs
        self._pending_brain_preferences.add(canonical)
        if not history_enabled:
            self.clear_brain_history(canonical)
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn:
                self._replay_brain_state(conn)
            self._pending_brain_preferences.discard(canonical)
            return True
        except sqlite3.Error:
            self.fallback_mode = True
            return False
        finally:
            conn.close()

    def clear_brain_history(self, canonical):
        for scope, rows in self._in_memory_recent_messages.items():
            self._in_memory_recent_messages[scope] = [row for row in rows if row['canonical_id'] != canonical]
        self._pending_brain_deletes[('history', canonical, '')] = time.time()
        return self._flush_brain_deletes()

    def forget_brain_fact(self, canonical, key):
        key = key.strip().lower()[:64]
        self._in_memory_memories.get(canonical, {}).pop(key, None)
        self._pending_memories.get(canonical, set()).discard(key)
        self._pending_brain_deletes[('fact', canonical, key)] = time.time()
        self.clear_brain_history(canonical)
        return self._flush_brain_deletes()

    def clear_brain_user(self, canonical):
        facts_saved = self.delete_user_memories(canonical)
        self.clear_brain_history(canonical)
        for scope in list(self._in_memory_stream_memory):
            if scope.endswith(':' + canonical):
                self._in_memory_stream_memory.pop(scope, None)
        for key in list(self._pending_stream_facts):
            if key[1] == canonical:
                self._pending_stream_facts.pop(key, None)
        self._pending_brain_deletes[('all', canonical, '')] = time.time()
        return self._flush_brain_deletes() and facts_saved

    def _flush_brain_deletes(self):
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn:
                self._replay_brain_state(conn)
            self._pending_brain_deletes.clear()
            return True
        except sqlite3.Error:
            self.fallback_mode = True
            return False
        finally:
            conn.close()

    def add_brain_feedback(self, canonical, helpful):
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn:
                conn.execute("INSERT INTO brain_feedback(canonical_id,helpful,created_at) VALUES(?,?,?)", (canonical, bool(helpful), time.time()))
                conn.execute("DELETE FROM brain_feedback WHERE created_at<? OR id NOT IN (SELECT id FROM brain_feedback ORDER BY id DESC LIMIT 1000)", (time.time()-30*86400,))
            return True
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def add_brain_knowledge(self, scope, title, body):
        title, body = title.strip()[:128], body.strip()[:4000]
        if not title or not body:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn:
                conn.execute("INSERT INTO brain_knowledge(scope,title,body,expires_at) VALUES(?,?,?,?) ON CONFLICT(scope,title) DO UPDATE SET body=excluded.body,expires_at=excluded.expires_at",
                             (scope, title, body, time.time()+30*86400))
                conn.execute("DELETE FROM brain_knowledge WHERE expires_at<=? OR id NOT IN (SELECT id FROM brain_knowledge ORDER BY id DESC LIMIT 1000)", (time.time(),))
            return True
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def search_brain_knowledge(self, scope, query):
        tokens = re.findall(r'\w+', query.casefold())[:8]
        if not tokens:
            return []
        conn = self._get_connection()
        if not conn:
            return []
        try:
            if self.brain_fts:
                rows = conn.execute("SELECT k.title,k.body FROM brain_knowledge k JOIN brain_knowledge_fts ON k.id=brain_knowledge_fts.rowid WHERE brain_knowledge_fts MATCH ? AND k.scope=? AND k.expires_at>? ORDER BY bm25(brain_knowledge_fts) LIMIT 3",
                                    (' OR '.join('"'+token+'"' for token in tokens), scope, time.time())).fetchall()
            else:
                rows = conn.execute("SELECT title,body FROM brain_knowledge WHERE scope=? AND expires_at>? LIMIT 1000", (scope, time.time())).fetchall()
                rows = sorted((row for row in rows if any(t in (row['title']+' '+row['body']).casefold() for t in tokens)),
                              key=lambda row: sum(t in (row['title']+' '+row['body']).casefold() for t in tokens), reverse=True)[:3]
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()
