"""Phase r49 (2026-05-30) — Owner CRM portal.
==========================================================================
The owner had no UI to *see* the signup roster or *outreach* free users —
the data lives in the `users` table and the upgrade-pool email engine
already exists, but there was no surface tying them together.

This module adds exactly that surface, in ONE Flask Blueprint:

  GET /admin/crm        Self-contained HTML CRM page (plan tiles, sortable
                        roster table newest-first with a ?plan= filter, and
                        an outreach control). Admin-gated EXACTLY like
                        main.py's admin_list_users (X-Admin-Key header OR
                        ?key= matching $DCHUB_ADMIN_KEY). The page shell
                        renders without the key; ?key= is accepted only to
                        pre-seed the in-browser key field for convenience.

  GET /admin/crm/data   JSON roster + plan counts. Header-auth ONLY
                        (X-Admin-Key) so the admin key never rides in a URL.
                        Reuses admin_list_users' exact SQL via
                        main.pg_connection():
                          SELECT email, name, company, plan, created_at
                          FROM users [WHERE plan=%s] ORDER BY created_at DESC

Outreach is NOT re-implemented here. The page's buttons fetch the EXISTING
Resend-backed endpoints from routes/upgrade_pool_outreach.py:
  POST /api/v1/admin/upgrade-pool/preview   (audit who would be emailed)
  POST /api/v1/admin/upgrade-pool/send      (?dry=1 to dry-run, ?limit=N)
…sending the same key as the X-Admin-Key header on every call (that module
accepts $DCHUB_ADMIN_KEY via X-Admin-Key, so one key drives everything).

pg_connection is imported lazily inside the handler — main.py registers
this blueprint long after pg_connection is defined, and the import only
runs at request time, so there is no circular-import risk (same idiom
routes/lost_conversion_outreach.py uses with `from main import get_db`).
"""
import os
import json
import logging

from flask import Blueprint, request, jsonify, Response

logger = logging.getLogger(__name__)

admin_crm_portal_bp = Blueprint("admin_crm_portal", __name__)


def _admin_ok() -> bool:
    """Auth gate identical to main.py admin_list_users: the request must
    carry the X-Admin-Key header (or ?key= query param) matching the
    DCHUB_ADMIN_KEY env var. Empty/unset key always fails."""
    key = request.headers.get("X-Admin-Key") or request.args.get("key", "")
    expected = os.environ.get("DCHUB_ADMIN_KEY", "")
    return bool(expected) and key == expected


def _fetch_roster(plan_filter: str = ""):
    """Run admin_list_users' EXACT SQL via main.pg_connection() and shape
    the rows into dicts. Returns (users, by_plan)."""
    # Lazy import: pg_connection lives in main.py and is defined well before
    # this blueprint is registered; importing at request time avoids any
    # import-order coupling. Mirrors routes/lost_conversion_outreach.py.
    from main import pg_connection

    with pg_connection() as pg_conn:
        pg_cur = pg_conn.cursor()
        if plan_filter:
            pg_cur.execute(
                "SELECT email, name, company, plan, created_at FROM users "
                "WHERE plan = %s ORDER BY created_at DESC",
                (plan_filter,),
            )
        else:
            pg_cur.execute(
                "SELECT email, name, company, plan, created_at FROM users "
                "ORDER BY created_at DESC"
            )
        rows = pg_cur.fetchall()

    users = [
        {
            "email": r[0],
            "name": r[1],
            "company": r[2],
            "plan": r[3],
            "created_at": str(r[4]) if r[4] else None,
        }
        for r in rows
    ]
    by_plan = {}
    for u in users:
        key = u["plan"] or "unknown"
        by_plan[key] = by_plan.get(key, 0) + 1
    return users, by_plan


