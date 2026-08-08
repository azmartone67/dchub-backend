"""r-str-coverage (2026-08-07) — johor, batam, pune, queretaro join DCPI.

Four markets that are published research subjects elsewhere in the industry
but could not be scored here: none of them can arrive via
_load_markets_dynamic (it filters country='US'), so a hardcoded tuple is the
only route in.

Each carries a distinct trap, and these tests pin the trap, not the tuple:

  - pune  state='IN' collides with Indiana. _normalize_us_isos gates on the
          CURRENT iso label rather than the state code, which is the only
          reason POSOCO survives. A future "simplification" to a state lookup
          would silently republish Pune on MISO.
  - johor sits ~17 km from singapore across an international border. The twin
          deduper must never collapse it, and it must not inherit Singapore's
          anchors.
  - batam runs an isolated island grid (bright PLN Batam), NOT the Java-Bali
          system. It must not inherit PLN's national anchors.
  - queretaro is the first MEXICAN market in the set, so CENACE is a brand-new
          ISO key — and an ISO absent from iso_defaults FAILS OPEN to WECC,
          publishing Western-US grid parameters over Mexico.
          ★NOT the first Latin American market: barueri (45.9) and osasco
          (45.8) were already scored via _load_markets_dynamic. A grep of
          _MARKETS_HARDCODED cannot see dynamically-loaded markets, which is
          exactly why the original claim here was wrong. The live scored
          universe is the only answer to "is city X in DCPI".

★ The whole-list test (test_every_new_market_is_in_the_intl_splice) is the
one that matters most: adding a tuple WITHOUT adding its ISO to
_INTL_MARKETS is a silent no-op — the market is simply never scored, and
nothing fails.
"""
import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402
from util.iso_taxonomy import STATE_ISO, resolve_iso  # noqa: E402

NEW_MARKETS = ("johor", "batam", "pune", "queretaro")


def _hardcoded(slug):
    rows = [m for m in dcpi._MARKETS_HARDCODED
            if isinstance(m, tuple) and m[0] == slug]
    assert len(rows) == 1, f"{slug} must appear exactly once in _MARKETS_HARDCODED"
    return rows[0]


def _iso_defaults():
    """Pull the iso_defaults dict out of the scorer without running it.

    It is a local inside the scoring function, so read it from the source the
    same way the rest of this suite reads shipped code.
    """
    import ast
    import inspect
    src = inspect.getsource(dcpi)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "iso_defaults"
                        for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            return ast.literal_eval(node.value)
    raise AssertionError("iso_defaults dict not found in routes/dcpi.py")


# ── the four tuples ──────────────────────────────────────────────────────

def test_johor_is_malaysia_on_tnb():
    slug, name, state, iso, lat, lon = _hardcoded("johor")
    assert state == "MY" and iso == "TNB", "Peninsular Malaysia, like kuala-lumpur"
    assert 1.0 < lat < 2.5 and 103.0 < lon < 104.5


def test_batam_is_on_its_own_island_grid_not_java_bali():
    slug, name, state, iso, lat, lon = _hardcoded("batam")
    assert state == "ID"
    assert iso == "PLN-BATAM", (
        "bright PLN Batam is an ISOLATED grid — inheriting 'PLN' would publish "
        "Java-Bali anchors over a system Batam is not connected to")
    assert 0.5 < lat < 1.6 and 103.5 < lon < 104.6


def test_pune_keeps_posoco_and_is_not_indiana():
    slug, name, state, iso, lat, lon = _hardcoded("pune")
    assert state == "IN" and iso == "POSOCO"
    assert 18.0 < lat < 19.0 and 73.0 < lon < 74.5, "Maharashtra, not Indianapolis"


def test_queretaro_is_mexico_on_cenace():
    slug, name, state, iso, lat, lon = _hardcoded("queretaro")
    assert state == "MX" and iso == "CENACE"
    assert 20.0 < lat < 21.5 and -101.5 < lon < -99.5


# ── the traps ────────────────────────────────────────────────────────────

def test_every_new_market_is_in_the_intl_splice():
    """A tuple whose ISO is missing from _INTL_MARKETS is never scored.

    _load_markets_dynamic filters country='US', so the splice is the ONLY
    path a non-US row has into the recompute universe. The failure is silent:
    no error, no missing-market alarm, just a slug that never gets a score.
    """
    spliced = {m[0] for m in dcpi._INTL_MARKETS
               if isinstance(m, tuple) and m}
    for slug in NEW_MARKETS:
        assert slug in spliced, (
            f"{slug} is in _MARKETS_HARDCODED but its ISO is not in the "
            f"_INTL_MARKETS filter — it will never be scored, silently")


