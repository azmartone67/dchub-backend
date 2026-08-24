"""routes/registry_acquisition.py — the ACQUISITION half of registry presence.

WHY (2026-07-27)
================
White-glove + registry-truth MAINTAIN the 16 listings we already have.
Nothing looks for directories we are ABSENT from. That is the growth half,
and it is the one that matters: directories produce ~84% of new agent
arrivals (262 of ~313 first-seen real-external IPs in 30d arrive as generic
`platform=mcp`), while arrivals are flat-to-declining (~12/day).

Maintenance keeps presence from rotting. Only acquisition grows it.

WHAT IT DOES
------------
For every candidate directory, two independent questions, each answered
honestly or not at all:

  1. IS THE DIRECTORY REAL?   fetch its home page. A candidate that does
                              not resolve is `dead_directory` and drops out
                              of the queue — the seed list below is a
                              STARTING GUESS, and the loop is what decides
                              which entries are real. Nothing here asserts
                              a directory exists because it appears in the
                              seed.
  2. ARE WE ON IT?            fetch its search/listing URL and look for a
                              DC Hub identity token.

Verdicts: `present` · `absent` (-> submission queue) · `dead_directory` ·
`unverified` (403/429/timeout — we could NOT check, and that is never
reported as "we're present" nor as "we're absent").

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not submit. The speculative registry-refresh webhooks were deleted
2026-07-17 after every POST 404'd; almost every directory takes a manual
form or a GitHub PR. So this produces a REVIEWED QUEUE — name, why we're
absent, and the exact submit URL — and a human (or a separate, explicitly
authorised job) acts on it. A module that pretends to submit is worse than
one that admits it cannot: that is precisely the fake-push lesson.

Surface:  GET|POST /api/v1/admin/registry-acquisition/scan  (crawl+persist)
          GET      /api/v1/admin/registry-acquisition       (queue, pure DB)
Kill:     REGISTRY_ACQUISITION_DISABLE=1
"""

from __future__ import annotations

import datetime
import logging
import os
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

