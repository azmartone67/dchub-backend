"""Route slug-composer delegation guards (r-routeslug, 2026-07-31).

The LAST three hand-compose sites outside main.py — routes/d1_sync.py (D1
mirror keys), routes/indexnow.py (IndexNow URL submitter, 2 sites) and
routes/facility_profile_page.py (comparable-facility links + legacy-301
targets) — composed `{provider_slug}-{name_slug}-{hash8}` WITHOUT the
provider-prefix dedupe + ascii folding the freeze stores, so every unfrozen
brand-prefixed row was keyed/submitted/linked under the doubled pre-dedupe
form (iron-mountain-iron-mountain-lon-3-…). All four sites now DELEGATE to
facility_slug_freeze (the one composer) and prefer the row's STORED
canonical_slug (probed via information_schema — live DDL can lag repo DDL),
completing the sweep #2015 (crawler surfaces) and #2016 (API emitters) began.

★ "THE LAST THREE" WAS WRONG. r-hubslug (2026-09-05) found a FOURTH copy —
  facilities_hub._fac_slug, whose own docstring claimed it was "byte-identical
  to the sitemap + live pages" and was neither: no dedupe, no ascii fold, and
  PR #3911's `len(name_slug) < 3` bug. 15% of the German hub's links were 301s.
  Its guard is tests/test_facilities_hub_stored_slug.py. A grep for the
  f-string compose form missed it because it composed with `stable_hash8` under
  a different local variable name — count copies by BEHAVIOUR, not by anchor.

Two deliberate asymmetries this file pins:
  • d1_sync delegates via frozen_slug_for_row — the D1 mirror KEY must equal
    the row's LIVE canonical URL, which for pre-dedupe-frozen rows is the
    stored doubled form, not today's builder output (forward-only freeze).
    Re-keying is safe: the upsert conflicts ON (id), prune is by synced_at.
  • facility_profile_page keeps its local _slugify: _resolve_legacy_slug
    MATCHES legacy indexed slugs, which were composed with the unconditional
    provider prefix — matching them against today's deduped form would break
    tier-0 for exactly the doubled-name population the resolver exists to
    save (validated 95% recovery / 0 mis-redirects). Matching stays on the
    pre-dedupe compose (_legacy_name_part); only EMITTED slugs delegate.

No DB, no network: source pinning + pure-function contract checks, same
style as tests/test_api_slug_compose_delegation.py. Deliberately does NOT
re-pin main.py's sites (r-apislug) or the r-lane5 surfaces — those keep
their own guards.
"""
import re
from pathlib import Path

import pytest

fsf = pytest.importorskip("routes.facility_slug_freeze")

ROOT = Path(__file__).resolve().parent.parent
D1 = (ROOT / "routes" / "d1_sync.py").read_text()
INX = (ROOT / "routes" / "indexnow.py").read_text()
PROF = (ROOT / "routes" / "facility_profile_page.py").read_text()

MARK = "r-routeslug (2026-07-31)"


def _regions(src, expect, label):
    """Delegation sites, each anchored on its own r-routeslug marker."""
    idxs = [m.start() for m in re.finditer(re.escape(MARK), src)]
    assert len(idxs) == expect, \
        f"expected {expect} r-routeslug sites in {label}, found {len(idxs)}"
    return [src[i: i + 1700] for i in idxs]


# ── every site delegates; no site hand-composes ────────────────────────────

def test_all_four_route_sites_delegate_to_the_freeze_composer():
    (d1,) = _regions(D1, 1, "routes/d1_sync.py")
    assert "from routes.facility_slug_freeze import frozen_slug_for_row" in d1, \
        "d1_sync mirror-key site does not import the freeze composer"
    assert "frozen_slug_for_row(" in d1, \
        "d1_sync mirror-key site does not call the freeze composer"
    for n, region in enumerate(_regions(INX, 2, "routes/indexnow.py"), 1):
        assert "from routes.facility_slug_freeze import build_canonical_slug" in region, \
            f"indexnow site {n} does not import the freeze builder"
        assert "build_canonical_slug(" in region, \
            f"indexnow site {n} does not call the freeze builder"
    (prof,) = _regions(PROF, 1, "routes/facility_profile_page.py")
    assert "from routes.facility_slug_freeze import build_canonical_slug" in prof, \
        "facility_profile_page emitter does not import the freeze builder"
    assert "build_canonical_slug(" in prof, \
        "facility_profile_page emitter does not call the freeze builder"


def test_old_fstring_compose_forms_are_gone():
    """The exact hand-compose forms the delegation replaced, plus the local
    slugify twins that carried them. facility_profile_page's _slugify is the
    one deliberate survivor — the legacy MATCHER needs the pre-dedupe form."""
    # d1_sync (old: f"{provider_slug}-{name_slug}-{short_hash}" / else-form)
    assert "{provider_slug}-{name_slug}-{short_hash}" not in D1
    assert "{name_slug}-{short_hash}" not in D1
    assert "def _slugify" not in D1, "local composer twin is back in d1_sync.py"
    assert "import stable_hash8" not in D1
    # indexnow twin sites (old: … -{short_hash} at the recent site, -{h8} at
    # the delta site, each with a no-provider else-form)
    assert "{name_slug}-{short_hash}" not in INX
    assert "{name_slug}-{h8}" not in INX
    assert "def _slugify" not in INX, "local composer twin is back in indexnow.py"
    assert "import stable_hash8" not in INX
    # facility_profile_page emitter (old: f"{ps}-{ns}-{h8}" if ps else …)
    assert "{ns}-{h8}" not in PROF
    assert "import stable_hash8" not in PROF
    assert "def _slugify" in PROF, \
        "_slugify must STAY — _legacy_name_part matches legacy slugs with it"


