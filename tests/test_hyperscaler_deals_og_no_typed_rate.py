"""/hyperscaler-deals types no deal RATE in its link preview (2026-09-23).

The landing page's og:description said "Live $1B+/week AI capex deals".
Nothing computes a weekly rate at render: it was typed copy, shown as fact in
every link preview. Removed (owner decision), alongside the same "$1B+/week
cadence" sentence in the LinkedIn hyperscaler post (#5368).

"$1B+" on its own is a different, legitimate claim (the tracker's deal-size
threshold) and is not what this pins.
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("flask")


def _landing_html():
    from flask import Flask
    from routes import hyperscaler_deals as hd
    app = Flask(__name__)
    app.register_blueprint(hd.hyperscaler_deals_bp)
    resp = app.test_client().get("/hyperscaler-deals")
    assert resp.status_code == 200, resp.data[:300]
    return resp.get_data(as_text=True)


def test_og_description_types_no_weekly_deal_rate():
    html = _landing_html()

    og = re.search(r'<meta property="og:description" content="([^"]*)">', html)
    # The control: the tag is served and still describes the feed.
    assert og and "AI capex deals" in og.group(1), og and og.group(1)
    # No rate anywhere on the served page, the preview included.
    assert "/week" not in html, re.findall(r".{0,60}/week.{0,20}", html)
