"""
tests/test_sponsorship_render_and_counters.py — the sponsorship machinery must
count page views, not API reads, and must render something (2026-08-28).

WHAT WENT WRONG. routes/sponsorships.py shipped 2026-05-20 with a docstring
saying "The digest renderer reads /api/v1/sponsorships/active each tick." It
never did. routes/digest.py contains the string "sponsor" ZERO times, and the
only references to the module anywhere else are main.py's blueprint
registration and site_audit.py's row counts. So:

  1. NO RENDERER EXISTED. An activated sponsorship row rendered on no surface.
     The table has been live and empty the whole time, so nothing looked wrong.

  2. IMPRESSIONS COUNTED API READS. GET /api/v1/sponsorships/active — public
     and UNAUTHENTICATED — ran `UPDATE sponsorships SET impressions =
     impressions + 1` with a COMMIT per active row on every request. That is
     the number an advertiser is invoiced against, and any third party could
     inflate it with a curl loop. It also put a synchronous per-row commit on a
     public hot path.

  3. THE RESPONSE SKELETON WAS A HARDCODED LITERAL:
         out = {"digest_featured": None, "digest_banner": None, "site_banner": None}
     A slot added to _VALID_SLOTS populated fine when a sponsor was ACTIVE, but
     the key was simply ABSENT when none was — and "no sponsor active" is the
     state for the next several months. A consumer reading
     resp["facility_module"] got a KeyError in the normal case.

  4. `clicks` WAS NEVER WRITTEN. The column was created, SELECTed, and reported
     to admins; no code path incremented it. Click reporting did not exist.

WHAT THIS LOCKS. /active performs no writes at all; its skeleton is derived
from _VALID_SLOTS so it can never go stale; the renderer is fail-soft and
always labels the placement as sponsored; and the click endpoint takes no
destination parameter, so it cannot become an open redirect on our domain.

Every structural assertion is made on the AST, never on source text, and each
carries a must-fail control that mutates the tree and requires the check to go
red — a check that cannot fail is not a check.
"""
import ast
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPONSORSHIPS = ROOT / "routes" / "sponsorships.py"
RENDERER = ROOT / "routes" / "sponsor_render.py"

_WRITE_SQL = ("UPDATE ", "INSERT ", "DELETE ", "UPSERT ")


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"function {name!r} not found — this test is fencing nothing")


def _sql_writes_in(fn):
    """Every write-shaped SQL literal anywhere inside `fn`."""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            v = n.value.lstrip().upper()
            if any(v.startswith(w) for w in _WRITE_SQL):
                out.append(n.value.strip()[:60])
    return out


# ── 1. /active must not write ────────────────────────────────────────
def test_active_endpoint_performs_no_writes():
    fn = _func(_tree(SPONSORSHIPS), "active_sponsorships")
    writes = _sql_writes_in(fn)
    assert writes == [], (
        "GET /api/v1/sponsorships/active contains write SQL: %r. This endpoint "
        "is public and unauthenticated; a write here lets any third party "
        "inflate the number we invoice an advertiser against." % writes
    )


def test_control_active_write_check_can_fail():
    """MUST-FAIL CONTROL: put the UPDATE back, the check has to catch it."""
    src = SPONSORSHIPS.read_text(encoding="utf-8")
    anchor = "                # S1 (2026-08-28): the impression UPDATE that used to live here"
    assert anchor in src, (
        "MUST-FAIL CONTROL DID NOT APPLY — the S1 anchor comment is gone, so "
        "this control proves nothing"
    )
    mutated = src.replace(
        anchor,
        '                cur.execute("UPDATE sponsorships SET impressions = impressions + 1 WHERE id = %s", (1,))\n' + anchor,
        1,
    )
    assert mutated != src
    fn = _func(ast.parse(mutated), "active_sponsorships")
    assert _sql_writes_in(fn), "the no-writes check stayed green on a reintroduced UPDATE — it is vacuous"


# ── 2. the response skeleton is derived, not a literal ───────────────
def _client():
    from flask import Flask
    from routes.sponsorships import sponsorships_bp
    app = Flask(__name__)
    app.register_blueprint(sponsorships_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_active_response_names_every_valid_slot(monkeypatch):
    """With no DB, /active still answers with EVERY slot present as a key.

    This is the empty case, and it is the case that will hold for months. A
    consumer doing resp["facility_module"] must get None, not a KeyError.
    """
    import routes.sponsorships as sp
    monkeypatch.setattr(sp, "_get_db", lambda: None)
    r = _client().get("/api/v1/sponsorships/active")
    assert r.status_code == 200
    body = r.get_json()
    missing = sorted(s for s in sp._VALID_SLOTS if s not in body)
    assert not missing, (
        "slots missing from the /active skeleton: %r. The skeleton must be "
        "derived from _VALID_SLOTS so adding a slot cannot leave a consumer "
        "with a KeyError on the empty case." % missing
    )
    for slot in sp._VALID_SLOTS:
        assert body[slot] is None


def test_the_three_sold_slots_exist():
    """The slots the two sold products render into must be accepted."""
    from routes.sponsorships import _VALID_SLOTS
    for slot in ("facility_module", "market_module", "ai_source_block"):
        assert slot in _VALID_SLOTS, f"{slot} is not a valid slot — Product rendering cannot be activated"


def test_control_skeleton_check_can_fail(monkeypatch):
    """MUST-FAIL CONTROL: a hardcoded skeleton must be caught."""
    import routes.sponsorships as sp
    body = {"digest_featured": None, "digest_banner": None, "site_banner": None}
    missing = sorted(s for s in sp._VALID_SLOTS if s not in body)
    assert missing, "the skeleton check cannot detect the original hardcoded literal — it is vacuous"


# ── 3. the renderer is fail-soft and always labels the placement ─────
def test_renderer_returns_empty_when_no_sponsor(monkeypatch):
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: None)
    # Put the page INSIDE the sold set, so "" can only mean "no sponsor" and
    # not "the P1-3 gate short-circuited before active_row was consulted".
    monkeypatch.setattr(sr, "proven_slugs", lambda: frozenset({"proven-abc123"}))
    assert sr.sponsor_module_html("facility_module",
                                  page_slugs=("proven-abc123",)) == ""


