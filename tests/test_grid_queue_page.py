"""
/grid/queue/<iso> — the per-ISO interconnection-queue SEO pages
(routes/grid_public_routes.py, 2026-08-02 query-win wave).

House rules: pytest functions only, no module-scope work, never import
main.py. The blueprint mounts on a bare Flask app; the DB layer is
replaced by monkeypatching `_queue_page_data` (its own DB path lazy-imports
main, which tests never do).
"""
import datetime

import pytest


def _canned():
    return {
        "total_count": 1866,
        "total_mw": 212_345.0,
        "active_count": 1512,
        "active_mw": 171_000.0,
        "dc_count": 41,
        "dc_mw": 9_800.0,
        "fuels": [("Solar", 700, 90_000.0), ("Storage", 500, 60_000.0),
                  ("Load", 41, 9_800.0)],
        "statuses": [("IA FULLY EXECUTED", 900, 120_000.0),
                     ("Unknown", 100, 9_000.0)],
        "top": [("Example Solar One", "Pecos", "TX", "Solar", 1200.0,
                 "IA FULLY EXECUTED", datetime.date(2024, 5, 1))],
        "queue_date_min": datetime.date(2016, 1, 4),
        "queue_date_max": datetime.date(2026, 7, 28),
    }


@pytest.fixture()
def queue_client(monkeypatch):
    import routes.grid_public_routes as gpr
    gpr._QUEUE_PAGE_CACHE.clear()
    monkeypatch.setattr(gpr, "_queue_page_data", lambda iso: _canned())
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(gpr.grid_public_bp)
    with app.test_client() as client:
        yield client


def test_ercot_queue_page_renders_counts_in_title(queue_client):
    r = queue_client.get("/grid/queue/ercot")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    title = body.split("<title>", 1)[1].split("</title>", 1)[0]
    # Count+year title format — the measured winner on this SERP class.
    assert "ERCOT Interconnection Queue" in title
    assert "1,866" in title and "Projects" in title
    assert "212 GW" in title
    assert str(datetime.datetime.utcnow().year) in title


def test_ercot_queue_page_body_tables_and_links(queue_client):
    body = queue_client.get("/grid/queue/ercot").get_data(as_text=True)
    # Crawlable tables from the canned rows
    assert "Example Solar One" in body and "Pecos" in body
    assert "IA FULLY EXECUTED" in body
    # Dataset JSON-LD + MCP discovery meta
    assert '"@type": "Dataset"' in body
    assert "get_interconnection_queue" in body
    # Interlinks + money path
    for href in ('href="/grid/ercot"', 'href="/interconnection-queues"',
                 'href="/pricing"', 'href="/enterprise"', 'href="/api-docs"'):
        assert href in body, f"missing link {href}"
    # Honesty line: queued MW is not delivered MW, and this feed has no
    # commercial-operation dates.
    assert "never reaches commercial operation" in body
    assert "canonical" in body and "/grid/queue/ercot" in body


def test_queue_page_db_down_serves_shell_without_numbers(queue_client, monkeypatch):
    import routes.grid_public_routes as gpr
    gpr._QUEUE_PAGE_CACHE.clear()
    monkeypatch.setattr(gpr, "_queue_page_data", lambda iso: None)
    r = queue_client.get("/grid/queue/ercot")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # No fabricated figures: the shell carries links, not counts.
    assert "1,866" not in body and "GW" not in body
    assert "/api/v1/interconnection-queue/by-iso" in body


def test_queue_page_db_down_prefers_stale_cache(queue_client, monkeypatch):
    import routes.grid_public_routes as gpr
    # Warm the cache with real data, then kill the DB and expire the TTL.
    warm = queue_client.get("/grid/queue/ercot").get_data(as_text=True)
    assert "1,866" in warm
    path_key = "/grid/queue/ercot"
    html, _ts = gpr._QUEUE_PAGE_CACHE[path_key]
    gpr._QUEUE_PAGE_CACHE[path_key] = (html, 0.0)   # long expired
    monkeypatch.setattr(gpr, "_queue_page_data", lambda iso: None)
    body = queue_client.get("/grid/queue/ercot").get_data(as_text=True)
    assert "1,866" in body, "stale cache should beat the numberless shell"


def test_queue_routing_redirects(queue_client):
    # Bare /grid/queue would otherwise fall into /grid/<iso> as 'queue' → 404.
    r = queue_client.get("/grid/queue")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/interconnection-queues")
    # Casing canonicalizes to lowercase like /grid/<iso>.
    r = queue_client.get("/grid/queue/ERCOT")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/grid/queue/ercot")
    # Not-yet-shipped ISOs land on the all-ISO page, never a 404.
    r = queue_client.get("/grid/queue/pjm")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/interconnection-queues")


def test_queue_sql_reads_the_table_that_exists():
    """Same naming fence as tests/test_dead_read_sweep.py: the table is
    interconnect_queue (not interconnection_queue) and the column is
    queue_status (not status)."""
    import inspect
    import routes.grid_public_routes as gpr
    src = inspect.getsource(gpr._queue_page_data)
    assert "FROM interconnect_queue" in src
    assert "FROM interconnection_queue" not in src
    assert "queue_status" in src
    assert "COALESCE(status," not in src


def test_queue_page_is_in_the_main_sitemap():
    """Deletion tripwire: the sitemap tuple and the route must move together."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
        blob = f.read()
    assert "'/grid/queue/ercot'" in blob
    with open(os.path.join(root, "routes", "sitemap_auto.py"),
              encoding="utf-8") as f:
        blob2 = f.read()
    assert '"/grid/queue/ercot"' in blob2
