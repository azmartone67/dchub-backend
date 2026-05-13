"""Phase RR cron-babysitter contract tests.

The babysitter's job: for every loop reported as stale/dead by the
truth endpoint, fire the loop's known refresh URL with the admin key.
Loops that are alive, idle, or have no refresh hook are skipped.

These tests cover the deterministic pieces — the loop→URL map and
the classify-which-action-to-take logic — without making real network
calls. The actual HTTP fan-out is exercised in production via the
loops-babysit GH workflow.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SL = os.path.join(ROOT, "routes", "system_loops.py")


def test_loop_refresh_map_covers_known_loops():
    """Every loop name that has a cron-fireable refresh endpoint must
    be in the LOOP_REFRESH map. Real-time loops are exempt; they
    have no cron we control."""
    src = open(SL, encoding="utf-8").read()
    # Extract the LOOP_REFRESH dict by exec'ing its literal
    m = re.search(r"^LOOP_REFRESH\s*:.*?\n=\s*\{.*?\}",
                  src, re.DOTALL | re.MULTILINE)
    # Simpler approach: find any occurrence of LOOP_REFRESH = { ... }
    m = re.search(r"LOOP_REFRESH[:\s]*[a-zA-Z]*\s*=\s*\{[\s\S]*?\n\}",
                  src)
    assert m, "LOOP_REFRESH map not found in system_loops.py"
    ns = {}
    exec(m.group(0), ns)
    refresh = ns.get("LOOP_REFRESH") or {}

    # These loops MUST be in the map — they're cron-driven and the
    # babysitter relies on the mapping to know what URL to POST.
    expected_in_map = {
        "brain_learn", "auto_press_daily", "testimonial_ingest",
        "dcpi_recompute", "iso_extract",
    }
    for loop in expected_in_map:
        assert loop in refresh, f"loop {loop!r} missing from LOOP_REFRESH"

    # These loops MUST NOT be in the map — they're real-time signals
    # with no cron to fire. Putting them in would be a no-op at best
    # or a wasted HTTP call at worst.
    expected_not_in_map = {"engagement_track", "mcp_traffic"}
    for loop in expected_not_in_map:
        assert loop not in refresh, (
            f"loop {loop!r} should NOT have a refresh hook — "
            f"it's a real-time signal"
        )


def test_loop_refresh_entries_are_method_path_tuples():
    """Every entry in LOOP_REFRESH must be a (method, path) 2-tuple
    where method is GET/POST and path starts with /api/."""
    src = open(SL, encoding="utf-8").read()
    m = re.search(r"LOOP_REFRESH[:\s]*[a-zA-Z]*\s*=\s*\{[\s\S]*?\n\}", src)
    ns = {}
    exec(m.group(0), ns)
    refresh = ns["LOOP_REFRESH"]

    for name, entry in refresh.items():
        assert isinstance(entry, tuple) and len(entry) == 2, (
            f"{name}: entry must be (method, path) 2-tuple, got {entry!r}"
        )
        method, path = entry
        assert method in {"GET", "POST"}, (
            f"{name}: method must be GET or POST, got {method!r}"
        )
        assert path.startswith("/api/"), (
            f"{name}: path must start with /api/, got {path!r}"
        )


def test_brain_layer5_source_map_covers_cron_loops():
    """LOOP_SOURCE_FILES in brain_v2_layer5 must cover every loop
    that has a refresh hook (so Layer 5 can grab its source code
    when proposing a code-level fix)."""
    layer5_src = open(
        os.path.join(ROOT, "routes", "brain_v2_layer5.py"),
        encoding="utf-8",
    ).read()
    m = re.search(r"LOOP_SOURCE_FILES[:\s]*[a-zA-Z]*\s*=\s*\{[\s\S]*?\n\}",
                  layer5_src)
    assert m, "LOOP_SOURCE_FILES not found in brain_v2_layer5.py"
    ns = {}
    exec(m.group(0), ns)
    src_map = ns["LOOP_SOURCE_FILES"]

    sl_src = open(SL, encoding="utf-8").read()
    rm = re.search(r"LOOP_REFRESH[:\s]*[a-zA-Z]*\s*=\s*\{[\s\S]*?\n\}",
                   sl_src)
    ns2 = {}
    exec(rm.group(0), ns2)
    refresh = ns2["LOOP_REFRESH"]

    for loop in refresh:
        assert loop in src_map, (
            f"loop {loop!r} is in LOOP_REFRESH (cron-fireable) but "
            f"missing from LOOP_SOURCE_FILES in brain_v2_layer5 — "
            f"Layer 5 won't be able to propose a code fix for it"
        )


def test_brain_layer5_source_files_actually_exist():
    """Every file path in LOOP_SOURCE_FILES must be a real file on
    disk. A typo here means Layer 5 calls Claude with empty context
    and burns an Anthropic call for nothing."""
    layer5_src = open(
        os.path.join(ROOT, "routes", "brain_v2_layer5.py"),
        encoding="utf-8",
    ).read()
    m = re.search(r"LOOP_SOURCE_FILES[:\s]*[a-zA-Z]*\s*=\s*\{[\s\S]*?\n\}",
                  layer5_src)
    ns = {}
    exec(m.group(0), ns)
    src_map = ns["LOOP_SOURCE_FILES"]

    for loop, files in src_map.items():
        for fp in files:
            full = os.path.join(ROOT, fp)
            assert os.path.exists(full), (
                f"{loop}: source file {fp!r} doesn't exist at {full}"
            )
