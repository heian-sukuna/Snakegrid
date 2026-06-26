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
#
# PERFORMANCE: this talks to Hyprland's command socket (.socket.sock) DIRECTLY
# instead of spawning the `hyprctl` binary for every query/dispatch. Forking
# hyprctl ~4x per relayout (and ~16x per window open) was the old lag source;
# a unix-socket round trip is sub-millisecond. We also (a) only send the
# dispatches that actually change something ("drift-skip"), so a window already
# in place costs zero commands and never re-jiggles, and (b) cache the monitor
# geometry, refreshing it only when monitors/workspaces actually change.
import socket, os, json, threading, time

# ── CONFIG ────────────────────────────────────────────────────────────────────
GRID_WS = [int(x) for x in os.environ.get("SNAKE_WS", "1,2").split(",")]  # grid desktops, in overflow order
SNAKE   = ["tl", "tr", "br", "bl"]   # the slide path (this defines the 2x2 snake)
GAP     = 10                          # pixels between tiles / screen edge
TOL     = 2                           # px slack before we bother moving/resizing a window
# ──────────────────────────────────────────────────────────────────────────────

# Optional latency tracing: run with SNAKE_DEBUG=1 to log timings to /tmp/snakegrid.log
DEBUG = bool(os.environ.get("SNAKE_DEBUG"))
def log(msg):
    if DEBUG:
        with open("/tmp/snakegrid.log", "a") as f:
            f.write(f"{time.time():.4f} {msg}\n")

PERWS = len(SNAKE)              # tiles per desktop (4)
MAX   = PERWS * len(GRID_WS)    # total managed tiles
order = []                      # newest-first list of "0x..." window addresses

UID = os.getuid()
HIS = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
if not HIS:
    base = f"/run/user/{UID}/hypr"
    HIS = max(os.listdir(base), key=lambda d: os.path.getmtime(f"{base}/{d}"))
SOCK1 = f"/run/user/{UID}/hypr/{HIS}/.socket.sock"   # command/query socket
SOCK2 = f"/run/user/{UID}/hypr/{HIS}/.socket2.sock"  # event stream

# ── direct IPC (no subprocess) ────────────────────────────────────────────────
def _ipc(req: str) -> bytes:
    """One request→response round trip on Hyprland's command socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(SOCK1)
            s.sendall(req.encode())
            buf = bytearray()
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return bytes(buf)
    except OSError:
        return b""

def Hj(cmd: str):
    """JSON query, e.g. Hj('clients'). Mirrors `hyprctl -j <cmd>`."""
    try:
        return json.loads(_ipc("j/" + cmd) or b"null")
    except Exception:
        return None

def H_batch(cmds):
    """Fire many dispatchers in one round trip. Mirrors `hyprctl --batch`."""
    if cmds:
        _ipc("[[BATCH]]" + " ; ".join(cmds))

# ── monitor geometry (cached) ─────────────────────────────────────────────────
_mon_cache = None   # (by_name, focused, ws2mon) or None when stale

def monitors():
    global _mon_cache
    if _mon_cache is None:
        mons = Hj("monitors") or []
        by_name = {m["name"]: m for m in mons}
        focused = next((m for m in mons if m.get("focused")), (mons[0] if mons else None))
        ws2mon  = {w["id"]: w.get("monitor") for w in (Hj("workspaces") or [])}
        _mon_cache = (by_name, focused, ws2mon)
    return _mon_cache

def invalidate_monitors():
    global _mon_cache
    _mon_cache = None

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

def _near(a, b):
    return a is not None and b is not None and abs(a - b) <= TOL

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
        # drift-skip: emit ONLY the dispatches that change something. A window
        # already floating, on the right desktop, and in its slot adds nothing
        # to the batch — no wasted IPC, no visual re-jiggle.
        if not c.get("floating"):
            batch.append(f"dispatch setfloating {ad}")
        if c["workspace"]["id"] != ws:
            batch.append(f"dispatch movetoworkspacesilent {ws},{ad}")
        cw0, ch0 = (c.get("size") or (None, None))
        cx0, cy0 = (c.get("at")   or (None, None))
        if not (_near(cw0, cw) and _near(ch0, ch)):
            batch.append(f"dispatch resizewindowpixel exact {cw} {ch},{ad}")
        if not (_near(cx0, sx) and _near(cy0, sy)):
            batch.append(f"dispatch movewindowpixel exact {sx} {sy},{ad}")
    if DEBUG:
        t = time.perf_counter()
        H_batch(batch)
        log(f"relayout {len(order)} win, {len(batch)} dispatches, {(time.perf_counter()-t)*1000:.1f}ms")
    else:
        H_batch(batch)

# ── deferred re-layout ──────────────────────────────────────────────────────
# Some apps (browsers like Zen/Firefox) restore their own remembered window size
# a moment AFTER they map, which clobbers the grid placement and pushes the
# window out of its 1/4 tile. So after a window opens we re-apply the layout a
# few times across the first second, snapping the settled window back into place.
# Thanks to drift-skip these passes are cheap: if nothing moved they send zero
# commands. A lock serialises them with the event loop so they don't trample
# `order`.
LOCK            = threading.Lock()
RESETTLE_DELAYS = (0.15, 0.45, 1.0)

def relayout_locked():
    with LOCK:
        relayout()

def resettle():
    for d in RESETTLE_DELAYS:
        threading.Timer(d, relayout_locked).start()

# events that change monitor geometry or workspace→monitor mapping, so the
# cached layout must be recomputed (and re-applied).
GEOM_EVENTS = {"monitoradded", "monitoraddedv2", "monitorremoved",
               "moveworkspace", "moveworkspacev2", "configreloaded"}

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
                    log(f"openwindow {addr} ws={ws} -> placing")
                    with LOCK:
                        order.insert(0, addr)
                        relayout()
                    log(f"openwindow {addr} placed")
                    resettle()   # re-snap if the app resizes itself on startup
            elif ev == "closewindow":
                addr = "0x" + data.strip()
                if addr in order:
                    with LOCK:
                        order.remove(addr)
                        relayout()
            elif ev in GEOM_EVENTS:
                invalidate_monitors()
                with LOCK:
                    relayout()
        except Exception:
            pass

if __name__ == "__main__":
    main()
