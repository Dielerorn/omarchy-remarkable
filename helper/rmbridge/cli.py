"""`omarchy-remarkable <command>`: one JSON object out per command.

Feature modules add their own commands through a `register(sub)` function
listed in FEATURE_MODULES; a handler returns a dict, and errors become
{"ok": false, "error": ..., "code": ...}.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import cloud, config

FEATURE_MODULES = ["library", "send", "notes", "transcribe", "search", "web", "agenda",
                   "printer", "mirror", "usb", "ssh", "desktop", "daemon"]

Handler = Callable[[argparse.Namespace], dict]


def ok(**kw: Any) -> dict:
    return {"ok": True, **kw}


def progress(message: str, done: int | None = None, total: int | None = None) -> None:
    """Progress lines go to stderr so stdout stays one JSON object."""
    line: dict[str, Any] = {"progress": message}
    if done is not None and total:
        line.update(done=done, total=total)
    print(json.dumps(line), file=sys.stderr, flush=True)


def notify(title: str, body: str = "", *, icon: str = "document-edit", urgency: str = "normal") -> None:
    try:
        subprocess.Popen(["notify-send", "-a", "reMarkable", "-i", icon, "-u", urgency, title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def open_path(path: Path | str) -> None:
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# ------------------------------------------------------------------ core cmds


def cmd_status(args: argparse.Namespace) -> dict:
    lib_file = config.DATA_DIR / "library.json"
    cached = {}
    try:
        cached = json.loads(lib_file.read_text())
    except (OSError, ValueError):
        pass
    state = config.load_state()
    return ok(
        paired=cloud.is_paired(),
        rmapi=str(cloud.rmapi_path()) if cloud.rmapi_path().exists() else None,
        documents=sum(1 for e in cached.get("entries", []) if not e.get("folder")),
        libraryFetched=cached.get("fetched"),
        lastChange=state.get("lastChange"),
        connectUrl=cloud.CONNECT_URL,
    )


def cmd_pair(args: argparse.Namespace) -> dict:
    cloud.pair(args.code)
    return ok(paired=True, account=cloud.account())


def cmd_unpair(args: argparse.Namespace) -> dict:
    cloud.unpair()
    return ok(paired=False)


def cmd_account(args: argparse.Namespace) -> dict:
    return ok(account=cloud.account())


def cmd_settings_show(args: argparse.Namespace) -> dict:
    return ok(settings=config.load_settings(), defaults=config.DEFAULTS)


def cmd_settings_set(args: argparse.Namespace) -> dict:
    settings = config.load_settings()
    settings[args.key] = config.coerce(args.key, args.value)
    config.save_settings(settings)
    return ok(settings=settings)


def cmd_settings_reset(args: argparse.Namespace) -> dict:
    settings = config.load_settings()
    settings[args.key] = config.DEFAULTS[args.key]
    config.save_settings(settings)
    return ok(settings=settings)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omarchy-remarkable", description="reMarkable tablet tools for Omarchy")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="connection and library summary")
    s.set_defaults(func=cmd_status)

    c = sub.add_parser("cloud", help="reMarkable cloud login")
    csub = c.add_subparsers(dest="action", required=True)
    x = csub.add_parser("pair", help="connect with a one-time code from my.remarkable.com")
    x.add_argument("code")
    x.set_defaults(func=cmd_pair)
    x = csub.add_parser("unpair", help="forget the cloud login on this computer")
    x.set_defaults(func=cmd_unpair)
    x = csub.add_parser("account", help="show the connected account")
    x.set_defaults(func=cmd_account)
    x = csub.add_parser("connect-url", help="where to get a one-time code")
    x.set_defaults(func=lambda a: ok(url=cloud.CONNECT_URL))

    st = sub.add_parser("settings", help="plugin settings")
    ssub = st.add_subparsers(dest="action", required=True)
    x = ssub.add_parser("show")
    x.set_defaults(func=cmd_settings_show)
    x = ssub.add_parser("set")
    x.add_argument("key", choices=sorted(config.DEFAULTS))
    x.add_argument("value")
    x.set_defaults(func=cmd_settings_set)
    x = ssub.add_parser("reset")
    x.add_argument("key", choices=sorted(config.DEFAULTS))
    x.set_defaults(func=cmd_settings_reset)

    for name in FEATURE_MODULES:
        try:
            mod = importlib.import_module(f".{name}", __package__)
        except ModuleNotFoundError as e:
            if e.name and e.name.endswith(f"rmbridge.{name}"):
                continue
            raise
        reg = getattr(mod, "register", None)
        if reg:
            reg(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    config.ensure_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    started = time.time()
    try:
        result = args.func(args)
        code = 0 if result is None or result.get("ok", True) else 1
    except cloud.NotPaired as e:
        result, code = {"ok": False, "error": str(e), "code": "not-paired"}, 2
    except cloud.CloudError as e:
        result, code = {"ok": False, "error": str(e), "code": "cloud"}, 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # report, don't traceback into the bar
        if os.environ.get("OMARCHY_REMARKABLE_DEBUG"):
            traceback.print_exc()
        result, code = {"ok": False, "error": f"{type(e).__name__}: {e}", "code": "internal"}, 1
    if result is not None and not getattr(args, "streaming", False):
        result.setdefault("elapsed", round(time.time() - started, 2))
        print(json.dumps(result, default=str), flush=True)
    return code