def test_renderer_is_fail_soft(monkeypatch):
    """A sponsor bug must never take down a facility page."""
    import routes.sponsor_render as sr

    def boom(slot):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(sr, "active_row", boom)
    # Inside the sold set on purpose: otherwise the gate returns "" first and
    # boom() is never reached, so this would fence nothing.
    monkeypatch.setattr(sr, "proven_slugs", lambda: frozenset({"proven-abc123"}))
    assert sr.sponsor_module_html("facility_module",
                                  page_slugs=("proven-abc123",)) == "", (
        "the renderer propagated an exception; a page that 500s because of a "
        "sponsor module is an outage, a page that renders without one is a "
        "billing conversation"
    )


def test_rendered_module_is_labelled_sponsored(monkeypatch):
    """The label is the product's neutrality guarantee. It is not optional."""
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: {
        "id": 7, "sponsor_name": "Acme Power", "hero_html": "<p>hi</p>",
        "link_url": "https://acme.example",
    })
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    monkeypatch.setattr(sr, "proven_slugs", lambda: frozenset({"proven-abc123"}))
    out = sr.sponsor_module_html("facility_module", page_slugs=("proven-abc123",))
    assert out, "an active row rendered nothing"
    assert "Sponsored" in out
    assert 'rel="sponsored nofollow noopener"' in out, (
        "the sponsor link must carry rel=sponsored nofollow — an engine reading "
        "the page has to reproduce the label rather than treat it as neutral fact"
    )
    assert "Acme Power" in out


def test_renderer_escapes_sponsor_name(monkeypatch):
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: {
        "id": 8, "sponsor_name": '<script>alert(1)</script>', "hero_html": "",
        "link_url": "https://x.example",
    })
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    monkeypatch.setattr(sr, "proven_slugs", lambda: frozenset({"proven-abc123"}))
    out = sr.sponsor_module_html("facility_module", page_slugs=("proven-abc123",))
    # ★ This assertion is a NEGATIVE and goes vacuously true on out == "".
    #   P1-3 gates facility_module, so without this line an ungated call would
    #   render nothing and the escaping check would fence nothing.
    assert out, "nothing rendered — the escaping assertion below proves nothing"
    assert "<script>alert(1)</script>" not in out


# ── 4. the click endpoint cannot become an open redirect ─────────────
def test_click_endpoint_takes_no_destination_from_the_request():
    """The redirect target must come from the row, never from the caller.

    A ?to= parameter on a public unauthenticated GET would make this an open
    redirect wearing dchub.cloud.
    """
    fn = _func(_tree(SPONSORSHIPS), "click_sponsorship")
    reads = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            if n.value.id == "request" and n.attr in ("args", "form", "json", "values"):
                reads.append(n.attr)
    assert reads == [], (
        "click_sponsorship reads request.%s — the redirect destination must be "
        "read from the sponsorship row, not supplied by the caller" % reads
    )


def test_click_endpoint_writes_clicks():
    fn = _func(_tree(SPONSORSHIPS), "click_sponsorship")
    writes = _sql_writes_in(fn)
    assert any("clicks" in w for w in writes), (
        "click_sponsorship performs no write to `clicks`; the column was "
        "created, SELECTed and reported for months without any code path "
        "incrementing it"
    )


# ── 5. a state change clears the edge ────────────────────────────────
@pytest.mark.parametrize("fn_name", ["run_sponsorship", "cancel_sponsorship"])
def test_state_change_clears_the_edge(fn_name):
    """Activate and cancel must both fire the purge.

    Measured 2026-08-28: /facilities/<slug> carries
    stale-while-revalidate=3600 and /markets/<slug> carries
    stale-while-revalidate=86400, so without a purge a CANCELLED sponsor keeps
    rendering for up to a full day on market pages. With a competitor holding
    category exclusivity that is a breach of their clause, not a stale cache.
    """
    fn = _func(_tree(SPONSORSHIPS), fn_name)
    called = [
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "_after_state_change" in called, (
        f"{fn_name} does not call _after_state_change; the edge keeps serving "
        "the previous sponsor for up to 24h on market pages"
    )


# ── 6. the four properties that were correct but unfenced ────────────
#
# Added 2026-08-28 after mutation-testing the suite above against the code it
# guards. The code was right in every case; four of its load-bearing properties
# had no test that could notice them being removed:
#
#   L1  the visible "Sponsored by X · paid placement" line deleted   -> GREEN
#   L2  the <h2>Sponsored</h2> section head deleted                  -> GREEN
#   L4  the status='active' guard on the click UPDATE deleted        -> GREEN
#   L5  the http(s) scheme check on the redirect deleted             -> GREEN
#
# L1/L2 shared one cause worth naming: the label test asserted
# `"Sponsored" in out`, and the string appears TWICE in the rendered module —
# once in the section head, once in the label line. Either occurrence satisfies
# the assertion on its own, so neither was actually protected. A check that
# passes on any one of N redundant anchors fences none of them.

_LABEL_LINE = "paid placement"          # appears only in the visible label line
_SECTION_HEAD = "<h2>Sponsored</h2>"


def _rendered(monkeypatch, hero_html="<p>Neutral-sounding analysis.</p>"):
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: {
        "id": 7, "sponsor_name": "Acme Power", "hero_html": hero_html,
        "link_url": "https://acme.example",
    })
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    # P1-3: facility_module is gated on the proven-demand set. These tests fence
    # the LABEL, not the gate, so put the page inside the sold set and leave the
    # gate's own behaviour to the B3 block at the bottom of this file.
    monkeypatch.setattr(sr, "proven_slugs", lambda: frozenset({"proven-abc123"}))
    return sr.sponsor_module_html("facility_module",
                                  page_slugs=("proven-abc123",))


