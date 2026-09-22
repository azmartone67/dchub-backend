"""Public doors never publish the retired facility floors the owner banned
(owner directive, 2026-09-21: 20,000+, 22,100+, 22,900+, 24,400+).

WHY. Agents that do not speak MCP read a door once, as text, and cite what it
says. Measured 2026-09-22: every /vs page's meta description published the
keeper floor instead of the canon floor, and the integration guides served
byte-identical from static/integrations/<x>/README.md at
https://dchub.cloud/integrations/<x>/README.md were three canon generations
behind (one said 4,000+ deals against a canon of 1,600+). A banned floor on any
of those is a citation the owner has said must not exist.

SCOPE, files an agent or crawler reads as text:
  * every tracked README*.md. GitHub renders all of them, and the
    static/integrations ones are also SERVED.
  * the hand-maintained agent surfaces (llms.txt, llms-full.txt and their
    static/ copies, mcp.json, .well-known/mcp.json).
  * the generated twins under static/.well-known/.
The /vs pages are rendered, not files: tests/test_competitive_seo_pages.py
renders them.

THE PIN EXEMPTION, and why it is derived rather than typed. A floor equal to
PINNED['public']['facilities'] is not judged here. That value is the cold-start
floor, and tests/test_agent_surface_floors_match_canon.py REQUIRES the
hand-maintained surfaces to state it, so banning it here would make the two
guards unsatisfiable together. The exemption follows the pin: the moment the
pin walks past a banned floor, that floor is banned everywhere in scope, with no
edit here. It also keeps a future walk DOWN honest: if canon ever returns to one
of these values, the pin moves with it and this guard does not fight it.

Matches are boundary-safe: "320,000+" is not "20,000+".

House rules: pytest functions only, no module-scope network or DB, never import
main.py.
"""
import ast
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The owner's list (2026-09-21 PM directive). Typed on purpose: this is the
#: record of what was banned and when, not a derivation.
OWNER_BANNED_FLOORS = ("20,000+", "22,100+", "22,900+", "24,400+")

HAND_SURFACES = ("llms.txt", "llms-full.txt", "static/llms.txt", "static/llms-full.txt",
                 "mcp.json", ".well-known/mcp.json")

#: Files that must be in scope; losing one means the scan silently shrank.
MUST_SCAN = ("README.md", "static/integrations/grok/README.md",
             "static/integrations/copilot/README.md", "llms.txt", ".well-known/mcp.json")


def _pinned_facilities():
    src = (ROOT / "ai_surface_canon.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "PINNED" for t in node.targets):
            return ast.literal_eval(node.value)["public"]["facilities"]
    raise AssertionError("PINNED not found in ai_surface_canon.py")


def banned_floors(pinned):
    """The owner's list minus the current cold-start pin (see module docstring)."""
    return tuple(f for f in OWNER_BANNED_FLOORS if f != pinned)


def find_banned(text, floors):
    """[(floor, lineno, line)] for every boundary-safe occurrence."""
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        for f in floors:
            if re.search(r"(?<![\d,])" + re.escape(f), line):
                hits.append((f, i, line.strip()[:120]))
    return hits


def _tracked():
    return subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True,
                          text=True, check=True).stdout.split("\n")


def _served_integration_files():
    """Everything under static/integrations/ — served at /integrations/<x>/<file>
    (READMEs, plugin manifests, function-calling schemas, instructions)."""
    return [p for p in _tracked() if p.startswith("static/integrations/") and (ROOT / p).is_file()]


def _scanned_files():
    out = _tracked()
    readmes = [p for p in out if re.search(r"(^|/)readme(\.[a-z]+)?$", p, re.I)]
    twins = [p for p in out if p.startswith("static/.well-known/") and p.endswith(".json")]
    files = sorted(set(readmes + twins + _served_integration_files()
                       + [p for p in HAND_SURFACES if (ROOT / p).exists()]))
    return [p for p in files if (ROOT / p).is_file()]


