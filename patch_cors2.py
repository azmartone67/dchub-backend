#!/usr/bin/env python3
"""
Run on Replit: python3 patch_cors2.py
Adds CORS to list_press_releases by finding the return jsonify(rows) line in that function.
"""

content = open('main.py').read()

# Find the list_press_releases function and patch its return statement
# The function returns jsonify(rows) — we need to wrap it with CORS headers

# Strategy: find the specific pattern inside list_press_releases
old = '        return jsonify(rows)\n\n    @app.route("/api/press-releases/<slug>"'
new = '''        from flask import make_response
        resp = make_response(jsonify(rows))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        return resp

    @app.route("/api/press-releases/<slug>"'''

if old in content:
    content = content.replace(old, new)
    open('main.py', 'w').write(content)
    print("✅  CORS added to list_press_releases")
    print("   Run: git add main.py && git commit -m 'fix: CORS on list_press_releases' && git push")
else:
    # Try to find what the actual context looks like
    idx = content.find('def list_press_releases')
    if idx == -1:
        print("❌  list_press_releases function not found in main.py")
    else:
        # Show the function so we can identify the pattern
        snippet = content[idx:idx+600]
        print("Function found but pattern didn't match. Snippet:")
        print("---")
        print(snippet)
        print("---")
        print("\nManual fix: find 'return jsonify(rows)' inside list_press_releases and replace with:")
        print("""
        from flask import make_response
        resp = make_response(jsonify(rows))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
""")
