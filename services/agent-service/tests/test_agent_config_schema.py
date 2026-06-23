from pydantic import ValidationError

from src.api.agent_config import AgentConfigPatch, AgentConfigUpsert


def test_agent_config_accepts_tessallite_chart_palette():
    assert AgentConfigUpsert(chart_color_palette="tessallite").chart_color_palette == "tessallite"
    assert AgentConfigPatch(chart_color_palette="tessallite").chart_color_palette == "tessallite"


def test_agent_config_rejects_unknown_chart_palette():
    try:
        AgentConfigUpsert(chart_color_palette="unknown")
    except ValidationError:
        return
    raise AssertionError("unknown chart palette should be rejected")
