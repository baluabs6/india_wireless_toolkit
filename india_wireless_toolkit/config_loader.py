"""
Loads config.yaml and exposes it as a nested dict, with a sensible
default location resolution so it works whether you run scripts from
the package root or from inside the package folder.

Secret-shaped values (SMTP credentials, webhook URLs) are never read
from config.yaml directly. They're sourced from environment variables
(optionally via a local .env file, which is gitignored) so config.yaml
stays safe to commit. See .env.example for the variable names.

Relative paths in config.yaml (charts_dir, data_dir, ...) are resolved
against BASE_DIR - the folder containing the config file that was
loaded - via resolve_path(), so behaviour no longer depends on the
current working directory.
"""

import os
import yaml

_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DEFAULT_CONFIG_CANDIDATES = [
    os.path.join(os.getcwd(), "config.yaml"),
    os.path.join(_PACKAGE_ROOT, "config.yaml"),
]

# Maps dotted config paths to the environment variable that overrides them.
# Anything secret-shaped listed here is intentionally kept out of config.yaml.
_ENV_OVERRIDES = {
    ("news_scraper", "alert_webhook_url"): "ALERT_WEBHOOK_URL",
    ("news_scraper", "alert_email", "smtp_host"): "SMTP_HOST",
    ("news_scraper", "alert_email", "smtp_port"): "SMTP_PORT",
    ("news_scraper", "alert_email", "from_addr"): "SMTP_FROM_ADDR",
    ("news_scraper", "alert_email", "to_addr"): "SMTP_TO_ADDR",
    ("news_scraper", "alert_email", "smtp_user"): "SMTP_USER",
    ("news_scraper", "alert_email", "smtp_password"): "SMTP_PASSWORD",
    ("redis", "host"): "REDIS_HOST",
    ("redis", "port"): "REDIS_PORT",
    ("redis", "password"): "REDIS_PASSWORD",
    ("redis", "ssl"): "REDIS_SSL",
    # Azure File Share mount paths (containers are ephemeral, so charts/
    # reports/news archive should live on persistent storage in Azure).
    ("paths", "charts_dir"): "AZURE_CHARTS_DIR",
    ("paths", "reports_dir"): "AZURE_REPORTS_DIR",
}


def _to_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# Environment values arrive as strings; coerce the ones that must not be.
_COERCE = {
    ("news_scraper", "alert_email", "smtp_port"): int,
    ("redis", "port"): int,
    ("redis", "ssl"): _to_bool,
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
        os.path.join(_PACKAGE_ROOT, ".env"),
    ):
        if os.path.exists(candidate):
            load_dotenv(candidate, override=False)
            break


def _apply_env_overrides(config: dict) -> dict:
    for path, env_var in _ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is None or value == "":
            continue
        coerce = _COERCE.get(path)
        if coerce is not None:
            try:
                value = coerce(value)
            except (TypeError, ValueError):
                raise ValueError(f"Environment variable {env_var} has an invalid value: {value!r}")
        node = config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return config


def _find_config_path(path: str = None):
    if path is None:
        path = os.environ.get("IWT_CONFIG")
    if path is None:
        for candidate in _DEFAULT_CONFIG_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
    return path


def load_config(path: str = None) -> dict:
    path = _find_config_path(path)
    if path is None or not os.path.exists(path):
        raise FileNotFoundError(
            "config.yaml not found. Pass an explicit path to load_config(), "
            "set IWT_CONFIG, or run the toolkit from the project root."
        )
    with open(path, "r") as f:
        config = yaml.safe_load(f) or {}

    _load_dotenv_if_present()
    return _apply_env_overrides(config)


def _compute_base_dir() -> str:
    path = _find_config_path()
    if path and os.path.exists(path):
        return os.path.dirname(os.path.abspath(path))
    return _PACKAGE_ROOT


# Load once at import time; modules can just do `from .config_loader import CONFIG`
try:
    CONFIG = load_config()
except FileNotFoundError:
    CONFIG = {}

BASE_DIR = _compute_base_dir()


def resolve_path(*parts: str) -> str:
    """Join `parts` and, if the result is relative, anchor it at BASE_DIR
    (the folder holding config.yaml) instead of the process's cwd."""
    joined = os.path.join(*parts)
    return joined if os.path.isabs(joined) else os.path.join(BASE_DIR, joined)
