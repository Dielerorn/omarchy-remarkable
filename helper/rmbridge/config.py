"""Paths and user settings.

Everything the helper writes lives outside the plugin directory: any write
under ~/.config/omarchy/plugins/<id>/ makes the shell reload every plugin.
"""

from __future__ import annotations

import json
import os
import stat
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
# Without XDG_RUNTIME_DIR, not a guessable path under the shared /tmp.
RUNTIME_DIR = (Path(os.environ["XDG_RUNTIME_DIR"]) if os.environ.get("XDG_RUNTIME_DIR")
               else CACHE_DIR / "run") / "omarchy-remarkable"

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


def private_dirs() -> tuple[Path, ...]:
    """Everything under these holds private document data or credentials: the
    search index and transcripts, downloaded documents, page images, names."""
    return CONFIG_DIR, CACHE_DIR, DATA_DIR, RUNTIME_DIR


def ensure_dirs() -> None:
    """Create the app's folders, owner-only. The helper also runs with umask
    077 (cli.main), so what it writes inside is owner-only too."""
    for d in private_dirs():
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if d.stat().st_mode & 0o077:
            d.chmod(0o700)
    for d in (DOC_CACHE, RENDER_CACHE, THUMB_CACHE, TMP_DIR):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)


def tighten_private() -> int:
    """Owner-only permissions for everything already under private_dirs(), for
    files an older version wrote with the default umask. Symlinks (in the
    Python environment) are left alone. Returns how many were changed."""
    changed = 0
    for root in private_dirs():
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                try:
                    st = os.lstat(path)
                    if stat.S_ISLNK(st.st_mode) or not st.st_mode & 0o077:
                        continue
                    os.chmod(path, stat.S_IMODE(st.st_mode) & 0o700)
                    changed += 1
                except OSError:
                    pass
    return changed


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
