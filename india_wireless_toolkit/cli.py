"""
Command-line entry point for india_wireless_toolkit.

Usage:
    python -m india_wireless_toolkit.cli all
    python -m india_wireless_toolkit.cli charts [--dashboard] [--map] [--all-features]
    python -m india_wireless_toolkit.cli spectrum [--whatif-mhz N] [--aps N] [--skip-montecarlo]
    python -m india_wireless_toolkit.cli infra [--npv] [--sensitivity] [--compare] [--breakeven] [--all-features]
    python -m india_wireless_toolkit.cli news [--dummy] [--rss URL ...] [--trend] [--alert]
    python -m india_wireless_toolkit.cli report
    python -m india_wireless_toolkit.cli agent "What if India released 700 MHz?" [--max-turns N] [--quiet]

Or, after installing the package (pip install -e .):
    india-wireless-toolkit all

Note: `agent` requires `pip install anthropic` and an ANTHROPIC_API_KEY
environment variable (never store it in config.yaml). It is excluded from
the `all` command since it needs a live API call and a question to answer.
"""

import sys
import argparse

from . import data_visualization, spectrum_simulation, infra_economics, news_scraper, report_generator, agent

COMMANDS = {
    "charts": data_visualization.main,
    "spectrum": spectrum_simulation.main,
    "infra": infra_economics.main,
    "news": news_scraper.main,
    "report": report_generator.main,
    "agent": agent.main,
}

# `agent` needs a question and a live Anthropic API call, so it's opt-in only
# — `all` runs the deterministic, fully-offline modules.
_ALL_COMMANDS = [c for c in COMMANDS if c != "agent"]


def main():
    parser = argparse.ArgumentParser(
        prog="india-wireless-toolkit",
        description="Tools for exploring current issues in India's wireless/telecom sector.",
    )
    parser.add_argument(
        "command",
        choices=list(COMMANDS.keys()) + ["all"],
        help="Which module to run: charts, spectrum, infra, news, report, or all",
    )
    # Remaining args are passed through to the individual module's own argparse
    args, remaining = parser.parse_known_args()
    sys.argv = [sys.argv[0]] + remaining  # let sub-module argparse see only its own flags

    if args.command == "all":
        for name in _ALL_COMMANDS:
            print(f"\n{'=' * 60}\nRunning: {name}\n{'=' * 60}")
            try:
                COMMANDS[name]()
            except Exception as e:
                print(f"[{name}] skipped/failed: {e}")
    else:
        COMMANDS[args.command]()


if __name__ == "__main__":
    sys.exit(main())
