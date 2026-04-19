#!/usr/bin/env python3
"""Patch 3: Surface founding spots counter in the upgrade UI (not just console)."""
import glob, re

# The upgrade modal button — inject urgency banner above it
UPGRADE_BTN_PATTERNS = [
    r'(Upgrade to Pro.*?\$99/mo)',
    r'(🚀 Upgrade to Pro)',
    r'(Upgrade to Pro —)',
]

URGENCY_BANNER = '''<div id="founding-urgency" style="
  background:linear-gradient(135deg,#1a0a3e,#2d1b6e);
  border:1px solid #7c3aed;border-radius:8px;
  padding:10px 16px;margin-bottom:12px;text-align:center;font-size:13px;color:#c4b5fd;">
  ⚡ <strong style="color:#a78bfa;">Founding member pricing</strong> —
  <span id="founding-count" style="color:#fbbf24;font-weight:700;">47</span> of 50 spots remaining.
  Price locks in at $99/mo forever.
</div>
<script>
// Fetch live founding count
fetch('/api/v1/founding-spots').then(r=>r.json()).then(d=>{
  var el=document.getElementById('founding-count');
  if(el&&d.remaining!==undefined) el.textContent=d.remaining;
}).catch(()=>{});
</script>'''

html_files = (glob.glob("static/**/*.html", recursive=True) +
              glob.glob("templates/**/*.html", recursive=True) +
              glob.glob("*.html"))

js_files = glob.glob("static/**/*.js", recursive=True) + glob.glob("*.js")
changed = 0

for path in html_files + js_files:
    try:
        src = open(path).read()
        new = src
        for pattern in UPGRADE_BTN_PATTERNS:
            if re.search(pattern, new, re.IGNORECASE):
                new = re.sub(
                    pattern,
                    URGENCY_BANNER + r'\1',
                    new, count=1, flags=re.IGNORECASE
                )
                break
        if new != src:
            open(path, "w").write(new)
            print(f"✅ Patched: {path}")
            changed += 1
    except Exception as e:
        print(f"  skip {path}: {e}")

print(f"\nDone — {changed} file(s) patched.")
print("\nAlso add this endpoint to main.py if not present:")
print("""
@app.route('/api/v1/founding-spots')
def founding_spots():
    # Pull from DB or hardcode and decrement as subscriptions come in
    remaining = 47  # update this dynamically
    return jsonify({'remaining': remaining, 'total': 50})
""")
print("git add -A && git commit -m 'growth: show founding spots urgency in upgrade modal' && git push")
