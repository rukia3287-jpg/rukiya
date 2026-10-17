from services.adaptive_behavior import AdaptiveBehaviorController


def test_complex_task_gets_deeper_effort():
    controller = AdaptiveBehaviorController()
    profile = controller.assess(
        text="Debug this architecture, plan a fix, and explain the root cause step by step",
        intent="help",
        platform="discord",
    )
    assert profile.effort_level in {"analytical", "deep"}
    assert profile.complexity_hint >= 0.6


def test_serious_intent_disables_humor():
    controller = AdaptiveBehaviorController()
    profile = controller.assess(
        text="I feel hopeless today",
        intent="sensitive",
        platform="discord",
    )
    assert "No sarcasm" in profile.style_instruction
    assert profile.effort_level in {"contextual", "analytical"}


def test_simple_greeting_is_low_effort():
    controller = AdaptiveBehaviorController()
    profile = controller.assess(text="hi", intent="greeting")
    assert profile.effort_level == "instant"
    assert profile.complexity_hint <= 0.1


def test_local_rules_are_opt_in(monkeypatch):
    monkeypatch.setenv("RUKIYA_LOCAL_RULES_ENABLED", "false")
    controller = AdaptiveBehaviorController()
    profile = controller.assess(text="hi", intent="greeting")
    assert profile.local_reply is None


def test_local_rule_can_handle_exact_greeting(monkeypatch):
    monkeypatch.setenv("RUKIYA_LOCAL_RULES_ENABLED", "true")
    controller = AdaptiveBehaviorController()
    profile = controller.assess(text="hi rukiya!", intent="greeting")
    assert profile.local_reply
    assert "back" in profile.local_reply.lower()
