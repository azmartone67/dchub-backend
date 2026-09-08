"""tests/test_route_auth_master_shell.py — Route-Auth Hardening shell (#41, 2026-07-31).

Guards routes/route_auth_master_shell.py. The shell MEASURES five route-auth-gap
classes over the source tree, files findings, and fires nothing. The ways it can
lie are the ways a static AUTH audit lies:

  (1) ORPHAN — the blueprint is written but never registered, so the endpoint
      404s forever while the module looks wired (the network_ix 4-month smell).
  (2) COUNT-DECOY — a lane that only counts "handler calls a sink / has an
      args.get" cannot tell the gated sibling from the open one. Each lane must
      reject the correctly-gated control while flagging the real offender. These
      tests drive the REAL detectors against the REAL repo files and a couple of
      synthetic snippets, asserting seed-flagged AND control-cleared.
  (3) SILENT-WRITE — the snapshot INSERT runs on the replica, raises "read-only
      transaction", is swallowed, and the trend table stays empty while every
      tick reports success.
  (4) CONFIDENT-GREEN — a lane whose load-bearing (critical) check could not be
      evaluated renders green anyway.
  (5) 5xx KILL — the kill switch returns 5xx, which the CF worker reads as a
      dead origin and fails the site over to the stale Render backend.
  (6) PERCENT-TRAP — a literal % in a no-params SQL string 500s.
  (7) NOT-READ-ONLY — the shell posts/sends/opens a PR or mutates a business
      table. Only route_auth_snapshots (write conn) and brain_findings (the
      canonical writer, status='open') may be written.

House rules: pre-merge pytest has NO DB and must NEVER import main — so the
wiring guards read main.py + the shell as TEXT and slice functions via ast, and
the detector-behaviour tests parse the shell + a handful of source files
directly (no DB, no Flask app). Nothing runs at module scope.

Run:  python3 -m pytest tests/test_route_auth_master_shell.py -v
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "main.py"
_SHELL = _ROOT / "routes" / "route_auth_master_shell.py"

sys.path.insert(0, str(_ROOT))


def _shell_src() -> str:
    return _SHELL.read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    """Slice `def name(` out of the shell by ast (parsed, not regex-guessed)."""
    tree = ast.parse(_shell_src())
    src = _shell_src().splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "\n".join(src[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name}() not found in {_SHELL.name}")


def _rec_from_src(src: str, rel: str = "synthetic.py") -> dict:
    """Build a scan record the way _scan_routes does, so a detector can be
    driven against an arbitrary snippet or file with no DB and no full scan."""
    from routes.route_auth_master_shell import _route_handlers
    tree = ast.parse(src)
    return {"rel": rel, "base": rel.split("/")[-1], "src": src,
            "src_lines": src.splitlines(), "tree": tree,
            "handlers": _route_handlers(tree)}


def _rec_from_file(relpath: str) -> dict:
    return _rec_from_src((_ROOT / relpath).read_text(encoding="utf-8"), relpath)


# ── (1) orphan check ──────────────────────────────────────────────────

def test_blueprint_is_registered_in_main():
    """The exact guard that would have caught network_ix_ingestion.py."""
    src = _MAIN.read_text(encoding="utf-8")
    assert "from routes.route_auth_master_shell import route_auth_master_shell_bp" in src
    assert "app.register_blueprint(route_auth_master_shell_bp)" in src


def test_shell_declares_its_routes():
    src = _shell_src()
    for route in ("/api/v1/admin/route-auth/master-tick",
                  "/admin/route-auth-shell",
                  "/api/v1/admin/route-auth-shell"):
        assert route in src, f"route {route} missing"


# ── (2) per-lane decoy / control — the detector must SEPARATE gated from open ──

def test_l1_flags_hardcoded_literal_but_not_env_only_control():
    """L1a: a baked-in credential literal inside an auth fn is a leak; the same
    fn reading os.environ is not. A count of 'auth fn has a string literal'
    cannot tell them apart — the value shape and the env-lookup exclusion must."""
    from routes.route_auth_master_shell import _detect_l1_hardcoded
    offender = _rec_from_src(
        "import hmac, os\n"
        "def _check_admin(req):\n"
        "    presented = req.headers.get('X-Admin-Key','')\n"
        "    candidates = ['dchub-admin-secret-2026']\n"
        "    for c in candidates:\n"
        "        if hmac.compare_digest(presented, c):\n"
        "            return True\n"
        "    return False\n", "offender.py")
    control = _rec_from_src(
        "import hmac, os\n"
        "def _admin_ok(req):\n"
        "    presented = req.headers.get('X-Admin-Key','')\n"
        "    expected = os.environ.get('DCHUB_ADMIN_KEY','')\n"
        "    return bool(expected) and hmac.compare_digest(presented, expected)\n",
        "control.py")
    files = {o["file"] for o in _detect_l1_hardcoded([offender, control])}
    assert "offender.py" in files, "hardcoded credential literal not flagged"
    assert "control.py" not in files, "env-only auth fn wrongly flagged"


def test_l1_does_not_flag_the_internal_auth_control():
    """The canonical control: internal_auth.is_valid_internal_key reads only
    os.environ and must never be flagged as a hardcoded-credential leak."""
    from routes.route_auth_master_shell import _detect_l1_hardcoded
    off = _detect_l1_hardcoded([_rec_from_file("internal_auth.py")])
    assert off == [], f"is_valid_internal_key wrongly flagged: {off}"


def test_l2_flags_ungated_trigger_but_not_the_decorator_gated_control():
    """L2: iso_ercot /extract and ai_outreach /api/outreach/run are ungated
    state-changing triggers; network_ix's sync routes call the same kind of
    ingest but carry a LOCALLY-DEFINED @require_internal_key. The detector must
    resolve the whole decorator stack — a grep for is_valid_internal_key() would
    false-flag it.

    (The former seeds — gas /feeds/ingest and sec_edgar /extract — were closed by
    fix(security): gate unauthenticated cron job-trigger endpoints, which added
    inline `is_valid_internal_key` gates. That the detector now clears BOTH is
    itself asserted by test_l2_clears_the_now_gated_cron_triggers below; here we
    use two triggers that remain intentionally ungated — DEFER'd per the
    silent-feed-death safety asymmetry — so the ungated lane still has seeds.)"""
    from routes.route_auth_master_shell import _detect_l2
    off = _detect_l2([_rec_from_file("routes/iso_ercot.py"),
                      _rec_from_file("ai_outreach_agent.py"),
                      _rec_from_file("network_ix_ingestion.py")])
    files = {o["file"] for o in off}
    assert any("iso_ercot" in f for f in files), "ungated /extract not flagged"
    assert any("ai_outreach_agent" in f for f in files), \
        "ungated /api/outreach/run not flagged"
    assert not any("network_ix" in f for f in files), \
        "network_ix @require_internal_key control wrongly flagged"


def test_l2_clears_the_now_gated_cron_triggers():
    """Regression guard for fix(security): gate unauthenticated cron job-trigger
    endpoints. gas /feeds/ingest and sec_edgar /extract now carry an inline
    `is_valid_internal_key(X-Internal-Key|X-Admin-Key)` gate, so the L2 detector
    must NO LONGER flag them — proving the gate resolves as a real gate, not just
    a decorator."""
    from routes.route_auth_master_shell import _detect_l2
    off = _detect_l2([_rec_from_file("routes/gas_price_feeds.py"),
                      _rec_from_file("routes/sec_edgar.py")])
    files = {o["file"] for o in off}
    assert not any("gas_price_feeds" in f for f in files), \
        "gated /feeds/ingest wrongly flagged as ungated"
    assert not any("sec_edgar" in f for f in files), \
        "gated /extract wrongly flagged as ungated"


def test_l2_resolves_a_locally_named_gate_decorator():
    """Directly: a handler with @require_internal_key is gated even though the
    decorator is not the canonical helper and is defined in its own file."""
    from routes.route_auth_master_shell import _route_handlers, _handler_is_gated
    rec = _rec_from_file("network_ix_ingestion.py")
    sync = [fn for fn in rec["handlers"] if fn.name == "network_sync_job"]
    assert sync, "network_sync_job handler not found"
    assert _handler_is_gated(sync[0]), \
        "@require_internal_key decorator not recognised as a gate"


def test_l3_flags_open_sink_but_not_the_gated_sibling_on_the_same_sink():
    """L3: a still-ungated outbound sink is flagged; its gated sibling is not.
    linkedin_poster guards post_to_linkedin with `if not _check_admin(request):
    401`. A 'file calls the sink' count cannot separate gated from ungated —
    gate-presence must.

    (Seed history: linkedin_autopost, then infrastructure_discovery, were each
    gated by the route-auth-criticals waves with `require_internal_or_admin ->
    401`, and the detector correctly stopped flagging them. cross_post_email
    (unauth /linkedin-quad/email-best Resend send) is the current still-ungated
    real offender that keeps this discrimination test honest.)"""
    from routes.route_auth_master_shell import _detect_l3
    off = _detect_l3([_rec_from_file("routes/cross_post_email.py"),
                      _rec_from_file("linkedin_poster.py")])
    files = {o["file"] for o in off}
    assert any("cross_post_email" in f for f in files), \
        "ungated outbound sink (cross_post_email) not flagged"
    assert not any("linkedin_poster" in f for f in files), \
        "gated outbound sink (linkedin_poster) wrongly flagged"


def test_l3_clears_the_now_gated_linkedin_autopost_and_intelligence_seeds():
    """Regression for the route-auth-criticals fix: linkedin_autopost
    (linkedin_post_now), intelligence_engine (api_linkedin_post) and both
    social_test twins now carry a fail-closed `require_internal_or_admin -> 401`
    gate dominating the outbound sink, so L3 must NOT flag any of them."""
    from routes.route_auth_master_shell import _detect_l3
    off = _detect_l3([_rec_from_file("linkedin_autopost.py"),
                      _rec_from_file("intelligence_engine.py"),
                      _rec_from_file("auto_pilot.py"),
                      _rec_from_file("routes/autopilot_routes.py")])
    files = {o["file"] for o in off}
    for cleared in ("linkedin_autopost", "intelligence_engine",
                    "auto_pilot", "autopilot_routes"):
        assert not any(cleared in f for f in files), \
            f"{cleared} still flagged — its outbound sink is not gate-dominated"


def test_l4_failopen_flags_allow_all_gate_but_not_the_fail_closed_control():
    """L4.1: `if not ADMIN_KEY: return True` allows all when the env var is
    unset; is_valid_internal_key returns False (fail-closed) and must not
    match."""
    from routes.route_auth_master_shell import _detect_l4_failopen
    off = _detect_l4_failopen([_rec_from_file("routes/iso_lmp_ingest.py"),
                               _rec_from_file("dchub_daily_automation.py"),
                               _rec_from_file("internal_auth.py")])
    files = {o["file"] for o in off}
    assert any("iso_lmp_ingest" in f for f in files), "fail-open _admin_gate not flagged"
    assert any("dchub_daily_automation" in f for f in files), \
        "fail-open _check_admin_auth not flagged"
    assert not any("internal_auth" in f for f in files), \
        "fail-CLOSED is_valid_internal_key wrongly flagged"


def test_l4_failopen_ignores_a_non_auth_skip_on_a_feature_flag():
    """A background helper that bails when its API key / DB url is unset
    (`if not _ANTHROPIC_KEY: return None`) is NOT an auth gate and must not be
    flagged — the crux false positive that separates a real detector from a
    grep for `if not <KEY>`."""
    from routes.route_auth_master_shell import _detect_l4_failopen
    rec = _rec_from_src(
        "import os\n"
        "_ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')\n"
        "def _call_claude(prompt):\n"
        "    if not _ANTHROPIC_KEY:\n"
        "        return None\n"
        "    return _http_post(prompt)\n", "helper.py")
    assert _detect_l4_failopen([rec]) == [], \
        "a non-auth skip-when-unconfigured helper was flagged as fail-open auth"


def test_l4_bypass_flags_method_scoped_gate_but_not_an_unconditional_one():
    """L4.2: `if request.method == 'POST' and provided != key: 401` lets a GET
    through; an unconditional `if provided != key: 401` is correct."""
    from routes.route_auth_master_shell import _detect_l4_bypass
    offender = _rec_from_src(
        "def h():\n"
        "    provided = request.headers.get('X-Admin-Key')\n"
        "    if request.method == 'POST' and _ADMIN_KEY and provided != _ADMIN_KEY:\n"
        "        return 'no', 401\n"
        "    return _do_side_effect()\n", "offender.py")
    control = _rec_from_src(
        "def h():\n"
        "    provided = request.headers.get('X-Admin-Key')\n"
        "    if provided != _ADMIN_KEY:\n"
        "        return 'no', 401\n"
        "    return _do_side_effect()\n", "control.py")
    files = {o["file"] for o in _detect_l4_bypass([offender, control])}
    assert "offender.py" in files, "method-scoped GET-bypass not flagged"
    assert "control.py" not in files, "unconditional 401 gate wrongly flagged"


def test_l4_bypass_flags_the_live_brain_seeds():
    from routes.route_auth_master_shell import _detect_l4_bypass
    off = _detect_l4_bypass([_rec_from_file("routes/brain_narrative.py")])
    assert any("brain_narrative" in o["file"] for o in off), \
        "live method-scoped bypass in brain_narrative not flagged"


def test_l5_flags_returned_traceback_but_not_logged_str_e():
    """L5: traceback.format_exc()/repr(e) RETURNED to the client is a leak; a
    str(e) written into an internal dict and logged server-side is NOT (the
    100+ benign str(e)[:200] false-positive trap)."""
    from routes.route_auth_master_shell import _detect_l5_leak
    offender = _rec_from_src(
        "import traceback\n"
        "def h():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as e:\n"
        "        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500\n",
        "offender.py")
    control = _rec_from_src(
        "def health():\n"
        "    out = {}\n"
        "    try:\n"
        "        out['db'] = check()\n"
        "    except Exception as e:\n"
        "        out['db'] = {'status': 'red', 'error': str(e)[:200]}\n"
        "    return out\n", "control.py")
    files = {o["file"] for o in _detect_l5_leak([offender, control])}
    assert "offender.py" in files, "returned traceback.format_exc() not flagged"
    assert "control.py" not in files, \
        "benign str(e) into an internal health dict wrongly flagged"


def test_l5_flags_the_live_observability_routes_seed():
    from routes.route_auth_master_shell import _detect_l5_leak
    off = _detect_l5_leak([_rec_from_file("routes/observability_routes.py")])
    assert any("observability" in o["file"] for o in off), \
        "live traceback.format_exc() returns in observability_routes not flagged"


# ── (2d) LANE 6 · webhook-signature — fail-open verifiers (a) + unverified
#         receivers (b), rejecting the fail-closed control and ordinary POSTs ──

def test_l6_flags_the_slack_signature_failopen_seed():
    """MUST-CATCH (bucket a): slack_app._verify_slack_signature reads
    SLACK_SIGNING_SECRET and `if not secret: return True` — it dev-mode-ALLOWS
    every request when the signing secret is unset, so the whole Slack surface
    (slack_command/events/interact all early-return on it) is unauthenticated.
    A count of 'reads a signing secret' cannot tell this from a fail-closed
    verifier; the missing-secret branch returning True is the tell."""
    from routes.route_auth_master_shell import _detect_l6_webhook_sig
    off = _detect_l6_webhook_sig([_rec_from_file("routes/slack_app.py")])
    assert any(o["handler"] == "_verify_slack_signature" for o in off), \
        "slack_app fail-open signature verifier not flagged"


# The bucket-(b) seed was routes/email_engagement.py until 2026-08-28, when that
# blueprint was deleted — its route had been SHADOWED by resend_webhook since
# 2026-07-17 and could never receive an event. A MUST-CATCH seed that points at
# a real file dies whenever that file is fixed or removed, which is what
# happened here; the shape is now inline, so the detector stays fenced whether
# or not any such handler currently exists in the tree.
_UNVERIFIED_RECEIVER_SEED = """
import psycopg2
from flask import Blueprint, request, jsonify
bp = Blueprint("email_engagement", __name__)

