# snakegrid 🐍

A tiny, dependency-free **layout daemon for [Hyprland](https://hypr.land)** that turns
two desktops into a flowing **2×2 "snake" grid**. New windows enter the top-left and
everything else *slides* one slot along the snake — and when the first desktop fills up,
the oldest window glides onto the next one.

> Pure `python3` + `hyprctl`. No plugins to compile, no extra dependencies.

## What it does

- The **newest window** always takes **Desktop A's top-left**; every existing window
  slides one step along the snake path: **TL → TR → BR → BL**.
- When Desktop A's four tiles are full, the **oldest window overflows to Desktop B** and
  the snake continues there — **8 tiles across two desktops**.
- A **9th** window just opens normally (the grid is capped at 8).
- **Every other desktop is left as normal Hyprland tiling.**
- **Multi-monitor aware** — each grid desktop is laid out on its own monitor, with correct
  offsets and HiDPI scaling (it reads your live geometry from `hyprctl`, nothing is hardcoded).
- The sliding is Hyprland's own `windowsMove` animation, so it looks smooth for free.

## Requirements

- Hyprland
- `python3` (standard library only)

## Install

```sh
git clone https://github.com/<you>/snakegrid.git
cd snakegrid
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
```

## How it works

The daemon connects to Hyprland's event socket (`.socket2.sock`). On every window
**open/close** it floats the managed windows and repositions them into the grid with
`hyprctl dispatch movewindowpixel / resizewindowpixel`. Because those moves go through
Hyprland's normal animation pipeline, the tiles **slide** into place.

## License

[MIT](LICENSE) © 2026 Ryan Wanyika (malvryn)
