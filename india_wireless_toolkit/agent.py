"""
Agentic AI research assistant for india_wireless_toolkit
----------------------------------------------------------
Lets a person ask a plain-English question ("What if India released 700 MHz
of 6 GHz spectrum instead of 500?", "Is shared last-mile infra worth it with
3 ISPs?", "What's trending in TRAI/DoT news lately?") and have an LLM (Claude)
autonomously decide which of the toolkit's own analysis functions to call,
in what order, and how many times, before answering — grounding its answer
in real numbers from this toolkit instead of guessing them.

Design goals (secure-by-construction agentic loop):
  1. No arbitrary code execution. The model never writes or runs Python; it
     only emits a tool name + JSON arguments, which we look up in a fixed
     allowlist (_TOOLS) and call directly. An unknown tool name is refused.
  2. Every numeric argument is clamped to the same safe ranges enforced on
     the public HTTP APIs (see services/sanic_api/app.py), so a hallucinated
     or adversarial argument (e.g. aps=5000000) can't cause a resource-
     exhaustion loop just because it came from a model instead of a user.
  3. Bounded turns. max_turns caps how many tool-call round-trips a single
     question can take, so a confused model can't loop indefinitely and run
     up API cost.
  4. Transparent by default. Every tool call and its (truncated) result is
     printed/logged as it happens, so the person can see exactly what data
     the final answer is grounded in — this is not a black box.
  5. No secrets in the loop. The Anthropic API key is read only from the
     ANTHROPIC_API_KEY environment variable (never config.yaml, never
     hardcoded, never logged).

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m india_wireless_toolkit.cli agent "What if India released 700 MHz?"

Or programmatically:
    from india_wireless_toolkit.agent import run_agent
    result = run_agent("Is shared infra worth it with 3 ISPs?")
    print(result["answer"])
"""

import io
import os
import json
import contextlib

from .config_loader import CONFIG
from . import spectrum_simulation as spec
from . import infra_economics as infra
from . import data_visualization as dv
from . import news_scraper as news
from . import report_generator as report

_CFG = CONFIG.get("agent", {})
DEFAULT_MODEL = _CFG.get("model", "claude-sonnet-4-6")
DEFAULT_MAX_TURNS = int(_CFG.get("max_turns", 6))
DEFAULT_MAX_TOKENS = int(_CFG.get("max_tokens", 1500))


def _clamp(value, lo, hi, default):
    """Coerce to int and clamp to [lo, hi]; falls back to default on bad input.
    Mirrors the bounds enforced on the public HTTP API endpoints so an
    agent-generated argument can't trigger a resource-exhaustion run just
    because it came from a model instead of a person."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _capture(func, *args, **kwargs):
    """Runs a toolkit function that prints to stdout and returns (return_value,
    captured_text) so the agent gets structured data plus the human-readable
    narration, without spamming the agent's own stdout mid-loop."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


# --- Tool implementations ----------------------------------------------------
# Each function here is deliberately narrow and side-effect-safe (reads bundled
# CSV/JSON data or the local SQLite archive; the only writes are chart PNGs
# under charts/, which are always safe to regenerate).

def tool_spectrum_capacity(mhz: int = 500, aps: int = 20):
    mhz = _clamp(mhz, 20, 2000, 500)
    aps = _clamp(aps, 1, 100, 20)
    scenario = spec.SpectrumScenario(f"{mhz} MHz scenario", mhz)
    rows, _ = _capture(spec.simulate_scenario, scenario)
    mc = spec.monte_carlo_ap_simulation(mhz, n_aps=aps, n_trials=200)
    return {
        "mhz": mhz, "aps": aps,
        "capacity_by_channel_width": [
            {"channel_width_mhz": w, "channels": n, "aggregate_phy_mbps": round(m)} for w, n, m in rows
        ],
        "monte_carlo_mean_sinr_db": mc["mean_sinr_db"],
        "monte_carlo_range_db": [mc["min_sinr_db"], mc["max_sinr_db"]],
    }


