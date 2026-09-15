"""
Loads config.yaml and exposes it as a nested dict, with a sensible
default location resolution so it works whether you run scripts from
the package root or from inside the package folder.

Secret-shaped values (SMTP credentials, webhook URLs) are never read
from config.yaml directly. They're sourced from environment variables
(optionally via a local .env file, which is gitignored) so config.yaml
stays safe to commit. See .env.example for the variable names.
"""

import os
import yaml

_DEFAULT_CONFIG_CANDIDATES = [
    os.path.join(os.getcwd(), "config.yaml"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"),
]

# Maps dotted config paths to the environment variable that overrides them.
# Anything listed here is treated as secret-shaped and is intentionally
# kept out of config.yaml.
_ENV_OVERRIDES = {
    ("news_scraper", "alert_webhook_url"): "ALERT_WEBHOOK_URL",
    ("news_scraper", "alert_email", "smtp_host"): "SMTP_HOST",
    ("news_scraper", "alert_email", "smtp_port"): "SMTP_PORT",
    ("news_scraper", "alert_email", "from_addr"): "SMTP_FROM_ADDR",
    ("news_scraper", "alert_email", "to_addr"): "SMTP_TO_ADDR",
    ("news_scraper", "alert_email", "smtp_user"): "SMTP_USER",
    ("news_scraper", "alert_email", "smtp_password"): "SMTP_PASSWORD",
    ("redis", "password"): "REDIS_PASSWORD",
    ("redis", "ssl"): "REDIS_SSL",
    # Azure File Share mount paths (containers are ephemeral, so charts/
    # reports/news archive should live on persistent storage in Azure).
    ("paths", "charts_dir"): "AZURE_CHARTS_DIR",
    ("paths", "reports_dir"): "AZURE_REPORTS_DIR",
}


def _load_dotenv_if_present():
    """Best-effort .env loader; no-op if python-dotenv isn't installed
    or no .env file exists. Never overrides variables already set in
    the real environment (e.g. by Docker/CI secrets)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    ):
        if os.path.exists(candidate):
            load_dotenv(candidate, override=False)
            break


def _apply_env_overrides(config: dict) -> dict:
    for path, env_var in _ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is None:
            continue
        node = config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return config


def load_config(path: str = None) -> dict:
    if path is None:
        for candidate in _DEFAULT_CONFIG_CANDIDATES:
            if os.path.exists(candidate):
                path = candidate
                break
    if path is None or not os.path.exists(path):
        raise FileNotFoundError(
            "config.yaml not found. Pass an explicit path to load_config(), "
            "or run the toolkit from the project root."
        )
    with open(path, "r") as f:
        config = yaml.safe_load(f) or {}

    _load_dotenv_if_present()
    return _apply_env_overrides(config)


# Load once at import time; modules can just do `from .config_loader import CONFIG`
try:
    CONFIG = load_config()
except FileNotFoundError:
    CONFIG = {}
