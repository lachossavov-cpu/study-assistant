#!/usr/bin/env python3
"""Where the code lives and where one person's data lives — two different places.

APP   the program: server/, tools/, the default config. Read-only once it is installed.
HOME  one user's stuff: config.json, data/, work/, reports/, feedback/.

For the original single-user checkout they are the same folder, which is why nothing
moved. A copy handed to someone else sets ASSISTANT_HOME (the launcher does), so their
tasks, their course list and their assistant's name never land inside the program folder
— and an update can replace the program without touching any of it.
"""
import json
import os
import shutil
import sys
from pathlib import Path

# Always the folder above tools/ — wherever THIS copy of the program was loaded from.
# It used to prefer PyInstaller's _MEIPASS, but the program is now a payload that can be
# replaced by an update living in the user's folder, and then _MEIPASS is the wrong one.
APP = Path(__file__).resolve().parent.parent
APP_NAME = "JARVIS"          # folder name only — what the assistant is CALLED is in config


def user_data_dir() -> Path:
    """The per-user folder the OS expects an app to write into."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


SHARED = bool(os.environ.get("ASSISTANT_HOME"))      # True for a handed-out copy
HOME = Path(os.environ["ASSISTANT_HOME"]) if SHARED else APP
CONFIG = HOME / "config.json"
DEFAULT_CONFIG = APP / "config.default.json"


def ensure_home():
    """First run of a handed-out copy: create the folder and seed the config."""
    if not SHARED:
        return
    HOME.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists() and DEFAULT_CONFIG.exists():
        shutil.copyfile(DEFAULT_CONFIG, CONFIG)


def read_user_config() -> dict:
    """Exactly what is in this person's config.json. Use this when WRITING it back."""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _merge(base: dict, mine: dict) -> dict:
    """Defaults underneath, the person's own values on top. Lists are taken whole."""
    out = dict(base)
    for k, v in mine.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_config() -> dict:
    """The config to act on.

    A handed-out copy seeds config.json from the defaults ONCE, on first run. Anything
    added to the defaults later — a new switch shipped in an update — would never reach
    the people who already have the app. So for those copies the current defaults are
    merged underneath on every read. Their own choices still win.

    Writers must use read_user_config(): writing the merged dict back would freeze
    today's defaults into the file and stop the next update from changing them.
    """
    mine = read_user_config()
    if not SHARED:
        return mine
    try:
        defaults = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return mine
    defaults.pop("_note", None)
    return _merge(defaults, mine)


def write_config(cfg: dict):
    """Write to a temp file, then swap. Path.write_text truncates before it encodes, so a
    failure half way leaves a zero-byte config — and this one holds the whole setup."""
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)


ensure_home()