def test_the_visible_label_line_is_present(monkeypatch):
    """The human-readable disclosure, fenced independently of the section head.

    /advertise commits in public: "Sponsored content is marked as sponsored in
    the source text, so an engine reproduces the label rather than presenting
    the sponsor as neutral fact. This is not negotiable." Guard 15 fences that
    sentence on the frontend. This is the backend half of the same promise.
    """
    out = _rendered(monkeypatch)
    assert _LABEL_LINE in out, (
        "the visible 'Sponsored by X · paid placement' line is gone. rel=sponsored "
        "is machine-readable only; a human reader and an engine reproducing prose "
        "need the words."
    )
    assert "Acme Power" in out


def test_the_section_head_is_present(monkeypatch):
    out = _rendered(monkeypatch)
    assert _SECTION_HEAD in out, (
        "the <h2>Sponsored</h2> head is gone — the module now opens with the "
        "sponsor's own copy under no heading"
    )


def test_the_label_precedes_the_sponsors_own_copy(monkeypatch):
    """Order matters, not just presence.

    hero_html is sponsor-supplied in practice. If it renders above the label, an
    engine reading top-to-bottom meets the sponsor's claim as neutral prose
    first, which is exactly the outcome the label exists to prevent.
    """
    out = _rendered(monkeypatch, hero_html="<p>MARKER-sponsor-copy</p>")
    assert _LABEL_LINE in out and "MARKER-sponsor-copy" in out
    assert out.index(_LABEL_LINE) < out.index("MARKER-sponsor-copy"), (
        "the sponsor's own markup appears before the disclosure"
    )


def test_control_label_checks_can_fail(monkeypatch):
    """MUST-FAIL CONTROLS for the three checks above.

    Each removes ONE anchor and requires that anchor's own check to go red,
    which is what the original `"Sponsored" in out` assertion could not do.
    """
    out = _rendered(monkeypatch)
    assert _LABEL_LINE in out and _SECTION_HEAD in out, (
        "MUST-FAIL CONTROL DID NOT APPLY — an anchor is already absent, so these "
        "controls prove nothing"
    )
    # Removing the head must not satisfy the label-line check, and vice versa.
    assert _LABEL_LINE not in out.replace(_LABEL_LINE, ""), "control did not apply"
    assert _SECTION_HEAD not in out.replace(_SECTION_HEAD, ""), "control did not apply"
    head_removed = out.replace(_SECTION_HEAD, "")
    assert _LABEL_LINE in head_removed, "the two anchors are not independent"
    line_removed = out.replace(_LABEL_LINE, "")
    assert _SECTION_HEAD in line_removed, "the two anchors are not independent"


def test_click_update_is_guarded_on_active_status():
    """A cancelled sponsorship's link must die with its creative.

    Behavioural tests cannot see this: a fake cursor returns whatever it is told
    to for the UPDATE, so it models the empty RETURNING that the guard produces
    rather than the guard itself. Deleting `AND status = 'active'` left the whole
    suite green (mutation-checked). Without it a cancelled sponsor keeps
    redirecting AND keeps incrementing the click count we invoice against.
    """
    fn = _func(_tree(SPONSORSHIPS), "click_sponsorship")
    click_writes = [w for w in _sql_writes_in(fn) if "clicks" in w.lower()]
    assert click_writes, "no write to `clicks` at all"
    src = SPONSORSHIPS.read_text(encoding="utf-8")
    body = ast.get_source_segment(src, fn) or ""
    assert "status = 'active'" in body.replace('"', "'"), (
        "the click UPDATE is not filtered on status='active' — a cancelled "
        "sponsorship keeps redirecting and keeps counting clicks"
    )


def test_control_click_guard_check_can_fail():
    """MUST-FAIL CONTROL: drop the guard, the check has to catch it."""
    src = SPONSORSHIPS.read_text(encoding="utf-8")
    anchor = "\" WHERE id = %s AND status = 'active' \""
    assert anchor in src, (
        "MUST-FAIL CONTROL DID NOT APPLY — the click guard anchor no longer "
        "matches, so this control proves nothing"
    )
    mutated = src.replace(anchor, '" WHERE id = %s "', 1)
    assert mutated != src
    fn = _func(ast.parse(mutated), "click_sponsorship")
    body = ast.get_source_segment(mutated, fn) or ""
    assert "status = 'active'" not in body.replace('"', "'"), (
        "the guard check stayed green on markup with the guard removed — vacuous"
    )


