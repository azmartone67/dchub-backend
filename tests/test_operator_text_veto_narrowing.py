"""Guards for the 2026-08-23 operator-lane text veto — 799 tokens of prose.

★ WHAT WAS WRONG. `_operator_exclusion_set()` unioned the return of
`_recently_posted_keys()` straight into the operator rotation veto.
_recently_posted_keys is documented as returning "dedup_keys (normalized
markets/isos/deals)"; it actually returns EVERY WHITESPACE TOKEN of the
concatenated bodies of up to 120 recent posts. Measured live that day off
/api/v1/brain/media/operator-lane-debug:

    exclusion_tokens_n: 799        lane_returns_lead: FALSE
    sample: 10 100 1000 2026 2030 5000 5000000 18603 15573 5b 10day
    supply: builds_30d=491  deals_60d=36  tracked_operator_keys=5448
    portfolio eligible 3/3 rotation_blocked · deal eligible 7/7 rotation_blocked

At most 9 of those tokens can come from the ledger (operator_spotlight fired 9
times in 14d). The rest is prose — and the prose is not even ours: the SQL
reads social_media_posts + linkedin_posts, while the quad writes
linkedin_quad_posts (press_crosspost_7d was 8 the same day).

THE FIX: a text token may veto only if it is itself a tracked operator key.
A positive membership test, not a stopword blocklist — so it cannot recreate
the nLighten stalemate (2026-08-07) the text side was added for.

★ WHY EACH "IT NARROWS" TEST IS PAIRED WITH A CONTROL. A test that only
asserts the junk token is gone passes just as happily if the whole text veto
were deleted. Every narrowing test below therefore also asserts that a REAL
operator mentioned in the same text still vetoes. Mutating the fix to
`text = set()` must fail the control, not slip through.

CI-SAFETY: no DB, no network — _conn, recent_lead_ledger, _recently_posted_keys
and the operator key space are all stubbed.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def med(monkeypatch):
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    monkeypatch.delenv("MEDIA_OPERATOR_TEXT_VETO_KEYS_ONLY_DISABLE",
                       raising=False)
    monkeypatch.delenv("MEDIA_OPERATOR_LANE_DISABLE", raising=False)
    import routes.media_editorial as m
    monkeypatch.setattr(m, "_conn", lambda: object())
    monkeypatch.setattr(m, "recent_lead_ledger", lambda days=14: [])
    return m


# The live shape: a handful of real operator names adrift in ~790 prose tokens.
PROSE = {"10", "100", "1000", "2026", "2030", "5000", "18603", "15573", "5b",
         "the", "capacity", "grid", "switch", "stack", "megawatt",
         "oracle", "microsoft"}
KNOWN_OPERATORS = {"oracle", "microsoft", "equinix", "nlighten", "switch",
                   "stack", "digitalrealty", "frontiertampa"}


def _known(monkeypatch, med, tokens):
    monkeypatch.setattr(med, "_known_operator_tokens", lambda conn: set(tokens))


def _text(monkeypatch, med, tokens):
    monkeypatch.setattr(med, "_recently_posted_keys",
                        lambda days=4: set(tokens))


# ── the narrowing itself, each with its control ──────────────────────────

def test_prose_tokens_are_dropped_from_the_veto(med, monkeypatch):
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, KNOWN_OPERATORS)
    _ledger, text, meta = med._operator_exclusion_parts(object())
    assert meta["mode"] == "keys_only"
    for junk in ("10", "5000", "18603", "the", "capacity", "megawatt", "2026"):
        assert junk not in text, f"prose token {junk!r} still vetoes an operator"


def test_CONTROL_a_real_operator_named_in_that_same_text_still_vetoes(
        med, monkeypatch):
    """★ THE PAIRED CONTROL. Deleting the text veto entirely also makes the
    test above pass. This one fails if the fix throws the baby out — and it is
    the nLighten stalemate (2026-08-07) that the text side exists to prevent."""
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, KNOWN_OPERATORS)
    _ledger, text, _meta = med._operator_exclusion_parts(object())
    assert "oracle" in text, "a genuinely mentioned operator stopped vetoing"
    assert "microsoft" in text


def test_the_veto_shrinks_by_the_prose_it_dropped(med, monkeypatch):
    """Counted, not just spot-checked: the narrowed set is exactly the
    intersection, so the 799→small collapse is arithmetic and not luck."""
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, KNOWN_OPERATORS)
    _ledger, text, meta = med._operator_exclusion_parts(object())
    assert text == PROSE & KNOWN_OPERATORS
    assert meta["raw_text_tokens_n"] == len(PROSE)
    assert len(text) < meta["raw_text_tokens_n"], "nothing was narrowed at all"


def test_a_common_word_that_IS_an_operator_still_vetoes(med, monkeypatch):
    """Switch and Stack are real operators AND ordinary English words. The fix
    is a membership test, not a stopword list, so they must keep vetoing —
    otherwise the desk's own text guard kills the lead the lane just offered
    and the slot is wasted."""
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, KNOWN_OPERATORS)
    _ledger, text, _ = med._operator_exclusion_parts(object())
    assert {"switch", "stack"} <= text


# ── the ledger side must be untouched ────────────────────────────────────

def test_ledger_veto_survives_a_token_absent_from_the_operator_key_space(
        med, monkeypatch):
    """The ledger records what WE published. It must veto regardless of the
    key space — narrowing applies to the text side only."""
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [
        {"kind": "operator_spotlight", "entity": "Frontier Tampa",
         "days_ago": 2.0}])
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, {"oracle"})     # deliberately does NOT list it
    ledger, text, _ = med._operator_exclusion_parts(object())
    assert "frontiertampa" in ledger
    assert "frontiertampa" not in text
    assert "frontiertampa" in med._operator_exclusion_set(object())


def test_ledger_ignores_rows_of_another_kind(med, monkeypatch):
    monkeypatch.setattr(med, "recent_lead_ledger", lambda days=14: [
        {"kind": "deal", "entity": "Ares", "days_ago": 1.0}])
    _text(monkeypatch, med, set())
    _known(monkeypatch, med, KNOWN_OPERATORS)
    ledger, _text_, _ = med._operator_exclusion_parts(object())
    assert "ares" not in ledger


# ── degradation must never widen the lane ────────────────────────────────

def test_unreadable_key_space_falls_back_to_the_OLD_wider_veto(med, monkeypatch):
    """★ THE DANGEROUS DIRECTION. If the key-space read fails and the code
    fell back to an EMPTY text veto, the lane would start offering operators
    the desk is about to refuse — more permissive than the behaviour being
    replaced. Degrade toward the old veto, never past it."""
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, set())          # read returned nothing
    _ledger, text, meta = med._operator_exclusion_parts(object())
    assert meta["mode"] == "all_tokens_fallback"
    assert text == PROSE, "degraded read made the veto MORE permissive"


def test_a_raising_key_space_read_also_falls_back_and_records_why(
        med, monkeypatch):
    def _boom(conn):
        raise RuntimeError("pool exhausted")
    monkeypatch.setattr(med, "_known_operator_tokens", _boom)
    _text(monkeypatch, med, PROSE)
    _ledger, text, meta = med._operator_exclusion_parts(object())
    assert text == PROSE
    assert meta["mode"] == "all_tokens_fallback"
    assert "RuntimeError" in (meta.get("error") or "")


def test_conn_none_does_not_crash_and_does_not_widen(med, monkeypatch):
    _text(monkeypatch, med, PROSE)
    _ledger, text, meta = med._operator_exclusion_parts(None)
    assert text == PROSE
    assert meta["mode"] == "all_tokens_fallback"


def test_kill_switch_restores_the_raw_bag_of_words(med, monkeypatch):
    monkeypatch.setenv("MEDIA_OPERATOR_TEXT_VETO_KEYS_ONLY_DISABLE", "1")
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, KNOWN_OPERATORS)
    _ledger, text, meta = med._operator_exclusion_parts(object())
    assert meta["mode"] == "all_tokens_disabled"
    assert text == PROSE, "kill switch did not restore the old behaviour"
    assert "5000" in text


# ── the key-space reader itself ──────────────────────────────────────────

class _FakeCur:
    def __init__(self, rows):
        self._rows = rows
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a, **k):
        pass
    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
    def cursor(self):
        return _FakeCur(self._rows)


def test_known_operator_tokens_normalizes_like_the_ledger(med, monkeypatch):
    """Multi-word keys must normalize the SAME way the ledger normalizes an
    entity, or the two halves of the veto disagree about identity."""
    conn = _FakeConn([("Frontier Tampa",), ("nLighten",), ("Digital Realty",)])
    toks = med._known_operator_tokens(conn)
    assert "frontiertampa" in toks
    assert "nlighten" in toks
    assert "digitalrealty" in toks
    assert "" not in toks


def test_known_operator_tokens_is_empty_for_no_conn(med):
    assert med._known_operator_tokens(None) == set()


# ── the lane end-to-end: prose must no longer starve it ──────────────────

def _stub_pick(monkeypatch, sequence):
    import routes.operator_spotlight as osp

    def fake(conn, exclude_keys=None):
        ex = set(exclude_keys or ())
        for cand in sequence:
            if cand and cand["key"] not in ex:
                return cand
        return None
    monkeypatch.setattr(osp, "pick_spotlight", fake)


EQUINIX = {"angle": "portfolio_growth", "operator": "Equinix", "key": "equinix",
           "added": 7, "sites": ["Dallas"], "fleet_n": 766, "fleet_mw": 4200}


def test_the_lane_now_returns_a_lead_prose_alone_used_to_block(med, monkeypatch):
    """The live failure, in miniature: Equinix is eligible and we have NOT
    written about it — but 'equinix' never appeared in the prose, and under the
    old bag-of-words the lane still died on unrelated tokens once enough of
    them accumulated. Here the only text overlap is junk."""
    _stub_pick(monkeypatch, [EQUINIX])
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, KNOWN_OPERATORS)
    lead = med._operator_spotlight_lead()
    assert lead is not None, "lane still starved after the narrowing"
    assert lead["entity"] == "Equinix"


def test_CONTROL_the_lane_still_skips_an_operator_the_prose_names(
        med, monkeypatch):
    """Paired control for the test above: narrowing must not make the lane
    offer someone we just wrote about."""
    ORACLE = dict(EQUINIX, operator="Oracle", key="oracle")
    _stub_pick(monkeypatch, [ORACLE, EQUINIX])
    _text(monkeypatch, med, PROSE)
    _known(monkeypatch, med, KNOWN_OPERATORS)
    lead = med._operator_spotlight_lead()
    assert lead is not None
    assert lead["entity"] == "Equinix", \
        "offered an operator named in recent post text — stalemate is back"
