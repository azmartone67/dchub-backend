"""SH52-138 sibling — SH52-130: /api/v1/iso/zones must not count BR/KR as US.

THE DEFECT (measured live 2026-08-07, /api/v1/iso/zones)
────────────────────────────────────────────────────────
countries = {US:59, EU:34, JP:9, CA:3, AU:1, GB:1, SG:1, TW:1} — no BR, no KR —
even though the Brazil (ONS) and Korea (KEPCO-KR) feeds are live-fresh. The
_ISO_COUNTRY map in main.py had no ONS / KEPCO-KR / BR_ entries, so the resolver
`_ISO_COUNTRY.get(iso, "US")` silently classed all seven ONS/BR_*/KEPCO-KR zones
as US — inflating the US total and dropping two countries from the coverage map
that is deliberately advertised as a TRUE, verifiable global-coverage claim.

THE FIX (source-asserted — the map is a local inside the route closure)
───────────────────────────────────────────────────────────────────────
  * ONS -> BR and KEPCO-KR -> KR in _ISO_COUNTRY.
  * a BR_ prefix roll-up branch (parent=ONS, country=BR) mirroring EU_/JP_.
  * plain "KEPCO" is intentionally NOT mapped (US Kansas Electric Power
    Cooperative name-collision) — locking that exclusion so nobody "fixes" it
    into a wrong KR label later.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _iso_country_block():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    m = re.search(r"_ISO_COUNTRY\s*=\s*\{(.*?)\}", src, re.S)
    assert m, "could not locate the _ISO_COUNTRY dict literal in main.py"
    return m.group(1), src


def test_brazil_and_korea_are_mapped():
    block, _ = _iso_country_block()
    assert re.search(r'"ONS"\s*:\s*"BR"', block), "ONS must map to BR"
    assert re.search(r'"KEPCO-KR"\s*:\s*"KR"', block), "KEPCO-KR must map to KR"


def test_br_prefix_rollup_branch_present():
    _, src = _iso_country_block()
    assert 'iso.startswith("BR_")' in src, (
        "the BR_ per-zone roll-up branch (parent=ONS, country=BR) is missing")


def test_plain_kepco_is_not_claimed_for_korea():
    # KEPCO (bare) collides with the US Kansas Electric Power Cooperative —
    # only the explicit KEPCO-KR token is Korea. Guard the intentional gap.
    block, _ = _iso_country_block()
    assert not re.search(r'"KEPCO"\s*:\s*"KR"', block), (
        'bare "KEPCO" must not be mapped to KR (US name-space collision)')
