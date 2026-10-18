"""
Central logging setup. Library modules log through `logging.getLogger(__name__)`;
entry points (CLI, web APIs) call `setup_logging()` once.

If APPLICATIONINSIGHTS_CONNECTION_STRING is set (the Bicep template sets it)
and the optional `azure-monitor-opentelemetry` package is installed, logs and
request telemetry are also exported to Azure Application Insights.
"""

import logging
import os

_CONFIGURED = False


def setup_logging(level: str = None, cli: bool = False) -> None:
    """Idempotent. `cli=True` prints bare messages (no timestamps) so command
    line output stays readable; services get timestamped lines."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    fmt = "%(message)s" if cli else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level_name, logging.INFO), format=fmt)
    _CONFIGURED = True

    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn and not cli:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            configure_azure_monitor(connection_string=conn)
            logging.getLogger(__name__).info("Azure Application Insights telemetry enabled.")
        except ImportError:
            logging.getLogger(__name__).info(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is set but 'azure-monitor-opentelemetry' "
                "is not installed; skipping App Insights export."
            )
        except Exception as e:  # never let telemetry break startup
            logging.getLogger(__name__).warning("App Insights setup failed: %s", e)