@bp.route("/api/v1/webhooks/resend", methods=["POST"])
def resend_webhook():
    d = request.get_json(silent=True) or {}
    event_type = (d.get("type") or "").replace("email.", "").strip()
    data = d.get("data") or {}
    if not event_type:
        return jsonify(error="missing_type"), 400
    c = _conn()
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO email_engagement (resend_email_id, event_type) "
            "VALUES (%s, %s) RETURNING id",
            (data.get("email_id"), event_type[:30]))
        r = cur.fetchone()
    return jsonify(ok=True, recorded_id=int(r[0]) if r else None), 200
"""


def test_l6_flags_the_unverified_resend_receiver_seed():
    """MUST-CATCH (bucket b): a receiver POSTed by Resend at
    /api/v1/webhooks/resend that INSERTs straight off the JSON body — no svix
    header read, no RESEND_WEBHOOK_SECRET, no verify call. Any unauthenticated
    caller can forge engagement rows; the count is a receiver that writes on the
    payload with zero signature verification."""
    from routes.route_auth_master_shell import _detect_l6_webhook_sig
    off = _detect_l6_webhook_sig(
        [_rec_from_src(_UNVERIFIED_RECEIVER_SEED, "routes/email_engagement.py")])
    assert any(o["handler"] == "resend_webhook" for o in off), \
        "unverified resend webhook receiver not flagged"


def test_l6_does_not_flag_the_fail_closed_control_or_ordinary_post():
    """MUST-NOT-CATCH: (1) paywall_middleware.stripe_tier_webhook reads
    STRIPE_WEBHOOK_SECRET and `if not secret: return 500` then construct_events —
    fail-CLOSED on the missing secret, the (a)-vs-(c) discriminator; (2) a
    planted handler that rejects on the missing secret and
    stripe.Webhook.construct_event-verifies; (3) an ordinary non-webhook POST
    that merely writes to the DB. None is a webhook-signature gap — a 'webhook
    route' or 'reads a secret' count would wrongly flag (1) and (3)."""
    from routes.route_auth_master_shell import _detect_l6_webhook_sig
    verified = _rec_from_src(
        "import os, stripe\n"
        "@bp.route('/webhooks/stripe', methods=['POST'])\n"
        "def stripe_hook():\n"
        "    secret = os.environ.get('STRIPE_WEBHOOK_SECRET')\n"
        "    if not secret:\n"
        "        return jsonify(error='not configured'), 500\n"
        "    sig = request.headers.get('Stripe-Signature')\n"
        "    event = stripe.Webhook.construct_event(request.data, sig, secret)\n"
        "    cur.execute('INSERT INTO stripe_events (id) VALUES (1) ON CONFLICT DO NOTHING')\n"
        "    return jsonify(ok=True)\n", "verified.py")
    ordinary = _rec_from_src(
        "@bp.route('/api/save-note', methods=['POST'])\n"
        "def save_note():\n"
        "    d = request.get_json() or {}\n"
        "    cur.execute('INSERT INTO notes (body) VALUES (%s) ON CONFLICT DO NOTHING', (d.get('body'),))\n"
        "    return jsonify(ok=True)\n", "ordinary.py")
    off = _detect_l6_webhook_sig(
        [_rec_from_file("paywall_middleware.py"), verified, ordinary])
    handlers = {o["handler"] for o in off}
    files = {o["file"] for o in off}
    assert "stripe_tier_webhook" not in handlers, \
        "fail-CLOSED paywall stripe webhook wrongly flagged"
    assert "verified.py" not in files, \
        "a construct_event-verified, fail-closed webhook wrongly flagged"
    assert "ordinary.py" not in files, \
        "an ordinary non-webhook POST wrongly flagged as a webhook gap"


