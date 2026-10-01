from telemetry_nerd.mcp.server import INSTRUCTIONS


def test_instructions_route_contradictions_through_findings():
    text = " ".join(INSTRUCTIONS.split())
    assert 'stance="against"' in text
    assert "hypothesis_update" in text


def test_instructions_prefer_distributions_for_latency():
    text = " ".join(INSTRUCTIONS.split())
    assert "query_distribution" in text and 'mark="histogram"' in text


def test_instructions_point_to_percentile_bands_and_ccdf():
    text = " ".join(INSTRUCTIONS.split())
    assert 'mark="percentiles"' in text and "ccdf" in text
