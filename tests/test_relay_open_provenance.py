"""r-relay-provenance / r-seed-rotation (2026-09-03).

Three defects found while asking why the agent->human funnel converts at zero.
None of them is "the relay is broken" — it works end to end. All three are the
instrument describing itself wrongly.

  1. THE SEED WENT STALE AND PUBLISHED A FALSE CONVERSION. `human_acted` v4
     excluded ONE named operator session (88e20dac). That client rotated its
     session id on 2026-08-20 (8c8e1d0d, first call 10.1s after 88e20dac's
     last, same platform/user_agent/tier), the new session opened a relay
     link, and the stage published 1 over 30d — the first non-zero in its
     life, on the exact stage v4 was built to keep honest.

  2. `relay_opens.referer` IS AN EDGE ARTIFACT, NOT PROVENANCE. The Cloudflare
     worker injects `Referer: https://dchub.cloud` on every proxied request:
     148 of 150 rows in the 30d to 2026-09-03 carry that exact value and the
     other 2 are empty. PROVEN by sending a request with NO Referer header and
     watching the row land with one. A self-traffic rule keyed on this column
     reads as "every open is ours" and would delete the first real human open
     the moment it arrived.

  3. THE STAGE CANNOT SEE MOST OF ITS OWN POPULATION. `human_acted` counts
     FROM mcp_high_intent_sessions and only then looks for a relay row, but
     relay links are minted on paths that never write that table. 148 of 163
     joinable opens (30d) sat on sessions absent from it — uncountable, not
     zero, and indistinguishable from zero in the published number.

WHAT THIS GUARD PINS — properties, not counts, which move:
  · the rotated session is excluded AND the pre-rotation figure is derivable
    (so the removal can be audited, never silent);
  · v5 is labelled an INFERENCE, because unlike v4 it is one;
  · no read path treats `referer` as a signal;
  · the provenance split and the denominator gap are published.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_calls_deloop import (  # noqa: E402
    SELF_TRAFFIC_SESSION_SEED_V4,
    external_session_predicate,
    self_traffic_session_prefixes,
)
from routes.handoff_definition import (  # noqa: E402
    HUMAN_ACTED_DEFINITION_CHANGELOG,
    HUMAN_ACTED_DEFINITION_VERSION,
)

_HERE = os.path.dirname(__file__)
_ROOT = os.path.join(_HERE, "..")


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ── 1. the rotated session is excluded, and v4 stays derivable ──────────────

def test_rotated_operator_session_is_excluded():
    """The 08-20 rotation must not be able to publish a human_acted non-zero."""
    live = self_traffic_session_prefixes()
    assert "8c8e1d0d" in live, (
        "the rotated operator session is not excluded — human_acted will "
        "publish its click as the stage's first-ever conversion")
    assert "88e20dac" in live, "the v4 session must stay excluded too"


def test_v4_figure_is_derived_not_retyped():
    """`human_acted_v4_before_rotation` must come from the named v4 seed.

    Retyping '88e20dac' at the call site is the restatement defect
    routes/handoff_definition.py exists to prevent: the copy rots and the
    published 'before' figure silently becomes the 'after' one.
    """
    assert SELF_TRAFFIC_SESSION_SEED_V4 == ("88e20dac",)
    assert set(SELF_TRAFFIC_SESSION_SEED_V4) < set(self_traffic_session_prefixes()), (
        "the v4 seed must be a strict subset of the live seed, or the two "
        "published figures cannot differ and the 'before' number is a lie")
    v4 = external_session_predicate("s.mcp_session_id", SELF_TRAFFIC_SESSION_SEED_V4)
    live = external_session_predicate("s.mcp_session_id")
    assert v4 != live, "v4 and v5 predicates are identical — nothing was excluded"
    assert "8c8e1d0d" not in v4, "the v4 predicate must not know about the rotation"
    assert "8c8e1d0d" in live

    src = _read("flask_mcp_endpoints.py")
    assert "_DELOOP_SELF_SEED_V4" in src, (
        "the funnel must DERIVE the v4 predicate from the named seed")
    # A session id may be NAMED in prose (excluded.basis explains which
    # removal is a fact). What it may never do is BUILD a predicate: a
    # hardcoded id next to a regex/LIKE operator is the copy that rots.
    built = [
        "%d: %s" % (n, ln.strip()[:100])
        for n, ln in enumerate(src.splitlines(), 1)
        if re.search(r"(88e20dac|8c8e1d0d)", ln)
        and re.search(r"(!~\*|~\*|\bnot\s+like\b|\blike\b|\^\()", ln, re.I)
    ]
    assert not built, (
        "flask_mcp_endpoints builds a session predicate from a hardcoded id "
        "— derive it from mcp_calls_deloop instead:\n  " + "\n  ".join(built))


def test_predicate_is_bound_param_safe():
    """No literal % — these predicates are embedded in `sql % iv` call sites.

    external_session_predicate's own docstring records that a LIKE form took
    the live handoff-funnel endpoint down inside one deploy. Passing a custom
    prefix tuple must not reintroduce it.
    """
    for prefixes in (None, SELF_TRAFFIC_SESSION_SEED_V4, ("88e20dac", "8c8e1d0d")):
        p = external_session_predicate("s.mcp_session_id", prefixes)
        assert "%" not in p, "literal %% in predicate: %r" % p


# ── 2. v5 must admit that it is an inference ────────────────────────────────

def test_v5_is_published_and_labelled_an_inference():
    """v4 was a named fact. v5 is not, and must not be dressed as one."""
    assert HUMAN_ACTED_DEFINITION_VERSION >= 5
    entry = HUMAN_ACTED_DEFINITION_CHANGELOG.get(5)
    assert entry, "v5 has no changelog entry"
    # ★ NOT `"INFERENCE" in entry` — that assertion is vacuous here. The
    # entry uses the word twice, so flipping the actual claim to "THIS IS A
    # NAMED FACT" left the substring intact and the guard passed (caught by
    # mutation M3, 2026-09-03). Anchor on the disclaimer itself.
    up = entry.upper()
    assert "IS AN INFERENCE, NOT A NAMED FACT" in up, (
        "v5 excludes a session on a 10-second timing coincidence the operator "
        "could not confirm; the changelog must disclaim it outright rather "
        "than letting it inherit v4's 'NAMED FACT' framing")
    assert up.count("IS A NAMED FACT") == 0, (
        "v5 claims to be a named fact; it is not")
    assert "8c8e1d0d" in entry, "v5 must name what it removed"
    # every version 1..N carries an entry (same rule the v4 guard pins)
    for v in range(1, HUMAN_ACTED_DEFINITION_VERSION + 1):
        assert HUMAN_ACTED_DEFINITION_CHANGELOG.get(v), "version %d undescribed" % v


def test_excluded_basis_declares_the_mixed_basis():
    """The payload's `excluded.basis` said 'NOT inferred from the row'.

    That sentence was true for v4 and is false for v5. A reader auditing the
    exclusion is entitled to know which removals are facts and which are not.
    """
    src = _read("flask_mcp_endpoints.py")
    m = re.search(r'"basis": "human_acted \(v%d\).*?% _HUMAN_ACTED_VERSION',
                  src, re.S)
    assert m, "the excluded.basis block moved or changed shape"
    basis = m.group(0)
    assert "MIXED BASIS" in basis, (
        "excluded.basis must declare that v5 mixes a named fact with an "
        "inference")
    assert "NOT inferred from" not in basis, (
        "excluded.basis still claims every exclusion is a named fact")


# ── 3. referer is an edge artifact — never a signal ─────────────────────────

_REFERER_READERS = (
    "flask_mcp_endpoints.py",
    "routes/human_relay.py",
    "routes/mcp_funnel.py",
    "routes/relay_conversion_watch.py",
)


def test_no_read_path_filters_on_relay_referer():
    """The CF worker injects a dchub.cloud Referer on EVERY proxied request.

    Filtering relay_opens on it does not select self-traffic; it selects
    everything. Writing the column is fine — reading it as provenance is the
    defect. Flags a SQL predicate against a referer column, not the INSERT.
    """
    bad = []
    for rel in _REFERER_READERS:
        path = os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            continue
        for n, line in enumerate(_read(rel).splitlines(), 1):
            low = line.lower()
            if "referer" not in low and "referrer" not in low:
                continue
            if low.lstrip().startswith("#"):
                continue          # a comment ABOUT the trap is the point
            # A SQL COMPARISON against the column. Writing it (an INSERT
            # column list or a bound param) is fine and must not trip this.
            if re.search(r"referr?er\s*(=|!=|<>|!?~\*?|\bilike\b|\blike\b)\s*\S",
                         low) \
               or re.search(r"\b(where|having)\b[^#]*\breferr?er\b", low):
                bad.append("%s:%d: %s" % (rel, n, line.strip()[:100]))
    assert not bad, (
        "relay_opens.referer is edge-injected (proven 2026-09-03: a request "
        "sending NO Referer landed a row with 'https://dchub.cloud'). These "
        "read paths treat it as a signal:\n  " + "\n  ".join(bad))


def test_referer_trap_is_documented_at_the_write_site():
    """The warning must live where the column is written, or it will be read
    as provenance by the next person who opens the table."""
    src = _read("routes/human_relay.py")
    assert "NOT PROVENANCE" in src.upper(), (
        "routes/human_relay.py writes relay_opens.referer with no warning "
        "that the edge injects it")


# ── 4. the funnel publishes what its source table actually contains ─────────

def test_funnel_publishes_relay_open_provenance_and_gap():
    """A 0 on human_acted is unreadable without knowing (a) what the source
    table holds and (b) how much of it the stage can even see."""
    src = _read("flask_mcp_endpoints.py")
    for key in ("relay_open_provenance", "human_acted_denominator_gap",
                "human_acted_v4_before_rotation"):
        assert '"%s"' % key in src, "funnel payload no longer publishes %s" % key
    for sub in ("probe_ua", "no_session_id", "countable_opens",
                "sessions_not_in_high_intent_table"):
        assert '"%s"' % sub in src, "provenance lost its %s bucket" % sub
    # both published blocks must carry a basis a reader can check
    prov = src.split('"relay_open_provenance"', 1)[1][:2600]
    assert '"basis"' in prov, "relay_open_provenance ships without a basis"
    gap = src.split('"human_acted_denominator_gap"', 1)[1][:2000]
    assert '"basis"' in gap, "human_acted_denominator_gap ships without a basis"
