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
import ast
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


def _sql_literals():
    """Every string the MODULE evaluates, f-string segments included.

    Comments and docstrings that merely talk about SQL are not SQL. A
    FormattedValue directly after a segment ending in `LIKE` IS a pattern being
    interpolated rather than bound, so that is reported as an inlined operand
    instead of being skipped for want of a following token.
    """
    out = []
    for n in ast.walk(ast.parse(_src())):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            parts = []
            for v in n.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    # mark the interpolation so a trailing `LIKE` is caught
                    parts.append("<<interpolated>>")
            out.append("".join(parts))
    return out


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
    """A literal % beside params raises unsupported-format-character and 500s.

    ★ 2026-09-20: this scanned the raw source and so read PROSE as SQL. The
    sentence "It carries NO LIKE" then "# wildcard" in a comment matched the
    operand `#` and failed the guard on a module whose
    every pattern was correctly bound. It now walks string literals only — the
    guard has to look at SQL to be a guard about SQL.
    """
    assert _INSTALL_PREFIX == "install-%"
    for sql in _sql_literals():
        for m in re.finditer(r"LIKE\s+(\S+)", sql):
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

    ★ Re-anchored 2026-08-25 when the row-summarising loop moved into
    _summarize() so the control could share it. The region now starts at that
    helper — the response FIELDS are built there — and still ends before
    register_install_stats, so this module's docstring stays excluded, which is
    the whole point of scoping it.
    """
    src = _src()
    start = src.find("def _summarize(rows):")
    end = src.find("def register_install_stats")
    assert start != -1 and end > start, "response-building region not found"
    region = src[start:end]
    # Guard the guard: re-anchoring is exactly when a region silently shrinks to
    # nothing and every assertion over it goes vacuous.
    assert len(region) > 1500, "response region suspiciously small (%d bytes)" % len(region)
    return region


def test_minted_called_returned_stay_three_separate_fields():
    """Registration is not function: web-map is 121 minted / 0 called."""
    region = _response_region()
    # ★ Substring-over-the-region is VACUOUS here: "minted" also appears in the
    # totals accumulator, so deleting it from the per-client record left the
    # check green (mutation-tested 2026-08-25). Pin the RECORD dict by its
    # "client" key and read ITS keys.
    tree = ast.parse(_src())
    summarize = _fn(tree, "_summarize")
    assert summarize is not None, "_summarize() helper is gone"
    recs = [n for n in ast.walk(summarize)
            if isinstance(n, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "client" for k in n.keys)]
    assert recs, "per-client record dict not found in _summarize"
    rec_keys = {k.value for k in recs[0].keys if isinstance(k, ast.Constant)}
    for field in ("minted", "called", "returned"):
        assert field in rec_keys, f"{field} dropped from the per-client record"
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


# ─────────────────────────────────────────────────────────────────────────────
# THE CONTROL SHIPS (2026-08-25, second pass)
#
# The docstring at the top of this file has described a 'web-%' control since
# the endpoint was written — but the control was never in the RESPONSE. Live
# keys on 2026-08-25 were exactly:
#
#   [basis, by_client, clients_tracked, evidence_status,
#    evidence_status_claims, generated_at, minted_by_window, ok, totals]
#
# No control. So the endpoint published `minted: 0` with `by_client: []` and no
# in-band evidence that the query CAN return non-zero — which is the precise
# failure this file's own docstring warns about. The prose was right and the
# implementation drifted, the same way the MCP instructions did.
#
# These assert on the AST, not on the docstring — the docstring is what was
# wrong.
# ─────────────────────────────────────────────────────────────────────────────

from routes.install_stats import _CONTROL_PREFIX  # noqa: E402


def _tree():
    return ast.parse(_src())


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def test_control_prefix_is_a_distinct_like_pattern():
    assert _CONTROL_PREFIX != _INSTALL_PREFIX
    assert _CONTROL_PREFIX.endswith("%"), "control must be a LIKE prefix pattern"
    # Bound as a parameter like the install prefix — a literal % inlined into a
    # psycopg2 query that also carries params 500s the route.
    assert "%s" in _src(), "the LIKE pattern must still be bound, never inlined"


def test_control_runs_the_SAME_ledger_sql():
    """A separately-written control proves only itself.

    If the control had its own query, a bug in the REAL one — wrong metadata
    key, wrong table, wrong join — would still read as 'no installs' while the
    control read green. Same SQL, different bound parameter, or it is not a
    control.
    """
    tree = _tree()
    ledger = _fn(tree, "_ledger")
    assert ledger is not None, "the shared _ledger() helper is gone"

    # Every cur.execute carrying the ledger SQL must be inside _ledger.
    outside = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "execute"):
            uses_ledger = any(isinstance(a, ast.Name) and a.id == "_LEDGER_SQL"
                              for a in n.args)
            if uses_ledger and not (ledger.lineno <= n.lineno <= (ledger.end_lineno or n.lineno)):
                outside.append(n.lineno)
    assert not outside, "ledger SQL executed outside _ledger() at lines %r" % outside

    # And _ledger is called with BOTH prefixes.
    called_with = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_ledger":
            for a in n.args:
                if isinstance(a, ast.Name):
                    called_with.add(a.id)
    assert {"_INSTALL_PREFIX", "_CONTROL_PREFIX"} <= called_with, (
        "_ledger must be called with both prefixes; saw %r" % sorted(called_with)
    )


def test_totals_dict_is_local_so_the_control_cannot_pollute_installs():
    """The totals accumulator must be built fresh per call.

    A module-level dict would be shared between the install ledger and the
    control, and the control's counts would silently land in the published
    install totals — the exact 'never summed' rule this endpoint is built on.
    """
    tree = _tree()
    summarize = _fn(tree, "_summarize")
    assert summarize is not None, "_summarize() helper is gone"
    # Pin the ACCUMULATOR specifically: the dict whose "minted" maps to the
    # constant 0. A looser "any dict with a minted key" check matches the
    # per-client `rec` dict instead and passes on a module-level accumulator —
    # mutation-tested, and the loose version did exactly that.
    accums = []
    for n in ast.walk(summarize):
        if not isinstance(n, ast.Dict):
            continue
        for k, v in zip(n.keys, n.values):
            if (isinstance(k, ast.Constant) and k.value == "minted"
                    and isinstance(v, ast.Constant) and v.value == 0):
                accums.append(n)
    assert accums, (
        "the totals accumulator must be a dict LITERAL inside _summarize — a "
        "module-level dict is shared with the control and its counts would land "
        "in the published install totals"
    )


def test_control_is_published_and_carries_its_verdict():
    src = _src()
    assert "control=control" in src, "the control block must reach the response"
    for field in ('"instrument"', '"reading"', '"is_not_an_install_channel"'):
        assert field in src, "control is missing %s" % field
    # The verdict must be DERIVED from the measured control, not hardcoded.
    assert '"live" if _instrument_live else "unproven"' in src, (
        "control.instrument must be derived from the control's own count"
    )
    assert "_instrument_live = control_tot[" in src


def test_an_empty_control_downgrades_the_reading():
    """If the control empties, the endpoint must say the zeros are unreadable."""
    src = _src()
    assert "cannot currently tell" in src and "evidence of absence" in src, (
        "an empty control must publish that install zeros are uninterpretable, "
        "not silently keep claiming absence"
    )
    assert '"observed" if _instrument_live else "hypothesis"' in src, (
        "evidence_status for the control must downgrade when the control empties"
    )


# ══════════════════════════════════════════════════════════════════════════════
# THE PROBE IS NOT AN INSTALL (2026-09-20)
#
# Measured on production that morning, this endpoint published:
#     minted: 1, clients_tracked: 1, by_client: [install-verify-durability]
# which is OUR OWN end-to-end mint probe (minted 2026-09-01, 0 calls, 0 returns)
# published keyless, to anyone we asked to cite the number. The true count of
# keys ever claimed from an /install/<client> page was 0. dchub-mcp-server's
# registry-discover.yml had named that key as ours since 2026-09-07; the public
# surface had not.
#
# These assertions read the AST, not the text, on purpose: this module quotes
# `install-verify`, `_NOT_A_PROBE` and `install-%` inside long comments, so a
# substring test could be satisfied by the commentary describing the bug.
# ══════════════════════════════════════════════════════════════════════════════

from routes.install_stats import (  # noqa: E402
    _EXCLUDE_NOTHING,
    _NOT_A_PROBE,
    _PROBE_PREFIX,
    _ledger,
)


def _formatted_names(node):
    """Names interpolated into an f-string node (empty for a plain string)."""
    if not isinstance(node, ast.JoinedStr):
        return set()
    return {v.value.id for v in node.values
            if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name)}


def _module_assign(tree, name):
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets):
            return n.value
    return None


def test_the_probe_namespace_is_narrower_than_the_install_namespace():
    """install-verify-% must select a strict SUBSET of install-%.

    Widen it to install-% and the endpoint reports every install as a probe;
    that mutation is caught by this pair, not by any count assertion.
    """
    assert _PROBE_PREFIX != _INSTALL_PREFIX
    assert _PROBE_PREFIX.startswith(_INSTALL_PREFIX.rstrip("%")), (
        "the probe namespace must live INSIDE install-, or excluding it cannot "
        "clean the install figures"
    )
    assert len(_PROBE_PREFIX) > len(_INSTALL_PREFIX), (
        "a probe pattern no narrower than install-% excludes the whole ledger"
    )
    # The sentinel used to exclude nothing must be unable to match anything.
    # Both LIKE wildcards. `_` matches any single character, so a sentinel of
    # underscores excludes every client_name of that length — this assertion
    # caught exactly that in the first draft.
    assert not set("%_") & set(_EXCLUDE_NOTHING), (
        "_EXCLUDE_NOTHING carries a LIKE wildcard (% or _) — it would silently "
        "drop real rows from the population it claims to leave alone"
    )


def test_both_counting_queries_carry_the_one_exclusion_clause():
    """The ledger and the windowed mint count must exclude probes from the SAME
    string. Two hand-written copies is how one painter starts counting the probe
    while the other does not — by_client 0 beside minted_by_window 1."""
    tree = _tree()
    ledger_sql = _module_assign(tree, "_LEDGER_SQL")
    assert ledger_sql is not None, "_LEDGER_SQL is no longer a module constant"
    assert "_NOT_A_PROBE" in _formatted_names(ledger_sql), (
        "the ledger SQL does not interpolate _NOT_A_PROBE — it counts probes as "
        "installs"
    )

    windowed = [
        n for n in ast.walk(_fn(tree, "install_stats"))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"
    ]
    assert len(windowed) == 1, (
        "expected exactly one inline cur.execute (the windowed mint count); the "
        "ledger runs through _ledger()"
    )
    sql, params = windowed[0].args[0], windowed[0].args[1]
    assert "_NOT_A_PROBE" in _formatted_names(sql), (
        "the windowed mint count does not carry the exclusion, so "
        "minted_by_window would report a probe the by_client rows exclude"
    )
    bound = {e.id for e in ast.walk(params) if isinstance(e, ast.Name)}
    assert "_PROBE_PREFIX" in bound, (
        "the windowed query interpolates the clause but never binds the probe "
        "pattern to its %s — psycopg2 would raise, or worse, bind the wrong arg"
    )


def test_the_clause_is_a_real_sql_predicate_not_a_label():
    """_NOT_A_PROBE has to be the actual NOT LIKE, bound, on the client_name."""
    assert _NOT_A_PROBE.strip().startswith("AND ")
    assert "NOT LIKE %s" in _NOT_A_PROBE
    assert "client_name" in _NOT_A_PROBE
    # The alias the windowed query adopted so one string can serve both queries.
    assert _NOT_A_PROBE.count("k.") == 1


def test_ledger_excludes_probes_by_default_and_probes_opt_out_explicitly():
    """_ledger's default must be the SAFE one: a new caller that forgets the
    third argument gets install figures WITHOUT probes."""
    import inspect
    sig = inspect.signature(_ledger)
    assert sig.parameters["exclude"].default == _PROBE_PREFIX, (
        "_ledger must default to excluding probes; a defaulted-open exclusion "
        "re-publishes them the next time someone adds a call site"
    )
    tree = _tree()
    calls = [n for n in ast.walk(_fn(tree, "install_stats"))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_ledger"]
    assert len(calls) == 3, "expected three ledgers: installs, probes, control"
    opted_out = [c for c in calls
                 if any(isinstance(a, ast.Name) and a.id == "_EXCLUDE_NOTHING"
                        for a in c.args)]
    assert len(opted_out) == 1, (
        "exactly one ledger — the probes block — may exclude nothing"
    )
    assert any(isinstance(a, ast.Name) and a.id == "_PROBE_PREFIX"
               for a in opted_out[0].args), (
        "the exclude-nothing ledger must be the one reading _PROBE_PREFIX, or it "
        "is publishing the install population with probes back in it"
    )


def test_probes_are_published_separately_and_never_summed_into_installs():
    src = _src()
    assert "probes=probes," in src, (
        "the probes block must reach the response — an exclusion nobody can see "
        "is indistinguishable from a query that found nothing"
    )
    assert "totals=tot," in src, "the install totals must stay the install totals"
    assert re.search(r'"totals":\s*probe_tot', src), (
        "the probes block must carry the probe counts, measured by the same SQL"
    )
    # No path may fold probe counts back into the install totals.
    assert not re.search(r"\btot\b\s*\[[^\]]*\]\s*[+\-]?=\s*[^\n]*probe", src), (
        "probe counts are being added into the published install totals"
    )
    assert not re.search(r"\btot\b\s*\.update\(\s*probe", src)


def test_the_control_reading_does_not_assert_an_emptiness_it_did_not_check():
    """The old sentence said "an empty install-% result is a real zero" while
    the result was NOT empty — it held the probe. The claim must be derived."""
    src = _src()
    assert 'if tot["minted"] == 0' in src or "if tot['minted'] == 0" in src, (
        "control.reading must decide 'it is empty' from the measured count"
    )


def test_basis_says_client_name_is_self_declared():
    """The population is what a caller CLAIMED, not verified provenance — the
    property that let our own curl mint into the install namespace at all."""
    src = _src()
    assert '"self_declared"' in src, "basis must publish the self-declared caveat"
    region = _response_region()
    assert "attests" in region and "not verified provenance" in region, (
        "the caveat must ship inside the response, not sit in a comment"
    )


# ── the rule has TWO consumers (2026-09-20) ───────────────────────────────────
# /api/v1/ops/install-stats was not the only surface counting the probe as an
# install: flask_mcp_endpoints.py's `install_artifact_30d` ladder runs its own
# hand-written `LIKE 'install-%'` (inlined, deliberately — it passes no params),
# and it published the same single probe row as "1 key minted". Fixing one
# surface and leaving the other is how the wrong number survives on the busier
# hop, so the exclusion is enforced across every inlined consumer.
#
# install_stats.py itself BINDS its patterns, so it does not appear in this scan
# by construction — test_both_counting_queries_carry_the_one_exclusion_clause
# and test_ledger_excludes_probes_by_default… are its copy of this rule.
_REPO_PY = ("flask_mcp_endpoints.py", "main.py")


def test_every_inlined_install_filter_excludes_the_probe_namespace():
    found = 0
    for rel in _REPO_PY:
        path = os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            sql = node.value
            if "LIKE 'install-%'" not in sql or "client_name" not in sql:
                continue
            found += 1
            assert "NOT LIKE 'install-verify-%'" in sql, (
                f"{rel} line {node.lineno}: filters client_name on "
                f"'install-%' without excluding the reserved 'install-verify-%' "
                f"probe namespace, so it counts our own mint probes as somebody "
                f"else's installs — which is the defect this rule exists for"
            )
    # ★ A scan that can silently find nothing is not a guard. The inlined
    # consumer exists (flask_mcp_endpoints.install_artifact_30d); if this floor
    # trips, the query was renamed or moved and the rule now covers nobody.
    assert found >= 1, (
        "no inlined install-% client_name filter found in %s — the scan no "
        "longer reaches the consumer it was written for" % (_REPO_PY,)
    )
