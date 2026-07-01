#!/usr/bin/env python3
# ─── snakegrid daemon ─────────────────────────────────────────────────────────
# A "snake" floating grid spread across two (or more) Hyprland desktops.
#   • the newest window enters Desktop-A's top-left; everyone slides one slot
#   • snake path is a boustrophedon over an R×C grid (default 2×2: TL→TR→BR→BL)
#   • when Desktop A overflows, the oldest hops to Desktop B and snakes there
#   • all other desktops are left as normal tiling
#   • multi-monitor aware: each grid desktop is laid out on its own monitor
#
# You normally control this with the `snakegrid` command, not directly.
# Stop a stray instance with:  pkill -f snake-grid.py
#
# PERFORMANCE: this talks to Hyprland's command socket (.socket.sock) DIRECTLY
# instead of spawning the `hyprctl` binary for every query/dispatch. We also
# (a) only send the dispatches that actually change something ("drift-skip"),
# so a window already in place costs zero commands and never re-jiggles, and
# (b) cache monitor geometry, refreshing it only when monitors/workspaces change.
#
# PLAYS NICE: dialogs/popups (windows that map floating) and any class listed in
# $SNAKE_IGNORE are left alone. Drag a managed window off the grid and it is
# released; drag any window onto the grid and it is adopted. Fullscreen windows
# are never fought.
import json
import os
import signal
import socket
import threading
import time

# ── CONFIG (all overridable via environment) ──────────────────────────────────
GRID_WS = [int(x) for x in os.environ.get("SNAKE_WS", "1,2").split(",")]  # grid desktops, in overflow order
GAP     = int(os.environ.get("SNAKE_GAP", "10"))   # pixels between tiles / screen edge
TOL     = int(os.environ.get("SNAKE_TOL", "2"))    # px slack before we bother moving/resizing a window
IGNORE  = {c for c in os.environ.get("SNAKE_IGNORE", "").split(",") if c}  # window classes to never manage


def parse_grid(spec):
    """'RxC' -> (rows, cols). Falls back to 2×2 on anything malformed."""
    try:
        r, _, c = str(spec).lower().partition("x")
        rows, cols = int(r), int(c)
        if rows >= 1 and cols >= 1:
            return rows, cols
    except (ValueError, AttributeError):
        pass
    return 2, 2


def make_snake(rows, cols):
    """Boustrophedon slot order: row 0 L→R, row 1 R→L, … as a list of (row, col)."""
    path = []
    for r in range(rows):
        col_range = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
        for c in col_range:
            path.append((r, c))
    return path


ROWS, COLS = parse_grid(os.environ.get("SNAKE_GRID", "2x2"))
SNAKE_PATH = make_snake(ROWS, COLS)   # the slide path (this defines the snake)
PERWS = len(SNAKE_PATH)               # tiles per desktop
MAX   = PERWS * len(GRID_WS)          # total managed tiles
# ──────────────────────────────────────────────────────────────────────────────

# Optional latency tracing: run with SNAKE_DEBUG=1 to log timings to /tmp/snakegrid.log
DEBUG = bool(os.environ.get("SNAKE_DEBUG"))


def log(msg):
    if DEBUG:
        with open("/tmp/snakegrid.log", "a") as f:
            f.write(f"{time.time():.4f} {msg}\n")


order = []   # newest-first list of "0x..." window addresses

UID = os.getuid()
RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{UID}"
STATE_FILE = f"{RUNTIME}/snakegrid.state"   # managed addresses, so teardown re-tiles only ours


def _detect_his():
    """Find the Hyprland instance signature (or None outside a Hyprland session)."""
    his = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if his:
        return his
    base = f"/run/user/{UID}/hypr"
    try:
        return max(os.listdir(base), key=lambda d: os.path.getmtime(f"{base}/{d}"))
    except (FileNotFoundError, ValueError):
        return None


HIS = _detect_his()
SOCK1 = f"/run/user/{UID}/hypr/{HIS}/.socket.sock" if HIS else None   # command/query socket
SOCK2 = f"/run/user/{UID}/hypr/{HIS}/.socket2.sock" if HIS else None  # event stream


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
    except OSError as e:
        log(f"ipc error: {e!r}")
        return b""


