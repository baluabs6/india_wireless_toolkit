"""
Loads config.yaml and exposes it as a nested dict, with a sensible
default location resolution so it works whether you run scripts from
the package root or from inside the package folder.
"""

import os
import yaml

_DEFAULT_CONFIG_CANDIDATES = [
    os.path.join(os.getcwd(), "config.yaml"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"),
]


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
        return yaml.safe_load(f)


# Load once at import time; modules can just do `from .config_loader import CONFIG`
try:
    CONFIG = load_config()
except FileNotFoundError:
    CONFIG = {}