#: A facility figure at facility magnitude followed (within a few words) by a
#: facility noun. The 10,000 floor keeps scoped per-market figures ("200+
#: tracked data center facilities" in one metro) out of the rule, the same band
#: tests/test_canonical_counts_drift.py uses.
_FACILITY_FIGURE = re.compile(
    r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d{5,6})\+?\s*"
    r"(?:(?:global|distinct|tracked|verified|physical)\s+)*"
    r"(?:data[\s-]?cent(?:er|re)s?(?:\s+facilities)?|facilities|DCs)\b", re.I)


def facility_figures_below(text, floor_int):
    """[(figure, lineno, line)] for facility-scale figures under the canon floor."""
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        for m in _FACILITY_FIGURE.finditer(line):
            v = int(m.group(1).replace(",", ""))
            if 10_000 <= v < floor_int:
                hits.append((m.group(0), i, line.strip()[:120]))
    return hits


def test_the_scan_is_not_vacuous():
    files = _scanned_files()
    missing = [p for p in MUST_SCAN if p not in files]
    assert not missing, f"scan lost required file(s) {missing}; it would pass over them"
    assert len(files) >= 20, f"only {len(files)} files in scope — the README/surface glob broke"


def test_no_public_door_publishes_a_banned_floor():
    floors = banned_floors(_pinned_facilities())
    assert floors, "every banned floor is exempt — the pin exemption swallowed the list"
    bad = []
    for rel in _scanned_files():
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        bad += [f"  {rel}:{i}: {f} -> {line!r}" for f, i, line in find_banned(text, floors)]
    assert not bad, ("banned facility floor(s) on a public door (owner directive "
                     "2026-09-21) — state the canon floor from /api/v1/canon/phrases:\n"
                     + "\n".join(bad))


def test_served_integration_files_state_no_facility_floor_below_canon():
    """★2026-09-22: the served integration guides and configs were three canon
    generations behind (10,706+, 10,400+, 21,000+) and no guard read them —
    they are served byte-identical at /integrations/<x>/<file>, outside every
    SURFACES list. A facility figure below the canon pin is stale whatever it
    is, so when the pin next walks, this names every served line that must
    walk with it."""
    pinned = _pinned_facilities()
    floor_int = int(pinned.rstrip("+").replace(",", ""))
    files = _served_integration_files()
    assert len(files) >= 30, f"only {len(files)} served integration files found — the scope broke"
    bad = []
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        bad += [f"  {rel}:{i}: {fig!r} -> {line!r}" for fig, i, line in facility_figures_below(text, floor_int)]
    assert not bad, (f"facility figure(s) below the canon floor {pinned} in served integration "
                     "files — state the canon floor:\n" + "\n".join(bad))


def test_control_below_canon_figures_are_caught_and_scoped_ones_are_not():
    assert facility_figures_below("Search 10,706+ data centers", 24_500)
    assert facility_figures_below("more than 21,000 data-center facilities", 24_500)
    assert facility_figures_below("10,706 DCs", 24_500)
    assert not facility_figures_below("Northern Virginia has 200+ tracked data center facilities", 24_500)
    assert not facility_figures_below("Search 24,500+ data centers", 24_500)


@pytest.mark.parametrize("floor", OWNER_BANNED_FLOORS)
def test_control_each_banned_floor_is_caught(floor):
    text = f"DC Hub tracks {floor} data center facilities."
    assert find_banned(text, OWNER_BANNED_FLOORS), f"{floor} was not detected"


def test_control_matches_are_boundary_safe():
    assert not find_banned("330,000+ mapped assets; 320,000+ before", ("20,000+",)), (
        "20,000+ matched inside a larger number")
    assert not find_banned("124,400+ rows", ("24,400+",))


def test_control_the_pin_is_exempt_and_only_the_pin():
    assert banned_floors("24,400+") == ("20,000+", "22,100+", "22,900+")
    assert banned_floors("24,500+") == OWNER_BANNED_FLOORS, (
        "once the pin walks past 24,400+ it must be banned again")
