"""r-discovered-country (2026-08-07) — guards for the discovered_facilities
country repair in routes/facility_geo_quality.py.

Every NEGATIVE case below is a false positive that a simpler version of this
repair actually produced when it was run against the live table. They are not
hypotheticals, and a change that "simplifies" a guard away will re-introduce
the exact bug the repair exists to fix: a facility silently disappearing from
its own market's DCPI page.
"""
import routes.facility_geo_quality as gq


def _row(rid, name, city, state, cc, lat=None, lon=None):
    return {"id": rid, "name": name, "city": city, "state": state,
            "cc": cc, "lat": lat, "lon": lon, "dup": None}


def _filler(start, city, cc, lat, lon, n=12):
    """n coordinate-bearing rows in one city — the population both the
    neighbour vote and the city gazetteer are computed from."""
    return [_row(start + i, "%s DC %d" % (city, i), city, "", cc,
                 lat + i * 0.001, lon + i * 0.001) for i in range(n)]


def _verdict(scan, rid):
    for r in scan["fixes"]:
        if r["id"] == rid:
            return r["to"]
    return None


# --------------------------------------------------------------------------
# POSITIVE: the three sub-classes the repair must catch
# --------------------------------------------------------------------------
def test_rule_a_coords_outside_tagged_box_are_relabeled():
    """A UK facility stamped country='US' — 44 real rows looked like this."""
    rows = _filler(1000, "London", "GB", 51.50, -0.12)
    rows.append(_row(1, "Bloomberg Datacentre", "London", "", "US", 51.4954, -0.0190))
    s = gq._df_classify(rows)
    assert _verdict(s, 1) == "GB", s["fixes"]


def test_rule_b_border_case_bbox_cannot_see():
    """Equinix TR2: city='Toronto', state='NY', country='US', coordinates in
    downtown Toronto. 43.65N/-79.36W is INSIDE the US bounding box (24-49N,
    125-66W), so the box cannot disprove the label. Neighbour + city vote can."""
    la, lo = 43.6509, -79.3617
    assert gq.BBOX["US"][0] <= la <= gq.BBOX["US"][1], "US box must contain Toronto"
    assert gq.BBOX["US"][2] <= lo <= gq.BBOX["US"][3], "US box must contain Toronto"
    rows = _filler(2000, "Toronto", "CA", 43.65, -79.38)
    rows.append(_row(2, "Equinix TR2", "Toronto", "NY", "US", la, lo))
    s = gq._df_classify(rows)
    assert _verdict(s, 2) == "CA", s["fixes"]


def test_rule_c_no_coordinates_at_all():
    """DREAM CLOUD Tokyo #1 has NULL lat/lon, so no coordinate check reaches
    it. The city gazetteer is the only evidence available."""
    rows = _filler(3000, "Tokyo", "JP", 35.68, 139.76)
    rows.append(_row(3, "DREAM CLOUD Tokyo #1", "Tokyo", "", "US"))
    s = gq._df_classify(rows)
    assert _verdict(s, 3) == "JP", s["fixes"]


# --------------------------------------------------------------------------
# NEGATIVE: each one was a measured false positive
# --------------------------------------------------------------------------
def test_melbourne_florida_is_not_australian():
    """THE namesake control. 'SD Data Center', city='Melbourne', state='FL',
    28.26N/-80.69W is Melbourne FLORIDA. Its absence from /dcpi/melbourne (the
    AEMO/VIC market) is r-namesake working correctly, not a defect. A city
    gazetteer that outranked coordinates would relabel it AU."""
    rows = _filler(4000, "Melbourne", "AU", -37.81, 144.96, n=57)
    rows += _filler(4100, "Melbourne", "US", 28.259, -80.694, n=2)
    rows.append(_row(4, "SD Data Center", "Melbourne", "FL", "US", 28.2591, -80.6948))
    s = gq._df_classify(rows)
    assert _verdict(s, 4) is None, "Melbourne FL must stay US"


def test_kaliningrad_stays_russian():
    """A Russian exclave at 20.5E sits OUTSIDE the RU box (which starts at
    27E), so rule A 'disproves' a correct label. The _AUTOFIX_FROM guard is
    what saves it: 'RU' was set deliberately, only the bulk 'US' default is
    outvoted automatically."""
    rows = _filler(5000, "Warsaw", "PL", 52.22, 21.01)
    rows.append(_row(5, "TiS-Dialog LLC", "Kaliningrad", "", "RU", 54.7102, 20.5172))
    s = gq._df_classify(rows)
    assert _verdict(s, 5) is None, "must not auto-relabel a deliberate tag"
    assert any(r["id"] == 5 for r in s["review"]), "should surface for review"


