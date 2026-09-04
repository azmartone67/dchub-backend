"""r-latam-twin (2026-09-03) — the .json twin must answer for a market that
has no NARRATIVE.

Measured live 2026-09-03, before this change:

    /markets/bogota       HTML 200   .json 404 {"error":"unknown_market"}
    /markets/mexico-city  HTML 200   .json 404
    /markets/santiago     HTML 200   .json 404
    /markets/sao-paulo    HTML 200   .json 404

All four are in CURATED_MARKET_SLUGS, all four are emitted into
sitemap-markets.xml, and all four carry real tracked inventory (40 / 31 / 102
/ 55 facilities). They 404'd because market_entity_json read its stats out of
market_deep_dives — the narrative table — and none of them has a row there:
cron_rotate targets market_power_scores rows and these four have none, so no
nightly could ever reach them.

A missing narrative is a publication state. The measurement is not, and the
twin is the measurement.

These are BEHAVIOURAL tests: they call the functions with a fake cursor. A
source-grep would pass against a route that still 404s.
"""
import routes.market_deep_dive as M


class FakeCursor:
    """Minimal cursor: returns a canned row for the fleet-count query."""

    def __init__(self, row, boom=False):
        self._row, self._boom, self.sql, self.args = row, boom, None, None

    def execute(self, sql, args=None):
        if self._boom:
            raise RuntimeError("db is down")
        self.sql, self.args = sql, args

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.closed = False

    def cursor(self, *a, **k):
        return self._cur

    def close(self):
        self.closed = True


class TestMeasuredFacts:
    def test_it_counts_a_market_with_no_market_power_scores_row(self):
        cur = FakeCursor((40, 0.0))
        out = M.measured_market_facts(cur, "Bogota", slug="bogota")
        assert out == {"facility_count": 40}, out

    def test_a_zero_capacity_is_OMITTED_not_reported_as_0_MW(self):
        """Bogota's PeeringDB rows carry power_mw NULL. Publishing '0 MW'
        would be indistinguishable from a real reading of zero."""
        out = M.measured_market_facts(FakeCursor((40, 0.0)), "Bogota")
        assert "total_mw" not in out

    def test_a_real_capacity_IS_reported(self):
        out = M.measured_market_facts(FakeCursor((55, 1234.5)), "São Paulo")
        assert out["facility_count"] == 55
        assert out["total_mw"] == 1234.5

    def test_no_rows_is_None_never_a_zero_filled_measurement(self):
        """The #1546 / r-nova-zero shape: a market we merely failed to key on
        must not publish '0 facilities'."""
        assert M.measured_market_facts(FakeCursor((0, 0.0)), "Nowhere") is None

    def test_a_db_error_is_None_not_a_zero(self):
        assert M.measured_market_facts(FakeCursor(None, boom=True), "X") is None

    def test_the_accent_folded_spelling_is_searched(self):
        """Facilities are stored city='Sao Paulo' (ASCII) while the market
        name carries the accent. If the fold is dropped the count silently
        goes to zero."""
        cur = FakeCursor((55, 10.0))
        M.measured_market_facts(cur, "São Paulo", slug="sao-paulo")
        assert "sao paulo" in cur.args["names"]

    def test_it_reuses_the_briefs_union_rather_than_forking_a_new_query(self):
        cur = FakeCursor((1, 1.0))
        M.measured_market_facts(cur, "Bogota")
        assert cur.sql.startswith(M._FAC_UNION_SQL)