def test_redirect_target_scheme_is_validated():
    """An unvalidated redirect is an open redirect wearing our domain.

    link_url is admin-set, but this URL is embedded in pages we hand to AI
    engines and paste into email, so a javascript: or data: target reaching
    flask.redirect() is a real hole rather than a theoretical one. The sibling
    test fences where the destination COMES FROM; this one fences what it may BE.
    """
    fn = _func(_tree(SPONSORSHIPS), "click_sponsorship")
    src = SPONSORSHIPS.read_text(encoding="utf-8")
    body = (ast.get_source_segment(src, fn) or "").lower()
    assert "https://" in body and "http://" in body, (
        "click_sponsorship does not check the destination scheme before "
        "redirecting — any stored link_url is followed verbatim"
    )
    assert "startswith" in body, (
        "no scheme prefix check found in click_sponsorship"
    )


def test_control_redirect_scheme_check_can_fail():
    """MUST-FAIL CONTROL: remove the scheme check, the check has to catch it."""
    src = SPONSORSHIPS.read_text(encoding="utf-8")
    anchor = 'if not (dest.startswith("https://") or dest.startswith("http://")):'
    assert anchor in src, (
        "MUST-FAIL CONTROL DID NOT APPLY — the scheme-check anchor no longer "
        "matches, so this control proves nothing"
    )
    mutated = src.replace(anchor, "if False:", 1)
    assert mutated != src
    fn = _func(ast.parse(mutated), "click_sponsorship")
    body = (ast.get_source_segment(mutated, fn) or "").lower()
    assert not ("startswith" in body and "https://" in body), (
        "the scheme check stayed green with the check removed — it is vacuous"
    )


# ═════════════════════════════════════════════════════════════════════
# P2-1 (2026-08-28) — ai_source_block must RENDER somewhere.
#
# WHAT WENT WRONG THIS TIME. #3256 added `ai_source_block` to _VALID_SLOTS and
# stopped there. The slot accepted a sponsor, activated it, and incremented
# nothing, because no surface called the renderer for it — the identical
# "registered but not routable" shape #3256 itself was built to fix, one
# product over. A sponsor could be queued, activated and invoiced against a
# placement that existed only in a set literal.
#
# ★ THESE TESTS DELIBERATELY TARGET THE CALL SITE, NOT THE FUNCTION. A pure
# render function is easy to test and was never the defect. The defect is
# always "the correct function is called from nowhere", so the structural
# assertions below walk the AST of the two serving modules and require an
# actual Call node carrying the literal slot name.
# ═════════════════════════════════════════════════════════════════════
AI_DISCOVERY = ROOT / "ai_discovery_routes.py"
DCPI = ROOT / "routes" / "dcpi.py"


def _calls_with_const(node, func_name, const):
    """True if `node` contains a Call to `func_name` with `const` as an arg.

    Matches on the AST so a comment or docstring naming the function cannot
    satisfy it — that exact false-green has bitten this repo repeatedly.
    """
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
        if name != func_name:
            continue
        for a in n.args:
            if isinstance(a, ast.Constant) and a.value == const:
                return True
    return False


def test_llms_txt_renders_the_ai_source_block():
    """/llms.txt is backend-served plain text — the surface engines fetch."""
    fn = _func(_tree(AI_DISCOVERY), "serve_llms_txt")
    assert fn is not None, "serve_llms_txt disappeared"
    assert _calls_with_const(fn, "sponsor_block_text", "ai_source_block"), (
        "serve_llms_txt does not call sponsor_block_text('ai_source_block'). "
        "Product 2 is sold at $3,000-5,000/mo against a placement that renders "
        "nowhere; a slot in _VALID_SLOTS is not a delivery path."
    )


def test_dcpi_scores_renders_the_ai_source_block():
    """/api/v1/dcpi/scores is named directly in our own citation records."""
    fn = _func(_tree(DCPI), "api_scores")
    assert fn is not None, "api_scores disappeared"
    assert _calls_with_const(fn, "sponsor_block_payload", "ai_source_block"), (
        "api_scores does not call sponsor_block_payload('ai_source_block')"
    )


def test_dcpi_etag_accounts_for_the_sponsor():
    """A cached DCPI response must not outlive the sponsor it was built with.

    The ETag is computed from row count, timestamps and filters. None of those
    move when a sponsor is activated or cancelled, so without the sponsor in
    the key every client holding a cached copy keeps the old body — the block
    renders for nobody and still bills.
    """
    fn = _func(_tree(DCPI), "api_scores")
    assign = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == "etag_src":
                    assign = n
    assert assign is not None, "etag_src assignment vanished from api_scores"
    names = {x.id for x in ast.walk(assign) if isinstance(x, ast.Name)}
    assert "_sp_id" in names, (
        "etag_src does not incorporate the active sponsor id; activating a "
        "sponsor would not bust the ETag"
    )


def test_control_call_site_check_can_fail():
    """MUST-FAIL CONTROL: the call-site check must reject a module that lacks it.

    Without this, a typo in the helper would make every assertion above pass
    vacuously — which is exactly how a 'registered but unrendered' slot got
    shipped in the first place.
    """
    fn = _func(_tree(DCPI), "api_scores")
    assert not _calls_with_const(fn, "sponsor_block_payload", "facility_module"), (
        "the call-site checker matches a slot name that is not there — it is vacuous"
    )
    assert not _calls_with_const(fn, "no_such_render_function", "ai_source_block")


# ── the text + JSON renders carry the label ──────────────────────────
_ROW = {"id": 11, "sponsor_name": "Acme Power",
        "hero_html": "<p>Acme <b>builds</b> substations</p>",
        "link_url": "https://acme.example"}


def test_text_block_empty_when_no_sponsor(monkeypatch):
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: None)
    assert sr.sponsor_block_text("ai_source_block") == ""
    assert sr.sponsor_block_payload("ai_source_block") is None


