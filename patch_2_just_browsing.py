#!/usr/bin/env python3
"""Patch 2: Remove 'Just Browsing' escape hatch — replace with email capture."""
import glob, re

OLD_BUTTON = 'Just Browsing'

NEW_HTML = '''<div style="margin-top:12px;">
  <input type="email" id="guest-email" placeholder="Enter your email to browse free"
    style="width:100%;padding:10px 14px;border-radius:8px;border:1px solid #444;
           background:#1a1a2e;color:#fff;font-size:14px;box-sizing:border-box;">
  <button onclick="
    var e=document.getElementById('guest-email').value;
    if(!e||!e.includes('@')){alert('Please enter a valid email.');return;}
    fetch('/api/v1/guest-browse',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:e})}).catch(()=>{});
    document.getElementById('sign-in-gate')&&document.getElementById('sign-in-gate').remove();
    document.querySelector('.gate-modal')&&document.querySelector('.gate-modal').remove();
  " style="margin-top:8px;width:100%;padding:10px;border-radius:8px;
           background:#333;color:#aaa;border:1px solid #555;cursor:pointer;font-size:13px;">
    Continue browsing (5 layers free) →
  </button>
</div>'''

html_files = (glob.glob("static/**/*.html", recursive=True) +
              glob.glob("templates/**/*.html", recursive=True) +
              glob.glob("*.html"))

js_files = glob.glob("static/**/*.js", recursive=True) + glob.glob("*.js")

changed = 0
for path in html_files + js_files:
    try:
        src = open(path).read()
        if OLD_BUTTON not in src:
            continue
        # Replace the Just Browsing button/link
        new = re.sub(
            r'<[^>]*(just.?brows|Just Browsing)[^>]*>.*?</[^>]+>',
            NEW_HTML,
            src, flags=re.IGNORECASE | re.DOTALL
        )
        # Fallback: plain text link
        if new == src:
            new = src.replace(
                'Just Browsing',
                'Continue with email (5 free layers)'
            )
        if new != src:
            open(path, "w").write(new)
            print(f"✅ Patched: {path}")
            changed += 1
    except Exception as e:
        print(f"  skip {path}: {e}")

print(f"\nDone — {changed} file(s) patched.")
print("git add -A && git commit -m 'growth: replace Just Browsing with email capture' && git push")
