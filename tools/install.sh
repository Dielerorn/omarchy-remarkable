#!/usr/bin/env bash
# Developer install: put this checkout into the running Omarchy shell, or update
# the installed copy from it. Everyone else: `omarchy plugin add` (see README).
#
# Writing into ~/.config/omarchy/plugins/<id>/ while the shell runs triggers a
# reload of every plugin (tens of seconds frozen on a busy bar), so this stops
# the shell first, updates the files, then starts it again (~10 s without bar).
set -euo pipefail

src="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
id="dielerorn.remarkable"
dest="$HOME/.config/omarchy/plugins/$id"
shell_path="/usr/share/omarchy/shell"
export OMARCHY_SHELL_IPC_TIMEOUT="${OMARCHY_SHELL_IPC_TIMEOUT:-30s}"

say() { printf '%s\n' "$*"; }

if omarchy-hyprland-session-locked 2>/dev/null; then
  say "The session is locked; not restarting the shell."
  exit 1
fi
if [[ -n "$(git -C "$src" status --porcelain)" ]]; then
  say "Uncommitted changes in $src — commit them first (the plugin installs from git)."
  exit 1
fi

say "Stopping the shell…"
timeout 60 quickshell kill -p "$shell_path" --any-display >/dev/null 2>&1 || true
for _ in $(seq 60); do
  pgrep -f "^quickshell -n -p $shell_path" >/dev/null || break
  sleep 1
done

if [[ -d "$dest/.git" ]]; then
  # From this checkout, not the installed copy's origin, so local commits install.
  say "Updating $dest"
  git -C "$dest" fetch -q "$src" HEAD
  git -C "$dest" merge --ff-only -q FETCH_HEAD
else
  say "Installing to $dest"
  git clone -q "$src" "$dest"
fi


say "Starting the shell…"
hyprctl dispatch 'hl.dsp.exec_cmd("omarchy-launch-shell")' >/dev/null
for _ in $(seq 60); do
  [[ "$(omarchy-shell shell ping 2>/dev/null)" == "ok" ]] && break
  sleep 1
done
if [[ "$(omarchy-shell shell ping 2>/dev/null)" != "ok" ]]; then
  say "The shell didn't answer; run: hyprctl dispatch 'hl.dsp.exec_cmd(\"omarchy-launch-shell\")'"
  exit 1
fi
say "Done. Optional extras (Open With entry, command on PATH): Settings → Desktop."
