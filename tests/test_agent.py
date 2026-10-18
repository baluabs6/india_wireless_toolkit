import json
from types import SimpleNamespace as NS

import pytest

from india_wireless_toolkit import agent


def text(t):
    return NS(type="text", text=t)


def tool_use(block_id, name, tool_input):
    return NS(type="tool_use", id=block_id, name=name, input=tool_input)


def reply(stop_reason, *blocks):
    return NS(stop_reason=stop_reason, content=list(blocks))


class FakeClient:
    """Stands in for anthropic.Anthropic: client.messages.create(...)."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _tool_result_payloads(call):
    last = call["messages"][-1]
    return [json.loads(block["content"]) for block in last["content"]]


def test_loop_runs_tools_tags_refs_and_returns_sources():
    client = FakeClient(
        reply("tool_use", text("checking"), tool_use("t1", "infra_breakeven", {"years": 10, "bogus": 1}),
              tool_use("t2", "does_not_exist", {})),
        reply("end_turn", text("Break-even is 3 ISPs [tool#1].")),
    )
    out = agent.run_agent("break-even?", client=client, verbose=False)
    assert out["answer"].startswith("Break-even") and out["turns"] == 2 and not out["truncated"]
    assert [s["ref"] for s in out["sources"]] == ["tool#1", "tool#2"]
    payloads = _tool_result_payloads(client.calls[1])
    assert payloads[0]["_ref"] == "tool#1" and "breakeven_n_isps" in payloads[0]
    assert "Unknown tool" in payloads[1]["error"]
    assert client.calls[0]["system"][0]["cache_control"] and client.calls[0]["tools"][-1]["cache_control"]


def test_truncated_answers_are_flagged_not_passed_off_as_complete():
    out = agent.run_agent("q", client=FakeClient(reply("max_tokens", text("The answer starts and"))), verbose=False)
    assert out["truncated"] is True and "cut off" in out["answer"]


def test_turn_limit_produces_a_summary_instead_of_discarding_work():
    loop = lambda i: reply("tool_use", tool_use(f"t{i}", "infra_summary", {"n_isps": 3}))
    client = FakeClient(loop(1), loop(2), reply("end_turn", text("Best answer so far [tool#1], [tool#2].")))
    out = agent.run_agent("q", max_turns=2, client=client, verbose=False)
    assert out["hit_turn_limit"] is True and out["answer"].startswith("Best answer so far")
    assert client.calls[-1]["tool_choice"] == {"type": "none"} and len(out["tool_calls"]) == 2


def test_turn_limit_falls_back_when_the_summary_call_fails():
    loop = reply("tool_use", tool_use("t1", "infra_summary", {}))
    out = agent.run_agent("q", max_turns=1, client=FakeClient(loop, RuntimeError("api down")), verbose=False)
    assert out["hit_turn_limit"] and "max-turns limit" in out["answer"]


def test_empty_model_reply_gets_a_message():
    assert agent.run_agent("q", client=FakeClient(reply("end_turn")), verbose=False)["answer"]


def test_arguments_are_clamped_filtered_and_errors_sanitised():
    big = agent._execute_tool("spectrum_capacity", {"mhz": 10**9, "aps": 999, "extra": "x"})
    assert big["mhz"] == 2000 and big["aps"] == 100
    low = agent._execute_tool("spectrum_capacity", {"mhz": "abc", "aps": -5})
    assert low["mhz"] == 500 and low["aps"] == 1
    err = agent._execute_tool("infra_npv", {"n_isps": "many"})
    assert "must be a number" in err["error"] and "/home" not in err["error"]
    assert agent._execute_tool("infra_npv", "not-a-dict")["npv_inr"] is not None


def test_infra_tools_accept_scenarios_now():
    base = agent._execute_tool("infra_npv", {})
    better = agent._execute_tool("infra_npv", {"n_isps": 8, "discount_rate": 0.08})
    worse = agent._execute_tool("infra_npv", {"n_isps": 2})
    assert worse["npv_inr"] < base["npv_inr"] < better["npv_inr"]
    assert agent._execute_tool("infra_breakeven", {"neutral_access_fee_per_building_per_year_inr": 1e6})["breakeven_n_isps"] is None
    assert agent._execute_tool("infra_compare", {"years": 12})["per_isp_costs"]["ftth"]["asset_life_years"] == 20
    sens = agent._execute_tool("infra_sensitivity", {"metric": "neutral_npv"})
    assert sens["metric"] == "neutral_npv" and sens["results"]
    assert "valid_metrics" in agent._execute_tool("infra_sensitivity", {"metric": "zzz"})


def test_oversized_tool_results_are_truncated():
    payload = agent._tool_result_content({"x": "a" * (agent.MAX_TOOL_RESULT_CHARS * 2)})
    assert len(payload) <= agent.MAX_TOOL_RESULT_CHARS + 20 and payload.endswith("[truncated]\"")


def test_every_tool_schema_matches_its_function():
    import inspect
    for name, tool in agent._TOOLS.items():
        params = inspect.signature(tool["fn"]).parameters
        declared = set(tool["input_schema"].get("properties", {}))
        has_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        explicit = {p for p, v in params.items() if v.kind is not inspect.Parameter.VAR_KEYWORD}
        assert explicit <= declared, f"{name}: arguments the model cannot see: {explicit - declared}"
        if not has_kwargs:
            assert declared <= explicit, f"{name}: schema promises unsupported arguments {declared - explicit}"


def test_news_tools_label_sample_data(tmp_path, monkeypatch):
    from india_wireless_toolkit import news_scraper as n
    monkeypatch.setattr(n, "DB_PATH", str(tmp_path / "a.db"))
    assert "No archived news" in agent._execute_tool("news_trend", {})["note"]
    assert agent._execute_tool("load_news_archive", {})["origin"] == "sample"
    found = agent._execute_tool("search_news_archive", {"limit": 3})
    assert len(found["items"]) == 3 and all(i["origin"] == "sample" for i in found["items"])
    assert "sample" in found["note"].lower()
    assert agent._execute_tool("news_trend", {})["origins"] == {"sample": 7}


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        import anthropic  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="anthropic"):
            agent._anthropic_client()
    else:
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            agent._anthropic_client()
