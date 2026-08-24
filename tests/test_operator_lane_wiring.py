"""Guards for the OPERATOR lane's wiring into the LinkedIn composer.

routes/linkedin_content_engine.py records the same failure twice in its own
comments — a story kind "registered in the desk but not the composer", called
"the known partially-registered failure mode". It is not a graceful degradation:

    landing = LANDING_BY_TYPE[story_type]
    og_url  = OG_IMAGE_BY_TYPE[story_type]

are BARE subscripts. A type present in _PULLERS but missing from either dict
raises KeyError at compose time, inside the scheduled slot, where the only
symptom is a slot that quietly stops posting.

So the first test here is not about the operator lane at all — it is the
structural invariant that would have caught every previous instance, and will
catch the next one.

Pure: no DB, no network, never imports main.
"""
import pytest

from routes import linkedin_content_engine as eng


# ── the structural invariant ─────────────────────────────────────────────────
def test_every_puller_is_registered_in_every_story_type_map():
    """★ THE GUARD THAT GENERALISES. Any story type reachable by the composer
    must resolve a landing URL and an OG image, or composing it is a KeyError
    in a scheduled slot."""
    missing = []
    for st in eng._PULLERS:
        if st not in eng.LANDING_BY_TYPE:
            missing.append(f"{st} -> LANDING_BY_TYPE")
        if st not in eng.OG_IMAGE_BY_TYPE:
            missing.append(f"{st} -> OG_IMAGE_BY_TYPE")
        if st not in eng._STORY_TYPE_TO_TOPIC:
            missing.append(f"{st} -> _STORY_TYPE_TO_TOPIC")
    assert not missing, "partially-registered story type(s): " + "; ".join(missing)


def test_operator_lane_is_reachable_by_the_selector():
    """A lane the rotation can never pick is a lane that never posts."""
    assert "operator_spotlight" in eng._PULLERS
    picks = {eng._pick_story_type(None) for _ in range(400)}
    assert "operator_spotlight" in picks, \
        "the selector never reaches operator_spotlight"


# ── the lane composes from operator material ─────────────────────────────────
_SPOT = {
    "angle": "portfolio_growth",
    "operator": "STACK Infrastructure",
    "headline": ("15 facilities: STACK Infrastructure added the most sites to "
                 "DC Hub's tracked fleet in the last 30 days, reaching 100 "
                 "facilities and 7,937 MW."),
    "fleet_n": 100, "fleet_mw": 7937, "added": 15, "key": "stack",
}


def test_prompt_carries_the_headline_verbatim():
    """The headline is already number-led and already clears the desk's
    number-lead gate. If the model rewrites the opening, both break — so the
    prompt must hand it over verbatim and say not to touch it."""
    out = eng._build_user_prompt("operator_spotlight", {"spotlight": _SPOT},
                                 "https://dchub.cloud/facilities")
    assert _SPOT["headline"] in out, "the exact headline must reach the model"
    assert "EXACTLY as given" in out


def test_prompt_fences_the_numbers_and_the_opinion():
    out = eng._build_user_prompt("operator_spotlight", {"spotlight": _SPOT},
                                 "https://dchub.cloud/facilities")
    low = out.lower()
    assert "only numbers you may use" in low or "only the figures above" in low \
        or "only numbers" in low
    # positive-only directive 2026-07-02 must be carried into the prompt
    assert "no rankings-against" in low or "positive-only" in low
    # and it must never claim the tracked fleet is the operator's whole estate
    assert "complete estate" in low


def test_empty_spotlight_yields_an_empty_prompt_not_a_generic_one():
    """★ NOT VACUOUS — this calls the real builder. With no material the block
    must return "" so nothing composes, rather than emitting a prompt the model
    would happily fill with an invented operator profile."""
    assert eng._build_user_prompt("operator_spotlight", {"spotlight": None},
                                  "https://dchub.cloud/facilities") == ""
    assert eng._build_user_prompt("operator_spotlight", {},
                                  "https://dchub.cloud/facilities") == ""


def test_compose_SKIPS_when_there_is_no_material(monkeypatch):
    """★ The lane must go silent, not fall through to filler. Asserted against
    the REAL compose_story_post with the puller stubbed to 'no material'."""
    monkeypatch.setitem(eng._PULLERS, "operator_spotlight",
                        lambda: {"type": "operator_spotlight", "spotlight": None})
    monkeypatch.setattr(eng, "_pick_story_type", lambda *a, **k: "operator_spotlight")
    out = eng.compose_story_post()
    assert out.get("skip") is True, f"expected a skip, got {out!r}"
    assert "operator" in (out.get("reason") or "").lower()