# ── (2c) 2026-07-31 coverage tightening — newly-caught seeds, excluded
#         signature-verifiers, and the KEPT real fail-opens (no over-suppress) ──

def test_l2_flags_the_proactive_discovery_run_handler_seed():
    """MUST-CATCH: proactive_discovery's `discover_fiber` is an ungated POST that
    calls engine.run_fiber_discovery() with no gate and no before_request hook.
    It slipped because the boundary-tight `\\bdiscover\\b` matched neither the
    path noun "discovery" nor the fn name "discover_fiber"; the leading-anchored
    fn-name verb prefix now catches the action handler."""
    from routes.route_auth_master_shell import _detect_l2
    off = _detect_l2([_rec_from_file("proactive_discovery.py")])
    handlers = {o["handler"] for o in off}
    assert "discover_fiber" in handlers, \
        "ungated POST run handler discover_fiber not flagged"


def test_l2_verb_prefix_does_not_flag_a_noun_read_handler():
    """MUST-NOT-CATCH control: a GET read whose name LEADS with 'get' (the noun
    'discovery' only appears mid-name/path) must stay clear — proving the
    fn-name-prefix fix did not resurrect the read-only false positives that a
    blanket trailing-`\\w*` on the verb regex would (get_discovery_stats calls
    DiscoveryEngine(), which `_is_state_changing` would otherwise accept)."""
    from routes.route_auth_master_shell import _detect_l2
    rec = _rec_from_src(
        "@bp.route('/api/discovery/stats', methods=['GET'])\n"
        "def get_discovery_stats():\n"
        "    engine = DiscoveryEngine()\n"
        "    return jsonify(engine.get_stats())\n", "reader.py")
    assert "reader.py" not in {o["file"] for o in _detect_l2([rec])}, \
        "a noun-read GET handler was wrongly flagged as an ungated trigger"