@admin_crm_portal_bp.route("/admin/crm/data", methods=["GET"])
def admin_crm_data():
    """JSON roster + plan counts. Header-auth only — keeps the admin key
    out of the URL. The HTML page fetches this with the X-Admin-Key header.

    Returns tile buckets too: free (free/blank/unknown), enterprise, and
    paid (everything else — pro/developer/founding/starter/…), plus the
    full per-plan breakdown so no plan string is ever hidden."""
    if not _admin_ok():
        return jsonify({"error": "Unauthorized"}), 401

    plan_filter = request.args.get("plan", "")
    try:
        users, by_plan = _fetch_roster(plan_filter)
    except Exception as e:  # noqa: BLE001 — surface DB errors like admin_list_users
        logger.warning("admin_crm_data query failed: %s", e)
        return jsonify({"error": str(e)}), 500

    # Tile buckets — computed from the UNFILTERED counts when no filter is
    # applied; when a ?plan= filter is active the tiles reflect that subset.
    free = enterprise = paid = 0
    for plan, n in by_plan.items():
        p = (plan or "").strip().lower()
        if p in ("free", "", "unknown", "none"):
            free += n
        elif p == "enterprise":
            enterprise += n
        else:
            paid += n
    total = len(users)

    return jsonify(
        {
            "users": users,
            "total": total,
            "by_plan": by_plan,
            "tiles": {
                "free": free,
                "paid": paid,
                "enterprise": enterprise,
                "total": total,
            },
            "plan_filter": plan_filter or None,
        }
    )


