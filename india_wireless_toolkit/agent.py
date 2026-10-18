"""
Agentic AI research assistant for india_wireless_toolkit
----------------------------------------------------------
Lets a person ask a plain-English question ("What if India released 700 MHz
of 6 GHz spectrum instead of 500?", "Is shared last-mile infra worth it with
3 ISPs and a 12% discount rate?", "What's trending in TRAI/DoT news?") and have
an LLM (Claude) decide which of the toolkit's own analysis functions to call,
in what order, before answering - grounding its answer in real numbers from
this toolkit instead of guessing them.

Design goals (secure-by-construction agentic loop):
  1. No arbitrary code execution. The model only emits a tool name + JSON
     arguments, which we look up in a fixed allowlist (_TOOLS). Unknown tool
     names are refused; unknown arguments are dropped.
  2. Every numeric argument is clamped to the same ranges the HTTP APIs use
     (infra_economics.PARAM_SPECS), so a hallucinated or adversarial argument
     cannot cause a resource-exhaustion loop.
  3. Bounded turns. max_turns caps tool-call round trips; when it is hit the
     model is asked for a final summary of what it has already gathered.
  4. Transparent and auditable. Every tool result is tagged with a reference
     (tool#1, tool#2, ...) that the model is told to cite, and the response
     lists `sources` so each figure in the answer can be traced to a call.
  5. No secrets in the loop. The API key is read only from ANTHROPIC_API_KEY.
  6. Honest about truncation: an answer cut off by max_tokens is flagged
     (`truncated: true`), never returned as if complete.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m india_wireless_toolkit.cli agent "What if India released 700 MHz?"

Programmatically:
    from india_wireless_toolkit.agent import run_agent
    result = run_agent("Is shared infra worth it with 3 ISPs?")
    print(result["answer"], result["sources"])
"""

import inspect
import json
import logging
import os
import re

from .config_loader import CONFIG, BASE_DIR
from . import spectrum_simulation as spec
from . import infra_economics as infra
from . import data_visualization as dv
from . import news_scraper as news
from . import report_generator as report

logger = logging.getLogger(__name__)

_CFG = CONFIG.get("agent", {})
DEFAULT_MODEL = _CFG.get("model", "claude-sonnet-4-6")
DEFAULT_MAX_TURNS = int(_CFG.get("max_turns", 6))
DEFAULT_MAX_TOKENS = int(_CFG.get("max_tokens", 1500))
MAX_TOOL_RESULT_CHARS = 20_000


def _clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# --- Tools (each returns plain JSON-serialisable data; none print or capture stdout) ----------

def tool_spectrum_capacity(mhz: int = 500, aps: int = 20):
    mhz = _clamp(mhz, 20, 2000, 500)
    aps = _clamp(aps, 1, 100, 20)
    result = spec.what_if_analysis(mhz, aps_per_building=aps)
    mc = result["monte_carlo"]
    return {
        "mhz": mhz, "aps": aps,
        "capacity_by_channel_width": [
            {"channel_width_mhz": w, "channels": n, "aggregate_phy_mbps": round(m)}
            for w, n, m in result["capacity_rows"]
        ],
        "dense_deployment_channel_reuse_factor": result["dense_reuse_factor"],
        "monte_carlo_mean_sinr_db": round(mc["mean_sinr_db"], 1),
        "monte_carlo_range_db": [round(mc["min_sinr_db"], 1), round(mc["max_sinr_db"], 1)],
        "note": "PHY rates are single-stream peak rates (upper bounds), not user throughput.",
    }


def tool_spectrum_sinr(tx_power_dbm: float = 20.0):
    try:
        tx_power_dbm = float(tx_power_dbm)
    except (TypeError, ValueError):
        tx_power_dbm = 20.0
    tx_power_dbm = max(-10.0, min(40.0, tx_power_dbm))
    return {"tx_power_dbm": tx_power_dbm, "results": spec.sinr_vs_distance_rows(tx_power_dbm=tx_power_dbm)}


def _infra_params(overrides):
    return infra.params_from_mapping(overrides)


def tool_infra_summary(**overrides):
    return infra.summary_report(_infra_params(overrides))


def tool_infra_npv(**overrides):
    p = _infra_params(overrides)
    result = infra.npv_irr_analysis(p)
    return {"discount_rate": p.discount_rate, "npv_inr": result["npv_inr"], "irr": result["irr"],
            "cashflows_inr": result["cashflows"], "verdict": result["verdict"],
            "includes_terminal_value": result["includes_terminal_value"]}


