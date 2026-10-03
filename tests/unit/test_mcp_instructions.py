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


def test_instructions_put_tier1_first_and_inputs_up_front_for_run_code():
    text = " ".join(INSTRUCTIONS.split())
    assert "run_code" in text and "tier-1 tools first" in text
    assert "inputs" in text and "uncertainty" in text


def test_instructions_point_to_the_tier2_skill_and_code_get():
    text = " ".join(INSTRUCTIONS.split())
    assert "tier2-code" in text and "code_get" in text


def test_instructions_state_claim_scope_and_supported_hypothesis_rules():
    """bvx: after qxp the daemon refuses claims beyond evidence and unsupported 'supported'."""
    text = " ".join(INSTRUCTIONS.split())
    for word in ("scope_note", "alternatives_considered", "source_flags", "beyond_evidence",
                 "claim_beyond_evidence", "concrete subject"):  # fmt: skip
        assert word in text, word


async def test_analyze_docstring_names_the_absent_as_zero_caveat(tmp_path):
    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp
    from tests.unit.fakes import make_service

    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert "absent_as_zero" in (tools["analyze"].description or "")


def test_instructions_say_new_question_is_new_workspace():
    text = " ".join(INSTRUCTIONS.split())
    assert "workspace_create(title, question)" in text and "workspace_switch" in text
    assert "never changes the workspace except these" in text