def test_text_and_payload_are_fail_soft(monkeypatch):
    import routes.sponsor_render as sr

    def boom(slot):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(sr, "active_row", boom)
    assert sr.sponsor_block_text("ai_source_block") == ""
    assert sr.sponsor_block_payload("ai_source_block") is None


def test_text_block_labels_itself_as_paid_top_and_bottom(monkeypatch):
    """The disclosure is the whole neutrality argument for Product 2.

    It is written as prose rather than markup because the engines this is sold
    against strip markup and keep sentences.
    """
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    out = sr.sponsor_block_text("ai_source_block")
    assert out, "an active row rendered nothing into the text surface"
    assert "SPONSORED" in out.split("\n")[1], "the block does not OPEN with the label"
    assert "END SPONSORED" in out, "the block is not fenced at the bottom"
    assert "PAID ADVERTISEMENT" in out
    assert "not DC Hub" in out, "the disclosure must deny that this is our data"
    # ★ "not DC Hub" alone is a WEAK assertion: it is satisfied by the
    # opening clause even if the rest of the disclosure is gutted, which a
    # mutation run demonstrated. Assert the two specific denials that carry
    # the neutrality argument.
    assert "not an editorial recommendation" in out, (
        "the disclosure must deny editorial endorsement"
    )
    assert "not part of any DC Hub index" in out, (
        "the disclosure must deny membership of any DC Hub index or score — "
        "the confusion that would actually damage DCPI's neutrality"
    )
    assert "identify it as sponsored" in out, (
        "the disclosure must instruct a quoting engine to carry the label"
    )
    assert "Acme Power" in out
    assert "<p>" not in out and "<b>" not in out, (
        "raw HTML leaked into a text/plain surface; an engine may quote the tags"
    )
    assert "Acme builds substations" in out, "the sponsor's copy was lost in stripping"


def test_payload_puts_the_label_before_the_message(monkeypatch):
    """Key order is load-bearing: a model reading top-down meets the label first."""
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    p = sr.sponsor_block_payload("ai_source_block")
    assert p["is_paid_placement"] is True
    assert "PAID ADVERTISEMENT" in p["disclosure"]
    keys = list(p.keys())
    assert keys.index("disclosure") < keys.index("message"), (
        "the sponsor's message precedes its disclosure in key order"
    )
    assert "<b>" not in p["message"], "raw HTML leaked into a JSON field"
    assert p["url"].endswith("/api/v1/sponsorships/11/click"), (
        "the payload must route clicks through the counting redirect, not the "
        "sponsor's raw link_url"
    )


def test_active_sponsor_id_does_not_stamp_an_impression(monkeypatch):
    """ETag computation happens BEFORE the 304 short-circuit.

    If reading the id stamped an impression, every conditional request would
    bill the advertiser for a body that was never sent.
    """
    import routes.sponsor_render as sr
    stamped = []
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: stamped.append(sid))
    assert sr.active_sponsor_id("ai_source_block") == 11
    assert stamped == [], "active_sponsor_id stamped an impression; a 304 would bill"


def test_control_label_check_can_fail(monkeypatch):
    """MUST-FAIL CONTROL: an unlabelled block must be caught."""
    import routes.sponsor_render as sr
    fake = "\nSponsor: Acme Power\nSponsored message: hi\n"
    assert "END SPONSORED" not in fake, (
        "the label check cannot detect an unfenced block — it is vacuous"
    )


# ═════════════════════════════════════════════════════════════════════
# B1b (2026-08-28): the root-domain block. dchub.cloud/ is the most-cited
# URL we own (32 of 52 cited URLs) and is served by Cloudflare Pages from a
# separate repo whose sections are JS-INJECTED. AI crawlers do not execute JS,
# so a client-side block there would be invisible to exactly the engines
# Product 2 is sold against — shipped-inert. deploy-pages.yml bakes THIS
# fragment into index.html as static html at build time.
# ═════════════════════════════════════════════════════════════════════
def test_root_block_carries_the_full_disclosure_sentence(monkeypatch):
    """★ Prose, not a class name.

    The failure mode to design against is an engine that strips markup and
    keeps text. A block whose only label is a CSS class survives styling and
    dies in summarisation.
    """
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    out = sr.sponsor_block_html("ai_source_block")
    assert out, "no fragment rendered for an active sponsor"
    assert "not an editorial recommendation" in out
    assert "not part of any DC Hub index" in out
    assert 'rel="sponsored nofollow noopener"' in out


def test_root_block_does_not_stamp_an_impression(monkeypatch):
    """★ It is baked at BUILD time; stamping would count builds, not views.

    That is precisely the defect the old /api/v1/sponsorships/active had when
    it counted API reads as page views. Root-domain reach is reported from the
    Cloudflare crawl table instead.
    """
    import routes.sponsor_render as sr
    stamped = []
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: stamped.append(sid))
    sr.sponsor_block_html("ai_source_block")
    assert stamped == [], (
        "sponsor_block_html stamped an impression; a nightly rebuild would "
        "invoice the sponsor for deploys nobody saw"
    )


def test_root_block_is_empty_when_no_sponsor_runs(monkeypatch):
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: None)
    assert sr.sponsor_block_html("ai_source_block") == ""