def tool_infra_sensitivity(metric: str = "savings", **overrides):
    if metric not in infra.SENSITIVITY_METRICS:
        return {"error": f"Unknown metric '{metric}'.", "valid_metrics": sorted(infra.SENSITIVITY_METRICS)}
    results, base = infra.sensitivity_analysis(_infra_params(overrides), metric=metric)
    return {"metric": metric, "base_value_inr": base,
            "results": [{k: r[k] for k in ("parameter", "low_value", "high_value", "low_cost",
                                           "high_cost", "swing", "influences")} for r in results[:6]]}


def tool_infra_compare(**overrides):
    data = infra.three_way_comparison_data(_infra_params(overrides))
    return {"per_isp_costs": data["detail"], "cheapest_cash": data["cheapest_cash"],
            "cheapest_asset_life_adjusted": data["cheapest_horizon_cost"],
            "note": "Cost only; service quality is not modeled."}


def tool_infra_breakeven(**overrides):
    return infra.breakeven_analysis(_infra_params(overrides))


def tool_news_trend(include_sample: bool = True):
    counts = news.keyword_counts(include_sample=bool(include_sample))
    if not counts:
        return {"note": "No archived news yet. Call load_news_archive first (sample data only)."}
    return {"keyword_counts": counts, "monthly": news.keyword_trend_series(include_sample=bool(include_sample)),
            "origins": news.archive_origin_summary()}


def tool_load_news_archive():
    """Loads the bundled SAMPLE press-release dataset (not real news) into the archive."""
    items = news.load_dummy_dataset()
    new_items = news.store_items(items)
    return {"fetched": len(items), "newly_stored": len(new_items), "origin": "sample"}


def tool_search_news_archive(limit: int = 10, include_sample: bool = True):
    records = news.query_archive_records(_clamp(limit, 1, 50, 10), include_sample=bool(include_sample))
    return {"items": [{k: r[k] for k in ("source", "title", "url", "date", "date_source", "origin")}
                      for r in records],
            "note": "Items with origin 'sample' are bundled demo data, not real press releases."}


def tool_generate_chart(name: str):
    if name not in dv.CHART_BUILDERS:
        return {"error": f"Unknown chart '{name}'.", "valid_names": sorted(dv.CHART_BUILDERS)}
    path = dv.ensure_chart(name)
    return {"chart_path": os.path.relpath(path, BASE_DIR)}


def tool_generate_full_report():
    return {"report_path": os.path.relpath(report.generate_report(), BASE_DIR)}


# --- Tool registry: name -> (python function, JSON schema for Claude) --------------------------------

_INFRA_PROPS = {
    "n_isps": {"type": "integer", "description": "Number of competing ISPs (1-100)."},
    "n_buildings": {"type": "integer", "description": "Buildings served (1-1,000,000)."},
    "years": {"type": "integer", "description": "Analysis horizon in years (1-30)."},
    "discount_rate": {"type": "number", "description": "Discount rate as a fraction, e.g. 0.12 for 12%."},
    "fibre_capex_per_building_inr": {"type": "number", "description": "Fibre capex per building, INR."},
    "fwa_capex_per_building_inr": {"type": "number", "description": "Fixed wireless capex per building, INR."},
    "satellite_capex_per_building_inr": {"type": "number", "description": "Satellite capex per building, INR."},
    "neutral_access_fee_per_building_per_year_inr": {
        "type": "number", "description": "Neutral operator lease fee per building per ISP per year, INR."},
    "opex_per_building_per_year_inr": {"type": "number", "description": "Opex per building per year, INR."},
}
_INFRA_SCHEMA = {"type": "object", "properties": _INFRA_PROPS}