registry_acquisition_bp = Blueprint("registry_acquisition", __name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_IDENTITY_TOKENS = ("dchub.cloud", "dchub", "dc hub")

# ── candidate directories ────────────────────────────────────────────
# A STARTING GUESS, not a claim. Each entry is verified at scan time: a
# candidate whose home page does not resolve becomes `dead_directory` and
# leaves the queue. Add rows freely — a wrong guess costs one fetch and
# self-removes; it can never produce a false "we should submit here".
#
#   home   — proves the directory exists at all
#   probe  — a URL that would show DC Hub IF we were listed (search page
#            preferred; these sites' search pages are the honest test)
#   submit — where a human goes to add us
# ★★2026-07-27 — mcpservers_org REMOVED: we are already PRESENT. Confirmed by
#   enumerating all 10 sitemap shards (32,807 listing URLs, 3 of them dchub) and
#   by page-size discrimination (a real listing renders 52-69KB, a nonsense slug
#   gets a 31KB shell). The live page is
#   https://mcpservers.org/servers/azmartone67/dchub-mcp-server and it is STALE
#   — advertises 15 tools / 20,000+ facilities vs canon 80 / 15,000+. It has a
#   "Request update" control, so it is a MAINTENANCE item for registry_truth,
#   not an acquisition target.
#   ★Why the loop could not see it: the site is fully CLIENT-RENDERED — even the
#   real listing page's raw HTML contains zero occurrences of "dchub" — so no
#   single-fetch identity probe can detect presence. The only server-side signal
#   is the sitemap shards, which a one-URL probe cannot cover. Absence here is
#   NOT measurable by this loop; do not re-add it as a candidate.
# RETIRED CANDIDATES — kept as a note so they are not re-added:
#   mcp_run (mcp.run) — 2026-07-27: www.mcp.run now 301s to turbomcp.ai, which
#   is a self-hosted MCP GATEWAY product behind a "stay tuned" holding page, not
#   a server directory. Every path 404s to a constant-size page. There is no
#   listing to be in, so no probe URL can be correct. NOTE this is a blind spot
#   in the classifier: a candidate whose home resolves 200 but has PIVOTED away
#   from being a directory can never be auto-detected as dead_directory.
CANDIDATE_DIRECTORIES = [
    {"name": "opentools", "home": "https://opentools.com/",
     "probe": "https://opentools.com/registry?q=dchub", "submit": "https://opentools.com/"},
    {"name": "mcpmarket", "home": "https://mcpmarket.com/",
     "probe": "https://mcpmarket.com/search?q=dchub", "submit": "https://mcpmarket.com/submit"},
    {"name": "himcp_ai", "home": "https://himcp.ai/",
     "probe": "https://himcp.ai/?s=dchub", "submit": "https://himcp.ai/submit"},
    {"name": "mcp_get", "home": "https://mcp-get.com/",
     "probe": "https://mcp-get.com/packages?q=dchub", "submit": "https://mcp-get.com/"},
    # ★ Probe is the toolkits SITEMAP, not a search page. composio.dev/toolkits
    # returns byte-identical HTML for ?q=dchub and ?q=<nonsense> (client-side
    # search), so absence could never be read there. The sitemap is a complete,
    # server-rendered enumeration of all ~1,096 toolkits — a definitive test.
    # submit=None: the toolkits are integrations Composio BUILDS; there is no
    # public submission route (/request-integration, /submit, /toolkits/request
    # all 404). Getting DC Hub in is a BD conversation at composio.dev/contact,
    # so this must never enter the submission queue as an actionable task.
    {"name": "composio", "home": "https://composio.dev/",
     "probe": "https://composio.dev/toolkits/sitemap.xml", "submit": None},
    # ★ submit=None: fleurmcp.com is a ONE-PAGE site — /submit, /apps, /directory,
    # /developers, /contact and every other path return the same 72,867-byte
    # landing page. No form, no mailto, no external form link, no sitemap. Its
    # catalog (Discord, Notion, Stripe, Linear...) is curated by Fleur, and its
    # own FAQ "Can I add my own MCP servers" is about END USERS wiring up a
    # server locally, not about being listed. There is no route in.
    {"name": "fleur", "home": "https://www.fleurmcp.com/",
     "probe": "https://www.fleurmcp.com/?q=dchub", "submit": None},
    # ★ submit=None: every path (/submit, /add, /servers, /directory, /contact,
    # /docs — even /robots.txt and /sitemap.xml) returns the same "Not Found"
    # HTML shell; only / and /dashboard are real pages. Toolbase is a desktop
    # app whose catalog is curated, with no public submission route. Its ?q=
    # probe also does not filter — the 3,476-char probe/control gap was
    # Cloudflare font CSS reordering, not results.
    {"name": "toolbase", "home": "https://gettoolbase.ai/",
     "probe": "https://gettoolbase.ai/?q=dchub", "submit": None},
    # ★ submit was .../pulls — WRONG. wong2's README line 4: "We do not accept
    # PRs. Please submit your MCP on the website: https://mcpservers.org/submit"
    #
    # ★★DO NOT conflate this README with the mcpservers.org WEBSITE. The repo's
    # GitHub homepage field points at mcpservers.org, and that inference cost a
    # duplicate submission on 2026-07-27: the README is a CURATED list of 537
    # entries, while the site is a separate database of 32,807 listing URLs —
    # 61x larger. DC Hub is absent from the README and PRESENT on the site
    # (three pages, incl. /servers/azmartone67/dchub-mcp-server). This probe
    # answers "are we in the README", nothing more.
    {"name": "wong2_awesome_mcp", "home": "https://mcpservers.org/",
     "probe": "https://raw.githubusercontent.com/wong2/awesome-mcp-servers/main/README.md",
     "submit": "https://mcpservers.org/submit"},
    {"name": "punkpeye_awesome_mcp",
     "home": "https://github.com/punkpeye/awesome-mcp-servers",
     "probe": "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
     "submit": "https://github.com/punkpeye/awesome-mcp-servers/pulls"},
    # ★ submit=None: PRs are OFF on this repo. The pulls REST endpoint 404s and
    # server-side search returns 0 PRs in ANY state (a control repo returns 292,
    # so it is not a token scope issue); no community PR has merged since
    # 2025-09-03. The README probe still works, so absence stays measurable —
    # there is simply no way to act on it.
    {"name": "appcypher_awesome_mcp",
     "home": "https://github.com/appcypher/awesome-mcp-servers",
     "probe": "https://raw.githubusercontent.com/appcypher/awesome-mcp-servers/main/README.md",
     "submit": None},
]


def _disabled() -> bool:
    return (os.environ.get("REGISTRY_ACQUISITION_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _db():
    try:
        from routes.brain_rag import _db as _rag_db
        return _rag_db()
    except Exception as e:  # noqa: BLE001
        logger.debug("[registry-acq] db unavailable: %s", e)
        return None


# ── the classifier (pure — unit-tested without network) ──────────────

CONTROL_TOKEN = "zqxjkbwmp"   # a term no directory can legitimately match

# Rotating nonces, CSRF tokens and the echoed query string all make a
# non-filtering page differ from its control by a few characters. Measured
# 2026-07-27 across the live seed, the gap is unambiguous: non-filtering pages
# differ by 0-81 chars, genuinely filtering ones by 3,475-148,179.
_CONTROL_NOISE_CHARS = 256


def _visible_text(html: str) -> str:
    """Rendered TEXT, not raw markup.

    Raw HTML is full of per-response noise that has nothing to do with whether
    a search filtered: Cloudflare font CSS whose @font-face blocks reorder
    (3,476 chars on gettoolbase.ai), email-protection nonces (2 chars on
    fleurmcp.com), and hydration/stream ids inside an i18n payload (20,953
    chars on mcpservers.org). All three looked like search results to a
    byte-level comparison. Measured across the live seed, comparing text
    instead cleanly separates the one directory that really filters
    (mcpmarket, 3,664 chars of text) from six that do not (0 chars each).
    """
    h = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html or "")
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def _bodies_equivalent(a, b) -> bool:
    """True when probe and control differ only TRIVIALLY.

    Exact equality was too brittle. fleurmcp.com defeated it with a single
    rotating Cloudflare email-protection nonce (#75 vs #e8) — TWO characters in
    a 72,857-byte page — so the loop read a static landing page as a filtering
    search and called it "submittable". Compare the differing REGION after
    stripping the common prefix and suffix instead: a few hundred characters is
    a nonce or an echoed query, not search results.
    """
    a = _visible_text(a)
    b = _visible_text(b)
    if not a or not b:
        return False
    if a == b:
        return True
    n = min(len(a), len(b))
    pre = 0
    while pre < n and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and a[len(a) - 1 - suf] == b[len(b) - 1 - suf]:
        suf += 1
    diff = max(len(a) - pre - suf, len(b) - pre - suf)
    # Absolute AND relative. An absolute-only threshold misjudges small pages,
    # where a 20-character difference is most of the content; a relative-only
    # one misjudges large pages, where real results can be under 1%.
    return diff < _CONTROL_NOISE_CHARS and diff < 0.02 * max(len(a), len(b))


def classify_candidate(home_status, probe_status, probe_body,
                       control_body=None, submittable=True) -> dict:
    """Two-question verdict from already-fetched evidence.

    Question 1 gates question 2: if the directory itself does not resolve,
    "are we listed" is meaningless and must not be answered.

    CONTROL PROBE (added after the first live scan): many directories render
    search CLIENT-SIDE, so `?q=dchub` returns byte-identical HTML to
    `?q=<nonsense>` and the absence of our name proves nothing about the
    directory's contents. Live: opentools and mcp-get both did exactly this,
    and the first scan called them "absent" — which would have sent someone
    to submit to directories we might already be on. When the control body
    matches the probe body, presence is UNKNOWN.
    """
    out = {"verdict": "unverified", "reason": ""}

    # Q1 — is the directory real?
    if home_status is None:
        out["reason"] = "directory home unreachable — cannot tell if it exists"
        return out
    if home_status >= 400 and home_status not in (401, 403, 429):
        out["verdict"] = "dead_directory"
        out["reason"] = ("home returns HTTP %s — not a live directory; drop it "
                         "from the candidate list" % home_status)
        return out

    # Q2 — are we on it?
    if probe_status is None or probe_status in (401, 403, 429):
        out["reason"] = ("directory is live but the probe could not be read "
                         "(HTTP %s) — presence UNKNOWN, not absent"
                         % (probe_status if probe_status else "timeout"))
        return out
    if probe_status >= 400:
        out["reason"] = ("probe URL returns HTTP %s — the probe is wrong, not "
                         "necessarily our absence" % probe_status)
        return out

    low = (probe_body or "").lower()
    if not low.strip():
        out["reason"] = "empty probe body — presence UNKNOWN"
        return out
    if any(tok in low for tok in _IDENTITY_TOKENS):
        out["verdict"] = "present"
        out["reason"] = "DC Hub found on the directory"
        return out
    # Absence is only meaningful if the probe actually FILTERS. If a nonsense
    # query returns the same bytes, the search is client-side and this page
    # would never have shown us either way.
    if control_body is not None and control_body.strip():
        if _bodies_equivalent(probe_body, control_body):
            out["verdict"] = "unverified"
            out["reason"] = ("search renders CLIENT-SIDE (a nonsense query "
                             "returns identical HTML) — absence cannot be "
                             "read from this page; needs a real listing URL")
            return out
    # Q3 — can we even get on it? Confirmed-absent is only a TASK if a
    # submission route exists. Composio's toolkits are integrations they build;
    # there is no public submit path, so queueing it would manufacture work
    # nobody can complete — the same busywork the unverified states exist to
    # prevent, just arriving from the other direction.
    if not submittable:
        out["verdict"] = "no_submit_path"
        out["reason"] = ("live and does not list DC Hub, but there is no public "
                         "submission route — BD contact, not a queue item")
        return out
    out["verdict"] = "absent"
    out["reason"] = "directory is live and does not list DC Hub — submittable"
    return out


def _fetch(url: str):
    try:
        import requests as _rq
        r = _rq.get(url, headers={"User-Agent": _UA}, timeout=20,
                    allow_redirects=True)
        return r.status_code, r.text
    except Exception as e:  # noqa: BLE001
        logger.debug("[registry-acq] fetch %s: %s", url, e)
        return None, ""


_DDL = """
CREATE TABLE IF NOT EXISTS registry_acquisition_candidates (
    name          TEXT PRIMARY KEY,
    home_url      TEXT,
    probe_url     TEXT,
    submit_url    TEXT,
    verdict       TEXT,
    reason        TEXT,
    home_status   INT,
    probe_status  INT,
    checked_at    TIMESTAMPTZ,
    first_absent_at TIMESTAMPTZ
)
"""


def run_scan() -> dict:
    c = _db()
    if c is None:
        return {"ok": False, "error": "no db"}
    results = []
    try:
        with c.cursor() as cur:
            cur.execute(_DDL)
        c.commit()
        for cand in CANDIDATE_DIRECTORIES:
            hs, _ = _fetch(cand["home"])
            ps, pb = _fetch(cand["probe"])
            cb = None
            probe = cand["probe"]
            if "dchub" in probe.lower():
                _, cb = _fetch(re.sub("dchub", CONTROL_TOKEN, probe,
                                      flags=re.I))
            v = classify_candidate(hs, ps, pb, cb,
                                   submittable=bool(cand.get("submit")))
            v.update({"name": cand["name"], "submit_url": cand["submit"],
                      "home_status": hs, "probe_status": ps})
            results.append(v)
            try:
                with c.cursor() as cur:
                    # NOTE: kept as ONE contiguous string. The
                    # insert-no-on-conflict lint reads a quote-bounded window,
                    # so an ON CONFLICT split across adjacent string fragments
                    # is invisible to it (third time this trap has bitten —
                    # brain_llm_usage and slow_requests were whitelisted, but
                    # this table genuinely upserts, so whitelisting would hide
                    # a real omission later).
                    # NOTE: no apostrophes may appear between INSERT INTO and
                    # ON CONFLICT — the insert-no-on-conflict lint reads a
                    # window that terminates at the first quote OR apostrophe,
                    # so a 'literal' in the VALUES clause hides the ON CONFLICT
                    # that follows it. The absent-flag is therefore passed as a
                    # BOOLEAN parameter rather than compared to a SQL literal.
                    # (Third recurrence of this trap: brain_llm_usage and
                    # slow_requests were whitelisted because they are
                    # append-only; this table genuinely upserts, so a
                    # whitelist would hide a real omission later.)
                    is_absent = (v["verdict"] == "absent")
                    cur.execute("""
                        INSERT INTO registry_acquisition_candidates
                          (name, home_url, probe_url, submit_url, verdict,
                           reason, home_status, probe_status, checked_at,
                           first_absent_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW() ON CONFLICT DO NOTHING,
                                CASE WHEN %s THEN NOW() END)
                        ON CONFLICT (name) DO UPDATE SET
                          verdict=EXCLUDED.verdict, reason=EXCLUDED.reason,
                          home_status=EXCLUDED.home_status,
                          probe_status=EXCLUDED.probe_status,
                          checked_at=NOW(),
                          first_absent_at = CASE
                            WHEN %s AND registry_acquisition_candidates.first_absent_at IS NOT NULL
                            THEN registry_acquisition_candidates.first_absent_at
                            WHEN %s THEN NOW() ELSE NULL END
                    """, (cand["name"], cand["home"], cand["probe"],
                          cand["submit"], v["verdict"], v["reason"][:400],
                          hs, ps, is_absent, is_absent, is_absent))
                c.commit()
            except Exception:  # noqa: BLE001
                try:
                    c.rollback()
                except Exception:
                    pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    counts: dict = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"ok": True, "checked": len(results), "counts": counts,
            "candidates": results,
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z"}


def read_queue() -> dict:
    """Pure-DB read — safe for a shell lane (no outbound HTTP; preserves the
    no-self-request invariant from the 2026-07-06 flywheel outage)."""
    c = _db()
    if c is None:
        return {"ok": False, "error": "no db"}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT name, verdict, reason, submit_url, checked_at,"
                "       first_absent_at"
                "  FROM registry_acquisition_candidates"
                " ORDER BY (verdict='absent') DESC, name")
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}
    finally:
        try:
            c.close()
        except Exception:
            pass
    counts, queue, unverified, no_route = {}, [], [], []
    routes_seen = {}
    for name, verdict, reason, submit, checked, since in rows:
        counts[verdict or "?"] = counts.get(verdict or "?", 0) + 1
        if verdict == "absent":
            queue.append({"directory": name, "submit_url": submit,
                          "reason": reason,
                          "absent_since": since.isoformat() if since else None})
            if submit:
                routes_seen.setdefault(submit, []).append(name)
        elif verdict == "unverified":
            unverified.append(name)
        elif verdict == "no_submit_path":
            # Confirmed absent, but nothing to DO about it — surfaced so the
            # finding is not lost, deliberately OUT of the submission queue so
            # it never reads as an actionable task.
            no_route.append(name)
    # ★ Depth counts DISTINCT SUBMIT ROUTES, not rows. mcpservers_org and
    # wong2_awesome_mcp are the same directory reached two ways (wong2's GitHub
    # homepage field IS mcpservers.org), so counting rows told a human they had
    # 4 submissions to make when 3 forms would clear the queue. Both rows stay —
    # two independent probes of one directory is good detection — but the WORK
    # is one item.
    shared = {u: n for u, n in routes_seen.items() if len(n) > 1}
    return {"ok": True, "counts": counts,
            "submission_queue": queue, "unverified": unverified,
            "no_submit_path": no_route,
            "queue_depth": len(routes_seen),
            "rows_absent": len(queue),
            "shared_submit_routes": shared,
            "note": ("absent = the directory is live and does not list us; "
                     "submit via submit_url. Nothing here auto-submits — most "
                     "directories take a manual form or a GitHub PR. "
                     "no_submit_path = confirmed absent with no public way in "
                     "(BD contact only) — counted, never queued.")}


@registry_acquisition_bp.route(
    "/api/v1/admin/registry-acquisition/scan", methods=["GET", "POST"])
def scan():
    if _disabled():
        return jsonify(ok=False, error="REGISTRY_ACQUISITION_DISABLE=1"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(run_scan())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@registry_acquisition_bp.route("/api/v1/admin/registry-acquisition",
                               methods=["GET"])
def queue():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    resp = jsonify(read_queue())
    resp.headers["Cache-Control"] = "no-store"
    return resp
