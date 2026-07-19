"""r-model-relations (2026-07-11): the platform-eval master shell.

Unit-level only (green-main rule: no main import, no DB, no network).
The invariants that MUST hold are the safety ones: no hardcoded partner
keys, origin-only harness execution, neutral framing, and no publication
path — those are what make the automation safe to run unattended.
"""

import pathlib

from model_relations import (_PLATFORMS, _SYSTEM, _KICKOFF, _parse_model_json,
                             DCHUB_BASE, MAX_MODEL_CALLS)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "model_relations.py").read_text()
ROUTES = (ROOT / "routes" / "model_relations_routes.py").read_text()


def test_no_hardcoded_partner_keys():
    # keys live in MODELREL_KEY_* env vars, never the repo (leaked-secret rule)
    assert "dchub_pro_" not in SRC and "dchub_pro_" not in ROUTES
    for cfg in _PLATFORMS.values():
        assert cfg["partner_key_env"].startswith("MODELREL_KEY_")


def test_harness_is_origin_locked():
    assert 'url.startswith(DCHUB_BASE)' in SRC
    assert DCHUB_BASE.startswith("https://dchub-backend-production")


def test_neutral_framing_no_praise_steering():
    low = (_SYSTEM + _KICKOFF).lower()
    assert "positive or negative" in low
    for banned in ("praise", "endorse", "glowing", "impressive", "best-in-class"):
        assert banned not in low


def test_never_publishes():
    # the shell writes runs + brain findings; it must have NO path to the
    # public wall (what-ais-say) or any frontend surface
    for s in (SRC, ROUTES):
        assert "what-ais-say" not in s and "what_ais_say" not in s
    assert "review queue" in ROUTES.lower()


def test_registry_shape_and_budget():
    # gemini added 2026-07-17 (the "malformed key" blocker was a paste
    # artifact, sanitized by routes._google_key.gemini_api_key);
    # moonshot/Kimi added same day (user-requested, api.moonshot.ai);
    # cohere added 2026-07-19 (partner-expansion — OpenAI-compat surface,
    # first live tick verified ok on command-a-03-2025).
    assert set(_PLATFORMS) == {"openai", "mistral", "meta", "perplexity",
                               "xai", "gemini", "moonshot", "cohere"}
    assert MAX_MODEL_CALLS == 8
    for cfg in _PLATFORMS.values():
        assert "pick" in cfg or "fixed" in cfg


def test_parse_model_json_tolerates_fences():
    wrap = lambda c: '{"choices":[{"message":{"content":%s}}]}' % c
    import json as j
    obj, _ = _parse_model_json(wrap(j.dumps('{"call": {"method": "GET", "url": "/x"}}')))
    assert obj == {"call": {"method": "GET", "url": "/x"}}
    fenced = '```json\n{"verdict": {"assessment": "ok"}}\n```'
    obj, _ = _parse_model_json(wrap(j.dumps(fenced)))
    assert obj == {"verdict": {"assessment": "ok"}}
    obj, content = _parse_model_json(wrap(j.dumps("not json at all")))
    assert obj is None and content == "not json at all"


def test_findings_use_canonical_writer():
    assert "from routes.brain_findings_writer import upsert_brain_finding" in SRC
    assert "INSERT INTO brain_findings" not in SRC  # never hand-rolled (wrong-col trap)