_TOOLS = {
    "spectrum_capacity": {
        "fn": tool_spectrum_capacity,
        "description": ("Wi-Fi channel capacity and interference for a given amount of delicensed 6 GHz "
                        "spectrum. Use for 'what if India released N MHz' or capacity questions."),
        "input_schema": {"type": "object", "properties": {
            "mhz": {"type": "integer", "description": "Total delicensed MHz (20-2000). India today: 500; full band: 1200."},
            "aps": {"type": "integer", "description": "Access points in one building for the dense-deployment stress test (1-100). Default 20."},
        }},
    },
    "spectrum_sinr": {
        "fn": tool_spectrum_sinr,
        "description": "Signal-to-interference-plus-noise ratio (SINR) vs distance/walls for a given Wi-Fi transmit power.",
        "input_schema": {"type": "object", "properties": {
            "tx_power_dbm": {"type": "number", "description": "Transmit power in dBm, -10 to 40. Default 20."}}},
    },
    "infra_summary": {
        "fn": tool_infra_summary,
        "description": ("Duplicate vs shared-neutral last-mile cost. Any parameter may be overridden for what-if "
                        "questions (number of ISPs, capex, fees, horizon...)."),
        "input_schema": _INFRA_SCHEMA,
    },
    "infra_custom_scenario": {
        "fn": tool_infra_summary,
        "description": "Alias of infra_summary, kept for compatibility.",
        "input_schema": _INFRA_SCHEMA,
    },
    "infra_npv": {
        "fn": tool_infra_npv,
        "description": "NPV/IRR of the neutral-operator investment. Accepts the same parameter overrides.",
        "input_schema": _INFRA_SCHEMA,
    },
    "infra_sensitivity": {
        "fn": tool_infra_sensitivity,
        "description": ("Which cost variables swing a chosen metric the most (tornado data, top 6). Metrics: "
                        f"{sorted(infra.SENSITIVITY_METRICS)}. Variables that do not move the chosen metric show "
                        "swing 0 but list the metrics they do drive in 'influences'."),
        "input_schema": {"type": "object", "properties": {
            "metric": {"type": "string", "enum": sorted(infra.SENSITIVITY_METRICS)}, **_INFRA_PROPS}},
    },
    "infra_compare": {
        "fn": tool_infra_compare,
        "description": "Per-ISP cost of FTTH vs Fixed Wireless Access vs Satellite, in cash and asset-life-adjusted terms.",
        "input_schema": _INFRA_SCHEMA,
    },
    "infra_breakeven": {
        "fn": tool_infra_breakeven,
        "description": "Minimum number of competing ISPs at which shared-neutral infrastructure makes sense, with each condition reported.",
        "input_schema": _INFRA_SCHEMA,
    },
    "load_news_archive": {
        "fn": tool_load_news_archive,
        "description": "Loads the bundled SAMPLE press-release dataset (demo data, not real news) into the archive. Call before news_trend/search_news_archive if they report no data.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "news_trend": {
        "fn": tool_news_trend,
        "description": "Keyword-frequency counts (total and monthly) across archived press releases.",
        "input_schema": {"type": "object", "properties": {
            "include_sample": {"type": "boolean", "description": "Include bundled sample items (default true)."}}},
    },
    "search_news_archive": {
        "fn": tool_search_news_archive,
        "description": "Most recent archived press releases (title, source, date, URL, origin). Items with origin 'sample' are demo data.",
        "input_schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Max items, 1-50. Default 10."},
            "include_sample": {"type": "boolean", "description": "Include bundled sample items (default true)."}}},
    },
    "generate_chart": {
        "fn": tool_generate_chart,
        "description": f"Regenerates one of the toolkit's static charts and returns its file path. Valid names: {sorted(dv.CHART_BUILDERS)}.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string", "enum": sorted(dv.CHART_BUILDERS)}},
                         "required": ["name"]},
    },
    "generate_full_report": {
        "fn": tool_generate_full_report,
        "description": "Regenerates the combined HTML report (all charts + spectrum + infra + news) and returns its file path.",
        "input_schema": {"type": "object", "properties": {}},
    },
}

SYSTEM_PROMPT = (
    "You are the research assistant for india-wireless-toolkit, a toolkit analyzing "
    "India's wireless/telecom sector (6 GHz Wi-Fi spectrum, last-mile infrastructure "
    "economics, TRAI/DoT/PIB regulatory news). Answer the person's question by calling "
    "the provided tools to get real, grounded numbers from the toolkit's own bundled "
    "datasets and simulations - never invent or estimate a figure a tool could have "
    "given you. If a question needs more than one tool (e.g. a comparison across "
    "scenarios), call them one at a time and reason across the results. All figures "
    "are illustrative planning assumptions from the toolkit's config, not official "
    "TRAI/ISP data - say so if the person's question implies otherwise. News items with "
    "origin 'sample' are bundled demo data, not real press releases - say so whenever you "
    "use them. Every tool result carries a '_ref' such as 'tool#2'; cite the ref next to "
    "each number you report, e.g. 'NPV is Rs 1.9 crore [tool#2]'. Keep the final answer concise."
)


def _anthropic_client():
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "The 'anthropic' package is required for agentic features. "
            "Install it with: pip install anthropic"
        ) from e
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Export it as an environment variable "
            "(never put it in config.yaml or commit it) and try again."
        )
    return anthropic.Anthropic(api_key=api_key)


def _tool_specs():
    specs = [
        {"name": name, "description": t["description"], "input_schema": t["input_schema"]}
        for name, t in _TOOLS.items()
    ]
    specs[-1] = {**specs[-1], "cache_control": {"type": "ephemeral"}}   # cache the static tool block
    return specs


def _system_blocks():
    return [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]


