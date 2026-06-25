#!/usr/bin/env bash
# Installs snakegrid: the daemon + the `snakegrid` command, plus a gated
# autostart line for Hyprland. Re-runnable (safe to run again to update).
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/hypr/scripts"
BIN="$HOME/.local/bin"
AUTO="$HOME/.config/hypr/conf/autostart.conf"

mkdir -p "$DEST" "$BIN"
install -m 755 "$SRC/snake-grid.py" "$DEST/snake-grid.py"
install -m 755 "$SRC/snakegrid"      "$DEST/snakegrid"
ln -sf "$DEST/snakegrid" "$BIN/snakegrid"

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
case ":$PATH:" in *":$BIN:"*) ;; *) echo "   ⚠  add $BIN to your PATH to use 'snakegrid' directly." ;; esac
