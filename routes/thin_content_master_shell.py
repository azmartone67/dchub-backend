"""
routes/thin_content_master_shell.py — Thin-Content Master Shell (2026-08-14).

3,563 facility pages sit in Google Search Console's "Crawled – currently not
indexed": fetched, judged not worth an index slot. Every previous attempt at
this treated it as an indexing problem and hit "Validate Fix", which cannot
move a bucket whose cause is the page itself.

MEASURED on discovered_facilities 2026-08-14 (17,948 live rows):

    has coordinates   12,942   72%
    has a real city   16,702   93%
    has power_mw       6,648   37%
    has an address     1,368    8%
    has NOTHING          408   2.3%

★ THE DATABASE IS NOT EMPTY — THE PAGE WAS. Only 408 facilities have no fact
worth indexing. The profile page rendered Status / City / Country plus two
sentences while DC Hub owns 320,000 mapped power/grid/fiber assets and the
facility carries coordinates 72% of the time.

THE THREE LANES (util/thin_content.py holds the logic; this shell measures it):

    LANE 3  suppress  408 contentless pages stop asking to be indexed
    LANE 2  context   market/ISO/DCPI facts already public elsewhere, rendered
    LANE 1  infra     shallow per-site infrastructure band — OFF by default,
                      armed with THIN_INFRA_SLICE=1, because it is adjacent to
                      the paid product and flipping it is a PRICING decision

★★ THIS SHELL MEASURES, IT DOES NOT WRITE. Every lane's effect is in the render
path; there is no backfill, no UPDATE, no job. The board exists so the three
numbers are checkable after deploy instead of asserted in a PR body — the whole
reason this finding survived so long is that nobody could see it.

★★★ WHAT THIS SHELL WILL NOT DO. It will never generate prose to raise a word
count. "Crawled – currently not indexed" is Google already detecting low-value
pages; padding them is the failure mode MEDIA_CLAIM_VERIFY and
PRESS_INTEGRITY_ENFORCE exist to prevent. A page with nothing to say gets
LANE 3, not filler.

GET /api/v1/admin/thin-content/board   (admin key)
"""
import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

thin_content_master_shell_bp = Blueprint("thin_content_master_shell", __name__)

# Kept in one place so the board and util/thin_content cannot drift on what
# counts as a placeholder. Equality, never substring — 'California Regional'
# and 'Connecticut Regional' (136 rows each) are REAL market labels.
_PLACEHOLDER_SQL = "lower(btrim(city)) IN ('regional','unknown','n/a','none','other')"


def _admin_ok() -> bool:
    key = request.headers.get("X-Admin-Key") or request.args.get("key") or ""
    want = os.environ.get("DCHUB_ADMIN_KEY") or ""
    return bool(want) and key == want


@thin_content_master_shell_bp.route("/api/v1/admin/thin-content/board",
                                    methods=["GET"])
def thin_content_board():
    if not _admin_ok():
        return jsonify({"success": False, "error": "unauthorized"}), 401
    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn:
            return jsonify({"success": False, "error": "no_db"}), 503
        try:
            c = conn.cursor()
            c.execute(f"""
                SELECT
                  COUNT(*),
                  COUNT(*) FILTER (WHERE power_mw IS NOT NULL AND power_mw > 0),
                  COUNT(*) FILTER (WHERE latitude IS NOT NULL AND longitude IS NOT NULL),
                  COUNT(*) FILTER (WHERE address IS NOT NULL AND btrim(address) <> ''),
                  COUNT(*) FILTER (WHERE COALESCE(btrim(city),'') <> ''
                                     AND NOT {_PLACEHOLDER_SQL}),
                  COUNT(*) FILTER (WHERE (power_mw IS NULL OR power_mw = 0)
                                     AND (latitude IS NULL OR longitude IS NULL)
                                     AND COALESCE(btrim(address),'') = ''
                                     AND (COALESCE(btrim(city),'') = ''
                                          OR {_PLACEHOLDER_SQL}))
                  FROM discovered_facilities
                 WHERE COALESCE(is_duplicate, 0) = 0
            """)
            live, power, coords, addr, city, nothing = c.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"thin-content board failed: {e}")
        return jsonify({"success": False, "error": str(e)[:200]}), 500

    def pct(n):
        return round(n / live * 100, 1) if live else 0.0

    return jsonify({
        "success": True,
        "live_facilities": live,
        "lanes": {
            "lane3_suppress": {
                "contentless_pages": nothing,
                "share_pct": pct(nothing),
                "action": "rendered with robots=noindex",
                "note": ("no power AND no coords AND no address AND no real "
                         "city — all four. Narrower than 'no coordinates', "
                         "which would de-index 45 real OSM facilities."),
            },
            "lane2_context": {
                "eligible_pages": live - nothing,
                "share_pct": pct(live - nothing),
                "action": "market/ISO/DCPI facts rendered in the context block",
                "note": "already public elsewhere on the site; no tier change",
            },
            "lane1_infra": {
                "armed": os.environ.get("THIN_INFRA_SLICE", "0") == "1",
                "pages_with_coords": coords,
                "share_pct": pct(coords),
                "action": ("distance BAND only when armed; the asset read "
                           "stays the paid product"),
                "note": ("OFF by default — arming it is a PRICING decision, "
                         "not an SEO one"),
            },
        },
        "evidence_coverage": {
            "has_power": power, "has_coords": coords,
            "has_address": addr, "has_real_city": city,
        },
        "basis": ("discovered_facilities WHERE COALESCE(is_duplicate,0)=0 — "
                  "the same fleet filter the profile page uses"),
    })