def test_l4_failopen_flags_the_env_truthiness_ai_citation_seeds():
    """MUST-CATCH: ai_citation_tracker run_cron (:1199) and record_observation
    (:511) gate with `if admin_key_env and provided != admin_key_env: 401`,
    read from the non-canonical `ADMIN_KEY` env. Unset ADMIN_KEY => '' is falsy
    => the `and` collapses => 401 skipped => the state-changing cron runs
    unauthenticated. The file carries no compare_digest/_AUTH_FN token, so only
    the new env-truthiness shape reaches it."""
    from routes.route_auth_master_shell import _detect_l4_env_failopen
    handlers = {o["handler"] for o in
                _detect_l4_env_failopen([_rec_from_file("routes/ai_citation_tracker.py")])}
    assert "run_cron" in handlers, "ai_citation run_cron env-truthiness fail-open not flagged"
    assert "record_observation" in handlers, \
        "ai_citation record_observation env-truthiness fail-open not flagged"


def test_l4_failopen_flags_the_split_list_alert_system_v2_seed():
    """MUST-CATCH: alert_system_v2.trigger_alert_check (:1020) gates with
    `if valid_keys and valid_keys[0] and api_key not in valid_keys: 401` where
    valid_keys = os.environ.get('DCHUB_API_KEYS','').split(','). Unset =>
    ''.split(',')==[''] => valid_keys[0]=='' is falsy => guard collapses => the
    state-changing process_all_alerts() runs unauthenticated."""
    from routes.route_auth_master_shell import _detect_l4_env_failopen
    off = _detect_l4_env_failopen([_rec_from_file("alert_system_v2.py")])
    assert any(o["handler"] == "trigger_alert_check" for o in off), \
        "alert_system_v2 split-list env-truthiness fail-open not flagged"