def _sanitize_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = text.replace(BASE_DIR, "<project>")
    return re.sub(r"(/[\w.\-]+){3,}", "<path>", text)[:200]


def _execute_tool(name: str, tool_input, ref: str = None, verbose: bool = True):
    """Looks up `name` in the fixed allowlist and calls it with the arguments the
    function actually declares (extras are ignored). Never evals/execs anything
    the model wrote."""
    if name not in _TOOLS:
        return {"error": f"Unknown tool '{name}'."}
    if not isinstance(tool_input, dict):
        tool_input = {}
    fn = _TOOLS[name]["fn"]
    params = inspect.signature(fn).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        tool_input = {k: v for k, v in tool_input.items() if k in params}
    if verbose:
        print(f"  [agent] -> {name}({json.dumps(tool_input, default=str)})")
    try:
        result = fn(**tool_input)
    except Exception as e:
        logger.info("Tool %s failed: %s", name, e)
        result = {"error": _sanitize_error(e)}
    if ref:
        result = {**result, "_ref": ref} if isinstance(result, dict) else {"results": result, "_ref": ref}
    if verbose:
        preview = json.dumps(result, default=str)
        print(f"  [agent] <- {preview[:300]}{'...(truncated)' if len(preview) > 300 else ''}")
    return result


def _tool_result_content(result) -> str:
    text = json.dumps(result, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '..."[truncated]"'
    return text


def _text_of(response) -> str:
    return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")


def run_agent(question: str, max_turns: int = None, model: str = None,
              max_tokens: int = None, verbose: bool = True, client=None):
    """Runs the tool-use loop for one question. Returns
    {"answer", "tool_calls", "sources", "turns", "truncated", "hit_turn_limit"}."""
    max_turns = max_turns or DEFAULT_MAX_TURNS
    model = model or DEFAULT_MODEL
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS

    client = client or _anthropic_client()
    messages = [{"role": "user", "content": question}]
    tool_calls_log, sources = [], []

    def finish(answer, turns, truncated=False, hit_turn_limit=False):
        return {"answer": answer, "tool_calls": tool_calls_log, "sources": sources,
                "turns": turns, "truncated": truncated, "hit_turn_limit": hit_turn_limit}

    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model=model, max_tokens=max_tokens, system=_system_blocks(),
            tools=_tool_specs(), messages=messages,
        )

        if response.stop_reason != "tool_use":
            answer = _text_of(response)
            if response.stop_reason == "max_tokens":
                return finish(answer + "\n\n[Answer cut off at the token limit - ask again or raise max_tokens.]",
                              turn, truncated=True)
            return finish(answer or "(The model returned no text.)", turn)

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            ref = f"tool#{len(tool_calls_log) + 1}"
            result = _execute_tool(block.name, block.input or {}, ref=ref, verbose=verbose)
            tool_calls_log.append({"ref": ref, "name": block.name, "input": block.input, "result": result})
            sources.append({"ref": ref, "tool": block.name, "input": block.input})
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": _tool_result_content(result)})
        messages.append({"role": "user", "content": tool_results})

    # Turn limit reached: ask for a summary of what was already gathered instead of discarding it.
    try:
        final = client.messages.create(
            model=model, max_tokens=max_tokens, system=_system_blocks(), tools=_tool_specs(),
            tool_choice={"type": "none"},
            messages=messages + [{"role": "user", "content": (
                "You have reached the tool-call limit. Using ONLY the tool results above, give your best "
                "final answer now, cite refs, and say what you could not determine.")}],
        )
        answer = _text_of(final)
        if answer:
            return finish(answer, max_turns, truncated=final.stop_reason == "max_tokens", hit_turn_limit=True)
    except Exception as e:
        logger.warning("Final summary call failed: %s", e)
    return finish(
        f"Reached the max-turns limit ({max_turns}) before finishing - narrow the question or raise --max-turns.",
        max_turns, hit_turn_limit=True)


def main(argv=None):
    from .logging_config import setup_logging
    setup_logging(cli=True)
    import argparse
    parser = argparse.ArgumentParser(description="Ask the india-wireless-toolkit agentic assistant a question")
    parser.add_argument("question", nargs="+", help="Your question, e.g. 'What if India released 700 MHz?'")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--quiet", action="store_true", help="Suppress the tool-call trace, print only the final answer")
    args, _ = parser.parse_known_args(argv)

    question = " ".join(args.question)
    print(f"Q: {question}\n")
    try:
        result = run_agent(question, max_turns=args.max_turns, model=args.model, verbose=not args.quiet)
    except RuntimeError as e:
        print(f"[agent] {e}")
        return
    print(f"\nA: {result['answer']}")


if __name__ == "__main__":
    main()
