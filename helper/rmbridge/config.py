"""Paths and user settings.

Everything the helper writes lives outside the plugin directory: any write
under ~/.config/omarchy/plugins/<id>/ makes the shell reload every plugin.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

HOME = Path.home()


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else HOME / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "omarchy-remarkable"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "omarchy-remarkable"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "omarchy-remarkable"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "omarchy-remarkable"

SETTINGS_FILE = CONFIG_DIR / "settings.json"
RMAPI_CONFIG = CONFIG_DIR / "rmapi.conf"
SSH_KEY = CONFIG_DIR / "id_ed25519"
STATE_FILE = DATA_DIR / "state.json"
INDEX_DB = DATA_DIR / "index.db"

DOC_CACHE = CACHE_DIR / "docs"
RENDER_CACHE = CACHE_DIR / "renders"
THUMB_CACHE = CACHE_DIR / "thumbs"
TMP_DIR = CACHE_DIR / "tmp"

ROOT = HOME / "reMarkable"

DEFAULTS: dict[str, Any] = {
    # Where things land on this computer.
    "exportDir": str(ROOT / "Exports"),
    "notesDir": str(ROOT / "Notes"),
    "mirrorDir": str(ROOT / "Library"),
    "sendDir": str(ROOT / "Send"),
    "backupDir": str(ROOT / "Backups"),
    # Where things land on the tablet.
    "uploadFolder": "",  # "" = top level
    "printFolder": "Printed",
    "webFolder": "Web",
    "screenshotFolder": "Screenshots",
    "agendaFolder": "Daily",
    # Background behaviour.
    "pollSeconds": 90,
    "notifyNewNotes": True,
    "mirrorEnabled": False,
    "sendFolderEnabled": True,
    "printerEnabled": True,
    "usbAutoBackup": False,
    # Handwriting to text.
    "transcribeEngine": "claude-cli",  # claude-cli | anthropic-api | off
    "transcribeModel": "claude-sonnet-5-5",
    "transcribeLanguages": "",  # e.g. "English and Russian"; helps Claude read mixed pages
    "autoTranscribeFolders": [],
    "notesIncludeImages": True,
    # Morning agenda page.
    "agendaEnabled": False,
    "agendaTime": "06:00",
    "agendaDays": 1,
    # Developer mode.
    "sshHost": "10.11.99.1",
    "sshUser": "root",
    "webPageFormat": "epub",  # epub | pdf
}


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, DATA_DIR, RUNTIME_DIR, DOC_CACHE, RENDER_CACHE, THUMB_CACHE, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULTS)
    try:
        stored = json.loads(SETTINGS_FILE.read_text())
        if isinstance(stored, dict):
            settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    clean = {k: settings[k] for k in DEFAULTS if k in settings and settings[k] != DEFAULTS[k]}
    write_json_atomic(SETTINGS_FILE, clean)


def coerce(key: str, raw: str) -> Any:
    """Turn a CLI string into the type the default has."""
    if key not in DEFAULTS:
        raise KeyError(key)
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if raw.lower() in ("1", "true", "yes", "on"):
            return True
        if raw.lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key} takes true or false")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, list):
        value = json.loads(raw) if raw.strip().startswith("[") else [s for s in raw.split(",") if s.strip()]
        return [str(v).strip() for v in value]
    return raw


def setting_path(settings: dict[str, Any], key: str) -> Path:
    return Path(os.path.expanduser(str(settings[key])))


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(STATE_FILE, state)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