def tool_spectrum_sinr(tx_power_dbm: float = 20.0):
    try:
        tx_power_dbm = float(tx_power_dbm)
    except (TypeError, ValueError):
        tx_power_dbm = 20.0
    tx_power_dbm = max(-10.0, min(40.0, tx_power_dbm))
    rows = []
    for d in (2, 5, 10, 15, 20, 30):
        walls = max(0, int(d // 8))
        sinr = spec.estimate_sinr_db(tx_power_dbm, d, wall_count=walls)
        rows.append({"distance_m": d, "walls": walls, "sinr_db": round(sinr, 1)})
    return {"tx_power_dbm": tx_power_dbm, "results": rows}


def tool_infra_summary():
    return infra.summary_report(infra.Params())


def tool_infra_npv():
    p = infra.Params()
    npv_val, irr_val = _capture(infra.npv_irr_report, p)[0]
    return {"discount_rate": p.discount_rate, "npv_inr": npv_val, "irr": irr_val}


def tool_infra_sensitivity():
    p = infra.Params()
    (results, base_cost), _ = _capture(infra.sensitivity_analysis, p)
    return {"base_cost_inr": base_cost, "results": results[:6]}  # top drivers only


def tool_infra_compare():
    p = infra.Params()
    result, _ = _capture(infra.three_way_comparison, p)
    return result


def tool_infra_breakeven():
    p = infra.Params()
    breakeven, _ = _capture(infra.breakeven_report, p)
    return {"breakeven_n_isps": breakeven}


def tool_infra_custom(n_isps: int = 5, fibre_capex_per_building_inr: float = None):
    """Re-runs the infra summary with a caller-adjustable ISP count (and,
    optionally, fibre capex per building) instead of the config.yaml default —
    lets the agent answer 'what if there were only 2 ISPs' style questions."""
    p = infra.Params()
    p.n_isps = _clamp(n_isps, 1, 100, p.n_isps)
    if fibre_capex_per_building_inr is not None:
        try:
            p.fibre_capex_per_building_inr = max(0.0, min(1e7, float(fibre_capex_per_building_inr)))
        except (TypeError, ValueError):
            pass
    return infra.summary_report(p)


def tool_news_trend():
    counts = news.keyword_trend_report()
    if counts is None:
        return {"note": "No archived news yet. Call load_news_archive first."}
    return {"keyword_counts": counts}


def tool_load_news_archive():
    """Loads the bundled offline press-release dataset into the local archive
    (safe, no network access — see news_scraper.load_dummy_dataset)."""
    items = news.load_dummy_dataset()
    new_items = news.store_items(items)
    return {"fetched": len(items), "newly_stored": len(new_items)}


def tool_search_news_archive(limit: int = 10):
    limit = _clamp(limit, 1, 50, 10)
    rows = news.query_archive(limit=limit)
    return [{"source": s, "title": t, "url": u, "date": d} for s, t, u, d, _ in rows]


_ALLOWED_CHARTS = {
    "broadband_split": dv.chart_broadband_split,
    "teledensity_gap": dv.chart_teledensity_gap,
    "6ghz_split": dv.chart_6ghz_split,
    "public_wifi_gap": dv.chart_public_wifi_gap,
    "subscriber_trend": dv.chart_subscriber_trend,
}


def tool_generate_chart(name: str):
    if name not in _ALLOWED_CHARTS:
        return {"error": f"Unknown chart '{name}'. Valid names: {sorted(_ALLOWED_CHARTS)}"}
    _ALLOWED_CHARTS[name]()
    charts_dir = CONFIG.get("paths", {}).get("charts_dir", "charts")
    return {"chart_path": os.path.join(charts_dir, f"{name}.png")}


def tool_generate_full_report():
    path, _ = _capture(report.generate_report)
    return {"report_path": path}


# --- Tool registry: name -> (python function, JSON schema for Claude) -------

_TOOLS = {
    "spectrum_capacity": {
        "fn": tool_spectrum_capacity,
        "description": (
            "Wi-Fi channel capacity and interference for a given amount of delicensed "
            "6 GHz spectrum. Use for 'what if India released N MHz' or capacity questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mhz": {"type": "integer", "description": "Total delicensed MHz available (20-2000). Current India allocation is 500; full band is 1200."},
                "aps": {"type": "integer", "description": "Access points in one building for the dense-deployment/Monte Carlo stress test (1-100). Default 20."},
            },
        },
    },
    "spectrum_sinr": {
        "fn": tool_spectrum_sinr,
        "description": "Signal-to-interference-plus-noise ratio (SINR) vs. distance/walls for a given Wi-Fi transmit power. Use for signal-quality questions.",
        "input_schema": {
            "type": "object",
            "properties": {"tx_power_dbm": {"type": "number", "description": "Transmit power in dBm, -10 to 40. Default 20."}},
        },
    },
    "infra_summary": {
        "fn": tool_infra_summary,
        "description": "Default-scenario comparison of duplicate vs. shared-neutral last-mile infrastructure cost.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "infra_custom_scenario": {
        "fn": tool_infra_custom,
        "description": "Same as infra_summary but lets you vary the number of competing ISPs (and optionally fibre capex per building) to answer 'what if' infra questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n_isps": {"type": "integer", "description": "Number of competing ISPs, 1-100."},
                "fibre_capex_per_building_inr": {"type": "number", "description": "Optional override for fibre capex per building in INR."},
            },
        },
    },
    "infra_npv": {
        "fn": tool_infra_npv,
        "description": "NPV/IRR of the shared-neutral-operator investment over the default horizon and discount rate.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "infra_sensitivity": {
        "fn": tool_infra_sensitivity,
        "description": "Which cost variable swings total infrastructure cost the most (tornado-chart data, top 6).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "infra_compare": {
        "fn": tool_infra_compare,
        "description": "Three-way per-ISP cost comparison: FTTH vs. Fixed Wireless Access (FWA) vs. Satellite.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "infra_breakeven": {
        "fn": tool_infra_breakeven,
        "description": "Minimum number of competing ISPs at which shared-neutral infra becomes cheaper than duplication.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "load_news_archive": {
        "fn": tool_load_news_archive,
        "description": "Loads the bundled offline TRAI/DoT/PIB press-release sample into the local archive. Call this before news_trend or search_news_archive if they report no data.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "news_trend": {
        "fn": tool_news_trend,
        "description": "Keyword-frequency counts across archived press releases (what's being talked about most).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "search_news_archive": {
        "fn": tool_search_news_archive,
        "description": "Most recent archived press releases (title, source, date, URL).",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max items to return, 1-50. Default 10."}},
        },
    },
    "generate_chart": {
        "fn": tool_generate_chart,
        "description": f"Regenerates one of the toolkit's static charts and returns its file path. Valid names: {sorted(_ALLOWED_CHARTS)}.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": sorted(_ALLOWED_CHARTS)}},
            "required": ["name"],
        },
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
    "datasets and simulations — never invent or estimate a figure a tool could have "
    "given you. If a question needs more than one tool (e.g. a comparison across "
    "scenarios), call them one at a time and reason across the results. All figures "
    "are illustrative planning assumptions from the toolkit's config, not official "
    "TRAI/ISP data — say so if the person's question implies otherwise. Keep the "
    "final answer concise and cite the specific numbers you used."
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
    return [
        {"name": name, "description": t["description"], "input_schema": t["input_schema"]}
        for name, t in _TOOLS.items()
    ]


