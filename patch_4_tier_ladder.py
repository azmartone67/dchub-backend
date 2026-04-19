#!/usr/bin/env python3
"""Patch 4: Add Developer tier ($49/mo) to upgrade modal so users see the full ladder."""
import glob, re

# What the modal currently shows (single Pro button)
OLD_PATTERNS = [
    r'(\$99/mo · Cancel anytime)',
    r'(Cancel anytime)',
]

TIER_LADDER = '''<div style="display:flex;gap:10px;margin-bottom:8px;flex-wrap:wrap;">

  <!-- Developer tier -->
  <div style="flex:1;min-width:180px;border:1px solid #444;border-radius:10px;
              padding:16px;background:#111;text-align:center;">
    <div style="font-size:12px;color:#9ca3af;text-transform:uppercase;
                letter-spacing:.05em;margin-bottom:4px;">Developer</div>
    <div style="font-size:26px;font-weight:700;color:#fff;">$49<span
      style="font-size:14px;color:#9ca3af;">/mo</span></div>
    <div style="font-size:12px;color:#6b7280;margin:6px 0 12px;">
      1,000 API calls/day<br>All infrastructure layers<br>Full data export
    </div>
    <a href="/pricing#developer" style="display:block;padding:9px;border-radius:7px;
       background:#1d4ed8;color:#fff;font-size:13px;font-weight:600;text-decoration:none;">
      Start Developer →
    </a>
  </div>

  <!-- Pro tier (highlighted) -->
  <div style="flex:1;min-width:180px;border:2px solid #7c3aed;border-radius:10px;
              padding:16px;background:#0f0a1e;text-align:center;position:relative;">
    <div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);
                background:#7c3aed;color:#fff;font-size:10px;font-weight:700;
                padding:2px 10px;border-radius:10px;letter-spacing:.05em;">MOST POPULAR</div>
    <div style="font-size:12px;color:#a78bfa;text-transform:uppercase;
                letter-spacing:.05em;margin-bottom:4px;">Pro</div>
    <div style="font-size:26px;font-weight:700;color:#fff;">$99<span
      style="font-size:14px;color:#9ca3af;">/mo</span></div>
    <div style="font-size:12px;color:#6b7280;margin:6px 0 12px;">
      10,000 API calls/day<br>Site scoring & evaluation<br>PDF & KMZ export
    </div>
    <a href="/pricing#pro" style="display:block;padding:9px;border-radius:7px;
       background:#7c3aed;color:#fff;font-size:13px;font-weight:600;text-decoration:none;">
      🚀 Upgrade to Pro →
    </a>
  </div>

</div>
<div style="text-align:center;margin-top:4px;">
  <a href="/pricing" style="font-size:12px;color:#6b7280;text-decoration:none;">
    Compare all plans →
  </a>
</div>'''

html_files = (glob.glob("static/**/*.html", recursive=True) +
              glob.glob("templates/**/*.html", recursive=True) +
              glob.glob("*.html"))
js_files = glob.glob("static/**/*.js", recursive=True) + glob.glob("*.js")
changed = 0

for path in html_files + js_files:
    try:
        src = open(path).read()
        # Look for the single upgrade button block
        new = re.sub(
            r'<button[^>]*Upgrade to Pro[^<]*</button>',
            TIER_LADDER,
            src, count=1, flags=re.IGNORECASE | re.DOTALL
        )
        # Also try anchor tag version
        if new == src:
            new = re.sub(
                r'<a[^>]*Upgrade to Pro[^<]*</a>',
                TIER_LADDER,
                src, count=1, flags=re.IGNORECASE | re.DOTALL
            )
        if new != src:
            open(path, "w").write(new)
            print(f"✅ Patched: {path}")
            changed += 1
    except Exception as e:
        print(f"  skip {path}: {e}")

if changed == 0:
    print("⚠️  Could not auto-patch — the upgrade button pattern didn't match.")
    print("Manually replace your upgrade button with the HTML below and save to your modal file:\n")
    print(TIER_LADDER)
else:
    print(f"\nDone — {changed} file(s) patched.")

print("\ngit add -A && git commit -m 'growth: add Developer $49 tier to upgrade modal' && git push")
