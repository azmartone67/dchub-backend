"""Guard the daily auto-press retry strategy (_attempt_plan).

Root cause of "1 auto-press in 30 days": the retry loop called Claude 3x with
the SAME MARKETING_MODEL every attempt, so a single stale/renamed primary model
id failed all three identically -> 502 -> no press persisted, every day. The fix
makes the loop try a known-good FALLBACK model from attempt 2. These tests pin
that contract: the plan must NOT use one model for every attempt, and the
fallback must appear before the final attempt.

NOTE: the CI unit-tests job installs ONLY pytest (not requirements.txt), so
`from routes.marketing_engine import _attempt_plan` crashes collection (the
module imports Flask). We AST-extract just _attempt_plan and exec it in a
namespace seeded with SENTINEL constants -- same pattern as
test_media_editorial_classify.py -- so the test validates the retry STRUCTURE
independent of the actual model id strings.
"""
import os
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = os.path.join(ROOT, "routes", "marketing_engine.py")


def _load_attempt_plan():
    src = open(ME, encoding="utf-8").read()
    tree = ast.parse(src)
    fn_src = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_attempt_plan":
            fn_src = ast.get_source_segment(src, node)
    assert fn_src, "_attempt_plan not found in marketing_engine.py"
    # Seed the module constants the function closes over with sentinels.
    ns = {
        "MARKETING_MODEL": "PRIMARY",
        "MARKETING_MODEL_FALLBACK": "FALLBACK",
        "_LAST_RESORT_TOPIC": ("last_resort_topic", "lr reason"),
    }
    exec(compile(fn_src, ME, "exec"), ns)
    return ns["_attempt_plan"]


_attempt_plan = _load_attempt_plan()


def test_plan_has_three_attempts_each_a_4tuple():
    plan = _attempt_plan("t", "r")
    assert len(plan) == 3
    for att in plan:
        assert len(att) == 4   # (topic, reason, simpler, model)


def test_first_attempt_is_primary_full_prompt():
    plan = _attempt_plan("t", "r")
    assert plan[0] == ("t", "r", False, "PRIMARY")


def test_not_all_attempts_use_the_same_model():
    # THE core guarantee: a single bad/stale primary model id can no longer fail
    # all three attempts identically (which is what produced "1 in 30 days").
    plan = _attempt_plan("t", "r")
    models = {att[3] for att in plan}
    assert len(models) >= 2
    assert "FALLBACK" in models


def test_fallback_tried_before_the_last_attempt():
    # Attempt 2 already swaps to the fallback model -> a bad primary costs one
    # wasted call, not the whole day.
    plan = _attempt_plan("t", "r")
    assert plan[1][3] == "FALLBACK"


def test_last_resort_keeps_platform_pulse_on_fallback_model():
    plan = _attempt_plan("t", "r")
    topic, reason, simpler, model = plan[2]
    assert topic == "last_resort_topic"
    assert simpler is True
    assert model == "FALLBACK"
