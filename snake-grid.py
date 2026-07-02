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
GAP     = int(os.environ.get("SNAKE_GAP", "10"))   # pixels between tiles / screen edge
TOL     = int(os.environ.get("SNAKE_TOL", "2"))    # px slack before we bother moving/resizing a window
IGNORE  = {c for c in os.environ.get("SNAKE_IGNORE", "").split(",") if c}  # window classes to never manage


def parse_ws(spec):
    """'1,2' -> [1, 2]. Skips blanks/space/junk tokens; falls back to [1, 2] if
    nothing usable is left (so a stray space like 'SNAKE_WS=1, 2' can't crash the
    daemon at import — it used to raise ValueError and never start)."""
    out = []
    for tok in str(spec).split(","):
        tok = tok.strip()
        if tok.lstrip("-").isdigit():
            out.append(int(tok))
    return out or [1, 2]


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


GRID_WS = parse_ws(os.environ.get("SNAKE_WS", "1,2"))   # grid desktops, in overflow order
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
            s.settimeout(2.0)   # if the compositor ever stalls mid-reply, fail out
            s.connect(SOCK1)    # instead of hanging forever while holding LOCK
            s.sendall(req.encode())
            buf = bytearray()
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return bytes(buf)
    except OSError as e:   # socket.timeout is an OSError subclass, so it lands here too
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


# ── monitor geometry (cached with a short TTL) ────────────────────────────────
# Caching makes a relayout pure arithmetic. Real monitor/workspace events
# invalidate the cache instantly (invalidate_monitors, below). But some geometry
# changes fire NO event — most importantly a bar (waybar) registering or resizing
# its reserved/exclusive area. At login the daemon can start BEFORE the bar
# reserves its strip, cache reserved=[0,0,0,0], and then place every tile under
# the bar forever, since nothing tells it to refresh. So the cache also
# self-expires after MON_TTL seconds — two sub-millisecond socket queries, which
# is nothing next to being persistently wrong.
_mon_cache = None      # (by_name, focused, ws2mon) or None when stale
_mon_cache_at = 0.0    # monotonic time the cache was last filled
MON_TTL = 2.0          # seconds before a re-query (heals event-less reserved changes)


def monitors():
    global _mon_cache, _mon_cache_at
    if _mon_cache is None or (time.monotonic() - _mon_cache_at) > MON_TTL:
        mons = Hj("monitors") or []
        by_name = {m["name"]: m for m in mons}
        focused = next((m for m in mons if m.get("focused")), (mons[0] if mons else None))
        ws2mon  = {w["id"]: w.get("monitor") for w in (Hj("workspaces") or [])}
        _mon_cache = (by_name, focused, ws2mon)
        _mon_cache_at = time.monotonic()
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


def _has_tag(c, tag):
    """True if window `c` carries `tag`. Hyprland reports a rule-applied tag with
    a trailing '*' (e.g. 'snakegrid*'), so compare on the stripped name."""
    return any(t.rstrip("*") == tag for t in (c.get("tags") or []))


def should_manage(c, cls):
    """A window we should slot into the grid (vs. a dialog/popup/ignored class).

    The base heuristic is "maps floating -> it's a dialog, leave it alone". The
    escape hatch is the 'snakegrid' tag: if you deliberately float some app on a
    grid desktop but still want it in the snake, a `tag +snakegrid` windowrule
    marks it as ours, so a tagged window is always managed while genuine (untagged)
    floating dialogs are still left alone. See README > Instant opens.
    """
    if cls in IGNORE:
        return False
    if _has_tag(c, "snakegrid"):   # pre-floated by our rule — manage it
        return True
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
            f.writelines(a + "\n" for a in order)   # trailing newline: teardown reads every line
    except OSError as e:
        log(f"state write failed: {e!r}")


def relayout(clients=None):
    global order, _chase
    if clients is None:
        if not order:
            return   # nothing managed and nothing to prune -> skip the clients query
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
    moved = bool(batch)
    if DEBUG:
        t = time.perf_counter()
        H_batch(batch)
        log(f"relayout {len(order)} win, {len(batch)} dispatches, {(time.perf_counter()-t)*1000:.1f}ms")
    else:
        H_batch(batch)
    # Chase snap-backs: some apps (Zen/Firefox) restore their own floating
    # geometry the instant we move them, so a single correction loses the race.
    # If we just moved something, re-check very soon and re-apply — drift-skip
    # makes this free the moment the window finally holds still. CHASE_MAX bounds
    # it so a window that fights forever only jitters briefly before we yield.
    if moved and _chase < CHASE_MAX:
        _chase += 1
        schedule(0.12)
    elif not moved:
        _chase = 0
    write_state()


# ── deferred re-layout (one scheduler thread) ────────────────────────────────
# Some apps (browsers like Zen/Firefox) restore their own remembered window size
# a moment AFTER they map, which clobbers the grid placement. So after a window
# opens we re-apply the layout a few times across the first second, snapping the
# settled window back into place. Thanks to drift-skip these passes are cheap: if
# nothing moved they send zero commands.
#
# Rather than spawn a fresh threading.Timer per delay per window (a 5-window
# session restore = 15 short-lived threads all waking to grab LOCK at once), a
# single long-lived scheduler thread sleeps until the nearest due time, then fires
# every deadline that's due as ONE coalesced relayout. LOCK serialises relayouts
# with the event loop so they never trample `order`.
LOCK            = threading.Lock()
# A few passes trailing off over the first several seconds. Browsers (Zen/Firefox)
# restore their remembered geometry a beat AFTER mapping, and keep re-asserting it
# through their startup — so an early pass alone loses. The later passes re-check
# after startup has settled; combined with chase-on-move (see relayout) they win
# and stick. Drift-skip makes a pass that finds nothing out of place send nothing.
RESETTLE_DELAYS = (0.15, 0.45, 1.0, 2.5, 5.0, 8.0)
CHASE_MAX       = 12    # max rapid re-applies to out-wait a window that snaps back
_chase          = 0     # consecutive relayouts that had to move something