# ── stored canonical_slug preferred, probed not assumed ────────────────────

def test_d1_mirror_key_is_stored_first_and_rekey_safe():
    """The D1 mirror key must equal the row's LIVE canonical URL segment:
    stored canonical_slug first (selected only when the live column exists —
    probed via information_schema), else the freeze builder. Re-keying only
    stays safe while the upsert conflicts on id (slug rewritten in place)."""
    assert "canonical_slug" in D1
    assert "information_schema.columns" in D1, \
        "d1_sync must PROBE for canonical_slug, not assume live DDL"
    assert "ON CONFLICT(id) DO UPDATE" in D1, \
        "the id-keyed upsert is the no-orphans guarantee re-keying relies on"


def test_indexnow_prefers_stored_canonical_where_selected():
    for n, region in enumerate(_regions(INX, 2, "routes/indexnow.py"), 1):
        assert "or build_canonical_slug(" in region, \
            f"indexnow site {n} lost the stored-first fallback shape"
    assert INX.count("information_schema.columns") >= 2, \
        "both indexnow queries must probe for canonical_slug before naming it"


def test_profile_legacy_matcher_keeps_the_pre_dedupe_compose():
    """_resolve_legacy_slug matches INCOMING legacy slugs, which were composed
    with the unconditional provider prefix — the matcher must reproduce that
    form, byte-for-byte, while emitters dedupe."""
    from routes.facility_profile_page import _fac_slug, _legacy_name_part
    assert _legacy_name_part("Iron Mountain", "Iron Mountain LON-3") == \
        "iron-mountain-iron-mountain-lon-3"
    assert _legacy_name_part(None, "Solo Site 9") == "solo-site-9"
    emitted = _fac_slug(1, "Iron Mountain", "Iron Mountain LON-3")
    assert emitted.startswith("iron-mountain-lon-3-"), emitted
    assert "iron-mountain-iron-mountain" not in emitted
    # the matcher is what tier-0 actually compares with
    resolver_body = PROF.split("def _resolve_legacy_slug", 1)[1] \
                        .split("def _comparables_html", 1)[0]
    assert "_legacy_name_part(" in resolver_body, \
        "tier-0 must match on the pre-dedupe compose, not the emitter"
    assert "== name_part" in resolver_body


# ── contracts the delegated helpers now honour ─────────────────────────────

def test_delegated_helper_contracts():
    from routes.d1_sync import _build_facility_slug
    from routes.facility_profile_page import _fac_slug
    # stored-first: a frozen (set-once, possibly doubled) slug WINS
    assert _build_facility_slug(
        {"provider": "NTT", "name": "NTT Frankfurt 1",
         "canonical_slug": "ntt-ntt-frankfurt-1-deadbeef"}) == \
        "ntt-ntt-frankfurt-1-deadbeef"
    # unfrozen: deduped + ascii-folded builder output
    s = _build_facility_slug({"provider": "Télécom", "name": "Télécom Paris DC"})
    assert s.startswith("telecom-paris-dc-"), s
    # ★ 2026-09-05: a one- or two-character facility name is a REAL name — the
    # stuck set was RZ (DE), Oi (BR), B4 (FR), 1A/1B/2/3/4 (CN/HK), SC, L7.
    # The len<3 rejection measured the name fragment, not the slug, and left
    # 28 Operational rows with no URL from March to September.
    assert _build_facility_slug({"provider": "X", "name": "ab"}) == "x-ab-c343e6d0"
    # un-sluggable rows (NO readable name at all) keep the '' contract
    assert _build_facility_slug({"provider": "X", "name": ""}) == ""
    assert _fac_slug(9, "X", "") == ""


def test_d1_unique_slug_assignment_still_holds_after_delegation():
    """_assign_unique_slugs' guarantees survive the re-key: unique, non-empty,
    deduped slugs; genuine provider|name twins de-collided by id; no-name rows
    on the deterministic id fallback."""
    from routes.d1_sync import _assign_unique_slugs
    rows = [
        {"id": 101, "provider": "Iron Mountain", "name": "Iron Mountain LON-3"},
        {"id": 202, "provider": "Iron Mountain", "name": "Iron Mountain LON-3"},
        {"id": 303, "provider": "", "name": ""},
    ]
    _assign_unique_slugs(rows)
    slugs = [r["__slug"] for r in rows]
    assert len(set(slugs)) == 3 and all(slugs), slugs
    assert slugs[0].startswith("iron-mountain-lon-3-"), \
        "mirror key still carries the doubled pre-dedupe form"
    assert slugs[1].startswith("iron-mountain-lon-3-") and slugs[1] != slugs[0]
    assert slugs[2] == "facility-303"


def test_hash8_tail_is_unchanged_by_delegation():
    """Resolution safety: /facilities/<slug> (and the D1 failover route) fall
    back on the hash8 tail, keyed on stable provider|name — delegation must
    not move it."""
    from routes.facility_slug import stable_hash8
    from routes.d1_sync import _build_facility_slug
    from routes.facility_profile_page import _fac_slug
    for prov, name in [("Iron Mountain", "Iron Mountain LON-3"),
                       (None, "Solo DC Campus"),
                       ("", "Café One Data")]:
        want = "-" + stable_hash8(prov, name)
        assert _build_facility_slug({"provider": prov, "name": name}).endswith(want)
        assert _fac_slug(1, prov, name).endswith(want)