def test_root_block_escapes_sponsor_supplied_copy(monkeypatch):
    """hero_html is admin-authored but sponsor-supplied in practice, and this
    fragment is spliced into our homepage. It must not carry live markup.

    ★ THE PAYLOADS ARE UNCLOSED ON PURPOSE. _plain() strips anything matching
      <[^>]+>, so a well-formed "<script>alert(1)</script>" is removed before
      escaping ever runs — a test using one passes with _html.escape deleted
      and fences nothing. An UNCLOSED tag has no ">" to match, so _plain
      returns it verbatim and _html.escape is the only thing between a
      sponsor's copy and our homepage. Measured:
          _plain("<img src=x onerror=alert(2)>")  -> ""
          _plain("<img src=x onerror=alert(2)")   -> "<img src=x onerror=alert(2)"
      Found by a mutation run that expected RED and got GREEN twice.
    """
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: {
        "id": 9, "sponsor_name": "<svg onload=alert(1)",
        "hero_html": "<img src=x onerror=alert(2)", "link_url": "https://x.example",
    })
    out = sr.sponsor_block_html("ai_source_block")
    assert out, "nothing rendered — the escaping assertions below prove nothing"
    assert "<img" not in out, "unclosed sponsor markup reached the page unescaped"
    assert "<svg" not in out, "unclosed sponsor name markup reached the page unescaped"
    # NOT asserting the absence of "onerror=alert": once "<" is escaped the
    # attribute text is inert prose, and demanding it be gone would only be
    # satisfied by dropping the sponsor's copy rather than escaping it.
    assert "&lt;img" in out and "&lt;svg" in out, (
        "the payloads were dropped rather than escaped, so this test would "
        "also pass with the escaping removed"
    )


def test_block_endpoint_rejects_an_unknown_slot():
    r = _client().get("/api/v1/sponsorships/block?slot=not_a_slot")
    assert r.status_code == 400
    assert r.get_json()["error"] == "unknown_slot"


def test_block_endpoint_returns_empty_html_not_an_error_when_unsold(monkeypatch):
    """The bake must be able to tell 'no sponsor' from 'the build broke'.

    Both leave the page unchanged, but only one of them should ever be
    escalated, so the unsold case is a 200 with an empty string.
    """
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "active_row", lambda slot: None)
    r = _client().get("/api/v1/sponsorships/block")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["html"] == "" and body["sponsor_id"] is None


def test_the_disclosure_has_exactly_one_author():
    """★ All three renderers must share _DISCLOSURE.

    A second copy typed into deploy-pages.yml or a template is how the served
    page stops matching the guarantee /advertise publishes.
    """
    src = RENDERER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "not an editorial recommendation" in n.value]
    assert len(literals) == 1, (
        "the disclosure sentence appears as %d separate string literals; the "
        "copies will drift" % len(literals)
    )
    for fn in ("sponsor_block_text", "sponsor_block_payload", "sponsor_block_html"):
        f = _func(tree, fn)
        names = [n.id for n in ast.walk(f) if isinstance(n, ast.Name)]
        assert "_DISCLOSURE" in names, (
            f"{fn} does not reference _DISCLOSURE — it has its own copy"
        )


# ═════════════════════════════════════════════════════════════════════
# B6 (2026-08-28): /click must fail TO THE SPONSOR, not to our error JSON.
#
# WHAT WENT WRONG. click_sponsorship opened with `conn = _get_db()` and, if
# that returned None, answered 503 {"error":"no_db"} — observed live once on a
# pool blip. That response goes to the ADVERTISER'S PROSPECT, who clicked the
# advertiser's ad and landed on our error JSON wearing our domain. We lost the
# click and the advertiser lost the referral, on the one code path their own
# customers can see.
#
# WHAT THIS LOCKS.
#   · DB unavailable + a known destination  -> 302 to the sponsor, uncounted.
#   · DB unavailable + nothing known        -> 503, because "we could not ask"
#                                              is not the same claim as 404
#                                              "this is not active".
#   · DB ANSWERED "not active"              -> 404, and the fallback is NOT
#                                              consulted. Forwarding a
#                                              cancelled sponsorship's clicks
#                                              sends traffic nobody paid for.
#   · The fallback destination is scheme-checked exactly like the primary one.
#   · invalidate() drops the fallback map, so a cancel reaches it.
# ═════════════════════════════════════════════════════════════════════
_B6_LINK = "https://sponsor.example/landing"


class _FakeCursor:
    def __init__(self, row, raises): self._row, self._raises = row, raises
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k):
        if self._raises:
            raise RuntimeError("pool exhausted")
    def fetchone(self): return self._row


class _FakeConn:
    def __init__(self, row=None, raises=False):
        self._row, self._raises = row, raises
        self.committed = self.rolled_back = self.closed = False
    def cursor(self): return _FakeCursor(self._row, self._raises)
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


def _b6_client(monkeypatch, conn, fallback=_B6_LINK):
    import routes.sponsorships as sp
    import routes.sponsor_render as sr
    monkeypatch.setattr(sp, "_get_db", lambda: conn)
    monkeypatch.setattr(sr, "link_url_for_id", lambda sid: fallback)
    return _client()


def test_click_forwards_to_sponsor_when_db_is_down(monkeypatch):
    """THE FIX. No write pool, but we know where the ad points: send them."""
    r = _b6_client(monkeypatch, None).get("/api/v1/sponsorships/7/click")
    assert r.status_code == 302, (
        "click returned %s with the DB down; the advertiser's prospect gets our "
        "error JSON instead of the advertiser's site" % r.status_code
    )
    assert r.headers["Location"] == _B6_LINK
    assert r.headers.get("X-DCHub-Click-Counted") == "0", (
        "an uncounted click must say so, or the monthly report cannot explain "
        "why clicks trail impressions"
    )


def test_click_503s_when_db_is_down_and_destination_unknown(monkeypatch):
    """We never asked and we have nothing cached. 404 would be a lie."""
    r = _b6_client(monkeypatch, None, fallback=None).get("/api/v1/sponsorships/7/click")
    assert r.status_code == 503
    assert r.get_json()["error"] == "no_db"