def test_l4_failopen_env_truthiness_ignores_the_fail_closed_or_form():
    """MUST-NOT-CATCH control: the SAME idiom written fail-CLOSED —
    `if not key or provided != key: 401` — is a BoolOp-OR, so an unset key makes
    `not key` True and the 401 fires. The new AND-shape must not match it; this
    is what separates a real fail-open detector from a grep for `key and !=`."""
    from routes.route_auth_master_shell import _detect_l4_env_failopen
    rec = _rec_from_src(
        "import os\n"
        "def h():\n"
        "    expected = os.environ.get('DCHUB_ADMIN_KEY', '')\n"
        "    provided = request.headers.get('X-Admin-Key')\n"
        "    if not expected or provided != expected:\n"
        "        return jsonify(error='unauthorized'), 401\n"
        "    return _do()\n", "closed.py")
    assert _detect_l4_env_failopen([rec]) == [], \
        "fail-CLOSED `if not key or provided != key: 401` wrongly flagged as fail-open"


def test_l4_failopen_excludes_the_hmac_signature_verifier():
    """MUST-NOT-CATCH control: slack_app._verify_slack_signature HMACs the
    request body/timestamp (a webhook-signature check) and dev-mode-allows on an
    unset signing secret. That IS a fail-open, but of a DIFFERENT class than an
    admin/internal-key gate; it is excluded from L4 by name+HMAC-body so it is
    not mis-filed here (it belongs in a webhook-signature lane)."""
    from routes.route_auth_master_shell import _detect_l4_failopen
    off = _detect_l4_failopen([_rec_from_file("routes/slack_app.py")])
    assert not any(o["handler"] == "_verify_slack_signature" for o in off), \
        "HMAC webhook signature verifier wrongly flagged as an L4 admin-key fail-open"


