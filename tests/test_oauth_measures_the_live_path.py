"""tests/test_oauth_measures_the_live_path.py — the OAuth rung is measured from
its ARTIFACT, and the counter that cannot see the live path says so.

Measured 2026-09-07 on production:

  /.well-known/oauth-protected-resource advertises
      authorization_servers: ["https://beloved-stream-52.authkit.app"]
  https://dchub.cloud/oauth/authorize                     -> HTTP 404

`oauth_authorize_started` is emitted ONLY by the built-in AS in the gateway's
oauth.mjs. That AS is not mounted and nothing advertises it, so the counter can
never be non-zero — yet it was published as 0 beside guidance telling readers
that a low value means "the 401 never turns into a browser hop". A number that
cannot move must not carry an interpretation that depends on it moving.

The outcome is NOT blind: a completed sign-in leaves a ROW.

  mcp_dev_keys WHERE api_key LIKE 'dch_oauth_%'
      9 all-time · 0 created in 30d · 3 active 7d · 4 active 30d
      7 contactable · 6 returned after their first day
      most recent created 2026-08-08

House rule: never import main. Route modules are read as source.

Mutations that must turn this RED (recorded in the PR body):
  1. the resolver stops reporting `created`, or derives it from something
     other than the INSERT's rowcount
  2. authorize_started_blind_on_live_path removed or flipped to False
  3. durable_identities reads events instead of rows
  4. the refutation is deleted (the wrong hypothesis becomes actionable again)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        s = fh.read()
    assert len(s) > 500, "%s read as %d bytes" % (rel, len(s))
    return s


def _code_only(src: str) -> str:
    """Drop `#` comments.

    ★ Every assertion below runs on this. The prose in these modules quotes the
    identifiers under test by design — the blindness note names
    `authorize_started_blind_on_live_path`, and the resolver's comment names
    `created`. Asserting against the raw file would let a comment satisfy a
    test whose subject had been deleted.
    """
    out = []
    for line in src.split("\n"):
        st = line.lstrip()
        if st.startswith("#"):
            continue
        out.append(line.split("  #")[0] if "  #" in line else line)
    return "\n".join(out)


# ── the resolver reports whether it minted ───────────────────────────────────

def test_resolver_reports_created_from_the_insert_rowcount():
    code = _code_only(_src("routes/mcp_oauth_2025_06_18.py"))
    assert "created = (cur.rowcount == 1)" in code, (
        "`created` is no longer derived from the INSERT's rowcount. With "
        "ON CONFLICT DO NOTHING, rowcount IS the answer — anything else "
        "(re-SELECTing, comparing timestamps) can disagree with what the "
        "statement actually did"
    )
    assert '"created": created,' in code, (
        "the resolve response dropped `created` — the gateway's identity_created "
        "stage fires only when this endpoint reports it, so the last rung of the "
        "OAuth funnel loses its only emitter on the live path"
    )


def test_created_is_read_before_any_later_execute():
    """rowcount belongs to the most recent execute() on the cursor."""
    code = _code_only(_src("routes/mcp_oauth_2025_06_18.py"))
    at = code.index("created = (cur.rowcount == 1)")
    # The LAST execute() before the read is the one whose rowcount is reported.
    # Anchoring on the INSERT's own index would land inside its SQL literal,
    # after the `cur.execute(` that opens it, and count zero either way.
    last_exec = code.rfind("cur.execute(", 0, at)
    assert last_exec != -1, "no cur.execute() precedes the rowcount read"
    stmt = code[last_exec:at]
    assert "INSERT INTO mcp_dev_keys" in stmt, (
        "the statement immediately before `created = cur.rowcount == 1` is no "
        "longer the INSERT, so `created` reports a different statement's "
        "rowcount. Whatever now runs in between must move after the read."
    )


# ── the blind counter declares itself, as data ───────────────────────────────

def test_authorize_started_publishes_its_blindness_as_data():
    code = _code_only(_src("routes/mcp_retention.py"))
    assert '"authorize_started_blind_on_live_path": True,' in code, (
        "the blindness flag is gone or no longer True. It must be DATA a "
        "consumer can branch on — prose gets skimmed, a boolean gets handled"
    )
    assert '"authorize_started_blind_reason":' in code, (
        "the reason string is gone — a bare flag does not tell the next reader "
        "that the live AS is WorkOS and the emitting AS answers 404"
    )


def test_the_old_misleading_guidance_only_survives_as_a_retraction():
    """The retired claim told readers to conclude something the number cannot
    support. It may stay in the file ONLY as a quoted retraction — deleting it
    outright loses the record of why, but leaving it bare re-arms it."""
    raw = _src("routes/mcp_retention.py")
    needle = "401 never turns into a browser hop"
    if needle not in raw:
        return  # removed entirely — also acceptable
    window = raw[max(0, raw.index(needle) - 900):raw.index(needle)]
    assert "2026-09-07" in window, (
        "the retired guidance appears without the dated retraction above it"
    )
    assert "BLIND" in window.upper(), (
        "the retired guidance appears without saying the counter is blind — a "
        "reader meeting it in isolation would act on it again"
    )


# ── the rung is measured from its artifact ───────────────────────────────────

def test_durable_identities_counts_rows_not_events():
    code = _code_only(_src("routes/mcp_retention.py"))
    assert '"durable_identities"' in code, (
        "durable_identities removed — the only OAuth rung not dependent on a "
        "counter the gateway remembers to emit"
    )
    blk = code[code.index('ib["durable_identities"] = {'):]
    blk = blk[: blk.index("except Exception:")] if "except Exception:" in blk else blk
    for key in ("all_time", "created_30d", "active_7d", "contactable",
                "returned_after_first_day", "most_recent_created_at", "small_n"):
        assert '"%s"' % key in blk, "durable_identities lost %r" % key
    # It must read the KEY TABLE, not the challenge rollup.
    q = code[code.index("SELECT\n                      COUNT(*)"):]
    q = q[: q.index('"""')]
    assert "FROM mcp_dev_keys" in q, "durable_identities no longer reads mcp_dev_keys"
    assert "mcp_oauth_challenges" not in q, (
        "durable_identities is reading the challenge rollup — that is the event "
        "side, which is exactly what this block exists to not depend on"
    )


def test_small_n_is_published_so_a_rate_cannot_be_quoted_bare():
    code = _code_only(_src("routes/mcp_retention.py"))
    assert '"small_n": int(_oi.get("all_time") or 0) < 30,' in code, (
        "small_n is gone or no longer derived from the population — the "
        "durable-vs-key-only return comparison this cohort is cited for rests "
        "on single digits and must not be quoted as a percentage without it"
    )


def test_the_refuted_hypothesis_stays_recorded():
    """The 'we stopped challenging on initialize' story is wrong and must stay
    written down, or it gets rediscovered and acted on."""
    code = _code_only(_src("routes/mcp_retention.py"))
    assert '"the_obvious_hypothesis_is_REFUTED"' in code, (
        "the refutation was deleted. 994 initialize challenges ran "
        "2026-08-09..08-15 and produced zero identities; the last identity "
        "predates that window. Without this, the adjacent series invite the "
        "wrong fix"
    )
    raw = _src("routes/mcp_retention.py")
    for fact in ("994", "2026-08-08", "2026-08-09"):
        assert fact in raw, "the refutation lost its supporting figure %r" % fact
