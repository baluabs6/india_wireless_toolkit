"""
Thread-safe, crash-safe helpers for writing charts and other generated files.

Why this exists
---------------
* `matplotlib.pyplot` keeps global "current figure" state and is NOT
  thread-safe. Both web APIs generate charts from worker threads, so every
  chart here is built on an independent `Figure` object instead.
* Concurrent requests used to write the same PNG path while another request
  was reading it. `save_figure` / `write_text_atomic` write to a temporary
  file and `os.replace()` it into place, so readers only ever see a complete
  file.
* `needs_refresh` lets the APIs skip regenerating a chart whose inputs have
  not changed.
"""

import os
import tempfile

from matplotlib.figure import Figure

_FILE_MODE = 0o644


def new_figure(figsize=(6, 5)) -> Figure:
    return Figure(figsize=figsize)


def _ensure_parent(path: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    return parent


def save_figure(fig: Figure, out_path: str, dpi: int = 150) -> str:
    parent = _ensure_parent(out_path)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp.png")
    os.close(fd)
    try:
        fig.savefig(tmp, dpi=dpi, format="png")
        os.chmod(tmp, _FILE_MODE)      # mkstemp creates 0600; shared mounts need group/other read
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out_path


def write_text_atomic(path: str, text: str, encoding: str = "utf-8") -> str:
    parent = _ensure_parent(path)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def needs_refresh(out_path: str, *source_paths: str) -> bool:
    """True if `out_path` is missing or older than any existing source file."""
    if not os.path.exists(out_path):
        return True
    out_mtime = os.path.getmtime(out_path)
    for src in source_paths:
        if src and os.path.exists(src) and os.path.getmtime(src) > out_mtime:
            return True
    return False
