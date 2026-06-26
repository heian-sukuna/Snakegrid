#!/usr/bin/env python3
# ─── snakegrid daemon ─────────────────────────────────────────────────────────
# A 2x2 "snake" floating grid spread across two Hyprland desktops.
#   • the newest window enters Desktop-A's top-left; everyone slides one slot
#   • snake path per grid:  TL -> TR -> BR -> BL
#   • when Desktop A (4 tiles) overflows, the oldest hops to Desktop B and snakes
#   • all other desktops are left as normal tiling
#   • multi-monitor aware: each grid desktop is laid out on its own monitor
#
# You normally control this with the `snakegrid` command, not directly.
# Stop a stray instance with:  pkill -f snake-grid.py
import socket, os, json, subprocess, threading

# ── CONFIG ────────────────────────────────────────────────────────────────────
GRID_WS = [int(x) for x in os.environ.get("SNAKE_WS", "1,2").split(",")]  # grid desktops, in overflow order
SNAKE   = ["tl", "tr", "br", "bl"]   # the slide path (this defines the 2x2 snake)
GAP     = 10                          # pixels between tiles / screen edge
# ──────────────────────────────────────────────────────────────────────────────

PERWS = len(SNAKE)              # tiles per desktop (4)
MAX   = PERWS * len(GRID_WS)    # total managed tiles
order = []                      # newest-first list of "0x..." window addresses

UID = os.getuid()
HIS = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
if not HIS:
    base = f"/run/user/{UID}/hypr"
    HIS = max(os.listdir(base), key=lambda d: os.path.getmtime(f"{base}/{d}"))
SOCK2 = f"/run/user/{UID}/hypr/{HIS}/.socket2.sock"

def H(*a):  return subprocess.run(["hyprctl", *a], capture_output=True, text=True).stdout
def Hj(*a):
    try:    return json.loads(H("-j", *a) or "null")
    except Exception: return None

def monitors():
    mons = Hj("monitors") or []
    by_name = {m["name"]: m for m in mons}
    focused = next((m for m in mons if m.get("focused")), (mons[0] if mons else None))
    ws2mon  = {w["id"]: w.get("monitor") for w in (Hj("workspaces") or [])}
    return by_name, focused, ws2mon

def slots_for(mon):
    sc = mon.get("scale", 1) or 1
    W, Ht = mon["width"] / sc, mon["height"] / sc          # logical size
    mx, my = mon["x"], mon["y"]                            # logical offset
    l, t, r, b = mon.get("reserved", [0, 0, 0, 0])         # [left, top, right, bottom]
    x0, y0 = mx + l + GAP, my + t + GAP
    cw = int((W - l - r - 3 * GAP) // 2)
    ch = int((Ht - t - b - 3 * GAP) // 2)
    return {
        "tl": (int(x0), int(y0)),                 "tr": (int(x0 + cw + GAP), int(y0)),
        "bl": (int(x0), int(y0 + ch + GAP)),      "br": (int(x0 + cw + GAP), int(y0 + ch + GAP)),
    }, cw, ch

def relayout():
    global order
    clients = {c["address"]: c for c in (Hj("clients") or [])}
    order = [a for a in order if a in clients][:MAX]
    by_name, focused, ws2mon = monitors()
    batch = []
    for i, a in enumerate(order):
        c  = clients[a]
        ws = GRID_WS[i // PERWS]
        mon = by_name.get(ws2mon.get(ws)) or focused
        if not mon:
            continue
        sl, cw, ch = slots_for(mon)
        sx, sy = sl[SNAKE[i % PERWS]]
        ad = f"address:{a}"
        if not c.get("floating"):
            batch.append(f"dispatch setfloating {ad}")
        if c["workspace"]["id"] != ws:
            batch.append(f"dispatch movetoworkspacesilent {ws},{ad}")
        batch.append(f"dispatch resizewindowpixel exact {cw} {ch},{ad}")
        batch.append(f"dispatch movewindowpixel exact {sx} {sy},{ad}")
    if batch:
        H("--batch", " ; ".join(batch))

# ── deferred re-layout ──────────────────────────────────────────────────────
# Some apps (browsers like Zen/Firefox) restore their own remembered window size
# a moment AFTER they map, which clobbers the grid placement and pushes the
# window out of its 1/4 tile. So after a window opens we re-apply the layout a
# few times across the first ~1.6s, snapping the settled window back into place.
# A lock serialises these with the event loop so they don't trample `order`.
LOCK            = threading.Lock()
RESETTLE_DELAYS = (0.3, 0.8, 1.6)

def relayout_locked():
    with LOCK:
        relayout()

def resettle():
    for d in RESETTLE_DELAYS:
        threading.Timer(d, relayout_locked).start()

def main():
    # grab any windows already on the grid desktops (e.g. after a reload)
    cs = [c for c in (Hj("clients") or []) if c["workspace"]["id"] in GRID_WS]
    cs.sort(key=lambda c: c.get("focusHistoryID", 1e9))   # most-recently-focused first
    for c in cs[:MAX]:
        order.append(c["address"])
    if order:
        relayout()
        resettle()   # snap any already-open self-resizers (e.g. a browser) back

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK2)
    for raw in s.makefile("r"):
        ev, _, data = raw.strip().partition(">>")
        try:
            if ev == "openwindow":
                p = data.split(",")
                addr = "0x" + p[0]
                ws = int(p[1]) if p[1].lstrip("-").isdigit() else None
                if ws in GRID_WS and addr not in order and len(order) < MAX:
                    with LOCK:
                        order.insert(0, addr)
                        relayout()
                    resettle()   # re-snap if the app resizes itself on startup
            elif ev == "closewindow":
                addr = "0x" + data.strip()
                if addr in order:
                    with LOCK:
                        order.remove(addr)
                        relayout()
        except Exception:
            pass

if __name__ == "__main__":
    main()