class TestTwinResolution:
    def _stub_conn(self, monkeypatch, cur):
        monkeypatch.setattr(M, "_conn", lambda: FakeConn(cur))

    def test_a_curated_market_with_inventory_resolves(self, monkeypatch):
        self._stub_conn(monkeypatch, FakeCursor((40, 0.0)))
        name, stats = M._twin_facts_without_a_brief("bogota")
        assert name == "Bogota"
        assert stats == {"facility_count": 40}

    def test_a_curated_market_the_join_MISSES_still_serves_200(self, monkeypatch):
        """Its HTML page serves 200. A twin that 404s for a page that 200s is
        exactly the drift the entity work exists to remove."""
        self._stub_conn(monkeypatch, FakeCursor((0, 0.0)))
        _name, stats = M._twin_facts_without_a_brief("santiago")
        assert stats == {}, "curated slug must resolve, with no invented numbers"

    def test_a_junk_slug_is_still_a_404(self, monkeypatch):
        """r-soft404 must not reopen: this fallback is not a 200-for-anything."""
        self._stub_conn(monkeypatch, FakeCursor((0, 0.0)))
        _name, stats = M._twin_facts_without_a_brief("not-a-market-at-all")
        assert stats is None

    def test_every_curated_sitemapped_slug_resolves(self, monkeypatch):
        """The sitemap emits all of CURATED_MARKET_SLUGS unconditionally, so
        none of them may be a twin 404."""
        self._stub_conn(monkeypatch, FakeCursor((0, 0.0)))
        dead = [s for s in M.CURATED_MARKET_SLUGS
                if M._twin_facts_without_a_brief(s)[1] is None]
        assert dead == [], f"sitemapped but twin-404: {dead}"


class TestTheEntityItBuilds:
    def test_it_publishes_the_count_and_INVENTS_no_dcpi_score(self):
        from util.market_entity import market_entity
        e = market_entity("bogota", "Bogota", {"facility_count": 40},
                          canonical_slug="bogota")
        names = [v["name"] for v in e["variableMeasured"]]
        assert names == ["Facilities"]
        assert "DCPI Score" not in names, "these markets have no DCPI row"

    def test_no_brief_means_no_fabricated_dateModified(self):
        from util.market_entity import market_entity
        e = market_entity("bogota", "Bogota", {"facility_count": 40})
        assert "dateModified" not in e


class TestTheShellStopsVouchingForNumbersItLacks:
    def test_the_placeholder_dash_is_not_a_reading(self):
        assert M._has_metric("—") is False
        assert M._has_metric(None) is False
        assert M._has_metric(40) is True


class TestTheROUTEActuallyServesIt:
    """The tests above exercise the resolver. This one drives the real Flask
    route — without it, unwiring market_entity_json from the resolver leaves
    the whole file green while /markets/bogota.json 404s in production
    (verified: that mutation passed 14/14 before this class existed).
    """

    def _client(self, monkeypatch, *, brief=None, row=(40, 0.0)):
        import flask
        monkeypatch.setattr(M, "read_deep_dive", lambda s: brief)
        monkeypatch.setattr(M, "_conn", lambda: FakeConn(FakeCursor(row)))
        app = flask.Flask(__name__)
        app.register_blueprint(M.market_deep_dive_bp)
        return app.test_client()

    def test_a_curated_market_with_no_brief_serves_200_json_ld(self, monkeypatch):
        r = self._client(monkeypatch).get("/markets/bogota.json")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert "ld+json" in r.headers["Content-Type"]
        body = r.get_json()
        assert body["@type"] == "Dataset"
        assert body["identifier"] == "bogota"
        by = {v["name"]: v["value"] for v in body["variableMeasured"]}
        assert by == {"Facilities": 40}

    def test_it_points_at_the_html_page_that_serves(self, monkeypatch):
        r = self._client(monkeypatch).get("/markets/bogota.json")
        assert r.get_json()["url"].endswith("/markets/bogota")
        assert 'rel="canonical"' in r.headers["Link"]

    def test_it_stamps_NO_dateModified_when_there_is_no_brief(self, monkeypatch):
        body = self._client(monkeypatch).get("/markets/bogota.json").get_json()
        assert "dateModified" not in body

    def test_an_unknown_slug_is_STILL_404(self, monkeypatch):
        c = self._client(monkeypatch, row=(0, 0.0))
        r = c.get("/markets/definitely-not-a-market.json")
        assert r.status_code == 404
        assert r.get_json()["error"] == "unknown_market"

    def test_all_four_measured_404s_now_serve(self, monkeypatch):
        c = self._client(monkeypatch)
        bad = [s for s in ("bogota", "mexico-city", "santiago", "sao-paulo")
               if c.get(f"/markets/{s}.json").status_code != 200]
        assert bad == [], f"still 404: {bad}"

    def test_a_market_WITH_a_brief_is_unchanged(self, monkeypatch):
        import datetime
        brief = {"market_slug": "chicago", "market_name": "Chicago",
                 "key_stats": {"facility_count": 120, "total_mw": 900.0,
                               "dcpi_score": 71},
                 "generated_at": datetime.datetime(2026, 9, 1, 12, 0, 0)}
        r = self._client(monkeypatch, brief=brief).get("/markets/chicago.json")
        assert r.status_code == 200
        body = r.get_json()
        assert body["dateModified"].startswith("2026-09-01")
        assert {v["name"] for v in body["variableMeasured"]} == {
            "Total Capacity", "Facilities", "DCPI Score"}


