"""routes/citation_signals — ingest re-classifies, read keeps its shape.

House rule: never import main.py. The blueprint is mounted on a bare Flask app
and the DB is replaced by an in-memory fake of ai_tracking._execute.
"""
import pytest

flask = pytest.importorskip("flask")
import routes.citation_signals as cs  # noqa: E402
import ai_citation_signals as acs  # noqa: E402 — plain import: a missing module must FAIL, not skip

CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


class FakeDB:
    def __init__(self):
        self.rows = {}   # (day, class, source, agent, path) -> hits
        self.fail = None

    def __call__(self, sql, params=None, fetchall=False):
        if self.fail:
            raise self.fail
        if "CREATE TABLE" in sql:
            return None
        if "ON CONFLICT" in sql:
            key = tuple(params[:5])
            self.rows[key] = self.rows.get(key, 0) + 1
            return None
        if "GROUP BY signal_class, source, agent" in sql:
            agg = {}
            for (d, c, s, a, p), n in self.rows.items():
                agg[(c, s, a)] = agg.get((c, s, a), 0) + n
            return [{"signal_class": c, "source": s, "agent": a, "hits": n}
                    for (c, s, a), n in agg.items()]
        if "GROUP BY day" in sql:
            agg = {}
            for (d, c, s, a, p), n in self.rows.items():
                agg[(d, c, s)] = agg.get((d, c, s), 0) + n
            return [{"day": d, "signal_class": c, "source": s, "hits": n}
                    for (d, c, s), n in sorted(agg.items())]
        if "GROUP BY landing_path" in sql:
            klass = sql.split("signal_class = '")[1].split("'")[0]
            agg = {}
            for (d, c, s, a, p), n in self.rows.items():
                if c == klass:
                    agg[(p, s)] = agg.get((p, s), 0) + n
            return [{"landing_path": p, "source": s, "hits": n}
                    for (p, s), n in sorted(agg.items(), key=lambda kv: -kv[1])]
        raise AssertionError(sql)


@pytest.fixture
def client(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(cs, "_execute", db)
    acs._ddl_done = False
    app = flask.Flask(__name__)
    app.register_blueprint(cs.citation_signals_bp)
    c = app.test_client()
    c.db = db
    return c


def _hit(client, **kw):
    return client.post("/api/ai/citation-hit", json=kw).get_json()


def test_ingest_classifies_and_records(client):
    r = _hit(client, path="/markets/ashburn", user_agent="ChatGPT-User/1.0")
    assert r == {"status": "recorded", "signal_class": "user_fetch",
                 "source": "openai", "agent": "ChatGPT-User"}
    r = _hit(client, path="/pricing", query="utm_source=chatgpt.com",
             user_agent=CHROME, fetch_dest="document")
    assert r["signal_class"] == "assistant_referral"
    assert r["status"] == "recorded"


def test_ingest_ignores_caller_supplied_class(client):
    r = _hit(client, path="/x", user_agent=CHROME,
             signal_class="assistant_referral", source="openai")
    assert r["status"] == "skipped"
    assert client.db.rows == {}


def test_ingest_skips_plain_browser_from_google(client):
    r = _hit(client, path="/x", user_agent=CHROME,
             referer="https://www.google.com/")
    assert r["status"] == "skipped"
    assert client.db.rows == {}


def test_ingest_db_failure_is_not_a_500(client):
    client.db.fail = RuntimeError("down")
    resp = client.post("/api/ai/citation-hit",
                       json={"path": "/x", "user_agent": "GPTBot/1.4"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "not_recorded"


def test_read_endpoint_shape_and_counts(client):
    _hit(client, path="/markets/ashburn", user_agent="Claude-User/1.0")
    _hit(client, path="/markets/ashburn", user_agent="Claude-User/1.0")
    _hit(client, path="/x", user_agent="GPTBot/1.4")
    _hit(client, path="/pricing?utm_source=chatgpt.com",
         query="utm_source=chatgpt.com", user_agent=CHROME)
    _hit(client, path="/pricing", referer="https://claude.ai/",
         user_agent=CHROME)
    resp = client.get("/api/v1/ai/citation-signals")
    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    body = resp.get_json()
    for key in ("success", "generated_at", "classes", "sources", "windows",
                "daily", "top_landing_paths", "method", "degraded"):
        assert key in body, key
    assert body["degraded"] is None
    assert set(body["windows"]) == {"today", "7d", "30d"}
    w = body["windows"]["7d"]
    assert w["by_class"]["user_fetch"] == 2
    assert w["by_class"]["training_crawler"] == 1
    assert w["by_class"]["assistant_referral"] == 2
    assert w["by_class_source"]["assistant_referral"] == {
        "openai": 1, "anthropic": 1}
    assert w["total"] == 5
    refs = body["top_landing_paths"]["assistant_referral"]["7d"]
    assert {"path": "/pricing", "source": "openai", "hits": 1} in refs
    assert body["top_landing_paths"]["user_fetch"]["7d"][0] == {
        "path": "/markets/ashburn", "source": "anthropic", "hits": 2}
    assert body["daily"] and body["daily"][0]["hits"] >= 1


def test_read_endpoint_unknown_is_null_not_zero(client):
    client.db.fail = RuntimeError('relation "ai_citation_daily" does not exist')
    body = client.get("/api/v1/ai/citation-signals").get_json()
    assert body["windows"] == {"today": None, "7d": None, "30d": None}
    assert body["daily"] is None
    assert "unknown, not zero" in body["degraded"]
