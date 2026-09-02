#!/usr/bin/env python3
"""The static sitemap shard names ONE geo hub class and every live report page.

NO NETWORK, NO DB — source-shape test of main.py's _build_sitemap_sections,
the house pattern (tests/test_sitemap_thin_gate.py, tests/test_sitemap_lists_
install_pages.py). Comments are stripped before matching so a path that
survives only in a comment cannot satisfy an assertion.

WHY (seo sweep F9 + expansion #4, measured 2026-09-02 ~00:40Z)
==============================================================
* /locations/au served 200, 13.5 KB, ZERO JSON-LD, title "Data Centers in AU
  - 22 Facilities". /facilities/in/au served 82 KB, ItemList JSON-LD, "Data
  Centers in Australia (470)". BOTH were in sitemap-static.xml (107
  /locations/* + 304 /facilities/in|hub URLs). Google was handed two listed
  pages, with contradictory counts, for one intent; the geo head terms
  ("singapore data centers" 57 impr, "hong kong data center" 18) sat at
  position 70-96 with 0 clicks. The frontend worker now 301s /locations/*
  to /facilities/in/<cc>; a sitemap must never list a redirect.
* /interconnection-queue, /data-center-power-availability, /powered-shell
  each probed 200 + SELF-canonical on the apex (2026-09-02 ~04:30Z) and
  were absent from every shard. /state-of-power was already listed.

THE LIMIT (stated because test_sitemap_thin_gate earned it): listing a URL
is discoverability, not indexing; removing one is crawl hygiene, not
ranking. Both claims stop there.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "main.py")

REPORT_PAGES = (
    "/interconnection-queue",
    "/data-center-power-availability",
    "/state-of-power",
    "/powered-shell",
)


def _builder_src():
    """Source of _build_sitemap_sections with comment lines removed."""
    with open(SRC, encoding="utf-8") as fh:
        s = fh.read()
    i = s.index("def _build_sitemap_sections(")
    j = s.index("\ndef ", i + 10)
    body = s[i:j]
    return "\n".join(ln for ln in body.splitlines()
                     if not ln.strip().startswith("#"))


def _static_tuple_paths(src):
    """Every path literal in a ('<path>', '<pri>', '<freq>') static tuple."""
    return set(re.findall(r"\(\s*'(/[^']*)'\s*,\s*'[0-9.]+'\s*,\s*'[a-z]+'\s*\)", src))


def test_no_locations_urls_are_emitted():
    src = _builder_src()
    assert "dchub.cloud/locations/" not in src, (
        "a /locations/<slug> emission is back in the static shard — those "
        "pages 301 to /facilities/in/<cc> now (F9); a sitemap must never "
        "list a redirect")
    assert "_LOCATION_STATIC_SLUGS" not in src, (
        "the curated /locations slug list was retired with its emission")


def test_the_one_geo_hub_class_is_still_listed():
    src = _builder_src()
    assert "dchub.cloud/facilities/in/{_hc}" in src, (
        "/facilities/in/<cc> is the geo hub the sitemap must keep naming")
    assert "dchub.cloud/facilities/in/us/{_ss}" in src


def test_every_live_report_page_is_in_the_static_tuples():
    paths = _static_tuple_paths(_builder_src())
    missing = [p for p in REPORT_PAGES if p not in paths]
    assert not missing, (
        f"live, self-canonical report pages missing from static_pages: "
        f"{missing} (each was probed 200 + self-canonical before listing)")


def test_report_pages_are_listed_exactly_once():
    src = _builder_src()
    for p in REPORT_PAGES:
        n = len(re.findall(r"\(\s*'" + re.escape(p) + r"'\s*,", src))
        assert n == 1, f"{p} appears {n} times in the static tuples (want 1)"
