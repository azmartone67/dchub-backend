"""/enterprise renders the approved customer quote, attributed to a person.

The public /enterprise page is THIS template (routes/enterprise.py), not the
frontend repo's enterprise.html, which the worker never serves for that path.
A quote added to the frontend file alone ships nowhere, so the page itself is
the thing to check.
"""
import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

from routes.enterprise import enterprise_bp  # noqa: E402


def test_enterprise_page_shows_named_customer_quote():
    app = Flask(__name__)
    app.register_blueprint(enterprise_bp)
    body = app.test_client().get("/enterprise").get_data(as_text=True)
    assert 'class="cq"' in body
    assert "DC Hub is now integral to how we evaluate every site." in body
    assert "Rich Bray" in body and "Development Manager, LPI Group" in body
    assert 'href="/testimonials"' in body
    # The template placeholders are still filled after the insert.
    assert "{{" not in body