def Hj(cmd: str):
    """JSON query, e.g. Hj('clients'). Mirrors `hyprctl -j <cmd>`."""
    try:
        return json.loads(_ipc("j/" + cmd) or b"null")
    except Exception as e:
        log(f"query {cmd!r} failed: {e!r}")
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
    """Grid origin (x0, y0) and cell size (cw, ch) for one monitor, in logical px."""
    sc = mon.get("scale", 1) or 1
    W, Ht = mon["width"] / sc, mon["height"] / sc          # logical size
    mx, my = mon["x"], mon["y"]                            # logical offset
    rv_l, rv_t, rv_r, rv_b = mon.get("reserved", [0, 0, 0, 0])   # [left, top, right, bottom]
    x0, y0 = mx + rv_l + GAP, my + rv_t + GAP
    cw = int((W - rv_l - rv_r - (COLS + 1) * GAP) // COLS)
    ch = int((Ht - rv_t - rv_b - (ROWS + 1) * GAP) // ROWS)
    return int(x0), int(y0), cw, ch


def slot_xy(x0, y0, cw, ch, cell):
    """Top-left pixel of a (row, col) cell."""
    row, col = cell
    return x0 + col * (cw + GAP), y0 + row * (ch + GAP)


def _near(a, b):
    return a is not None and b is not None and abs(a - b) <= TOL


def should_manage(c, cls):
    """A window we should slot into the grid (vs. a dialog/popup/ignored class)."""
    if cls in IGNORE:
        return False
    if c.get("floating"):   # dialogs & popups map floating; leave them be
        return False
    return True


# ── state file (so `snakegrid` teardown re-tiles only windows we manage) ───────
_state_written = None


def write_state():
    global _state_written
    key = tuple(order)
    if key == _state_written:
        return
    _state_written = key
    try:
        with open(STATE_FILE, "w") as f:
            f.write("\n".join(order))
    except OSError as e:
        log(f"state write failed: {e!r}")


def relayout(clients=None):
    global order
    if clients is None:
        clients = {c["address"]: c for c in (Hj("clients") or [])}
    order = [a for a in order if a in clients][:MAX]
    by_name, focused, ws2mon = monitors()
    batch = []
    for i, a in enumerate(order):
        c  = clients[a]
        if c.get("fullscreen"):   # never fight a fullscreen window
            continue
        ws = GRID_WS[i // PERWS]
        mon = by_name.get(ws2mon.get(ws)) or focused
        if not mon:
            continue
        x0, y0, cw, ch = slots_for(mon)
        sx, sy = slot_xy(x0, y0, cw, ch, SNAKE_PATH[i % PERWS])
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
    write_state()


# ── deferred re-layout ──────────────────────────────────────────────────────
# Some apps (browsers like Zen/Firefox) restore their own remembered window size
# a moment AFTER they map, which clobbers the grid placement and pushes the
# window out of its tile. So after a window opens we re-apply the layout a few
# times across the first second, snapping the settled window back into place.
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


def _parse_addr_ws(data):
    """From an event payload 'ADDR,WORKSPACEID[,...]' → ('0x…', ws_int_or_None)."""
    p = data.split(",")
    addr = "0x" + p[0]
    ws = int(p[1]) if len(p) > 1 and p[1].lstrip("-").isdigit() else None
    return addr, ws


def handle_event(raw):
    ev, _, data = raw.strip().partition(">>")
    try:
        if ev == "openwindow":
            p = data.split(",")
            addr = "0x" + p[0]
            ws = int(p[1]) if len(p) > 1 and p[1].lstrip("-").isdigit() else None
            cls = p[2] if len(p) > 2 else ""
            if ws in GRID_WS and addr not in order and len(order) < MAX:
                placed = False
                with LOCK:
                    clients = {c["address"]: c for c in (Hj("clients") or [])}
                    c = clients.get(addr)
                    if c is not None and should_manage(c, cls):
                        log(f"openwindow {addr} ws={ws} cls={cls} -> placing")
                        order.insert(0, addr)
                        relayout(clients)
                        placed = True
                    else:
                        log(f"openwindow {addr} cls={cls} left alone")
                if placed:
                    resettle()   # re-snap if the app resizes itself on startup
        elif ev == "closewindow":
            addr = "0x" + data.strip()
            if addr in order:
                with LOCK:
                    order.remove(addr)
                    relayout()
        elif ev == "movewindowv2":
            addr, ws = _parse_addr_ws(data)
            with LOCK:
                if addr in order and ws not in GRID_WS:
                    # user pulled a managed window off the grid — release it
                    log(f"release {addr} (moved to ws {ws})")
                    order.remove(addr)
                    relayout()
                elif addr not in order and ws in GRID_WS and len(order) < MAX:
                    # user dropped a window onto the grid — adopt it into the next free tile
                    clients = {c["address"]: c for c in (Hj("clients") or [])}
                    c = clients.get(addr)
                    if c is not None and should_manage(c, c.get("class", "")):
                        log(f"adopt {addr} (moved to ws {ws})")
                        order.append(addr)
                        relayout(clients)
        elif ev == "fullscreen":
            with LOCK:
                relayout()
        elif ev in GEOM_EVENTS:
            invalidate_monitors()
            with LOCK:
                relayout()
    except Exception as e:
        log(f"error handling {ev!r}: {e!r}")


def event_loop():
    """Read Hyprland's event stream, reconnecting with backoff if it drops."""
    backoff = 0.5
    while True:
        s = None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(SOCK2)
            backoff = 0.5
            for raw in s.makefile("r"):
                handle_event(raw)
        except OSError as e:
            log(f"event socket error: {e!r}")
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        log(f"event socket closed; reconnecting in {backoff:.1f}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 10)
        invalidate_monitors()   # geometry may have changed while we were disconnected


_lock_sock = None   # kept alive for the process lifetime; releasing it frees the singleton


def acquire_singleton():
    """Bind a per-instance abstract socket so a second daemon can't start."""
    global _lock_sock
    _lock_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        _lock_sock.bind("\0snakegrid-" + (HIS or "default"))
    except OSError:
        log("another snakegrid daemon is already running; exiting")
        raise SystemExit(0)


def main():
    if not HIS:
        raise SystemExit("snakegrid: no Hyprland session found (HYPRLAND_INSTANCE_SIGNATURE unset).")
    acquire_singleton()
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))

    # grab any manageable windows already on the grid desktops (e.g. after a reload)
    cs = [c for c in (Hj("clients") or [])
          if c["workspace"]["id"] in GRID_WS and should_manage(c, c.get("class", ""))]
    cs.sort(key=lambda c: c.get("focusHistoryID", 1e9))   # most-recently-focused first
    for c in cs[:MAX]:
        order.append(c["address"])
    if order:
        relayout()
        resettle()   # snap any already-open self-resizers (e.g. a browser) back

    event_loop()


if __name__ == "__main__":
    main()
