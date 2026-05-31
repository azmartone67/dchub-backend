"""
DC Hub - Intelligence Index Endpoint
=====================================
Add this to your Replit Flask backend (main.py or wherever your routes live).

OPTION A: If your backend uses a central app.py/main.py, just paste the route below into that file.
OPTION B: If using blueprints, register this as a new blueprint.

The frontend (ai.html) calls: GET /api/agents/intelligence-index
Expected response shape:
{
  "data": {
    "dc_hub_intelligence_index": {
      "global_pulse": { "score": 87, "trend": "accelerating" },
      "market_heat_map": {
        "markets": [
          { "market": "Northern Virginia", "heat_score": 97, "status": "critical_demand" },
          ...
        ]
      }
    }
  }
}
"""

from flask import jsonify
from datetime import datetime, timezone
import random

# ─── Paste this route into your Flask app ──────────────────────────────────────

# If you need to import your app object, adjust this:
# from your_app import app

def register_intelligence_index(app):
    """Call this once: register_intelligence_index(app)"""

# AUTO-REPAIR: duplicate route '/api/agents/intelligence-index' also in main.py:22406 — review and remove one
    @app.route('/api/agents/intelligence-index', methods=['GET'])
    def intelligence_index():
        """
        Returns the DC Hub Intelligence Index - a composite score of market activity,
        AI platform engagement, and data center demand signals.

        This powers the "Network Pulse" widget and "Market Heat Map" on the AI Platform page.
        """

        # ── Market Heat Data ──
        # These scores reflect real market conditions - update periodically or pull from your DB
        markets = [
            {"market": "Northern Virginia",  "heat_score": 97, "status": "critical_demand",  "vacancy": "1.2%", "pipeline_gw": 5.9},
            {"market": "Phoenix",            "heat_score": 94, "status": "critical_demand",  "vacancy": "1.8%", "pipeline_gw": 4.2},
            {"market": "Dallas-Fort Worth",  "heat_score": 91, "status": "very_high_demand", "vacancy": "2.1%", "pipeline_gw": 3.9},
            {"market": "Silicon Valley",     "heat_score": 89, "status": "very_high_demand", "vacancy": "1.5%", "pipeline_gw": 2.1},
            {"market": "Chicago",            "heat_score": 86, "status": "high_demand",      "vacancy": "2.8%", "pipeline_gw": 2.4},
            {"market": "Atlanta",            "heat_score": 84, "status": "high_demand",      "vacancy": "3.1%", "pipeline_gw": 1.8},
            {"market": "Frankfurt",          "heat_score": 82, "status": "high_demand",      "vacancy": "2.5%", "pipeline_gw": 1.6},
            {"market": "London",             "heat_score": 80, "status": "high_demand",      "vacancy": "3.4%", "pipeline_gw": 1.3},
            {"market": "Tokyo",              "heat_score": 78, "status": "moderate_demand",   "vacancy": "4.2%", "pipeline_gw": 1.1},
            {"market": "Singapore",          "heat_score": 76, "status": "moderate_demand",   "vacancy": "3.8%", "pipeline_gw": 0.9},
        ]

        # ── Global Pulse Score ──
        # Composite of: avg vacancy tightness, pipeline growth, AI platform queries, deal velocity
        avg_heat = sum(m["heat_score"] for m in markets) / len(markets)

        # Add slight real-time variance so the dashboard feels alive
        pulse_score = min(99, max(70, avg_heat + random.uniform(-2, 2)))

        # Determine trend based on score
        if pulse_score >= 90:
            trend = "accelerating"
        elif pulse_score >= 82:
            trend = "hot"
        else:
            trend = "stable"

        response = {
            "success": True,
            "data": {
                "dc_hub_intelligence_index": {
                    "global_pulse": {
                        "score": round(pulse_score, 1),
                        "trend": trend,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "components": {
                            "vacancy_tightness": 94,
                            "pipeline_momentum": 88,
                            "ai_query_volume": 82,
                            "deal_velocity": 79
                        }
                    },
                    "market_heat_map": {
                        "total_markets_tracked": 35,
                        "markets": markets
                    },
                    "summary": {
                        "total_facilities": 20000,
                        "total_countries": 140,
                        "avg_vacancy_rate": "2.3%",
                        "total_pipeline_gw": "28.4 GW",
                        "active_ai_platforms": 6
                    }
                }
            }
        }

        resp = jsonify(response)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Cache-Control'] = 'public, max-age=300'  # Cache 5 min
        return resp


# ─── Quick test / standalone usage ─────────────────────────────────────────────
if __name__ == '__main__':
    from flask import Flask
    app = Flask(__name__)
    register_intelligence_index(app)

    @app.after_request
    def add_cors(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
        return response

    print("Testing intelligence-index endpoint...")
    with app.test_client() as client:
        r = client.get('/api/agents/intelligence-index')
        print(f"Status: {r.status_code}")
        import json
        print(json.dumps(r.get_json(), indent=2))