class TestTheNoteNamesOnlyWhatItShows:
    """r-note-precision (2026-09-04). Measured live after the first fix:

        /markets/bogota   tiles=[Facilities 53]   (no Inventory tile)
        note: "...facility counts AND CAPACITY above are live..."

    The count was real; the capacity was not on the page at all. Vouching for
    an absent number is the same defect the first pass removed, one notch
    smaller — the gate accepted ANY fleet tile.

    Drives the REAL renderer. Every case passes a truthy MARKET_DATA row so the
    request reaches the shell rather than the soft-404 branch — a 404 body has
    no note at all, and asserting "no false claim" against an empty string is
    vacuous.
    """

    def _note(self, monkeypatch, md):
        import sys, types, re, flask
        # the route imports main unconditionally for MARKET_ALIASES; the real
        # module is a 31k-line import that opens DB connections.
        stub = types.ModuleType("main")
        stub.MARKET_ALIASES = {}
        stub.RAILWAY_EXCLUSION = ""
        monkeypatch.setitem(sys.modules, "main", stub)
        monkeypatch.setattr(M, "_render_deep_dive_body", lambda s: None)
        monkeypatch.setattr(M, "_conn", lambda: None)
        import market_intelligence_api as MI
        monkeypatch.setattr(MI, "MARKET_DATA", {"Bogota": dict(md)}, raising=False)
        app = flask.Flask(__name__)
        app.register_blueprint(M.market_deep_dive_bp)
        r = app.test_client().get("/markets/bogota")
        assert r.status_code == 200, f"expected the shell, got {r.status_code}"
        html = r.get_data(as_text=True)
        m = re.search(r'<p class="note">(.*?)</p>', html, re.S)
        assert m, "no note rendered — nothing to assert about"
        return re.sub(r"\s+", " ", m.group(1))

    def test_a_count_with_no_capacity_does_not_claim_capacity(self, monkeypatch):
        n = self._note(monkeypatch, {"region": "LATAM", "num_facilities": 53})
        assert "facility count above is live" in n
        assert "capacity" not in n, f"vouched for capacity it never showed: {n}"

    def test_both_present_claims_both(self, monkeypatch):
        n = self._note(monkeypatch,
                       {"region": "LATAM", "num_facilities": 53, "inventory_mw": 92})
        assert "facility counts and capacity above are live" in n

    def test_capacity_alone_does_not_claim_a_count(self, monkeypatch):
        n = self._note(monkeypatch, {"region": "LATAM", "inventory_mw": 92})
        assert "capacity above is live" in n
        assert "facility count" not in n

    def test_neither_present_vouches_for_nothing(self, monkeypatch):
        n = self._note(monkeypatch, {"region": "LATAM"})
        assert "research coverage yet" in n, "the note itself must still render"
        assert "live from our infrastructure database" not in n, (
            f"claimed live fleet data with no fleet tile rendered: {n}")
