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


# ── upstream-intermittent streams: a longer leash, never immunity ────────────
# 2026-08-10. The iso domain is judged on the WORST of 109 streams, so EU_BG —
# tagged INTERMITTENT in routes/iso_eu_entsoe.py's own zone registry — pinned
# the domain (and the surveillance sweep) red at 8h37m against a 4h target
# while the other 108 streams, every ENTSO-E sibling included, had updated 9
# minutes earlier. Nothing was broken; Bulgaria had simply not published.

def test_intermittent_stream_does_not_set_the_worst():
    import routes.freshness_public as fp
    out = fp.summarize_stream_ages(
        [("EU_BG", 8.6), ("EU_FR", 0.2), ("EU_DE_LU", 0.2)],
        table="grid_data", col="timestamp", stream="iso",
        intermittent={"EU_BG"})
    assert out["age_hours"] == 0.2, \
        "a known-intermittent stream must not become the domain's worst"
    assert out["worst_object"] != "grid_data:EU_BG"


def test_intermittent_stream_is_still_reported():
    """Excluded from the verdict, never hidden from the reader."""
    import routes.freshness_public as fp
    out = fp.summarize_stream_ages(
        [("EU_BG", 8.6), ("EU_FR", 0.2)],
        table="grid_data", col="timestamp", stream="iso",
        intermittent={"EU_BG"})
    assert [s["stream"] for s in out["intermittent_streams"]] == ["EU_BG"]
    assert any(s["stream"] == "EU_BG" for s in out["per_stream"])
    assert "intermittent" in out["basis"]


def test_intermittent_stream_still_breaches_once_truly_dead():
    """The leash is bounded. Past _INTERMITTENT_MAX_H it is judged like any
    other stream — otherwise this list becomes a place to hide a dead feed,
    which is the exact failure the worst-stream rule exists to prevent."""
    import routes.freshness_public as fp
    dead = fp._INTERMITTENT_MAX_H + 1.0
    out = fp.summarize_stream_ages(
        [("EU_BG", dead), ("EU_FR", 0.2)],
        table="grid_data", col="timestamp", stream="iso",
        intermittent={"EU_BG"})
    assert out["age_hours"] == dead, \
        "past the cap an intermittent stream must set the worst again"
    assert out["worst_object"] == "grid_data:EU_BG"


def test_all_streams_intermittent_still_yields_a_verdict():
    """Degenerate case: if the leash empties the judgement set there is nothing
    left to be right about, so judge them all rather than return a fake 0."""
    import routes.freshness_public as fp
    out = fp.summarize_stream_ages(
        [("EU_BG", 8.6)], table="grid_data", col="timestamp", stream="iso",
        intermittent={"EU_BG"})
    assert out["age_hours"] == 8.6


def test_every_intermittent_entry_is_annotated_at_its_own_source_registry():
    """Guard against this becoming a dumping ground for inconvenient feeds.

    ★ 2026-09-03 — this used to assert `entries == {"EU_BG"}`. That pinned the
      COUNT when the rule it exists to enforce is the SENTENCE beside it:
      "every entry must be annotated INTERMITTENT at its own source registry".
      A hardcoded set cannot tell an entry that earned its place from one
      somebody dropped in — it just fails on both, and gets edited to whatever
      the new set is, which is a fence that teaches you to move it.

      So it now enforces the actual invariant, BOTH directions, against
      routes/iso_eu_entsoe._ZONE_REGISTRY — the source registry the sentence
      names. A new entry is impossible to add without documenting it where the
      zone is defined, and a zone annotated there but missing from the leash is
      caught too (the half-registration that silently does nothing).

      EU_DK_1 / EU_DK_2 / EU_GR / EU_IE_SEM joined on 2026-09-03, each probed
      live: all four answer HTTP 200 with an Acknowledgement_MarketDocument
      (ENTSO-E for "nothing published"), against EU_BE returning a
      GL_MarketDocument with real fuel data as the control."""
    import routes.freshness_public as fp
    from routes.iso_eu_entsoe import _ZONE_REGISTRY

    leashed = {s for v in fp._INTERMITTENT_STREAMS.values() for s in v}
    annotated = {"EU_" + row[0] for row in _ZONE_REGISTRY
                 if "INTERMITTENT" in str(row[-1]).upper()}

    undocumented = leashed - annotated
    assert not undocumented, (
        f"{undocumented} got a longer leash without being annotated "
        f"INTERMITTENT in routes/iso_eu_entsoe._ZONE_REGISTRY — document the "
        f"evidence at the zone, or take it off the leash")
    orphaned = annotated - leashed
    assert not orphaned, (
        f"{orphaned} are annotated INTERMITTENT at the registry but are NOT in "
        f"_INTERMITTENT_STREAMS, so the annotation does nothing — a half-"
        f"registration reads as handled and is not")
    assert "EU_BG" in leashed, "the original entry must survive"


def test_intermittent_streams_reach_the_domain_payload():
    """Shipped and immediately caught by reading the live payload: the
    summariser computed `intermittent_streams` and _domain_entries never copied
    it, so /api/v1/freshness advertised "excludes 1 upstream-intermittent
    stream" in the basis string with no way to see WHICH. An exclusion you
    cannot enumerate is a silent one."""
    import routes.freshness_public as fp
    real = fp.summarize_stream_ages(
        [("EU_BG", 9.4), ("EU_FR", 0.2)],
        table="grid_data", col="timestamp", stream="iso",
        intermittent={"EU_BG"})
    assert real.get("intermittent_streams"), "summariser must produce the field"
    src = open(fp.__file__).read()
    assert 'entry["intermittent_streams"] = real["intermittent_streams"]' in src, \
        "the domain entry must copy intermittent_streams through to the payload"
