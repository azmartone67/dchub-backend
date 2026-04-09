#!/usr/bin/env python3
"""
Run on Replit: python3 patch_cors.py
Adds CORS headers to the press-releases endpoints in main.py
"""

content = open('main.py').read()
changes = 0

# Fix 1: Add CORS to list_press_releases
old1 = '''def list_press_releases():
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur = conn.cursor()'''

new1 = '''def list_press_releases():
        from flask import make_response
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur = conn.cursor()'''

old1_return = '''        return jsonify(rows)

    @app.route("/api/press-releases/<slug>"'''

new1_return = '''        resp = make_response(jsonify(rows))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        return resp

    @app.route("/api/press-releases/<slug>"'''

# Fix 2: Add CORS to get_press_release
old2_return = '''        return jsonify({"id":r[0],"title":r[1],"slug":r[2],"category":r[3],"date":r[4],"subheadline":r[5],"body":r[6],"meta_description":r[7]})'''

new2_return = '''        resp_data = {"id":r[0],"title":r[1],"slug":r[2],"category":r[3],"date":str(r[4]) if r[4] else None,"subheadline":r[5],"body":r[6],"meta_description":r[7]}
        from flask import make_response
        resp = make_response(jsonify(resp_data))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        return resp'''

# Apply fixes
for old, new, label in [
    (old1_return, new1_return, 'CORS on list_press_releases'),
    (old2_return, new2_return, 'CORS on get_press_release'),
]:
    if old in content:
        content = content.replace(old, new)
        changes += 1
        print(f"✅  {label}")
    else:
        print(f"⚠️   Pattern not found for: {label} — add manually")

if changes > 0:
    open('main.py', 'w').write(content)
    print(f"\n✅  Saved {changes} fix(es)")
    print("   Run: git add main.py && git commit -m 'fix: CORS headers on press-releases API' && git push")
else:
    print("\n❌  No changes — check main.py manually")
