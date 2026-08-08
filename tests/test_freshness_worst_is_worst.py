"""GUARD — /api/v1/freshness must publish the WORST stream age, never the freshest.

The defect (live 2026-08-08T03:15Z): the `iso` domain's real-data age came from
`SELECT MAX(timestamp) FROM grid_data` — one row per (iso, timestamp, metric),
so MAX() is the single FRESHEST row in the table. One live ISO made the whole
domain read green regardless of how many other ISO feeds had stopped:

    "iso": {"worst_age_hours": 0.11,                 <- the freshest row
            "surface_worst_age_hours": 1653.93,
            "worst_surface": "/sitemap-grids.xml",   <- a different object
            "status": "within_sla"}

Pure tests — no database. Mutation-tested; see the module docstring in
routes/freshness_public.py for the field contract.
"""
import routes.freshness_public as fp


def test_worst_is_the_oldest_stream_not_the_freshest():
    """THE regression. Six live ISOs and one dead one must report the DEAD
    one's age, not the live one's."""
    rows = [("PJM", 0.11), ("ERCOT", 0.4), ("CAISO", 0.6), ("MISO", 0.9),
            ("SPP", 1.2), ("NYISO", 1.4), ("ISONE", 1653.93)]
    out = fp.summarize_stream_ages(rows, table="grid_data", col="timestamp",
                                   stream="iso")
    assert out["age_hours"] == 1653.93
    assert out["freshest_age_hours"] == 0.11
    assert out["worst_object"] == "grid_data:ISONE"
    assert out["streams_total"] == 7


def test_one_live_stream_cannot_mask_the_rest():
    """The failure this endpoint exists to catch: everything dead but one."""
    rows = [("PJM", 0.1)] + [(iso, 500.0) for iso in
                             ("ERCOT", "CAISO", "MISO", "SPP", "NYISO", "ISONE")]
    out = fp.summarize_stream_ages(rows, table="grid_data", col="timestamp",
                                   stream="iso")
    assert out["age_hours"] == 500.0, "a single fresh feed must not clear the domain"


def test_worst_object_names_the_stream_the_age_came_from():
    """worst_age_hours and the object beside it must describe the SAME thing —
    the pairing that was broken."""
    rows = [("PJM", 2.0), ("ERCOT", 91.5)]
    out = fp.summarize_stream_ages(rows, table="grid_data", col="timestamp",
                                   stream="iso")
    assert out["worst_object"].endswith(":ERCOT")
    worst = max(s["age_hours"] for s in out["per_stream"])
    assert out["age_hours"] == worst


def test_per_stream_is_sorted_worst_first_and_complete():
    rows = [("A", 1.0), ("B", 9.0), ("C", 5.0)]
    out = fp.summarize_stream_ages(rows, table="t", col="ts", stream="k")
    assert [s["stream"] for s in out["per_stream"]] == ["B", "C", "A"]
    assert len(out["per_stream"]) == 3


def test_null_ages_are_counted_not_silently_dropped():
    rows = [("A", 3.0), ("B", None), ("C", None)]
    out = fp.summarize_stream_ages(rows, table="t", col="ts", stream="k")
    assert out["age_hours"] == 3.0
    assert out["streams_total"] == 1
    assert out["streams_unrated"] == 2


def test_no_rated_streams_returns_none_rather_than_a_false_zero():
    assert fp.summarize_stream_ages([], table="t", col="ts", stream="k") is None
    assert fp.summarize_stream_ages([("A", None)], table="t", col="ts",
                                    stream="k") is None


def test_basis_string_says_it_is_the_worst():
    out = fp.summarize_stream_ages([("A", 1.0), ("B", 2.0)], table="grid_data",
                                   col="timestamp", stream="iso")
    basis = out["basis"].lower()
    assert "worst" in basis and "oldest" in basis
    assert "grid_data" in basis


def test_iso_domain_is_declared_multi_stream():
    """grid_data holds independent per-ISO feeds, so the iso domain MUST carry
    a stream column — without it the query collapses back to MAX() over the
    whole table, which is the defect."""
    spec = fp._DOMAIN_SOURCE["iso"]
    assert len(spec) == 3, "iso must declare (table, ts_col, stream_col)"
    assert spec[0] == "grid_data"
    assert spec[2] == "iso", "iso domain must be grouped per ISO feed"


def test_append_only_domains_declare_no_stream_and_say_why():
    """news/press/mna are append-only: 'when did anything last arrive' IS the
    measure. They must declare stream=None and must not claim to be a worst."""
    for dom in ("news", "press", "mna"):
        assert fp._DOMAIN_SOURCE[dom][2] is None
    single = fp._SINGLE_BASIS.format(col="published_at", table="news_articles")
    assert "not a worst-case" in single.lower()
