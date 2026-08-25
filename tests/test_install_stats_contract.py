"""The public install ledger keeps its published contract (2026-08-25).

WHAT WAS MEASURED
=================
/install/{grok,claude,chatgpt,perplexity,cursor} has minted keys as
client_name=install-<client> since 08-19, and NO surface could read them back —
every probe for one 404'd. Measured against production on 2026-08-25:

    install-* keys minted, all time : 0
    CONTROL, prefix 'web-%'         : web-map 121 minted / 0 called / 0 returned

The control matters: an empty result from a query that can only ever return
empty is not a finding. 'web-%' returning 121 proves the shape works, so the
zero for install-* is real.

THE CONTRACT being guarded
==========================
- the route is registered in the SAFE ZONE of main.py. Late-line registration
  silently 404s in production; the dead-man ledger this endpoint is modelled on
  IS late-line and works by luck, which main.py itself calls out as "not a
  precedent to copy". A guard that only checked "the blueprint exists" would
  pass on a dead endpoint.
- the LIKE pattern is a BOUND PARAMETER, never inlined. A literal '%' in a
  psycopg2 query that also carries params raises "unsupported format character"
  and 500s the route.
- minted / called / returned are three separate fields and are never summed.
  Registration is not function — measured here as web-map's 121 minted against
  0 called.
- the `basis` and `evidence_status` blocks SHIP. They are the reason a partner
  can cite these numbers at all; a figure whose population is undocumented is
  exactly what this endpoint exists to stop publishing.
- `basis.known_gap` states that keyless pastes are uncountable, so the figure is
  a FLOOR. Dropping that sentence turns a floor into an implied total.

NO NETWORK, NO DB. The handler is exercised through Flask's test client on the
no-DSN branch; the SQL is read as text.
"""
import os
import re

import pytest

flask = pytest.importorskip("flask")

from routes.install_stats import (  # noqa: E402
    _INSTALL_PREFIX,
    install_stats_bp,
    register_install_stats,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "routes", "install_stats.py")
_MAIN = os.path.join(_ROOT, "main.py")


def _src():
    with open(_SRC, encoding="utf-8") as fh:
        return fh.read()


def _app():
    app = flask.Flask(__name__)
    register_install_stats(app)
    return app


def test_route_is_registered_at_the_public_ops_path():
    rules = {str(r) for r in _app().url_map.iter_rules()}
    assert "/api/v1/ops/install-stats" in rules


def test_registration_lives_in_the_safe_zone_not_late_line():
    """Late-line registration silently 404s in prod — main.py says so itself."""
    with open(_MAIN, encoding="utf-8") as fh:
        main_src = fh.read()
    idx = main_src.find("from routes.install_stats import register_install_stats")
    assert idx != -1, "install_stats is not registered in main.py at all"
    line_no = main_src[:idx].count("\n") + 1
    # The safe zone is the early registration block (~1900-2400). The late-line
    # region begins around 30000+ and is where market_deep_dive / press_loop /
    # competitor_recon each silently 404'd before being moved.
    assert line_no < 10000, (
        f"install_stats registered at main.py:{line_no} — that is the late-line "
        "region, which silently 404s in production. Move it to the SAFE ZONE."
    )


def test_like_pattern_is_bound_never_inlined():
    """A literal % beside params raises unsupported-format-character and 500s."""
    assert _INSTALL_PREFIX == "install-%"
    src = _src()
    # every LIKE in the SQL must compare against a placeholder, not a literal
    for m in re.finditer(r"LIKE\s+(\S+)", src):
        operand = m.group(1).strip().rstrip(",")
        assert operand.startswith("%s"), (
            f"LIKE compares against {operand!r} — inline the pattern and the "
            "route 500s. Bind it as a parameter instead."
        )


def test_no_dsn_degrades_to_503_not_a_crash():
    saved = {k: os.environ.pop(k, None) for k in ("DATABASE_URL", "NEON_DATABASE_URL")}
    try:
        r = _app().test_client().get("/api/v1/ops/install-stats")
        assert r.status_code == 503
        assert r.get_json()["ok"] is False
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _response_region():
    """Just the code that builds the payload — not the docstring prose.

    Scoped deliberately: an earlier version of this guard matched the word
    "installs" inside this module's own docstring and failed on correct code.
    A guard that reads comments is measuring the wrong artifact.
    """
    src = _src()
    start = src.find("    by_client, tot = [], {")
    end = src.find("def register_install_stats")
    assert start != -1 and end > start, "response-building region not found"
    return src[start:end]


def test_minted_called_returned_stay_three_separate_fields():
    """Registration is not function: web-map is 121 minted / 0 called."""
    region = _response_region()
    for field in ("minted", "called", "returned"):
        assert f'"{field}"' in region, f"{field} dropped from the response"
    # the three must never be collapsed behind one 'installs' key
    assert '"installs"' not in region and "installs=" not in region, (
        "a single 'installs' field collapses minted/called/returned — the exact "
        "conflation this endpoint exists to prevent"
    )


def test_basis_and_evidence_status_blocks_ship():
    """These are why the numbers are citable. Losing them is a silent regression.

    Reads the RESPONSE REGION, not the file: mutation-tested 2026-08-25 and the
    whole-file version passed while basis.known_gap was deleted, because the key
    name also appears in this module's docstring. A guard that reads prose is
    measuring documentation, not behaviour.
    """
    region = _response_region()
    # Top-level blocks are jsonify kwargs; their contents are quoted dict keys.
    # Word-boundary regex, not `in`: a substring check passes on `_basis={`,
    # which ships the block under a name no client reads (mutation-tested).
    for kwarg in ("basis", "evidence_status", "evidence_status_claims"):
        assert re.search(rf"\b{kwarg}\s*=\s*\{{", region), (
            f"{kwarg} block stopped shipping as a response field"
        )
    for key in ("population", "not_counted", "minted_vs_called", "known_gap"):
        assert f'"{key}"' in region, f"basis.{key} stopped shipping"
    for key in ("evidence_status_version", "observed", "hypothesis", "verified"):
        assert f'"{key}"' in region, f"evidence_status.{key} stopped shipping"


def test_known_gap_still_says_the_number_is_a_floor():
    """Keyless pastes are uncountable; without this the floor reads as a total."""
    region = _response_region()
    assert "FLOOR" in region, (
        "basis.known_gap no longer states that the figure is a FLOOR — a human "
        "who pastes the keyless URL never mints a key and is not counted, so "
        "reporting this as a total overstates installs"
    )


def test_scoring_is_on_keys_not_sessions_or_ips():
    """Grok rotates egress IP per request and opens a session per tool call."""
    src = _src()
    assert "sessions and IPs" in src, "the not_counted rationale was dropped"
    # the SQL must never group or count by session_id / ip
    sql_region = src[src.find("WITH ik AS"):src.find("rows = cur.fetchall()")]
    for banned in ("session_id", "COUNT(DISTINCT l.session_id"):
        assert banned not in sql_region, (
            f"{banned} in the scoring SQL — inflates the figure ~10x"
        )
