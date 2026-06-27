# snakegrid 🐍

A tiny, dependency-free **layout daemon for [Hyprland](https://hypr.land)** that turns
two desktops into a flowing **2×2 "snake" grid**. New windows enter the top-left and
everything else *slides* one slot along the snake — and when the first desktop fills up,
the oldest window glides onto the next one.

> Pure `python3` standard library. It talks **straight to Hyprland's IPC sockets** — no
> `hyprctl` forking at runtime, no plugins to compile, no extra dependencies.

## What it does

- The **newest window** always takes **Desktop A's top-left**; every existing window
  slides one step along the snake path: **TL → TR → BR → BL**.
- When Desktop A's four tiles are full, the **oldest window overflows to Desktop B** and
  the snake continues there — **8 tiles across two desktops**.
- A **9th** window just opens normally (the grid is capped at 8).
- **Every other desktop is left as normal Hyprland tiling.**
- **Multi-monitor aware** — each grid desktop is laid out on its own monitor, with correct
  offsets and HiDPI scaling (it reads your live geometry from Hyprland, nothing is hardcoded).
- The sliding is Hyprland's own `windowsMove` animation, so it looks smooth for free.

## Fast by design

- **Direct socket IPC** — queries (`j/clients`, `j/monitors`) and dispatches (`[[BATCH]]…`)
  go straight to Hyprland's command socket instead of spawning the `hyprctl` binary, which
  is ~**25× faster per call** (≈0.5 ms vs ≈12.5 ms). A relayout is sub-millisecond.
- **Drift-skip** — each pass only sends the dispatches that actually change something
  (position/size/float-state/desktop, within a small pixel tolerance). A window already in
  place costs **zero** commands, so nothing re-jiggles and there's no wasted work.
- **Cached geometry** — monitor/workspace info is read once and reused, refreshed only when
  monitors or workspaces actually change.

> If a window *creation* ever feels slow, that's the **application** spawning its window —
> snakegrid places it in well under a millisecond *after* it appears (see Debugging).

## Requirements

- Hyprland
- `python3` (standard library only)

## Install

```sh
git clone https://github.com/heian-sukuna/Snakegrid.git
cd Snakegrid
./install.sh
```

This copies the daemon + the `snakegrid` command into `~/.config/hypr/scripts`, links
`snakegrid` into `~/.local/bin`, and adds a *gated* autostart line to your Hyprland config
(it only runs at login if you ask it to — see below).

## Usage — just four commands

| Command | What it does |
|---|---|
| `snakegrid` | toggle the grid **on/off for this session** |
| `snakegrid --always-on` | start it **automatically at every login** |
| `snakegrid --always-off` | stop auto-starting it at login (and turn it off now) |
| `snakegrid --help` | show help |

## Configure

Edit the `CONFIG` block at the top of `snake-grid.py`:

```python
GRID_WS = [1, 2]                 # the two grid desktops (also via $SNAKE_WS, e.g. SNAKE_WS=4,5)
SNAKE   = ["tl","tr","br","bl"]  # the slide path (defines the 2x2 snake)
GAP     = 10                     # pixels between tiles and screen edges
TOL     = 2                      # px slack before a window is considered "out of place"
```

**Tip:** for a snappier feel, give Hyprland a quick, no-overshoot move animation in your
config, e.g. `animation = windowsMove, 1, 2, snappy` (200 ms). snakegrid's placement is
instant; this just controls how the slide *looks*.

**Tip — instant opens:** a window opens *tiled and fullscreen* for an instant before
snakegrid floats it into its 1/4 tile, so you briefly see it pop up huge and shrink. Since
the **newest** window always goes to the **top-left** slot, you can open it there directly
with a window rule and skip that shrink entirely — drift-skip then leaves it put while the
older windows slide along the snake. Put the TL slot's geometry for your monitor in
`hyprland.conf` (here: 1920×1080, a top bar, `GAP=10` → position `10 60`, size `945 500`):

```
windowrule = float true,    match:workspace 1
windowrule = size 945 500,  match:workspace 1
windowrule = move 10 60,    match:workspace 1
```

(`x = GAP`, `y = reserved_top + GAP`, `w = (mon_w − 3·GAP)/2`, `h = (mon_h − reserved_top − 3·GAP)/2`;
add the same three lines for your other grid workspace.) On a different monitor the coords
won't match and snakegrid just corrects with a one-off move, so it fails safe.

## How it works

The daemon connects to Hyprland's **event socket** (`.socket2.sock`) and reacts to window
**open/close** (and monitor/workspace changes). For each event it computes the target grid
from your live monitor geometry, then repositions the managed windows over the **command
socket** (`.socket.sock`) in a single batched request — floating each window and snapping it
to its slot with `movewindowpixel` / `resizewindowpixel`. Because those moves go through
Hyprland's normal animation pipeline, the tiles **slide** into place.

Some apps (browsers like Zen/Firefox) restore their own remembered window size a moment
*after* they map. To handle that, snakegrid re-applies the layout a few times over the first
second after a window opens — and thanks to drift-skip, those passes send nothing once the
window has settled.

## Debugging

Run the daemon with `SNAKE_DEBUG=1` to trace timings to `/tmp/snakegrid.log`:

```sh
SNAKE_DEBUG=1 python3 ~/.config/hypr/scripts/snake-grid.py
```

You'll see per-relayout duration + dispatch count, and `openwindow … → placed` markers —
handy for confirming where any latency really lives. It's off (and free) by default.

## License

[MIT](LICENSE) © 2026 Ryan Wanyika (malvryn)