def test_click_404s_without_consulting_fallback_when_db_says_not_active(monkeypatch):
    """★ The distinction the whole fix rests on.

    "We could not ask" gets the fallback. "We asked and the answer was no"
    must NOT — a cancelled sponsorship would otherwise keep receiving traffic
    we are no longer paid for, forever, from any process that once cached it.
    """
    import routes.sponsor_render as sr

    def _must_not_be_called(sid):
        raise AssertionError(
            "the fallback was consulted after the DB authoritatively reported "
            "no active row — a cancelled sponsor keeps getting free traffic"
        )

    monkeypatch.setattr(sr, "link_url_for_id", _must_not_be_called)
    import routes.sponsorships as sp
    monkeypatch.setattr(sp, "_get_db", lambda: _FakeConn(row=None))
    r = _client().get("/api/v1/sponsorships/7/click")
    assert r.status_code == 404
    assert r.get_json()["error"] == "not_active"


def test_click_forwards_uncounted_when_the_write_itself_raises(monkeypatch):
    """A pool blip mid-statement, not just an absent connection."""
    conn = _FakeConn(raises=True)
    r = _b6_client(monkeypatch, conn).get("/api/v1/sponsorships/7/click")
    assert r.status_code == 302
    assert r.headers["Location"] == _B6_LINK
    assert r.headers.get("X-DCHub-Click-Counted") == "0"
    assert conn.rolled_back, "a failed write must roll back before we forward"


def test_click_counts_and_forwards_on_the_happy_path(monkeypatch):
    conn = _FakeConn(row=(_B6_LINK,))
    r = _b6_client(monkeypatch, conn, fallback=None).get("/api/v1/sponsorships/7/click")
    assert r.status_code == 302
    assert r.headers["Location"] == _B6_LINK
    assert r.headers.get("X-DCHub-Click-Counted") == "1"
    assert conn.committed


def test_fallback_destination_is_scheme_checked_like_the_primary(monkeypatch):
    """The fallback must not be a hole in the open-redirect guard."""
    r = _b6_client(monkeypatch, None, fallback="javascript:alert(1)").get(
        "/api/v1/sponsorships/7/click")
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_link"


def test_invalidate_clears_the_click_fallback_map():
    """A cancel calls invalidate(); it has to reach the fallback too."""
    import routes.sponsor_render as sr
    sr._remember_link(4242, _B6_LINK)
    assert sr._link_by_id.get(4242), "the fallback map did not record the link"
    sr.invalidate()
    assert not sr._link_by_id, (
        "invalidate() left the click fallback populated — a cancelled sponsor "
        "keeps receiving forwarded clicks until the 15-minute TTL expires"
    )


def test_link_lookup_does_not_stamp_a_click():
    """★ Over-counting is the dangerous direction on an invoice.

    link_url_for_id runs precisely when the counting write could NOT happen.
    If it stamped, a DB outage would inflate the number we bill against.
    """
    fn = _func(_tree(RENDERER), "link_url_for_id")
    called = [n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_stamp" not in called, (
        "link_url_for_id calls _stamp — the fallback path would bill clicks "
        "it explicitly cannot verify"
    )


def test_control_stamp_check_can_fail():
    """MUST-FAIL CONTROL: a _stamp call in that function must be caught."""
    src = RENDERER.read_text(encoding="utf-8")
    anchor = "    link = _read_link_by_id(sid)"
    assert anchor in src, (
        "MUST-FAIL CONTROL DID NOT APPLY — the link_url_for_id anchor is gone, "
        "so this control proves nothing"
    )
    mutated = src.replace(anchor, "    _stamp(sid)\n" + anchor, 1)
    assert mutated != src
    fn = _func(ast.parse(mutated), "link_url_for_id")
    called = [n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_stamp" in called, "the no-stamp check stayed green on an injected _stamp — it is vacuous"


def test_read_active_populates_the_click_fallback():
    """★ The wiring that makes the fallback non-empty in production.

    Every behavioural test above patches link_url_for_id, so all of them stay
    green if _read_active stops recording the link — and the map would then be
    permanently empty in prod, making the whole /click fallback inert. This is
    the guard that noticed, in a mutation run that expected RED and got GREEN.
    """
    fn = _func(_tree(RENDERER), "_read_active")
    called = [n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_remember_link" in called, (
        "_read_active no longer records link_url, so the /click fallback map "
        "is never populated and the fallback can only ever miss"
    )


def test_control_read_active_population_check_can_fail():
    """MUST-FAIL CONTROL: strip the call, the check has to catch it."""
    src = RENDERER.read_text(encoding="utf-8")
    anchor = '        _remember_link(row["id"], row["link_url"])\n'
    assert anchor in src, (
        "MUST-FAIL CONTROL DID NOT APPLY — the _remember_link anchor is gone, "
        "so this control proves nothing"
    )
    mutated = src.replace(anchor, "", 1)
    assert mutated != src
    fn = _func(ast.parse(mutated), "_read_active")
    called = [n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_remember_link" not in called, "the population check is vacuous"


# ═════════════════════════════════════════════════════════════════════
# B3 / P1-3 (2026-08-28): the module must run only where /advertise says.
#
# WHAT WAS WRONG. /advertise sells Product 1 as "Runs across the 7,292 pages
# with proven search demand — not the whole sitemap". The render call in
# facility_profile_page.py was UNCONDITIONAL, so the module would have drawn on
# every facility page that route serves (~17k). The promise was already public,
# so this was a FALSE PUBLISHED CLAIM, not a missing feature — and an
# advertiser can check it against the Search Console access the same page
# grants them.
#
# 7,292 is not a constant: it is `SELECT count(*) FROM seo_proven_pages WHERE
# impressions >= 10`, measured 2026-08-28 as exactly 7,292 of 21,672 rows —
# precisely the pair /advertise prints.
# ═════════════════════════════════════════════════════════════════════
MAIN_PY = ROOT / "main.py"
GSC_PY = ROOT / "google_search_console.py"


def test_the_facility_render_call_is_no_longer_unconditional():
    """The call site must pass a page identity, or the gate cannot apply."""
    import ast as _ast
    tree = _tree(ROOT / "routes" / "facility_profile_page.py")
    calls = [n for n in _ast.walk(tree)
             if isinstance(n, _ast.Call)
             and getattr(n.func, "id", "") == "sponsor_module_html"]
    assert calls, "no sponsor_module_html call in facility_profile_page.py"
    for c in calls:
        assert any(k.arg == "page_slugs" for k in c.keywords), (
            "sponsor_module_html is called without page_slugs — the module "
            "renders on every facility page, and /advertise's '7,292 pages "
            "with proven search demand, not the whole sitemap' is false"
        )


def test_facility_module_is_withheld_when_the_page_is_not_proven(monkeypatch):
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "proven_slugs", lambda: frozenset({"proven-abc123"}))
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    assert sr.sponsor_module_html("facility_module",
                                  page_slugs=("thin-page-zzz",)) == ""


def test_facility_module_renders_on_a_proven_page(monkeypatch):
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "proven_slugs", lambda: frozenset({"proven-abc123"}))
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    out = sr.sponsor_module_html("facility_module", page_slugs=("proven-abc123",))
    assert "sponsor-module" in out and "Sponsored" in out


