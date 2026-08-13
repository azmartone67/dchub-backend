"""
util/hifld_layers.py — 2026-08-12, SH52-057. ONE definition of the HIFLD
ArcGIS layers, because two modules in this repo disagreed about which
transmission layer is real and the smaller one won in the lane nobody watched.

WHY THIS MODULE EXISTS
──────────────────────
Electric_Power_Transmission_Lines is published under that same service name by
more than one ArcGIS org, and the orgs do NOT serve the same population.
Measured live 2026-08-12:

    services5/HDRa0B57OVrv2E1q   89,744 features   ← MAINTAINED
    services1/Hp6G80Pky0om7QvQ   52,244 features   ← superseded

land_power_crawler.py had already worked this out: it validates candidates
against a 70,000-row floor precisely to keep the 52,244 layer OUT, and its
comment says so in as many words. But infrastructure_discovery.py carried its
own hardcoded URL pointing at exactly the layer the crawler rejects, so the
fiber-route discovery lane was reading a superseded population — 37,500 fewer
lines — while the module next door asserted the opposite.

Two hardcoded URLs cannot disagree if there is only one URL. That is all this
module is.

★ THE CANONICAL CHOICE IS services5, AND IT IS SAFE TO REPOINT ONTO IT.
The two layers share ONE id space, which is the only reason this is a config
change rather than a migration. Verified against the live layers and the live
fiber_routes table 2026-08-12:

    60/60  sampled services1 IDs exist on services5
    1,706 / 1,742  upstream_uids already held by fiber_routes (source='hifld')
                   resolve on services5 (97.9%)

So the SH52-054 identity (fiber_routes.upstream_uid, arbitrated by the partial
unique index on (source, upstream_uid)) keeps matching after the swap: a line
we already hold is still recognised as the same line and discarded by
ON CONFLICT DO NOTHING. Repointing does NOT re-insert the held rows.

★★ THE 36 THAT DO NOT RESOLVE. 36 held uids are absent from services5 — real
in-service lines (Dominion, Portland General, Oncor) that the maintained layer
simply does not carry. Nothing deletes them: they stay published exactly as
they are and merely stop being re-seen. Stated here because "97.9%" is the
honest number and a later reader will otherwise re-derive this the hard way.
Neither layer is a strict superset of the other; services5 is chosen because
it is the maintained one, it is 72% larger, and it is the one the crawler
already validates.

★★★ NOT THE SAME THING AS transmission_lines. The `transmission_lines` table
(94,626 rows) is fed by routes/transmission_ingest.py from the EIA org
(FiaPA4ga0iQKduv3, US_Electric_Power_Transmission_Lines) — a THIRD service,
with its own id space. This module governs the HIFLD ArcGIS layers that are
queried SPATIALLY (point + radius) by the discovery lanes. Do not point the
EIA ingest here, and do not point these lanes at the EIA service, without
first checking what it does to upstream_uid — see util/transmission_tables.py.
"""

# Declarative only — no HTTP, no DB, no import-time work. Callers that need
# candidate resolution (row floor + required fields) use the resolver in
# land_power_crawler; callers that just need the preferred URL use layer_url().
HIFLD_LAYERS = {
    'hifld-substations': {
        'label': 'HIFLD electric substations',
        # live 75,328 (2026-07-31); floor well below that so a partial refresh
        # upstream does not hard-fail the crawl, but a decoy layer cannot pass.
        'min_rows': 40000,
        'required_fields': ('ID', 'NAME', 'STATE', 'COUNTY', 'CITY',
                            'MAX_VOLT', 'MIN_VOLT', 'TYPE', 'STATUS'),
        'candidates': (
            # verified 2026-07-31: 75,328 points, 59 states/provinces,
            # every required field present
            'https://services5.arcgis.com/HDRa0B57OVrv2E1q/ArcGIS/rest/'
            'services/Electric_Substations/FeatureServer/0',
            # verified same day: valid FeatureServer, all fields — and only 128
            # rows. Kept DELIBERATELY as a live regression case for the row
            # floor: if the floor is ever removed this candidate silently wins
            # on a day the first one is down.
            'https://services.arcgis.com/G4S1dGvn7PIgYd6Y/ArcGIS/rest/'
            'services/HIFLD_electric_power_substations/FeatureServer/0',
        ),
    },
    'hifld-transmission': {
        'label': 'HIFLD electric power transmission lines',
        # live 89,744 (re-verified 2026-08-12) against a maintained
        # transmission_lines table of 94,626 — same population. The
        # 52,244-feature layer on services1/Hp6G80Pky0om7QvQ is NOT, and the
        # floor is what keeps it out. That superseded layer is deliberately
        # NOT listed as a fallback candidate: falling back to it would be the
        # silent population swap this whole module exists to prevent.
        'min_rows': 70000,
        'required_fields': ('ID', 'OWNER', 'VOLTAGE', 'SUB_1', 'SUB_2',
                            'TYPE', 'STATUS'),
        'candidates': (
            'https://services5.arcgis.com/HDRa0B57OVrv2E1q/ArcGIS/rest/'
            'services/Electric_Power_Transmission_Lines/FeatureServer/0',
        ),
    },
}


def layer_url(key):
    """The preferred (first) candidate URL for a layer.

    For callers that query the layer directly and do not run the full
    candidate resolution. It returns the SAME url the resolver would pick on a
    healthy day, so the two paths cannot drift onto different populations.

    Raises KeyError on an unknown key rather than returning None — a caller
    that typos the key must fail loudly, not fall through to querying ''.
    """
    return HIFLD_LAYERS[key]['candidates'][0]
