from telemetry_nerd.mcp.server import INSTRUCTIONS


def test_instructions_route_contradictions_through_findings():
    text = " ".join(INSTRUCTIONS.split())
    assert 'stance="against"' in text
    assert "hypothesis_update" in text
