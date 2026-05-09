"""Phase 104: QA pattern library + auto-fix registry."""
from __future__ import annotations
import os, re, json, hashlib
import psycopg2, psycopg2.extras
from typing import Optional, Callable, Any
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

qa_patterns_bp = Blueprint("qa_patterns", __name__)

def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db: raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(db, sslmode="require")

def _ensure_tables():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qa_patterns (
                id SERIAL PRIMARY KEY,
                signature TEXT UNIQUE NOT NULL,
                test_name TEXT, http_code INT, severity TEXT,
                fix_func_name TEXT, fix_args_json JSONB,
                hit_count INT DEFAULT 1,
                first_seen TIMESTAMPTZ DEFAULT NOW(),
                last_seen TIMESTAMPTZ DEFAULT NOW(),
                auto_fix_at TIMESTAMPTZ, auto_fix_ok BOOLEAN, auto_fix_info TEXT,
                novel BOOLEAN DEFAULT TRUE, example_detail TEXT
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qa_fix_log (
                id SERIAL PRIMARY KEY,
                pattern_id INT REFERENCES qa_patterns(id) ON DELETE CASCADE,
                attempted_at TIMESTAMPTZ DEFAULT NOW(),
                ok BOOLEAN, info TEXT
            )""")
        c.commit()

def make_signature(test_name, http_code, error_detail):
    detail = error_detail or ""
    detail = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", detail, flags=re.IGNORECASE)
    detail = re.sub(r"\b[0-9a-f]{12,}\b", "<hex>", detail, flags=re.IGNORECASE)
    detail = re.sub(r"\b\d{10,}\b", "<num>", detail)
    detail = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\d:.+Z-]*", "<ts>", detail)
    detail = detail[:500]
    return hashlib.sha256(f"{test_name}|{http_code}|{detail}".encode("utf-8")).hexdigest()[:16]

def record_failure(test_name, http_code, error_detail, severity="p1"):
    _ensure_tables()
    sig = make_signature(test_name, http_code, error_detail)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO qa_patterns (signature, test_name, http_code, severity, example_detail)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (signature) DO UPDATE
              SET hit_count = qa_patterns.hit_count + 1, last_seen = NOW(), novel = FALSE
            RETURNING id""", (sig, test_name, http_code, severity, (error_detail or "")[:1000]))
        row = cur.fetchone(); c.commit()
    return row[0]

FIX_REGISTRY = {}
def register_fix(name):
    def deco(fn): FIX_REGISTRY[name] = fn; return fn
    return deco

@register_fix("fix_csp_add_directive")
def fix_csp_add_directive(directive="style-src-elem https://fonts.googleapis.com", url="https://fonts.googleapis.com", **_):
    return _open_pr_with_python_patch(
        title=f"qa-autofix: add {url} to CSP {directive}",
        body=f"Auto-opened by qa_patterns. Adds `{url}` to `{directive}` in main.py.",
        patch_func=lambda src: _csp_add(src, directive, url),
        target_files=["main.py"])

@register_fix("fix_placeholder_replace")
def fix_placeholder_replace(pattern=r"__\$\$\$\$__", replacement="—", **_):
    return _open_pr_with_python_patch(
        title=f"qa-autofix: replace literal placeholder {pattern!r}",
        body=f"Replaces /{pattern}/ with {replacement!r}.",
        patch_func=lambda src: re.subn(pattern, replacement, src)[0],
        target_files="*.py,*.html,*.js")

@register_fix("fix_trigger_refresh")
def fix_trigger_refresh(refresh_url="/api/v1/data-pulse/run", **_):
    import urllib.request as _ur
    target = refresh_url
    if target.startswith("/"): target = "https://dchub.cloud" + target
    try:
        req = _ur.Request(target, method="POST")
        with _ur.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"ok": 200 <= resp.status < 300, "info": f"refresh HTTP {resp.status}: {body[:200]}"}
    except Exception as e:
        return {"ok": False, "info": f"refresh:{type(e).__name__}: {str(e)[:200]}"}