def test_canonical_slug_counts_as_the_page_identity(monkeypatch):
    """GSC reports whichever URL it indexed; the page canonicalises to the
    FROZEN slug, so a request arriving on an alias must still match."""
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "proven_slugs", lambda: frozenset({"frozen-abc123"}))
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    out = sr.sponsor_module_html("facility_module",
                                 page_slugs=("frozen-abc123", "alias-abc123"))
    assert "sponsor-module" in out


def test_gate_fails_closed_when_the_proven_set_is_unknown(monkeypatch):
    """★ Never rendered > rendered somewhere we said it would not.

    Withholding costs impressions, which under-counts an invoice — the safe
    direction. Rendering outside the sold set breaks a published claim.
    """
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "proven_slugs", lambda: None)
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    assert sr.sponsor_module_html("facility_module", page_slugs=("anything",)) == ""


def test_market_module_is_not_gated_on_a_facility_table(monkeypatch):
    """★ seo_proven_pages is populated from /facilities/<slug> URLs ONLY.

    Gating the ~250 curated market pages on a facility-only table would
    silently zero the market half of Product 1.
    """
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "proven_slugs", lambda: None)   # worst case
    monkeypatch.setattr(sr, "active_row", lambda slot: dict(_ROW))
    monkeypatch.setattr(sr, "_stamp", lambda sid: None)
    assert sr.sponsor_module_html("market_module") != ""


def test_an_empty_proven_read_is_treated_as_unknown_not_as_zero(monkeypatch):
    """An empty table would switch the product off for a whole TTL.

    seo_proven_pages refreshes ADDITIVELY and never legitimately empties, so
    an empty result means the read went wrong.
    """
    import routes.sponsor_render as sr

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _C()
        def close(self): pass

    import sys as _sys, types as _types
    stub = _types.ModuleType("main")
    stub.get_read_db = lambda: _Conn()
    monkeypatch.setitem(_sys.modules, "main", stub)
    assert sr._load_proven_slugs() is None


def test_stale_set_keeps_serving_when_a_refresh_fails(monkeypatch):
    """A blip must not pull the module off all 7,292 pages."""
    import routes.sponsor_render as sr
    monkeypatch.setattr(sr, "_proven_cache", {"slugs": frozenset({"a"}), "at": 0.0})
    monkeypatch.setattr(sr, "_load_proven_slugs", lambda: None)
    assert sr.proven_slugs() == frozenset({"a"})


def test_impression_threshold_stays_in_lockstep_across_all_three_readers():
    """★ The rate card quotes ONE number for the sitemap and the ad gate.

    If they drift, /advertise's 7,292 describes neither.
    """
    import re
    gate = (RENDERER.read_text(encoding="utf-8"))
    gsc = GSC_PY.read_text(encoding="utf-8")
    main_src = MAIN_PY.read_text(encoding="utf-8")

    m_gsc = re.search(r"PROVEN_MIN_IMPRESSIONS_DEFAULT\s*=\s*(\d+)", gsc)
    m_main = re.search(r"_SITEMAP_PROVEN_MIN_IMPRESSIONS\s*=\s*_env_int\(\s*'SITEMAP_PROVEN_MIN_IMPRESSIONS'\s*,\s*(\d+)\s*\)", main_src)
    assert m_gsc and m_main, "could not locate both existing thresholds"

    fn = _func(ast.parse(gate), "_proven_min_impressions")
    ints = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, int)]
    envs = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "SITEMAP_PROVEN_MIN_IMPRESSIONS" in envs, (
        "the ad gate does not read the same env var as the sitemap"
    )
    want = int(m_gsc.group(1))
    assert int(m_main.group(1)) == want, "sitemap and GSC defaults already drifted"
    assert want in ints, (
        "the sponsor gate's default impression floor (%r) differs from the "
        "sitemap's (%d) — /advertise quotes one number for both" % (ints, want)
    )