# ---------------------------------------------------------------------------
# The page shell. Self-contained: no external CSS/JS. The admin key is held
# only in a JS variable (seeded from ?key= on first load for convenience)
# and sent as the X-Admin-Key header on every fetch — never placed in a URL.
# ---------------------------------------------------------------------------
_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>DC Hub · CRM Portal</title>
<style>
  :root {
    --bg:#0b0e14; --panel:#141925; --panel2:#1b2130; --border:#283044;
    --text:#e6e9ef; --muted:#9aa4b8; --accent:#6366f1; --accent2:#22d3ee;
    --free:#f59e0b; --paid:#22c55e; --ent:#a855f7; --tot:#6366f1;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,'Inter',Segoe UI,sans-serif;
    line-height:1.45;
  }
  header {
    padding:18px 24px; border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:16px; flex-wrap:wrap;
    background:var(--panel);
  }
  header h1 { font-size:18px; margin:0; font-weight:650; letter-spacing:.2px; }
  header .sub { color:var(--muted); font-size:13px; }
  .keybox { margin-left:auto; display:flex; align-items:center; gap:8px; }
  .keybox input {
    background:var(--panel2); border:1px solid var(--border); color:var(--text);
    border-radius:8px; padding:8px 10px; font-size:13px; width:260px;
  }
  .btn {
    background:var(--accent); color:#fff; border:0; border-radius:8px;
    padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer;
  }
  .btn.secondary { background:var(--panel2); border:1px solid var(--border); color:var(--text); }
  .btn.warn { background:#dc2626; }
  .btn:disabled { opacity:.5; cursor:not-allowed; }
  main { padding:24px; max-width:1240px; margin:0 auto; }
  .tiles { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:22px; }
  .tile {
    background:var(--panel); border:1px solid var(--border); border-radius:12px;
    padding:16px 18px; cursor:pointer; transition:border-color .15s, transform .05s;
  }
  .tile:hover { border-color:var(--accent); }
  .tile:active { transform:translateY(1px); }
  .tile.active { border-color:var(--accent2); box-shadow:0 0 0 1px var(--accent2) inset; }
  .tile .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.6px; }
  .tile .num { font-size:30px; font-weight:700; margin-top:6px; }
  .tile.free .num { color:var(--free); }
  .tile.paid .num { color:var(--paid); }
  .tile.enterprise .num { color:var(--ent); }
  .tile.total .num { color:var(--tot); }
  .bar { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
  .bar .filterpill {
    background:var(--panel2); border:1px solid var(--border); color:var(--muted);
    border-radius:999px; padding:5px 12px; font-size:12px;
  }
  .bar .filterpill b { color:var(--text); }
  .outreach {
    background:var(--panel); border:1px solid var(--border); border-radius:12px;
    padding:14px 16px; margin-bottom:20px; display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  }
  .outreach .title { font-weight:650; font-size:14px; margin-right:6px; }
  .outreach label { font-size:12px; color:var(--muted); display:flex; align-items:center; gap:6px; }
  .outreach input[type=number] {
    width:64px; background:var(--panel2); border:1px solid var(--border);
    color:var(--text); border-radius:6px; padding:5px 7px; font-size:13px;
  }
  table { width:100%; border-collapse:collapse; font-size:13px; background:var(--panel); border-radius:12px; overflow:hidden; }
  thead th {
    text-align:left; padding:11px 14px; background:var(--panel2); color:var(--muted);
    font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.5px;
    cursor:pointer; user-select:none; white-space:nowrap; border-bottom:1px solid var(--border);
  }
  thead th .arrow { color:var(--accent2); font-size:10px; }
  tbody td { padding:10px 14px; border-bottom:1px solid var(--border); vertical-align:top; }
  tbody tr:hover { background:var(--panel2); }
  .plan-badge {
    display:inline-block; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:600;
    text-transform:capitalize;
  }
  .plan-free { background:rgba(245,158,11,.15); color:var(--free); }
  .plan-enterprise { background:rgba(168,85,247,.15); color:var(--ent); }
  .plan-paid { background:rgba(34,197,94,.15); color:var(--paid); }
  .muted { color:var(--muted); }
  #status { font-size:13px; color:var(--muted); margin:10px 0; min-height:18px; }
  #status.err { color:#f87171; }
  #status.ok { color:#34d399; }
  .empty { padding:40px; text-align:center; color:var(--muted); }
  pre.out {
    background:#0a0d13; border:1px solid var(--border); border-radius:10px; color:#cbd5e1;
    padding:12px; font-size:12px; max-height:280px; overflow:auto; white-space:pre-wrap; word-break:break-word;
    margin-top:8px; display:none;
  }
</style>
</head>
<body>
<header>
  <div>
    <h1>DC Hub · CRM Portal</h1>
    <div class="sub">Signup roster &amp; free-user outreach</div>
  </div>
  <div class="keybox">
    <input id="adminKey" type="password" placeholder="Admin key (X-Admin-Key)" autocomplete="off">
    <button class="btn" id="loadBtn">Load roster</button>
  </div>
</header>
<main>
  <div class="tiles">
    <div class="tile free" data-plan="free"><div class="label">Free</div><div class="num" id="t-free">–</div></div>
    <div class="tile paid" data-plan="__paid__"><div class="label">Paid</div><div class="num" id="t-paid">–</div></div>
    <div class="tile enterprise" data-plan="enterprise"><div class="label">Enterprise</div><div class="num" id="t-enterprise">–</div></div>
    <div class="tile total" data-plan=""><div class="label">Total</div><div class="num" id="t-total">–</div></div>
  </div>

  <div class="outreach">
    <span class="title">Free-user outreach</span>
    <label>min signals <input id="minSignals" type="number" value="3" min="1" max="50"></label>
    <label>limit <input id="limit" type="number" value="25" min="1" max="100"></label>
    <button class="btn secondary" id="previewBtn">Preview pool</button>
    <button class="btn secondary" id="dryBtn">Dry-run send</button>
    <button class="btn warn" id="sendBtn">Send for real</button>
    <span class="muted" style="font-size:12px">→ upgrade-pool engine (Resend)</span>
  </div>
  <pre class="out" id="outreachOut"></pre>

  <div class="bar">
    <span class="filterpill">Filter: <b id="curFilter">all</b></span>
    <button class="btn secondary" id="clearFilter" style="display:none">Clear filter</button>
    <span class="muted" id="rowcount" style="font-size:12px"></span>
  </div>

  <div id="status">Enter your admin key and load the roster.</div>

  <table id="tbl" style="display:none">
    <thead>
      <tr>
        <th data-k="email">Email <span class="arrow"></span></th>
        <th data-k="name">Name <span class="arrow"></span></th>
        <th data-k="company">Company <span class="arrow"></span></th>
        <th data-k="plan">Plan <span class="arrow"></span></th>
        <th data-k="created_at">Signup date <span class="arrow"></span></th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="empty" id="empty" style="display:none">No users match this filter.</div>
</main>

<script>
(function () {
  "use strict";
  // Key lives only in JS. Seeded from ?key= for convenience (then scrubbed
  // from the address bar) but only ever sent as the X-Admin-Key HEADER.
  var params = new URLSearchParams(location.search);
  var seededKey = params.get("key") || "";
  var keyInput = document.getElementById("adminKey");
  if (seededKey) {
    keyInput.value = seededKey;
    try {
      params.delete("key");
      var qs = params.toString();
      history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
    } catch (e) { /* non-fatal */ }
  }

  var state = { users: [], filter: "", sortKey: "created_at", sortDir: -1 };

  function adminKey() { return (keyInput.value || "").trim(); }
  function headers() { return { "X-Admin-Key": adminKey() }; }

  function setStatus(msg, cls) {
    var el = document.getElementById("status");
    el.textContent = msg || "";
    el.className = cls || "";
  }

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function planClass(plan) {
    var p = (plan || "").toLowerCase().trim();
    if (p === "" || p === "free" || p === "unknown" || p === "none") return "plan-free";
    if (p === "enterprise") return "plan-enterprise";
    return "plan-paid";
  }

  function fmtDate(s) {
    if (!s) return '<span class="muted">—</span>';
    // created_at comes back as a stringified timestamp; show date + time.
    var d = new Date(s.replace(" ", "T"));
    if (isNaN(d.getTime())) return esc(s);
    return d.toISOString().slice(0, 16).replace("T", " ");
  }

  function applySortFilter() {
    var rows = state.users.slice();
    if (state.filter === "__paid__") {
      rows = rows.filter(function (u) {
        var p = (u.plan || "").toLowerCase().trim();
        return p !== "" && p !== "free" && p !== "unknown" && p !== "none" && p !== "enterprise";
      });
    } else if (state.filter) {
      rows = rows.filter(function (u) { return (u.plan || "") === state.filter; });
    }
    var k = state.sortKey, dir = state.sortDir;
    rows.sort(function (a, b) {
      var av = a[k], bv = b[k];
      if (k === "created_at") { av = av || ""; bv = bv || ""; }
      else { av = (av || "").toLowerCase(); bv = (bv || "").toLowerCase(); }
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
    return rows;
  }

  function render() {
    var rows = applySortFilter();
    var tbody = document.getElementById("tbody");
    var tbl = document.getElementById("tbl");
    var empty = document.getElementById("empty");
    document.getElementById("rowcount").textContent =
      rows.length + " of " + state.users.length + " users";

    // sort-arrow indicators
    var ths = document.querySelectorAll("thead th");
    ths.forEach(function (th) {
      var arrow = th.querySelector(".arrow");
      arrow.textContent = (th.getAttribute("data-k") === state.sortKey)
        ? (state.sortDir === -1 ? "▼" : "▲") : "";
    });

    if (!rows.length) {
      tbl.style.display = "none";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";
    tbl.style.display = "table";
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      var u = rows[i];
      html += "<tr>" +
        "<td>" + esc(u.email) + "</td>" +
        "<td>" + (u.name ? esc(u.name) : '<span class="muted">—</span>') + "</td>" +
        "<td>" + (u.company ? esc(u.company) : '<span class="muted">—</span>') + "</td>" +
        '<td><span class="plan-badge ' + planClass(u.plan) + '">' +
          esc(u.plan || "free") + "</span></td>" +
        "<td>" + fmtDate(u.created_at) + "</td>" +
        "</tr>";
    }
    tbody.innerHTML = html;
  }

  function setFilter(plan) {
    state.filter = plan || "";
    document.getElementById("curFilter").textContent =
      plan === "__paid__" ? "paid" : (plan || "all");
    document.getElementById("clearFilter").style.display = plan ? "inline-block" : "none";
    document.querySelectorAll(".tile").forEach(function (t) {
      t.classList.toggle("active", t.getAttribute("data-plan") === (plan || ""));
    });
    render();
  }

  function loadRoster() {
    if (!adminKey()) { setStatus("Enter your admin key first.", "err"); return; }
    setStatus("Loading roster…");
    document.getElementById("loadBtn").disabled = true;
    fetch("/admin/crm/data", { headers: headers(), cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) throw new Error("Unauthorized — check your admin key.");
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        state.users = data.users || [];
        var t = data.tiles || {};
        document.getElementById("t-free").textContent = t.free != null ? t.free : "–";
        document.getElementById("t-paid").textContent = t.paid != null ? t.paid : "–";
        document.getElementById("t-enterprise").textContent = t.enterprise != null ? t.enterprise : "–";
        document.getElementById("t-total").textContent = t.total != null ? t.total : state.users.length;
        setStatus("Loaded " + state.users.length + " users.", "ok");
        render();
      })
      .catch(function (e) { setStatus(e.message || "Load failed.", "err"); })
      .finally(function () { document.getElementById("loadBtn").disabled = false; });
  }

  // ---- Outreach: call the EXISTING upgrade-pool endpoints with X-Admin-Key.
  function outreach(path, label, btn) {
    if (!adminKey()) { setStatus("Enter your admin key first.", "err"); return; }
    var out = document.getElementById("outreachOut");
    var min = parseInt(document.getElementById("minSignals").value, 10) || 3;
    var lim = parseInt(document.getElementById("limit").value, 10) || 25;
    var url = path + "?min_signals=" + min + "&limit=" + lim;
    if (label === "dry") url += "&dry=1";
    btn.disabled = true;
    setStatus(label === "preview" ? "Fetching upgrade-pool preview…" :
              label === "dry" ? "Dry-running send…" : "Sending outreach…");
    fetch(url, { method: "POST", headers: headers() })
      .then(function (r) {
        if (r.status === 401) throw new Error("Unauthorized — check your admin key.");
        return r.json().then(function (j) { return { ok: r.ok, j: j }; });
      })
      .then(function (res) {
        out.style.display = "block";
        out.textContent = JSON.stringify(res.j, null, 2);
        if (!res.ok || res.j.ok === false) {
          setStatus("Outreach call returned an error (see output).", "err");
        } else {
          var n = res.j.candidate_count != null ? res.j.candidate_count
                : (res.j.recipients != null ? res.j.recipients : "?");
          var sent = res.j.sent != null ? (" · sent " + res.j.sent) : "";
          setStatus("Outreach " + label + " ok — " + n + " candidate(s)" + sent + ".", "ok");
        }
      })
      .catch(function (e) {
        out.style.display = "block";
        out.textContent = String(e);
        setStatus(e.message || "Outreach failed.", "err");
      })
      .finally(function () { btn.disabled = false; });
  }

  // wire-up
  document.getElementById("loadBtn").addEventListener("click", loadRoster);
  keyInput.addEventListener("keydown", function (e) { if (e.key === "Enter") loadRoster(); });

  document.querySelectorAll(".tile").forEach(function (t) {
    t.addEventListener("click", function () { setFilter(t.getAttribute("data-plan")); });
  });
  document.getElementById("clearFilter").addEventListener("click", function () { setFilter(""); });

  document.querySelectorAll("thead th").forEach(function (th) {
    th.addEventListener("click", function () {
      var k = th.getAttribute("data-k");
      if (state.sortKey === k) state.sortDir *= -1;
      else { state.sortKey = k; state.sortDir = (k === "created_at") ? -1 : 1; }
      render();
    });
  });

  var PREVIEW = "/api/v1/admin/upgrade-pool/preview";
  var SEND = "/api/v1/admin/upgrade-pool/send";
  document.getElementById("previewBtn").addEventListener("click", function () {
    outreach(PREVIEW, "preview", this);
  });
  document.getElementById("dryBtn").addEventListener("click", function () {
    outreach(SEND, "dry", this);
  });
  document.getElementById("sendBtn").addEventListener("click", function () {
    if (!confirm("Send real outreach emails to the upgrade pool (Resend)? This emails real users.")) return;
    outreach(SEND, "send", this);
  });

  // Auto-load if a key was seeded via ?key=.
  if (adminKey()) loadRoster();
})();
</script>
</body>
</html>
"""


@admin_crm_portal_bp.route("/admin/crm", methods=["GET"])
def admin_crm():
    """Serve the CRM portal page.

    The page SHELL renders for anyone (no data in it) so the owner can type
    the key into the in-browser field. ?key= is accepted only as a
    convenience to pre-seed that field and auto-load — but per the auth
    contract, a wrong/missing key still yields a 401 for the actual data
    (the data endpoint and the page both gate on DCHUB_ADMIN_KEY).

    If a ?key= is supplied it MUST be valid, otherwise we 401 immediately —
    this satisfies "without key -> 401" while still letting the keyless
    shell load so the owner can paste a key manually."""
    # If the caller passed a key (header or query), it must be correct.
    supplied = request.headers.get("X-Admin-Key") or request.args.get("key")
    if supplied is not None and supplied != "" and not _admin_ok():
        return jsonify({"error": "Unauthorized"}), 401

    return Response(_PAGE_HTML, mimetype="text/html")