def _csp_add(src, directive, url):
    pat = re.compile(rf"({re.escape(directive)}[^;\"']*)")
    def repl(m):
        chunk = m.group(1)
        if url in chunk: return chunk
        return chunk.rstrip() + " " + url
    return pat.sub(repl, src)

def _open_pr_with_python_patch(title, body, patch_func, target_files):
    import subprocess, tempfile, shutil, glob, time
    gh = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not gh: return {"ok": False, "info": "GH_TOKEN not set"}
    repo = os.environ.get("GH_REPO", "azmartone67/dchub-backend")
    branch = f"qa-autofix-{int(time.time())}"
    work = tempfile.mkdtemp(prefix="qa-autofix-")
    try:
        url = f"https://x-access-token:{gh}@github.com/{repo}.git"
        subprocess.check_call(["git","clone","--depth","1",url,work], stderr=subprocess.DEVNULL)
        globs = target_files if isinstance(target_files, list) else [g.strip() for g in target_files.split(",") if g.strip()]
        n_changed = 0
        for pat in globs:
            for fp in glob.glob(os.path.join(work,"**",pat), recursive=True):
                if "__pycache__" in fp or "/.git/" in fp: continue
                try:
                    with open(fp,"r",encoding="utf-8",errors="replace") as fh: src = fh.read()
                    new = patch_func(src)
                    if new != src:
                        with open(fp,"w",encoding="utf-8") as fh: fh.write(new)
                        n_changed += 1
                except Exception: continue
        if n_changed == 0: return {"ok": False, "info": "patch made no changes"}
        subprocess.check_call(["git","-C",work,"checkout","-b",branch])
        subprocess.check_call(["git","-C",work,"config","user.email","qa-autofix@dchub.cloud"])
        subprocess.check_call(["git","-C",work,"config","user.name","DC Hub QA Autofix"])
        subprocess.check_call(["git","-C",work,"add","-A"])
        subprocess.check_call(["git","-C",work,"commit","-m",title])
        subprocess.check_call(["git","-C",work,"push","origin",branch], stderr=subprocess.DEVNULL)
        import urllib.request as _ur
        api = f"https://api.github.com/repos/{repo}/pulls"
        payload = json.dumps({"title": title, "body": body, "head": branch, "base": "main"}).encode("utf-8")
        req = _ur.Request(api, data=payload, headers={
            "Authorization": f"token {gh}", "Accept": "application/vnd.github+json",
            "Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "info": f"PR opened: {d.get('html_url')} ({n_changed} files)"}
    except Exception as e:
        return {"ok": False, "info": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)

def run_auto_fix(pattern_id):
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT fix_func_name, fix_args_json FROM qa_patterns WHERE id=%s", (pattern_id,))
        row = cur.fetchone()
        if not row: return {"ok": False, "info": "pattern not found"}
        fn_name, args = row
        if not fn_name: return {"ok": False, "info": "no fix_func_name"}
        fn = FIX_REGISTRY.get(fn_name)
        if not fn: return {"ok": False, "info": f"unknown fix function: {fn_name}"}
        try: result = fn(**(args or {})) or {}
        except Exception as e: result = {"ok": False, "info": f"{type(e).__name__}: {str(e)[:300]}"}
        cur.execute("UPDATE qa_patterns SET auto_fix_at=NOW(), auto_fix_ok=%s, auto_fix_info=%s WHERE id=%s",
            (bool(result.get("ok")), str(result.get("info",""))[:1000], pattern_id))
        cur.execute("INSERT INTO qa_fix_log (pattern_id, ok, info) VALUES (%s, %s, %s)",
            (pattern_id, bool(result.get("ok")), str(result.get("info",""))[:1000]))
        c.commit()
    return result

@qa_patterns_bp.route("/api/v1/qa/patterns", methods=["GET"])
def list_patterns():
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT id, signature, test_name, http_code, severity,
            fix_func_name, hit_count, first_seen, last_seen, auto_fix_at,
            auto_fix_ok, auto_fix_info, novel,
            substr(example_detail,1,200) AS example_detail
            FROM qa_patterns ORDER BY last_seen DESC LIMIT 200""")
        rows = cur.fetchall()
    for r in rows:
        for k in ("first_seen","last_seen","auto_fix_at"):
            if r.get(k): r[k] = r[k].isoformat()
    return jsonify(patterns=rows, count=len(rows), registered_fixes=list(FIX_REGISTRY.keys())), 200

@qa_patterns_bp.route("/api/v1/qa/patterns/<int:pattern_id>/fix", methods=["POST"])
def fix_pattern(pattern_id):
    if request.args.get("dry") == "1":
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT fix_func_name, fix_args_json FROM qa_patterns WHERE id=%s", (pattern_id,))
            row = cur.fetchone()
        return jsonify(dry_run=True, would_run=row), 200
    res = run_auto_fix(pattern_id)
    return jsonify(res), (200 if res.get("ok") else 500)

@qa_patterns_bp.route("/api/v1/qa/patterns/<int:pattern_id>/assign", methods=["POST"])
def assign_fix(pattern_id):
    body = request.get_json(silent=True) or {}
    fn = body.get("fix_func_name"); args = body.get("fix_args_json") or {}
    if fn and fn not in FIX_REGISTRY:
        return jsonify(error=f"unknown fix function {fn}", valid_funcs=list(FIX_REGISTRY.keys())), 400
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE qa_patterns SET fix_func_name=%s, fix_args_json=%s WHERE id=%s",
            (fn, json.dumps(args), pattern_id))
        c.commit()
    return jsonify(ok=True), 200

@qa_patterns_bp.route("/api/v1/qa/coverage", methods=["GET"])
def coverage():
    from flask import current_app
    _ensure_tables()
    tested_urls = set()
    try:
        from routes.site_qa import TESTS
        for t in TESTS: tested_urls.add(t.get("url","").rstrip("/"))
    except Exception: pass
    rules = []
    for r in current_app.url_map.iter_rules():
        if "static" in r.rule or r.rule.startswith("/api/v1/qa"): continue
        url = r.rule.rstrip("/")
        rules.append({"rule": r.rule, "methods": sorted(r.methods - {"HEAD","OPTIONS"}),
            "endpoint": r.endpoint,
            "tested": (url in tested_urls or any(url.startswith(t) for t in tested_urls if t))})
    untested_p0 = [r for r in rules if not r["tested"] and "GET" in r["methods"] and "<" not in r["rule"]]
    return jsonify(total_routes=len(rules),
        tested=sum(1 for r in rules if r["tested"]),
        untested=sum(1 for r in rules if not r["tested"]),
        untested_static_get=len(untested_p0),
        sample_untested=[r["rule"] for r in untested_p0[:30]]), 200

@qa_patterns_bp.route("/api/v1/qa/learn", methods=["POST"])
def learn_novel():
    _ensure_tables()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT id, test_name, http_code, example_detail, hit_count
            FROM qa_patterns WHERE fix_func_name IS NULL AND hit_count >= 3
            ORDER BY hit_count DESC LIMIT 20""")
        novel = cur.fetchall()
    suggestions = []
    for n in novel:
        s = _heuristic_suggest(n)
        if s: suggestions.append({"pattern_id": n["id"], **s})
    return jsonify(novel_count=len(novel), suggestions=suggestions), 200

def _heuristic_suggest(pattern):
    detail = (pattern.get("example_detail") or "").lower()
    if "content security policy" in detail or "violates the following" in detail:
        m_url = re.search(r"(https?://[^\s'\"]+)", detail)
        m_dir = re.search(r"directive:\s*[\"']([^\"']+)[\"']", detail)
        return {"fix_func_name": "fix_csp_add_directive",
            "fix_args_json": {"directive": (m_dir.group(1).split(" ")[0] if m_dir else "style-src-elem https://fonts.googleapis.com"),
                              "url": (m_url.group(1) if m_url else "https://fonts.googleapis.com")}}
    if "placeholder leak" in detail or "__$" in detail:
        return {"fix_func_name": "fix_placeholder_replace",
            "fix_args_json": {"pattern": r"__\$+__", "replacement": "—"}}
    if "stale" in detail or "older than" in detail:
        return {"fix_func_name": "fix_trigger_refresh",
            "fix_args_json": {"refresh_url": "/api/v1/data-pulse/run"}}
    return None