def test_l4_failopen_keeps_the_true_admin_key_failopens():
    """MUST-CATCH control (no over-suppression): the two genuine DCHUB_ADMIN_KEY
    fail-opens — wins_poster._admin_ok and linkedin_poster._check_admin, each
    `if not <ADMIN_KEY>: return True` — MUST stay flagged. The signature-verifier
    exclusion is narrow enough (name AND HMAC body) that it does not sweep these
    out."""
    from routes.route_auth_master_shell import _detect_l4_failopen
    wins = {o["handler"] for o in _detect_l4_failopen([_rec_from_file("routes/wins_poster.py")])}
    assert "_admin_ok" in wins, \
        "wins_poster._admin_ok real fail-open wrongly suppressed"
    li = {o["handler"] for o in _detect_l4_failopen([_rec_from_file("linkedin_poster.py")])}
    assert "_check_admin" in li, \
        "linkedin_poster._check_admin real fail-open wrongly suppressed"


def test_l4_bypass_flags_the_brain_layer7_wrapper_seed():
    """MUST-CATCH: brain_layer7.propose_detector routes POST AND GET, but its
    only auth check (`!= _ADMIN_KEY -> 401`) is nested inside
    `if request.method == 'POST':`, so a GET skips the whole block and reaches
    the state-changing body (Claude call + INSERT). The co-located one-if rule
    could not see the outer-method-if / inner-key-if nesting."""
    from routes.route_auth_master_shell import _detect_l4_bypass
    off = _detect_l4_bypass([_rec_from_file("routes/brain_layer7_evolving.py")])
    assert any("brain_layer7" in o["file"] for o in off), \
        "method-scoped WRAPPER GET-bypass in brain_layer7 not flagged"


def test_l4_bypass_wrapper_flags_get_allowing_but_not_post_only_route():
    """The WRAPPER shape must depend on the route allowing a non-POST verb: a
    GET/POST route with the method-wrapped auth is a real bypass; a POST-only
    route with the identical wrapper is NOT (there is no GET to fall through)."""
    from routes.route_auth_master_shell import _detect_l4_bypass
    get_allowed = _rec_from_src(
        "@bp.route('/x', methods=['POST', 'GET'])\n"
        "def h():\n"
        "    if request.method == 'POST':\n"
        "        if _ADMIN_KEY and provided != _ADMIN_KEY:\n"
        "            return jsonify(error='no'), 401\n"
        "    return _do_side_effect()\n", "getallowed.py")
    post_only = _rec_from_src(
        "@bp.route('/x', methods=['POST'])\n"
        "def h():\n"
        "    if request.method == 'POST':\n"
        "        if _ADMIN_KEY and provided != _ADMIN_KEY:\n"
        "            return jsonify(error='no'), 401\n"
        "    return _do_side_effect()\n", "postonly.py")
    files = {o["file"] for o in _detect_l4_bypass([get_allowed, post_only])}
    assert "getallowed.py" in files, "GET-allowing method-wrapper bypass not flagged"
    assert "postonly.py" not in files, \
        "POST-only route (no GET to bypass) wrongly flagged as a GET-bypass"


# ── (2b) load-bearing checks must be critical at EVERY call site ──────

def _check_calls(check_id: str) -> list:
    found = []
    for node in ast.walk(ast.parse(_shell_src())):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_check"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == check_id):
            found.append(node)
    return found


def _is_critical(call) -> bool:
    return any(kw.arg == "critical"
               and isinstance(kw.value, ast.Constant)
               and kw.value.value is True
               for kw in call.keywords)


@pytest.mark.parametrize("check_id", [
    "l1_hardcoded",   # lane 1 — a credential compared, not read from env
    "l2_unauth",      # lane 2 — an ungated state-changing trigger
    "l3_outbound",    # lane 3 — an ungated outbound sink
    "l4_failopen",    # lane 4 — a fail-open gate
    "l4_bypass",      # lane 4 — a method-scoped GET bypass
    "l5_leak",        # lane 5 — a client-visible traceback
    "l6_signature",   # lane 6 — a fail-open / absent webhook-signature check
    "l7_verdict",     # lane 7 — the verdict itself
])
def test_load_bearing_checks_are_critical_at_every_call_site(check_id):
    """An undetermined load-bearing check must force its lane to '?', never
    green — at EVERY branch that can emit it (the scan-failed branch AND the
    real branch)."""
    calls = _check_calls(check_id)
    assert calls, f"{check_id} no longer exists"
    non_critical = [c.lineno for c in calls if not _is_critical(c)]
    assert not non_critical, (
        f"{check_id} is emitted without critical=True at line(s) {non_critical} "
        "— that branch can render the lane green while proving nothing")