_sched_cond = threading.Condition()
_deadlines  = []   # monotonic times at which a relayout is due


def relayout_locked():
    with LOCK:
        relayout()


def schedule(*delays):
    """Ask the scheduler thread to run a relayout `delay` seconds from now, for
    each delay. Duplicate/near deadlines coalesce into a single relayout."""
    now = time.monotonic()
    with _sched_cond:
        _deadlines.extend(now + d for d in delays)
        _sched_cond.notify()


def resettle():
    schedule(*RESETTLE_DELAYS)


def _scheduler():
    while True:
        with _sched_cond:
            while not _deadlines:
                _sched_cond.wait()
            wait = min(_deadlines) - time.monotonic()
            if wait > 0:
                _sched_cond.wait(timeout=wait)
                continue
            now = time.monotonic()
            due = [t for t in _deadlines if t <= now]     # everything ripe now…
            _deadlines[:] = [t for t in _deadlines if t > now]
        if due:
            relayout_locked()   # …collapses to one relayout (run outside the cond)


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
            cls = p[2] if len(p) > 2 else ""
            # p[1] is the workspace NAME, not its id — a renamed workspace ("web")
            # makes int() fail and the window is silently never managed. So use the
            # name only as a cheap pre-filter (a numeric name that isn't a grid ws
            # can't be ours), and take the authoritative id from the clients query.
            ws_name = p[1] if len(p) > 1 else ""
            if ws_name.lstrip("-").isdigit() and int(ws_name) not in GRID_WS:
                return
            if addr in order or len(order) >= MAX:
                return
            t0 = time.perf_counter() if DEBUG else 0.0
            placed = False
            with LOCK:
                clients = {c["address"]: c for c in (Hj("clients") or [])}
                t1 = time.perf_counter() if DEBUG else 0.0
                c = clients.get(addr)
                if c is not None and c["workspace"]["id"] in GRID_WS and should_manage(c, cls):
                    order.insert(0, addr)
                    relayout(clients)
                    placed = True
                    if DEBUG:
                        log(f"openwindow {addr} ws={c['workspace']['id']} cls={cls} -> placing "
                            f"[query {(t1-t0)*1000:.1f}ms, total {(time.perf_counter()-t0)*1000:.1f}ms]")
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
        elif ev == "activewindowv2":
            # Focusing a managed window is our chance to fix any drift Hyprland
            # never told us about (an app repositioning its own floating window
            # fires no move event). If it's still in place, drift-skip sends
            # nothing; we ignore focus on windows we don't manage.
            addr = "0x" + data.strip()
            if addr in order:
                schedule(0)
        elif ev == "fullscreen":
            with LOCK:
                relayout()
        elif ev in GEOM_EVENTS:
            # Monitor hotplug / configreloaded tend to arrive in bursts. Drop the
            # cache now, but debounce the relayout through the scheduler so a burst
            # collapses to a single pass instead of one per event.
            invalidate_monitors()
            schedule(0.15)
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
            # errors="replace": event payloads carry window titles, which can hold
            # arbitrary bytes. Strict UTF-8 would raise UnicodeDecodeError (a
            # ValueError, NOT an OSError) here and silently kill the reader — the
            # daemon would just "stop working". Replace undecodable bytes instead.
            for raw in s.makefile("r", errors="replace"):
                handle_event(raw)
        except OSError as e:
            log(f"event socket error: {e!r}")
        except Exception as e:   # never let a parse/decode error escape and stop the loop
            log(f"event loop error: {e!r}")
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
    log(f"daemon start: pid={os.getpid()} grid={ROWS}x{COLS} ws={GRID_WS} his={HIS}")

    # the settle/debounce scheduler runs for the life of the process
    threading.Thread(target=_scheduler, daemon=True).start()

    # Adopt what's already on the grid desktops at startup, in two passes:
    clients = {c["address"]: c for c in (Hj("clients") or [])}
    seen = set()

    # 1) windows we managed before a restart/crash. The state file survives an
    #    unclean exit (SIGTERM -> os._exit, no teardown), and those windows are
    #    still FLOATING from last time — which should_manage would reject as
    #    dialogs — so we trust the state file to take them back, preserving the
    #    previous snake order (the file is stored newest-first).
    try:
        with open(STATE_FILE) as f:
            for a in (ln.strip() for ln in f):
                c = clients.get(a)
                if a and a not in seen and c and c["workspace"]["id"] in GRID_WS:
                    order.append(a)
                    seen.add(a)
    except OSError:
        pass

    # 2) plus any other manageable (tiling, non-dialog) windows sitting on the
    #    grid — e.g. a genuine first run, or windows opened while we were down.
    fresh = [c for c in clients.values()
             if c["address"] not in seen
             and c["workspace"]["id"] in GRID_WS
             and should_manage(c, c.get("class", ""))]
    fresh.sort(key=lambda c: c.get("focusHistoryID", 1e9))   # most-recently-focused first
    for c in fresh:
        order.append(c["address"])
    del order[MAX:]

    if order:
        relayout(clients)
        resettle()   # snap any already-open self-resizers (e.g. a browser) back

    event_loop()


if __name__ == "__main__":
    main()