def test_compose_does_NOT_skip_when_material_exists(monkeypatch):
    """★ THE ANTI-VACUOUS TWIN. A skip path that always skips is a lane that
    never posts — the same silent-death this file already records twice."""
    monkeypatch.setitem(eng._PULLERS, "operator_spotlight",
                        lambda: {"type": "operator_spotlight", "spotlight": _SPOT})
    monkeypatch.setattr(eng, "_pick_story_type", lambda *a, **k: "operator_spotlight")
    # ≥200 chars on purpose: compose_story_post treats a shorter body as a
    # thinned composer and SKIPS rather than publishing filler
    # (2026-07-15, "the posts are terrible" — silence beats a template).
    # A 130-char stub made the first version of this test fail against
    # correct code, which is the test being unrealistic, not the lane broken.
    body = (_SPOT["headline"] + " An independent, machine-readable record of "
            "the fleet is useful to the people financing and siting it. "
            "Every figure is computed over the operator's canonical name group. "
            "More at https://dchub.cloud/facilities #DataCenter")
    assert len(body) >= 200, "stub must clear the composer's thinness floor"
    monkeypatch.setattr(eng, "_compose_with_claude", lambda *a, **k: body)
    out = eng.compose_story_post()
    assert not out.get("skip"), f"lane skipped despite material: {out!r}"
    assert out.get("story_type") == "operator_spotlight"
    assert _SPOT["headline"] in (out.get("text") or "")


def test_card_fallback_never_prints_zero_mw():
    """★ Unknown capacity is not zero. Most tracked buildings carry no
    power_mw, so '0 MW' is a false statement about a real company — the same
    rule the headline builder enforces, applied to the image card."""
    src = open(eng.__file__, encoding="utf-8").read()
    i = src.index('elif story_type == "operator_spotlight":')
    block = src[i:i + 900]
    assert "float(_mw) > 0" in block, \
        "the card must gate the MW clause on a positive value"


def test_operator_lane_landing_is_a_checkable_surface():
    """The CTA must land somewhere a reader can verify the claim."""
    assert eng.LANDING_BY_TYPE["operator_spotlight"].startswith("https://dchub.cloud")


def test_retired_lane_is_not_resurrected():
    """hyperscaler_drama was retired 2026-07-02 — third-party commentary. The
    operator lane is the opposite (our records, no opinion) and must not bring
    the retired one back with it."""
    assert "hyperscaler_drama" not in eng._PULLERS


# ── the operator-lane debug endpoint (2026-08-15) ────────────────────────────
# The lane going quiet cost a deploy to diagnose because three unrelated causes
# — no supply, no candidate clearing a threshold, everyone inside the rotation
# window — all present from outside as the same silent None. The endpoint below
# answers it without one. It reads production data, so the gate is the guard.

def _debug_app(monkeypatch, admin_key="k-test"):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", admin_key)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    from flask import Flask
    from routes.media_editorial import media_editorial_bp
    app = Flask(__name__)
    app.register_blueprint(media_editorial_bp)
    return app.test_client()


_DEBUG_PATH = "/api/v1/brain/media/operator-lane-debug"


@pytest.mark.parametrize("req", [
    {},
    {"query_string": {"admin_key": "wrong"}},
    {"query_string": {"admin_key": ""}},
    {"headers": {"X-Admin-Key": "wrong"}},
])
def test_operator_lane_debug_refuses_without_the_admin_key(monkeypatch, req):
    """★ It reports the operator pipeline's raw candidate table off production
    data. An unauthenticated caller must get nothing — and an EMPTY submitted
    key must not match an empty expected key, which is how a gate that compares
    two blanks silently opens in an environment that never set the variable."""
    c = _debug_app(monkeypatch)
    assert c.get(_DEBUG_PATH, **req).status_code == 403


def test_operator_lane_debug_opens_for_the_admin_key(monkeypatch):
    """Both accepted channels work — otherwise the gate is not a gate, it is an
    outage, and the next quiet lane costs a deploy again."""
    c = _debug_app(monkeypatch)
    for req in ({"headers": {"X-Admin-Key": "k-test"}},
                {"query_string": {"admin_key": "k-test"}}):
        r = c.get(_DEBUG_PATH, **req)
        assert r.status_code == 200, r.get_json()
        # No DB in CI: it must degrade to a named reason, never a 500.
        assert "lane_disabled" in r.get_json()


def test_operator_lane_debug_stays_shut_when_no_key_is_configured(monkeypatch):
    """★ THE DANGEROUS DIRECTION. If neither DCHUB_ADMIN_KEY nor
    DCHUB_INTERNAL_KEY is set, `sent == expected` compares "" to "" — and the
    endpoint would be wide open on exactly the deployment that forgot to
    configure it."""
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    from flask import Flask
    from routes.media_editorial import media_editorial_bp
    app = Flask(__name__)
    app.register_blueprint(media_editorial_bp)
    c = app.test_client()
    assert c.get(_DEBUG_PATH).status_code == 403
    assert c.get(_DEBUG_PATH, query_string={"admin_key": ""}).status_code == 403


