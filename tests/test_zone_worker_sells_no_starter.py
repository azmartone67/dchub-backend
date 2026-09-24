"""r-sku-wall (2026-09-24): the zone worker's discovery surfaces sell no Starter.

Starter ($9) is retired from every offer (owner, 2026-09-24; be#5410). At
WORKER_VERSION 4.9.75 the three pricing blocks this worker serves —
/.well-known/mcp.json (api.dchub.cloud and dchub.cloud) and
/.well-known/mcp/server-card.json — still listed "starter: $9/mo" plus a raw
Starter Payment Link (starter_url), and the unlock_more_data fallback
description said "Also $9/mo Starter". Comments are stripped first: the
changelog records the history, it is not an offer.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _js_without_comments(path):
    """Drop only what is unambiguously a comment: /** … */ doc blocks and
    whole-line // comments. A general /* … */ strip also eats string globs
    like '/api/*' up to the next */ — measured: it swallowed a pricing block."""
    src = (ROOT / path).read_text(encoding="utf-8")
    src = re.sub(r"/\*\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//[^\n]*$", "", src)


def test_zone_worker_sells_no_starter():
    js = _js_without_comments("worker.js")
    assert not re.search(r"\$9/mo", js)
    assert "starter_url" not in js
    assert "8x2dRa5sS0x75uteGuaZi0g" not in js  # the Starter Payment Link


def test_each_pricing_block_offers_the_pack_instead():
    js = _js_without_comments("worker.js")
    assert js.count("credit_pack: '$10 one-time") == 3


def test_the_scan_sees_code_not_just_comments():
    # a stripper that ate everything would pass the ban vacuously
    js = _js_without_comments("worker.js")
    assert "const WORKER_VERSION = '" in js
    assert "developer_url:" in js