def test_new_isos_have_defaults_and_do_not_fail_open_to_wecc():
    """iso_defaults.get(iso, iso_defaults['WECC']) fails OPEN.

    A missing key does not raise — it publishes Western-US grid parameters
    for a grid on another continent, and marks the row as if the anchors
    matched.
    """
    defaults = _iso_defaults()
    for iso in ("PLN-BATAM", "CENACE"):
        assert iso in defaults, f"{iso} would fail open to WECC anchors"
        row = defaults[iso]
        for key in ("queue_wait_months", "reserve_margin_pct",
                    "curtailment_pct", "queue_approval_rate_pct",
                    "btm_headroom_mw"):
            assert key in row, f"{iso} missing {key}"
        assert row != defaults["WECC"], f"{iso} must not be a WECC copy"

    # Batam's island grid is smaller than PLN's national system in absolute
    # megawatts even though its reserve percentage is comfortable. If someone
    # later "tidies" PLN-BATAM into an alias of PLN, this catches it.
    assert defaults["PLN-BATAM"]["btm_headroom_mw"] < defaults["PLN"]["btm_headroom_mw"]


def test_us_iso_normalizer_leaves_all_four_alone():
    rows = [_hardcoded(s) for s in NEW_MARKETS]
    assert dcpi._normalize_us_isos(list(rows)) == rows


def test_pune_iso_LABEL_survives_the_state_code_collision():
    """Scope corrected 2026-08-08. This test only ever proved the LABEL is safe.

    ★It was originally named "..._collision_is_defused_by_the_label_guard" and
    was cited as evidence that Pune's Indiana collision was handled. It was not.
    `_normalize_us_isos` gates on the ISO label, so the label is safe — but the
    LIVE ADAPTERS key on `state` directly and never look at the ISO, so Pune was
    scored on Indiana's interconnection queue (35,229.5 MW, byte-identical to
    Indianapolis) while this test passed green.

    The data path is covered by tests/test_dcpi_country_code_state_collision.py.
    Keep both: this one guards the label, that one guards the score.
    """
    assert STATE_ISO.get("IN") == "MISO", (
        "if this changes, re-check the guard rather than deleting this test")
    assert "POSOCO" not in dcpi._US_DCPI_ISOS
    assert resolve_iso("pune", "IN", default="POSOCO") == "MISO", (
        "resolve_iso alone WOULD mis-stamp Pune — proving the label guard, "
        "not resolve_iso, is what protects it")
    # The label being safe must never again be read as the data being safe.
    assert dcpi._live_state_reads_allowed("IN", "POSOCO") is False, (
        "the label guard does NOT protect the score — that is what the "
        "country-code-collision suite is for")


def test_twin_dedup_keeps_johor_separate_from_singapore():
    rows = [_hardcoded("johor"), _hardcoded("singapore")]
    assert dcpi._dedup_market_twins(list(rows)) == rows, (
        "an international border is not a metro twin")


def test_no_source_claims_queretaro_is_the_first_latam_market():
    """A factual error, fenced so it cannot come back.

    queretaro was published — in a commit message, a code comment, a test
    docstring and an external deck — as "the first Latin American market in
    DCPI". It is not: barueri and osasco were already scored at 45.9 and 45.8.

    The error was structural, not careless. The check that produced it grepped
    _MARKETS_HARDCODED, and those two markets arrive from
    _load_markets_dynamic — so the source file it read could never have
    contained them. Anyone re-deriving this claim from the same file would make
    the same mistake, which is why it is fenced here rather than just fixed.

    ★This file is DELIBERATELY excluded from its own scan. A guard that quotes
    the pattern it bans will always flag itself — the first version of this test
    failed on a clean tree by matching its own docstring and its own assertion
    message. Scan the source, never the fence.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    banned = re.compile(r"first\s+Latin[\s-]*American", re.I)
    # Lines that legitimately name the wrong claim in order to correct it.
    allowed = re.compile(r"NOT the first|CORRECTION|was WRONG|called queretaro"
                         r"|only \"first\" worth claiming", re.I)
    offenders = []
    for p in sorted((root / "routes").glob("*.py")):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not banned.search(text):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if banned.search(line) and not allowed.search(line):
                offenders.append(f"routes/{p.name}:{n}: {line.strip()[:100]}")
    assert not offenders, (
        "queretaro is the first MEXICAN market, not the first Latin American "
        "one - barueri and osasco predate it:\n  " + "\n  ".join(offenders))


def test_no_slug_collides_with_an_existing_market():
    slugs = [m[0] for m in dcpi._MARKETS_HARDCODED
             if isinstance(m, tuple) and m]
    for slug in NEW_MARKETS:
        assert slugs.count(slug) == 1, f"{slug} defined more than once"
