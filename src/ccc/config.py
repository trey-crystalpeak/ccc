"""Configuration loading — global (~/.ccc/config/ccc.json) and per-project (.ccc.json)."""

import json
from pathlib import Path

CONFIG_CCC = Path.home() / ".ccc" / "config" / "ccc.json"


def load_file(path: Path) -> dict:
    """Load a JSON config file. Returns empty dict on missing or malformed files."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"Warning: failed to load {path}: {e}")
        return {}


def load_global() -> dict:
    return load_file(CONFIG_CCC)


def load_project(project_path: str) -> dict:
    return load_file(Path(project_path) / ".ccc.json")
