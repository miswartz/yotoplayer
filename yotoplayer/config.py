"""Load user configuration from ~/.yotoplayer/config.json."""

import json
import os
from pathlib import Path

_CONFIG_DIR = Path.home() / ".yotoplayer"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        if _CONFIG_FILE.is_file():
            _cache = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        else:
            _cache = {}
    return _cache


def get(key: str, env_var: str | None = None) -> str | None:
    """Return a config value. Checks config.json first, then env var."""
    value = _load().get(key)
    if value:
        return value
    if env_var:
        return os.environ.get(env_var)
    return None
