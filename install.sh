#!/usr/bin/env bash
# Installs snakegrid: the daemon + the `snakegrid` command, plus a gated
# autostart line for Hyprland. Re-runnable (safe to run again to update).
#
#   ./install.sh              install / update
#   ./install.sh --uninstall  remove everything this script added
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/hypr/scripts"
BIN="$HOME/.local/bin"
AUTO="$HOME/.config/hypr/conf/autostart.conf"
MARK="$HOME/.config/hypr/.snakegrid-autostart"
STATE="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/snakegrid.state"

uninstall() {
    pkill -f 'python3 .*snake-grid\.py' 2>/dev/null || true
    rm -f "$DEST/snake-grid.py" "$DEST/snakegrid" "$BIN/snakegrid" "$MARK" "$STATE"
    if [ -f "$AUTO" ]; then
        sed -i '/# snakegrid autostart/d; /snakegrid-autostart.*snake-grid\.py/d' "$AUTO"
        echo "• removed autostart line from $AUTO"
    fi
    echo "✅ uninstalled."
    exit 0
}

case "${1:-}" in
    --uninstall|-u) uninstall ;;
    "") ;;
    *) echo "usage: ./install.sh [--uninstall]"; exit 1 ;;
esac

# If a daemon is already running, note it now — after copying the new files we
# restart it, otherwise the old (pre-update) process keeps running and the update
# silently does nothing until the next login.
WAS_RUNNING=0
pgrep -f 'python3 .*snake-grid\.py' >/dev/null 2>&1 && WAS_RUNNING=1

mkdir -p "$DEST" "$BIN"
install -m 755 "$SRC/snake-grid.py" "$DEST/snake-grid.py"
install -m 755 "$SRC/snakegrid"      "$DEST/snakegrid"
ln -sf "$DEST/snakegrid" "$BIN/snakegrid"

# Restart a running daemon so the just-installed version takes effect immediately.
# We toggle through the CLI (not a bare pkill) so its stop() re-tiles the windows
# it managed via the state file before the old process dies: OFF, then ON again.
if [ "$WAS_RUNNING" = 1 ]; then
    "$BIN/snakegrid" >/dev/null 2>&1 || true   # running -> toggles OFF (clean teardown)
    "$BIN/snakegrid" >/dev/null 2>&1 || true   # now off  -> toggles ON with the new daemon
    echo "• restarted the running daemon to apply the update"
fi

# add the (marker-gated) autostart line once, if a Hyprland autostart.conf exists
if [ -f "$AUTO" ] && ! grep -q 'snakegrid-autostart' "$AUTO"; then
cat >> "$AUTO" <<'HEND'

# snakegrid autostart — only runs if `snakegrid --always-on` set the marker file
exec-once = sh -c '[ -f "$HOME/.config/hypr/.snakegrid-autostart" ] && exec python3 "$HOME/.config/hypr/scripts/snake-grid.py"'
HEND
  echo "• added gated autostart line to $AUTO"
elif [ ! -f "$AUTO" ]; then
  echo "• no $AUTO found — add this to your Hyprland config to enable --always-on:"
  echo "    exec-once = sh -c '[ -f \"\$HOME/.config/hypr/.snakegrid-autostart\" ] && exec python3 \"\$HOME/.config/hypr/scripts/snake-grid.py\"'"
fi

echo "✅ installed."
echo "   Try:  snakegrid          (toggle on/off)"
echo "         snakegrid --help"
echo "   Undo: ./install.sh --uninstall"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "   ⚠  add $BIN to your PATH to use 'snakegrid' directly." ;; esac