def test_country_with_no_bbox_is_never_relabeled():
    """Trinidad has no box of its own, so its coordinates land inside
    Venezuela's and read as a confident single match. 41 rows looked like
    this (TT, RW, SV, MW, BI, XK, BS, CG, SD)."""
    assert "TT" not in gq.BBOX, "test is only meaningful while TT has no box"
    rows = _filler(6000, "Caracas", "VE", 10.49, -66.90)
    rows.append(_row(6, "Trinidad DC", "Port of Spain", "", "TT", 10.65, -61.51))
    s = gq._df_classify(rows)
    assert _verdict(s, 6) is None
    assert not any(r["id"] == 6 for r in s["review"])


def test_windsor_ontario_is_not_detroit():
    """Goyeau Data Centre, Windsor ON: 31 of its 32 neighbours within 25 km
    are in Detroit, so the neighbour vote alone says US. The city vote vetoes
    it. Without this the repair relabels Canadian rows American — the very
    defect it exists to fix, in reverse."""
    rows = _filler(7000, "Detroit", "US", 42.33, -83.05, n=31)
    rows += _filler(7100, "Windsor", "CA", 42.317, -83.03, n=6)
    rows.append(_row(7, "Goyeau Data Centre", "Windsor", "ON", "CA", 42.3149, -83.0364))
    s = gq._df_classify(rows)
    assert _verdict(s, 7) is None, "Windsor ON must stay CA"


def test_johor_bahru_is_not_singapore():
    """Ten rows with city='Singapore' tagged MY are genuinely mislabeled; six
    genuine Johor Bahru rows 20 km away are not. They sit inside the same box
    and share the same neighbours, so ONLY the city vote tells them apart.

    Note where each one lands: the Singapore row is DETECTED but routed to
    `review`, not `fixes`, because 'MY' is a deliberate tag rather than the
    bulk 'US' default. The Johor Bahru row is not detected at all."""
    rows = _filler(8000, "Singapore", "SG", 1.35, 103.82, n=80)
    rows += _filler(8100, "Johor Bahru", "MY", 1.4655, 103.7578, n=6)
    good = _row(8, "Starhub Data Center @ Loyang", "Singapore", "", "MY", 1.3745, 103.9652)
    keep = _row(9, "Open DC JB1", "Johor Bahru", "Johor", "MY", 1.4629, 103.7717)
    s = gq._df_classify(rows + [good, keep])
    assert _verdict(s, 8) is None, "a deliberate tag is never auto-applied"
    assert [r["to"] for r in s["review"] if r["id"] == 8] == ["SG"], s["review"]
    assert not any(r["id"] == 9 for r in s["review"]), "Johor Bahru must stay MY"
    assert _verdict(s, 9) is None, "Johor Bahru must stay MY"


def test_scraped_page_titles_are_not_relabeled():
    """source='providerwebsites' scraped Equinix site navigation into 300+
    rows like name='Chicago', city='London', coords (0,0) — page titles, not
    facilities. Their country is the one field that is RIGHT; the broken field
    is city. Without the name-is-a-city guard, rule C flipped 22 of them
    US->GB."""
    rows = _filler(9000, "London", "GB", 51.50, -0.12, n=20)
    rows += _filler(9100, "Chicago", "US", 41.88, -87.63, n=20)
    rows.append(_row(10, "Chicago", "London", "", "US", 0.0, 0.0))
    s = gq._df_classify(rows)
    assert _verdict(s, 10) is None, "a scraped page title must not be relabeled"


def test_zero_zero_is_unknown_not_the_gulf_of_guinea():
    assert gq._df_usable(0.0, 0.0) is False
    assert gq._df_usable(None, None) is False
    assert gq._df_usable(51.5, -0.12) is True


def test_a_row_does_not_vote_on_its_own_label():
    """The city vote discounts the row's OWN tagged country, so a mislabeled
    row cannot dilute the evidence against itself.

    Tuned to straddle the 0.90 threshold, which is the only place the discount
    changes an outcome: 17 Toronto rows tagged CA and 2 tagged US. For one of
    the US rows the city vote is 17/19 = 0.895 (silent) if it counts itself,
    and 17/18 = 0.944 (CA) if it does not. Both rows sit INSIDE the US box, so
    rule A cannot see them and only this vote decides."""
    rows = _filler(12000, "Toronto", "CA", 43.65, -79.38, n=17)
    rows += [_row(200, "Twin A", "Toronto", "NY", "US", 43.6509, -79.3617),
             _row(201, "Twin B", "Toronto", "NY", "US", 43.6512, -79.3620)]
    assert gq.BBOX["US"][0] <= 43.65 <= gq.BBOX["US"][1]
    s = gq._df_classify(rows)
    assert _verdict(s, 200) == "CA", s
    assert _verdict(s, 201) == "CA", s
