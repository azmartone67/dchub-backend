"""/pockets must not hand a credentialed render to the next anonymous caller.

NO NETWORK — evaluates the pinned canon through scripts/cf_expression.py.

★ 2026-09-24 (canon repinned at ruleset v69). Rule 16 caches /pockets for 3600s
with override_origin, and rule 24's credential bypass did not cover it. Measured
live before the fix: a request carrying X-API-Key or a dchub_token cookie
MISSed, and the next ANONYMOUS request received that paid render (HIT age 2) —
/pockets renders full ranked rows for paid callers. The fix was made in the
zone first; this pins it so a later repin or canon edit cannot drop it quietly.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "scripts" / "cf_cache_ruleset_canon.json").read_text())["rules"]
_spec = importlib.util.spec_from_file_location("_cfexpr_pockets", ROOT / "scripts" / "cf_expression.py")
X = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(X)


def _verdict(req):
    verdict, _, err = X.disposition(RULES, req)
    assert err is None, err
    return verdict


@pytest.mark.parametrize("path", ["/pockets", "/pockets/ashburn-va"])
@pytest.mark.parametrize("channel", [{"headers": ("x-api-key",)}, {"cookies": ("dchub_token",)},
                                     {"headers": ("authorization",)}])
def test_credentialed_pockets_request_bypasses_the_edge(path, channel):
    assert _verdict(X.Request(path, **channel)) == "bypass"


def test_anonymous_pockets_is_still_cached():
    """Control: the bypass is credential-keyed, not a blanket no-cache. Rule 16
    caches bare /pockets only (live: HIT); /pockets/<slug> has no cache rule."""
    assert _verdict(X.Request("/pockets")) == "cached"
