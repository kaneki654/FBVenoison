"""Load local credentials without embedding them in published source files."""
import json
import os
from pathlib import Path
import sys


def settings_path():
    override = os.environ.get("FBPOISON_PRIVATE_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    directory = (Path(sys.executable).resolve().parent
                 if getattr(sys, "frozen", False)
                 else Path(__file__).resolve().parent)
    return directory / "poison_private.json"


def get_private_setting(key, environment_name):
    """Environment values, including empty values, override the local file."""
    if environment_name in os.environ:
        return os.environ[environment_name]
    try:
        settings = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = settings.get(key, "") if isinstance(settings, dict) else ""
    return value if isinstance(value, str) else ""