def test_the_lane_and_its_diagnostic_share_one_exclusion_set(monkeypatch):
    """★ A diagnostic that re-derives the exclude set can drift from the one the
    lane applies and then confidently report the wrong cause — the failure this
    endpoint exists to end. Both must derive it from the same function.

    ★★ 2026-08-23 — THIS USED TO GREP SOURCE TEXT for the literal
    `_operator_exclusion_set()`. That pins a spelling, not a behaviour: adding
    an argument broke it while the invariant it names held perfectly, and
    conversely a copy-pasted body that merely MENTIONED the name would have
    satisfied it. Both callers are now driven for real and asserted to observe
    the same tokens — see reference_dchub_drift_root_causes_0820 (GREP ≠
    behaviour)."""
    import routes.media_editorial as m

    calls = []
    LEDGER, TEXT = {"ledgeroperator"}, {"textoperator"}

    def _spy(conn=None):
        calls.append(conn)
        return set(LEDGER), set(TEXT), {"mode": "keys_only",
                                        "raw_text_tokens_n": 99,
                                        "known_operators_n": 7}
    monkeypatch.setattr(m, "_operator_exclusion_parts", _spy)

    # 1) the lane
    import routes.operator_spotlight as osp
    seen = {}

    def _fake_pick(conn, exclude_keys=None):
        seen["exclude_first_call"] = set(exclude_keys or ())
        return {"angle": "portfolio_growth", "operator": "Ledgeroperator",
                "key": "ledgeroperator", "added": 9, "sites": ["X"],
                "fleet_n": 50, "fleet_mw": 0}
    monkeypatch.setattr(osp, "pick_spotlight", _fake_pick)
    monkeypatch.setattr(m, "_conn", lambda: object())
    lane_lead = m._operator_spotlight_lead()
    assert calls, "the lane did not derive its veto from the shared function"
    # It was offered an operator the shared veto names, so it must refuse it.
    assert lane_lead is None,         "lane ignored the shared exclusion set it just derived"

    # 2) the diagnostic, over the SAME shared function
    n_before = len(calls)
    c = _debug_app(monkeypatch)
    body = c.get(_DEBUG_PATH, headers={"X-Admin-Key": "k-test"}).get_json()
    assert len(calls) > n_before,         "the diagnostic re-derived the exclude set instead of sharing it"
    assert body["exclusion_ledger_n"] == len(LEDGER)
    assert body["exclusion_text_n"] == len(TEXT)
    assert body["exclusion_tokens_n"] == len(LEDGER | TEXT)


def test_the_diagnostic_reports_the_veto_CAUSE_not_just_a_boolean(monkeypatch):
    """★ 2026-08-23. `rotation_blocked` was one boolean over the union of two
    unrelated causes — "we featured this operator on Tuesday" and "another
    publisher's prose contained its name" — which need opposite fixes. Two of
    the three causes this endpoint exists to separate were still blurred, and
    that is what made the 799-token text veto invisible for weeks."""
    import routes.media_editorial as m

    monkeypatch.setattr(m, "_operator_exclusion_parts",
                        lambda conn=None: ({"fromledger"}, {"fromtext"},
                                           {"mode": "keys_only",
                                            "raw_text_tokens_n": 799,
                                            "known_operators_n": 5448}))
    monkeypatch.setattr(m, "_conn", lambda: object())

    def _fake_diag(conn, **kw):
        return {"portfolio_candidates": [
                    {"key": "fromledger", "operator": "Fromledger"},
                    {"key": "fromtext", "operator": "Fromtext"},
                    {"key": "free", "operator": "Free"}],
                "deal_candidates": []}
    import routes.operator_spotlight as osp
    monkeypatch.setattr(osp, "spotlight_diagnostics", _fake_diag)
    monkeypatch.setattr(m, "_operator_spotlight_lead", lambda: None)

    c = _debug_app(monkeypatch)
    body = c.get(_DEBUG_PATH, headers={"X-Admin-Key": "k-test"}).get_json()
    rows = {r["key"]: r for r in body["diagnostics"]["portfolio_candidates"]}

    assert rows["fromledger"]["blocked_by_ledger"] is True
    assert rows["fromledger"]["blocked_by_text"] is False
    assert rows["fromtext"]["blocked_by_text"] is True
    assert rows["fromtext"]["blocked_by_ledger"] is False
    assert rows["free"]["blocked_by_ledger"] is False
    assert rows["free"]["blocked_by_text"] is False
    # and the old field stays the OR of the two, so nothing reading it breaks
    for r in rows.values():
        assert r["rotation_blocked"] == (r["blocked_by_ledger"]
                                         or r["blocked_by_text"])
    assert body["exclusion_text_mode"] == "keys_only"
    assert body["exclusion_raw_text_tokens_n"] == 799
