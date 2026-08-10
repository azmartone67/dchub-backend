"""The canonical-floor fence must measure the SAME population as the floor.

2026-08-09. `check_canonical_floor_exceeds_live` fenced
`ai_surface_canon.PINNED['public']['facilities']` against its own ad-hoc query
(`COUNT(*) FROM discovered_facilities WHERE duplicate_of_id IS NULL` = 15,565)
instead of against canon (`COUNT(DISTINCT canonical_slug) WHERE
COALESCE(is_duplicate,0)=0` = 17,260) — the number every public surface
publishes and the number the floor was set from.

A floor of 17,000 is correctly BELOW live canon. The fence convicted it of
over-claiming by 1,435 and fired 1,436 times. A fence that measures a narrower
population than the floor it guards manufactures over-claims.
"""
import re
import pathlib
import pytest

RADAR = pathlib.Path(__file__).resolve().parents[1] / "routes" / "brain_consistency_radar.py"


def _pinned_block() -> str:
    """The PINNED['public'] half of the fence, as CODE — comments stripped.

    ★These assertions are about what the fence EXECUTES. The comments in that
    block necessarily quote the old query to explain why it was wrong, so an
    un-stripped grep convicts the documentation instead of the code.
    """
    src = RADAR.read_text(encoding="utf-8")
    start = src.index("the OTHER canon: ai_surface_canon.PINNED")
    end = src.index("return findings", start)
    code = [re.sub(r"#.*$", "", ln) for ln in src[start:end].splitlines()]
    return "\n".join(code)


def test_the_comment_stripper_actually_strips():
    """A stripper that silently returns everything makes every guard below
    vacuous — and these guards exist because a comment already fooled one."""
    assert "duplicate_of_id" in RADAR.read_text(encoding="utf-8")
    assert "#" not in _pinned_block()


def test_the_fence_does_NOT_re_derive_the_fleet_from_its_own_SQL():
    """The regression that let the fence drift away from the floor.

    Any `discovered_facilities` query inside this block is the fence inventing
    a second definition of the fleet. There must be exactly one definition and
    it must come from canonical_stats.
    """
    block = _pinned_block()
    assert "discovered_facilities" not in block, (
        "the PINNED fence re-derives the facility count from its own SQL; it "
        "must read canon (canonical_stats.facilities_verified) so the fence "
        "and the floor can never measure different populations again")


def test_the_fence_reads_facilities_from_canon():
    assert "facilities_verified" in _pinned_block()


def test_duplicate_of_id_is_not_used_as_a_count_filter_here():
    """`is_duplicate` is a VISIBILITY flag; `duplicate_of_id` is the
    consolidation pointer. A pointer-carrying row stays live, counted and
    serving 200 — filtering it out deletes ~1,983 distinct live slugs."""
    assert "duplicate_of_id" not in _pinned_block()


# ── behavioural: the fence's verdict, driven by canon ────────────────────────

class _Cur:
    """Cursor that answers only the markets query (the fence's one remaining
    SQL read). Any facilities SQL reaching here is itself the bug."""

    def __init__(self, sql_log):
        self._log = sql_log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self._log.append(sql)
        assert "discovered_facilities" not in sql, (
            "the fence issued a facilities query; it must use canon")

    def fetchone(self):
        return [10_000]      # markets, comfortably above any floor


class _Conn:
    def __init__(self, sql_log):
        self._log = sql_log

    def cursor(self):
        return _Cur(self._log)

    def close(self):
        pass


@pytest.fixture
def fence(monkeypatch):
    """Wire the fence up with a controllable canon + a stub DB."""
    import psycopg2
    import canonical_stats as cs
    import routes.mcp_honest_numbers as hn
    from routes.brain_consistency_radar import check_canonical_floor_exceeds_live

    sql_log: list = []
    monkeypatch.setattr(psycopg2, "connect",
                        lambda *a, **k: _Conn(sql_log), raising=False)

    def _run(*, facilities_verified, floor):
        fb = dict(cs._FALLBACK)
        # every non-facilities floor sits far below live so only the key under
        # test can produce a finding
        live = {k: (int(v) * 100 if str(v).isdigit() else v)
                for k, v in fb.items()}
        live["facilities_verified"] = facilities_verified
        monkeypatch.setattr(cs, "_query_live", lambda: live, raising=False)
        monkeypatch.setattr(hn, "as_dict",
                            lambda *a, **k: {"facilities": floor}, raising=False)
        return [f for f in check_canonical_floor_exceeds_live()
                if f.get("url") == "ai_surface_canon.PINNED.public"]

    return _run


def test_a_floor_BELOW_live_canon_is_not_convicted(fence):
    """The live case: floor 17,000 vs canon 17,260. This fired 1,436 times."""
    assert fence(facilities_verified=17_260, floor=17_000) == []


def test_a_floor_ABOVE_live_canon_still_convicts(fence):
    """The fence must not be defanged — a real over-claim still fires."""
    out = fence(facilities_verified=16_500, floor=17_000)
    assert len(out) == 1
    assert out[0]["issue"] == "canonical_floor_above_live_reality"
    assert out[0]["count"] == 500


def test_canon_equal_to_its_fallback_is_BLIND_not_a_verdict(fence, monkeypatch):
    """`_query_live` returns _FALLBACK verbatim on a DB outage. An unanswered
    read must omit the key, never convict the floor of exceeding a fallback."""
    import canonical_stats as cs
    assert fence(facilities_verified=int(cs._FALLBACK["facilities_verified"]),
                 floor=17_000) == []
