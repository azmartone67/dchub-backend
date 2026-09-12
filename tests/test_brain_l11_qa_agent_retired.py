"""L11 QA agent is retired — and nothing reads it as current health any more.

L11's 6h surface sweep was DISABLED on 2026-05-19 (container crash-loop) and
never re-enabled. Its GET endpoint kept serving that sweep as the current
snapshot. Measured live 2026-09-12: latest_sweep_at 2026-05-19, verdict
"errors", 116 days old — while three consumers treated it as live:

  * brain_self_test marked L11 "active": its marker "agent|ok" is a substring
    test, and the stale body contains "ok".
  * L14 fed the May sweep into the causal prompt as current symptoms.
  * L8 fed qa_verdict / qa_errors / qa_slow from May into its prompt.

House rules: no DB, never import main.
"""
import ast
import pathlib

import pytest

flask = pytest.importorskip("flask")

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_L11_PATH = "/api/v1/brain/qa-agent"


def _app():
    from routes.brain_layer11_qa_agent import brain_layer11_bp
    app = flask.Flask(__name__)
    app.register_blueprint(brain_layer11_bp)
    return app


# ── the endpoint says it is retired, without touching a database ─────
def test_get_answers_410_retired_not_a_health_snapshot():
    r = _app().test_client().get(_L11_PATH)
    assert r.status_code == 410, r.status_code
    body = r.get_json()
    assert body["retired"] is True
    assert body["ok"] is False, "a retired endpoint must not claim ok"
    assert "verdict" not in body and "errors" not in body and "rows" not in body, \
        "the retired payload must not look like a surface-health snapshot"
    assert body["superseded_by"]["live_qa"]


def test_post_cannot_rearm_the_crash_looping_sweep():
    r = _app().test_client().post(_L11_PATH)
    assert r.status_code == 410, r.status_code
    assert r.get_json()["retired"] is True


def test_the_routes_still_bind_to_their_own_views():
    """Retiring meant inserting code above a decorated view — the exact edit
    that broke /brain-live on 2026-09-12. Ask the url_map, not the source."""
    m = {str(rule.rule): rule.endpoint for rule in _app().url_map.iter_rules()}
    assert m.get(_L11_PATH, "").endswith(".qa_agent"), m.get(_L11_PATH)
    assert m.get(_L11_PATH + "/history", "").endswith(".qa_history"), \
        m.get(_L11_PATH + "/history")


def test_history_is_left_reachable():
    """/history serves timestamped rows, so it cannot pass for current health."""
    r = _app().test_client().get(_L11_PATH + "/history")
    assert r.status_code == 400, "history should still validate its path param"


# ── no consumer reads L11 as live any more ───────────────────────────
def test_self_test_no_longer_probes_l11():
    from routes.brain_self_test import _LAYER_PROBES
    paths = [path for _name, path, _marker in _LAYER_PROBES]
    assert _L11_PATH not in paths, "self-test would report a retired layer active"
    assert paths, "self-test probe list extracted empty"


def test_l14_no_longer_feeds_l11_into_the_causal_prompt():
    from routes.brain_layer14_causal import _CONTEXT_PROBES
    paths = [path for _name, path, _timeout in _CONTEXT_PROBES]
    assert _L11_PATH not in paths
    assert paths, "L14 context probes extracted empty"


def _l8_tree():
    return ast.parse((_ROOT / "routes" / "brain_layer8_orchestrator.py")
                     .read_text(encoding="utf-8"))


def test_l8_no_longer_calls_l11():
    """Checked on executable Calls, so the removal comment (which names the
    path) cannot satisfy or break it."""
    hits = [n for n in ast.walk(_l8_tree())
            if isinstance(n, ast.Call) and n.args
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
            and n.args[0].value.startswith(_L11_PATH)]
    assert not hits, "L8 still reads the retired L11 endpoint"


def _dict_note(tree, key):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == key and isinstance(v, ast.Dict):
                for k2, v2 in zip(v.keys, v.values):
                    if (isinstance(k2, ast.Constant) and k2.value == "note"
                            and isinstance(v2, ast.Constant)):
                        return v2.value
    return None


def test_l8_labels_the_qa_gap_instead_of_going_silent():
    """A missing key reads as "no QA failures" — the model infers health from
    silence. The gap must be explicit, like outreach's."""
    note = _dict_note(_l8_tree(), "qa")
    assert note is not None, "L8 dropped the qa key — silence reads as healthy"
    assert "NOT READ" in note and "DELIBERATE GAP" in note, note


def test_l8_does_not_publish_the_old_qa_fields():
    src = (_ROOT / "routes" / "brain_layer8_orchestrator.py").read_text(encoding="utf-8")
    for ghost in ('"qa_verdict"', '"qa_errors"', '"qa_slow"'):
        assert ghost not in src, "L8 publishes %s again" % ghost


# ── the scheduler says retired, and why ──────────────────────────────
def _module_dict(tree, name):
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name and isinstance(node.value, ast.Dict)):
            return node.value
    return None


def test_scheduler_entry_is_retired_with_a_reason():
    tree = ast.parse((_ROOT / "dchub-scheduler.py").read_text(encoding="utf-8"))
    disabled, jobs = _module_dict(tree, "DISABLED_JOBS"), _module_dict(tree, "JOBS")
    assert disabled is not None and jobs is not None, "scheduler dicts not found"
    job_keys = {k.value for k in jobs.keys if isinstance(k, ast.Constant)}
    assert "brain_qa_agent_sweep_DISABLED" not in job_keys, \
        "the retired L11 sweep was moved back into JOBS"
    entry = None
    for k, v in zip(disabled.keys, disabled.values):
        if isinstance(k, ast.Constant) and k.value == "brain_qa_agent_sweep_DISABLED":
            entry = v
    assert isinstance(entry, ast.Dict), "retired entry missing from DISABLED_JOBS"
    reason = None
    for k, v in zip(entry.keys, entry.values):
        if isinstance(k, ast.Constant) and k.value == "disabled_reason":
            reason = ast.literal_eval(v)
    assert reason and "RETIRED" in reason, reason