# ── (4) confident-green + lane/actuator structure ────────────────────

def test_lane_verdict_never_greens_an_undetermined_critical_check():
    from routes.route_auth_master_shell import _check, _lane_verdict
    ok = _check("a", "fine", True, "")
    undetermined_critical = _check("b", "load-bearing", None, "", critical=True)
    undetermined_gauge = _check("c", "gauge", None, "")

    assert _lane_verdict([ok]) is True
    assert _lane_verdict([ok, undetermined_gauge]) is True
    assert _lane_verdict([ok, undetermined_critical]) is None, \
        "a lane with an unevaluated critical check must render '?', not green"
    assert _lane_verdict([ok, _check("d", "broken", False, "")]) is False
    assert _lane_verdict([undetermined_gauge]) is None


def test_every_lane_has_a_critical_check():
    from routes.route_auth_master_shell import _LANES
    assert len(_LANES) == 7, f"expected 7 lanes, found {len(_LANES)}"
    for key, _label, fn, _actuator in _LANES:
        assert "critical=True" in _func_src(fn.__name__), \
            f"lane {key} ({fn.__name__}) has no critical check"


def test_every_lane_names_an_actuator():
    from routes.route_auth_master_shell import _LANES
    assert len(_LANES) == 7
    for key, label, fn, actuator in _LANES:
        assert callable(fn)
        assert actuator and len(actuator) > 20, f"{key} has no named actuator"


# ── (5) kill switch must not 5xx + admin gate ─────────────────────────

def test_kill_switch_returns_404_not_5xx():
    """Walks the AST for every `if _disabled():` guard and asserts each returns
    404 — a 5xx trips the CF worker's failover to the stale Render origin."""
    tree = ast.parse(_shell_src())
    guards = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If)
                and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            continue
        guards += 1
        codes = [c.value for c in ast.walk(node)
                 if isinstance(c, ast.Constant) and isinstance(c.value, int)
                 and 100 <= c.value < 600]
        assert codes, f"_disabled() guard at line {node.lineno} returns no status code"
        for code in codes:
            assert code == 404, (
                f"_disabled() guard at line {node.lineno} returns {code} — a kill "
                "switch must never emit 5xx (CF reads it as a dead origin)")
    assert guards >= 2, f"expected a kill guard on each endpoint, found {guards}"


def test_shell_is_admin_gated():
    src = _shell_src()
    assert src.count("_admin_ok()") >= 2, "an endpoint is missing the admin gate"
    assert "DCHUB_ADMIN_KEY" in src


def test_kill_switch_env_var_is_documented():
    assert "ROUTE_AUTH_SHELL_DISABLE" in _shell_src()
    assert "ROUTE_AUTH_SHELL_DISABLE" in _MAIN.read_text(encoding="utf-8")


# ── (3) snapshot must not be written on the replica ───────────────────

def test_snapshot_uses_a_separate_write_connection():
    src = _shell_src()
    assert "def _write_conn" in src, "_write_conn() is gone"
    write_src = _func_src("_write_conn")
    assert "NEON_REPLICA_URL" not in write_src, \
        "_write_conn must NEVER resolve to the read replica"
    tick = _func_src("_run_tick")
    assert "_write_conn()" in tick, "snapshot no longer uses the write connection"
    assert "route_auth_snapshots" in tick


def test_read_conn_prefers_replica():
    assert "NEON_REPLICA_URL" in _func_src("_conn")


def test_tick_reports_whether_it_persisted():
    assert '"persisted"' in _func_src("_run_tick")


# ── (6) percent trap + read-only discipline ──────────────────────────

def test_no_literal_percent_in_no_params_sql():
    """_row()/_scalar() execute literal SQL with no params tuple, so a literal %
    would %-substitute against an empty tuple and 500. Only the deliberately
    parameterised snapshot INSERT may carry a %."""
    sql_kw = re.compile(r"\b(SELECT|INSERT INTO|UPDATE|DELETE FROM|CREATE TABLE)\b", re.I)
    offenders = []
    for node in ast.walk(ast.parse(_shell_src())):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        s = node.value
        if not sql_kw.search(s) or "%" not in s:
            continue
        if "INSERT INTO route_auth_snapshots" in s:
            continue
        offenders.append(s[:120])
    assert not offenders, (
        "literal % inside a SQL string — psycopg2 will %-substitute against an "
        f"empty tuple and 500: {offenders}")


def test_sql_pattern_matches_use_regex_not_like():
    """Every SQL pattern match is a POSIX regex (~), so no SQL string carries a
    literal % wildcard. Scoped to SQL SELECT/INSERT/CREATE literals."""
    sql_kw = re.compile(r"\b(SELECT|INSERT INTO|CREATE TABLE)\b")
    for node in ast.walk(ast.parse(_shell_src())):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        s = node.value
        if sql_kw.search(s) and re.search(r"\bLIKE\s+'", s, re.I):
            raise AssertionError(f"use a POSIX regex (~) instead of LIKE: {s[:120]}")


