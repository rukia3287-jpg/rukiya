import random
import unittest
from services.config import Config
from services.models import ChatMessage, UserIdentity
from services.decision_service import DecisionService


class TestDecisionService(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.rng = random.Random(42)  # Fixed seed for determinism
        self.decision_svc = DecisionService(self.config, rng=self.rng)
        self.user = UserIdentity(
            canonical_id="youtube:UC_alice",
            platform="youtube",
            user_id="UC_alice",
            username="alice",
            display_name="Alice",
            interaction_count=3,
            is_welcomed_in_stream=True
        )

    def test_direct_mention_responds(self):
        msg = ChatMessage(
            platform="youtube",
            message_id="m1",
            user_id="UC_alice",
            username="alice",
            display_name="Alice",
            text="hey rukiya how are you?"
        )
        decision = self.decision_svc.decide(msg, self.user)
        self.assertTrue(decision.should_respond)
        self.assertIn("direct mention", decision.reason.lower())
        self.assertTrue(decision.priority > 0.5)

    def test_direct_question_receives_high_priority(self):
        msg = ChatMessage(
            platform="youtube",
            message_id="m2",
            user_id="UC_alice",
            username="alice",
            display_name="Alice",
            text="rukiya who is the strongest soul reaper?"
        )
        decision = self.decision_svc.decide(msg, self.user)
        self.assertTrue(decision.should_respond)
        self.assertEqual(decision.intent, "question")
        self.assertGreaterEqual(decision.priority, 0.6)

    def test_bot_user_ignored(self):
        bot_user = UserIdentity(
            canonical_id="youtube:nightbot",
            platform="youtube",
            user_id="nightbot",
            username="Nightbot",
            display_name="Nightbot"
        )
        msg = ChatMessage(
            platform="youtube",
            message_id="m3",
            user_id="nightbot",
            username="Nightbot",
            display_name="Nightbot",
            text="rukiya check this link"
        )
        decision = self.decision_svc.decide(msg, bot_user)
        self.assertFalse(decision.should_respond)
        self.assertIn("bot_users", decision.reason)

    def test_banned_content_ignored(self):
        msg = ChatMessage(
            platform="youtube",
            message_id="m4",
            user_id="UC_alice",
            username="alice",
            display_name="Alice",
            text="rukiya this is fake spam"
        )
        decision = self.decision_svc.decide(msg, self.user)
        self.assertFalse(decision.should_respond)
        self.assertIn("banned word", decision.reason)

    def test_low_priority_unrelated_chatter_ignored(self):
        msg = ChatMessage(
            platform="youtube",
            message_id="m5",
            user_id="UC_alice",
            username="alice",
            display_name="Alice",
            text="random chatter in stream"
        )
        # Not a newcomer, not a question, no mention
        decision = self.decision_svc.decide(msg, self.user)
        self.assertFalse(decision.should_respond)

    def test_anti_repetition_similarity_rejection(self):
        resp1 = "Tch. Don't be reckless, Ichigo. Keep your guard up."
        resp2 = "Tch. Don't be reckless, Ichigo. Keep your guard up now."
        resp_diff = "Welcome in, chat! Glad to have you here tonight."

        self.decision_svc.record_response(self.user.canonical_id, resp1)

        # High similarity (> 0.70) should be flagged as repetitive
        self.assertTrue(self.decision_svc.is_repetitive(resp2))

        # Different response should not be flagged
        self.assertFalse(self.decision_svc.is_repetitive(resp_diff))

    def test_new_viewer_greeting_prioritized(self):
        new_user = UserIdentity(
            canonical_id="youtube:UC_bob",
            platform="youtube",
            user_id="UC_bob",
            username="bob",
            display_name="Bob",
            is_welcomed_in_stream=False
        )
        msg = ChatMessage(
            platform="youtube",
            message_id="m6",
            user_id="UC_bob",
            username="bob",
            display_name="Bob",
            text="hello chat"
        )
        decision = self.decision_svc.decide(msg, new_user)
        # Even without trigger, new viewer greeting has high newcomer signal
        self.assertEqual(decision.intent, "greeting")
        self.assertTrue(decision.should_respond)


if __name__ == "__main__":
    unittest.main()
