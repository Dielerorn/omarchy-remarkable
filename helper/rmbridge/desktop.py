"""Optional desktop integration, added only when you ask for it.

- "Send to reMarkable" in file managers' Open With menus: a .desktop file in
  ~/.local/share/applications.
- The `omarchy-remarkable` command on your PATH: a link in ~/.local/bin.

`omarchy plugin add` only clones the plugin, so neither exists until you turn
it on (Settings → Desktop, or `omarchy-remarkable desktop add`). Removing
touches only files that point back at this plugin.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ENTRY_NAME = "omarchy-remarkable-send.desktop"
MIME_TYPES = ["application/pdf", "application/epub+zip", "image/png", "image/jpeg", "image/webp",
              "image/gif", "text/plain", "text/markdown", "text/html"]


def plugin_dir() -> Path:
    return Path(os.environ.get("OMARCHY_REMARKABLE_PLUGIN") or Path(__file__).resolve().parents[2])


def launcher() -> Path:
    return plugin_dir() / "bin" / "omarchy-remarkable"


def applications_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications"


def entry_path() -> Path:
    return applications_dir() / ENTRY_NAME


def command_link() -> Path:
    return Path.home() / ".local/bin" / "omarchy-remarkable"


def entry_text() -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Send to reMarkable",
        "Comment=Send this file to your reMarkable tablet",
        f'Exec="{launcher()}" send --notify %F',
        f"Icon={plugin_dir() / 'assets' / 'icon.svg'}",
        "Terminal=false",
        "NoDisplay=true",
        "MimeType=" + ";".join(MIME_TYPES) + ";",
        "X-Omarchy-Plugin=dielerorn.remarkable",
        "",
    ])


def _ours_entry(path: Path) -> bool:
    try:
        text = path.read_text()
    except OSError:
        return False
    return "X-Omarchy-Plugin=dielerorn.remarkable" in text or "omarchy-remarkable send" in text


def _ours_link(path: Path) -> bool:
    return path.is_symlink() and os.readlink(path).endswith("bin/omarchy-remarkable")


def status() -> dict:
    link = command_link()
    return {
        "openWith": entry_path().is_file() and _ours_entry(entry_path()),
        "command": _ours_link(link),
        "commandTaken": (link.exists() or link.is_symlink()) and not _ours_link(link),
        "commandOnPath": bool(shutil.which("omarchy-remarkable")),
    }


def _refresh_menus() -> None:
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(applications_dir())],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def add(open_with: bool = True, command: bool = True) -> dict:
    if open_with:
        target = entry_path()
        if target.exists() and not _ours_entry(target):
            return {"ok": False, "error": f"{target} exists and isn't ours; leaving it alone"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry_text())
        _refresh_menus()
    if command:
        link = command_link()
        if (link.exists() or link.is_symlink()) and not _ours_link(link):
            return {"ok": False, "error": f"{link} exists and isn't ours; leaving it alone"}
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp = link.with_name(link.name + ".tmp")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(launcher())
        tmp.replace(link)
    return {"ok": True, **status()}


def remove(open_with: bool = True, command: bool = True) -> dict:
    removed: list[str] = []
    if open_with and entry_path().is_file() and _ours_entry(entry_path()):
        entry_path().unlink()
        removed.append(str(entry_path()))
        _refresh_menus()
    if command and _ours_link(command_link()):
        command_link().unlink()
        removed.append(str(command_link()))
    return {"ok": True, "removed": removed, **status()}


def _parts(args: argparse.Namespace) -> tuple[bool, bool]:
    if not args.open_with and not args.command:
        return True, True
    return args.open_with, args.command


def register(sub) -> None:
    p = sub.add_parser("desktop", help="optional Open With entry and command on PATH")
    dsub = p.add_subparsers(dest="action", required=True)
    s = dsub.add_parser("status")
    s.set_defaults(func=lambda a: {"ok": True, **status()})
    for name, fn, text in (("add", add, "add them (both unless you pick one)"),
                           ("remove", remove, "remove what this plugin added")):
        x = dsub.add_parser(name, help=text)
        x.add_argument("--open-with", action="store_true", help="only the Open With entry")
        x.add_argument("--command", action="store_true", help="only the command link")
        x.set_defaults(func=lambda a, fn=fn: fn(*_parts(a)))