def test_shell_only_mutates_its_own_snapshot_table():
    """READ-ONLY: the only mutating SQL literal in this module targets
    route_auth_snapshots. brain_findings is written via the canonical
    upsert_brain_finding writer, whose SQL lives in that module, not here."""
    mut = re.compile(r"\b(INSERT INTO|UPDATE|DELETE FROM|TRUNCATE|CREATE TABLE)\s+"
                     r"(?:IF NOT EXISTS\s+)?(\w+)", re.I)
    for node in ast.walk(ast.parse(_shell_src())):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for verb, table in mut.findall(node.value):
            assert table == "route_auth_snapshots", \
                f"shell mutates `{table}` via `{verb}` — it must be read-only"


def test_shell_makes_no_outbound_or_business_write_call():
    """No requests.post/get, no email/social send, no subprocess/gh, no
    hand-rolled brain_findings INSERT. The outbound sink names appear ONLY as
    detector string literals, never as calls."""
    tree = ast.parse(_shell_src())
    banned_attr = {"post", "put", "patch", "delete", "request",  # requests.*
                   "post_to_linkedin", "post_to_twitter", "create_text_post",
                   "create_article_post", "send_email", "_send_email",
                   "sendmail", "system"}
    banned_name = {"post_to_linkedin", "post_to_twitter", "tweet",
                   "_p99_send_email", "_send_email"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in banned_attr:
                raise AssertionError(f"outbound/mutating call .{f.attr}() at line {node.lineno}")
            if isinstance(f, ast.Name) and f.id in banned_name:
                raise AssertionError(f"outbound call {f.id}() at line {node.lineno}")
    src = _shell_src()
    for bad in ("import requests", "import smtplib", "import subprocess",
                "INSERT INTO brain_findings"):
        assert bad not in src, f"shell must not contain `{bad}`"


def test_findings_are_filed_open_via_the_canonical_writer():
    """Offenders are filed with the correct columns (issue/url/detail/detector)
    through upsert_brain_finding, status='open' so a HUMAN triages — never
    resolved/wont_fix/dismissed, and never a hand-rolled INSERT."""
    src = _shell_src()
    assert "from routes.brain_findings_writer import upsert_brain_finding" in src
    ff = _func_src("_file_findings")
    assert "upsert_brain_finding(" in ff
    for col in ("issue=", "url=", "detail=", "detector="):
        assert col in ff, f"findings write is missing the {col} column"
    assert 'status="open"' in ff, "findings must be filed status='open' for triage"
    for forbidden in ('status="resolved"', 'status="wont_fix"', 'status="dismissed"'):
        assert forbidden not in src, f"the shell must never file {forbidden}"


# ── end-to-end: the tick runs on the real source with NO DB ───────────

def test_tick_runs_over_real_source_and_returns_seven_lanes():
    """The detectors are source-based, so a full tick runs WITHOUT a DB — it
    just skips the snapshot/findings writes (persisted False). Proves the lanes
    assemble, the verdict is one of the allowed values, and the shape matches
    the master_tick contract."""
    import routes.route_auth_master_shell as m
    m._SCAN = None
    m._cache = {"ts": 0.0, "payload": None}
    saved = {k: os.environ.pop(k, None)
             for k in ("NEON_REPLICA_URL", "DATABASE_URL", "NEON_DATABASE_URL")}
    try:
        p = m._run_tick()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert p["ok"] is True
    assert p["lanes_total"] == 7
    assert len(p["lanes"]) == 7
    assert p["verdict"] in ("GATES_CLEAN", "GAPS_OPEN", "UNKNOWN")
    assert p["persisted"] is False, "no DB was configured, so it cannot have persisted"
    # lane 1 (secret-leak critical) is clean post-#2049; the outbound/traceback/
    # webhook-signature lanes have live gaps, so the verdict must be GAPS_OPEN.
    assert p["verdict"] == "GAPS_OPEN"
    lane_keys = [lane["lane"] for lane in p["lanes"]]
    assert lane_keys == ["l1", "l2", "l3", "l4", "l5", "l6", "l7"]


def test_scan_excludes_the_shell_itself():
    """A detector scanning its own pattern-definition strings is noise — the
    module excludes its own file from the scan."""
    from routes.route_auth_master_shell import _iter_route_files, _SELF_BASENAME
    bases = {base for _p, base in _iter_route_files()}
    assert _SELF_BASENAME not in bases


# ── MUST-FAIL control — proves the runner actually executes this file ─
# (silent-green trap: a conftest/exit-3 collection abort renders GREEN with 0
# tests run). Marked xfail(strict=True): pytest expects it to fail, so a genuine
# failure is reported as XFAIL (green) and an unexpected PASS is a hard error —
# either way it proves these assertions were evaluated, not skipped.

@pytest.mark.xfail(strict=True, reason="MUST-FAIL control proving the runner "
                   "executes this file; a green run reports it XFAIL, an "
                   "unexpected PASS is a hard error")
def test_MUSTFAIL_control_runner_is_alive():
    assert 1 == 2, "MUST-FAIL control: if you see this as a failure the runner ran"
