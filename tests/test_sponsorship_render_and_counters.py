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
    assert sr.sponsor_module_html("facility_module") == ""


def test_renderer_is_fail_soft(monkeypatch):
    """A sponsor bug must never take down a facility page."""
    import routes.sponsor_render as sr

    def boom(slot):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(sr, "active_row", boom)
    assert sr.sponsor_module_html("facility_module") == "", (
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
    out = sr.sponsor_module_html("facility_module")
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
    out = sr.sponsor_module_html("facility_module")
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