def _execute_tool(name: str, tool_input: dict, verbose: bool = True):
    """Looks up `name` in the fixed allowlist and calls it with keyword
    arguments from `tool_input`. Never evals/execs anything the model wrote —
    this is a plain dict lookup, so an unrecognized tool name is simply
    refused rather than attempted."""
    if name not in _TOOLS:
        return {"error": f"Unknown tool '{name}'."}
    if verbose:
        print(f"  [agent] -> {name}({json.dumps(tool_input)})")
    try:
        result = _TOOLS[name]["fn"](**tool_input)
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    if verbose:
        preview = json.dumps(result, default=str)
        if len(preview) > 300:
            preview = preview[:300] + "...(truncated)"
        print(f"  [agent] <- {preview}")
    return result


def run_agent(question: str, max_turns: int = None, model: str = None,
               max_tokens: int = None, verbose: bool = True):
    """Runs the tool-use agentic loop for a single question and returns
    {"answer": str, "tool_calls": [...], "turns": int}."""
    max_turns = max_turns or DEFAULT_MAX_TURNS
    model = model or DEFAULT_MODEL
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS

    client = _anthropic_client()
    messages = [{"role": "user", "content": question}]
    tool_calls_log = []

    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=_tool_specs(),
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            final_text = "".join(b.text for b in response.content if b.type == "text")
            return {"answer": final_text, "tool_calls": tool_calls_log, "turns": turn}

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = _execute_tool(block.name, block.input or {}, verbose=verbose)
            tool_calls_log.append({"name": block.name, "input": block.input, "result": result})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    return {
        "answer": (
            f"Reached the max-turns limit ({max_turns}) before finishing — the question "
            "may need to be narrowed down, or try again with a higher --max-turns."
        ),
        "tool_calls": tool_calls_log,
        "turns": max_turns,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ask the india-wireless-toolkit agentic assistant a question")
    parser.add_argument("question", nargs="+", help="Your question, e.g. 'What if India released 700 MHz?'")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--quiet", action="store_true", help="Suppress the tool-call trace, print only the final answer")
    args, _ = parser.parse_known_args()

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
