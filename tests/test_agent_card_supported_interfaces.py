"""A2A v1.0 supportedInterfaces on the live agent card (routes/agent_a2a.py).

Cloudflare Agent Readiness failed the card with 'Missing or empty required
field "supportedInterfaces"'. The entry must name the same endpoint and
binding as the v0.3 url/preferredTransport pair, so the two shapes agree.
No main.py, no DB, no network.
"""
import routes.agent_a2a as agent_a2a


def test_supported_interfaces_present_and_non_empty():
    card = agent_a2a._card()
    ifaces = card.get("supportedInterfaces")
    assert isinstance(ifaces, list) and ifaces
    for i in ifaces:
        assert i["url"] and i["protocolBinding"] and i["protocolVersion"]


def test_first_interface_matches_v03_fields():
    card = agent_a2a._card()
    first = card["supportedInterfaces"][0]
    assert first["url"] == card["url"]
    assert first["protocolBinding"] == card["preferredTransport"]
    assert first["protocolVersion"] == card["protocolVersion"]
