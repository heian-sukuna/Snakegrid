#!/usr/bin/env bash
# Record a short screen capture of snakegrid and turn it into docs/demo.gif.
#
# Needs: wf-recorder (Wayland capture) and one of gifski / ffmpeg to encode.
# Usage:  ./scripts/record-demo.sh [seconds]   (default 10)
#
# Suggested script while it records: open ~5 windows on grid workspace 1,
# then close a couple, so the "snake" slide is clearly visible.
set -e
SECS="${1:-10}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs"
mkdir -p "$OUT"
RAW="$(mktemp --suffix=.mp4)"
trap 'rm -f "$RAW"' EXIT

command -v wf-recorder >/dev/null || { echo "install wf-recorder first"; exit 1; }

echo "Recording ${SECS}s… open/close a few windows on your grid workspace now."
wf-recorder -f "$RAW" &
REC=$!
sleep "$SECS"
kill -INT "$REC" 2>/dev/null || true
wait "$REC" 2>/dev/null || true

if command -v gifski >/dev/null; then
    gifski --fps 20 --width 960 -o "$OUT/demo.gif" "$RAW"
elif command -v ffmpeg >/dev/null; then
    ffmpeg -y -i "$RAW" -vf "fps=20,scale=960:-1:flags=lanczos" "$OUT/demo.gif"
else
    echo "install gifski or ffmpeg to encode the gif"; exit 1
fi

echo "✅ wrote $OUT/demo.gif — then uncomment the image line in README.md"
