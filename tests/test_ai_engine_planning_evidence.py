"""tests/test_ai_engine_planning_evidence.py
Unit tests for planning, context compilation, and evidence scoring:
- test_query_planning
- test_context_compilation
- test_source_ranking
- test_source_conflict
"""
import datetime
import unittest

from services.ai_engine.context_compiler import ContextCompiler
from services.ai_engine.evidence_engine import EvidenceEngine
from services.ai_engine.models import AIEngineRequest, EvidenceItem, Plan
from services.ai_engine.planner import Planner
from services.models import MemoryEntry


class TestAIEnginePlanningAndEvidence(unittest.TestCase):
    def test_query_planning(self):
        """Planner should extract date dynamically, leverage memory, and cap query count."""
        planner = Planner(max_search_queries=3)
        req = AIEngineRequest(
            text="hey rukiya what is the latest update for the game I play?",
            author="Renji",
            persistent_memory=[
                MemoryEntry(key="favorite_game", value="Bleach Brave Souls")
            ]
        )
        plan = planner.plan(req)
        self.assertTrue(plan.search_required)
        self.assertTrue(plan.freshness_required)
        self.assertEqual(plan.preferred_provider, "gemini")
        self.assertLessEqual(len(plan.search_queries), 3)

        # Dynamic date check
        now = datetime.datetime.now(datetime.timezone.utc)
        current_year = str(now.year)

        primary_query = plan.search_queries[0].query
        self.assertIn("Bleach Brave Souls", primary_query)
        self.assertIn(current_year, primary_query)
        self.assertNotIn("the game i play", primary_query.lower())

    def test_context_compilation(self):
        """Verify bounded message context, memory relevance filtering, and source limits."""
        compiler = ContextCompiler()

        # Provide 12 persistent memories, 10 stream memories, 10 history items, 7 sources
        persistent_mems = [
            MemoryEntry(key=f"fact_{i}", value=f"detail_{i}")
            for i in range(12)
        ]
        # Make one fact highly relevant
        persistent_mems.append(MemoryEntry(key="favorite_anime", value="Bleach", confidence=0.95))

        stream_mems = [
            MemoryEntry(key=f"stream_event_{i}", value=f"val_{i}")
            for i in range(10)
        ]
        recent_chat = [
            {"role": "user", "author": f"viewer_{i}", "text": f"hello {i}"}
            for i in range(10)
        ]
        evidence = [
            EvidenceItem(url=f"https://source{i}.com", title=f"Source {i}", snippet=f"Snippet {i}", score=0.8)
            for i in range(7)
        ]

        req = AIEngineRequest(
            text="what is my favorite anime?",
            author="Ichigo",
            persistent_memory=persistent_mems,
            stream_memory=stream_mems,
            recent_messages=recent_chat
        )
        plan = Plan(intent="question", freshness_required=False, search_required=False)

        messages = compiler.compile(
            request=req,
            plan=plan,
            evidence_items=evidence
        )

        user_content = messages[-1]["content"]
        # Budgets check:
        # Sources <= 5
        self.assertIn("[1] Source 0", user_content)
        self.assertIn("[5] Source 4", user_content)
        self.assertNotIn("[6] Source 5", user_content)

        # Relevant memory should be prioritized
        self.assertIn("favorite_anime", user_content)

        # Chat history capped to 8
        self.assertIn("hello 9", user_content)
        self.assertNotIn("hello 0", user_content)

    def test_source_ranking(self):
        """Source scores prioritize authoritative domains (.gov, .org, official) and relevance."""
        engine = EvidenceEngine()
        raw_sources = [
            {"url": "https://random-blog.net/post", "title": "Random Rumors", "snippet": "I heard something"},
            {"url": "https://genshin.hoyoverse.com/en/news", "title": "Genshin Impact Official Update", "snippet": "Official patch 5.0 details released today"}
        ]
        ranked = engine.evaluate_sources(raw_sources, query="Genshin Impact update")
        self.assertEqual(len(ranked), 2)
        # Official source should rank first with significantly higher score
        self.assertEqual(ranked[0].url, "https://genshin.hoyoverse.com/en/news")
        self.assertGreater(ranked[0].score, ranked[1].score)
        self.assertGreaterEqual(ranked[0].authority, 0.8)

    def test_source_conflict(self):
        """Contradictory release dates or versions across sources trigger conflict detection and penalty."""
        engine = EvidenceEngine()
        conflicting_sources = [
            {"url": "https://sourceA.com", "title": "Patch 2.4 Notes", "snippet": "Version 2.4 releases on Friday"},
            {"url": "https://sourceB.com", "title": "Patch 3.1 Notes", "snippet": "Version 3.1 is the new update"},
            {"url": "https://sourceC.com", "title": "Patch 4.0 Notes", "snippet": "Version 4.0 just dropped"}
        ]
        items = engine.evaluate_sources(conflicting_sources, query="version update")
        has_conflict, conflicts = engine.detect_conflicts(items)
        self.assertTrue(has_conflict)
        self.assertGreater(len(conflicts), 0)

        # Quality should be penalized
        quality_with_conflict = engine.calculate_evidence_quality(items, has_conflict=True)
        quality_no_conflict = engine.calculate_evidence_quality(items, has_conflict=False)
        self.assertLess(quality_with_conflict, quality_no_conflict)


if __name__ == "__main__":
    unittest.main()
