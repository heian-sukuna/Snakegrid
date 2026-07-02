# snakegrid — Fix & Optimization Guide (for Claude Opus 4.8)

You are working on **snakegrid**, a zero-dependency Python daemon that arranges
Hyprland windows into a floating "snake" grid across workspaces 1 and 2
(newest window enters top-left, everyone slides one slot, overflow hops to the
next workspace). Repo: `~/Desktop/snakegrid` (GitHub: heian-sukuna/Sysops
owner's portfolio piece — public, MIT).

The owner (Ryan) reports: **still getting errors after fixing, and slow /
laggy responsiveness**. The root causes below were diagnosed live on his
machine on 2026-07-02 with evidence. Follow the steps **in order** — Step 1
explains why his past fixes appeared to do nothing.

## Hard constraints (do not violate)

- **Zero dependencies.** Python stdlib only. No pip packages, no asyncio
  rewrite unless it stays stdlib and simple.
- **Keep it generic and reusable.** This is a public portfolio repo. Nothing
  shipped in the repo may hardcode Ryan's machine (1920×1080, waybar height,
  etc.). Machine-specific values may only live in *his* Hyprland config, and
  even those should be eliminated where possible (see Step 3).
- **Explain what you change.** Ryan's rule: no black boxes. Keep the file's
  existing comment style — comments explain *why*, at the same density as the
  current code. He must be able to read the diff and understand it.
- **Keep README.md in sync** with any behavior you change (it documents the
  pre-place windowrule tip from commit `cf67e06` — Step 3 changes that story).
- Don't break `install.sh`, `PKGBUILD`, or `tests/` — run the checks in Step 7.

## Environment facts (verified 2026-07-02, don't re-derive)

- Hyprland **0.55.3**, single monitor `eDP-1` 1920×1080 scale 1.0,
  `reserved = [0, 50, 0, 0]` (waybar, top, 50px).
- Grid config is all defaults: `SNAKE_WS=1,2`, `SNAKE_GRID=2x2`, `GAP=10`,
  `TOL=2`. Correct TL slot today: **pos (10,60), size 945×500**.
- `hyprctl -j clients` on this version exposes:
  `address, at, size, floating, fullscreen (int), workspace{id,name}, class,
  initialClass, initialTitle, tags, pinned, xwayland, focusHistoryID, mapped,
  hidden, pid, stableId, xdgTag, …` — there is **no** parent/transient field,
  so "is this a dialog?" cannot be read directly from clients JSON.
- Repo tests: `HYPRLAND_INSTANCE_SIGNATURE= python3 -m pytest tests/ -q`
  → 20 passed. The bugs below are integration-level; tests don't catch them.
- Daemon is currently **not running**; autostart marker
  `~/.config/hypr/.snakegrid-autostart` is not set.
- **Prior "it's slow" diagnoses on this box (don't repeat them):**
  (a) 2026-06-26: felt-lag was Hyprland's `windowsMove` animation curve —
  fixed by tuning `conf/animations.conf`, daemon relayout measured ~2 ms;
  (b) also 06-26: "creating a window takes time" was ghostty cold-start
  (~2.9 s, fixed via `gtk-single-instance = true`) — event→placement was
  0.6 ms. If responsiveness still feels off after this guide's fixes,
  measure app-exec→openwindow and the animation config **before** touching
  the daemon again. The *new* daemon-side lag element fixed here is the
  stale-cache mis-correction in Step 2 (an animated ~50 px slide + wrong
  final position on every open).

---

## Step 1 — Fix the deploy pipeline (why "my fixes don't work")

**Evidence:** `~/.config/hypr/scripts/snake-grid.py` is the **Jun 26**
version (9.6 KB, no `should_manage`, no `SNAKE_IGNORE`, no signal handler,
no state-file logic). The repo copy is **Jul 1** (15 KB). They differ.
`snakegrid` (the CLI, `find_daemon()` at `snakegrid:15-26`) searches
`~/.config/hypr/scripts` **first** and the checkout copy **last** — so even
when Ryan ran `snakegrid` from the repo directory, it launched the stale
installed daemon. The Hyprland autostart line also points at the installed
copy. Every repo fix since Jun 26 has never executed.

**Fix:**
1. In `snakegrid`'s `find_daemon()`, search `$self/snake-grid.py` (the copy
   next to the resolved script) **first**, then the installed locations.
   When installed, `~/.local/bin/snakegrid` is a symlink into
   `~/.config/hypr/scripts/`, and `readlink -f` resolves it, so `$self` still
   finds the installed daemon — behavior is unchanged for installed users,
   but running from a checkout now always uses the checkout.
2. Make `install.sh` **restart a running daemon** after copying (it currently
   installs new files and leaves the old process running — the second half of
   this trap). Use the same stop logic as the CLI (`stop()` re-tiles managed
   windows from the state file before killing) — don't bare-`pkill`.
3. **Before any live testing:** kill stray old instances — but NOT with a
   bare `pkill -f snake-grid.py`: that pattern matches the invoking shell's
   own command line and kills your shell mid-command (verified gotcha on
   this box). Use the CLI's anchored pattern
   `pkill -f 'python3 .*snake-grid\.py'` or kill by PID via
   `ps -eo pid=,comm=,args= | awk '$2=="python3" && /snake-grid\.py/ {print $1}'`.
   This matters: the Jun 26 daemon predates `acquire_singleton()`, so it
   never bound the lock socket — a new daemon will happily start *alongside*
   it and the two will fight over window positions (jiggle wars). Kill
   everything, then start fresh.

**Verify:** `./install.sh`, then `pgrep -af snake-grid` shows exactly one
process, running the file you just edited (check mtime/size or add a startup
log line under `SNAKE_DEBUG=1`).

## Step 2 — Fix stale monitor geometry (the screenshot bug)

**Evidence:** Ryan's screenshot shows a Zen browser tile at ≈(10,10) sized
≈945×525, top edge under the waybar. That is *exactly* the TL slot computed
with `reserved = [0,0,0,0]`: `y0 = 0+0+10 = 10`, `ch = (1080−0−3·10)//2 = 525`.
The correct values (reserved top = 50) are (10,60) 945×500.

**Mechanism:** `monitors()` (`snake-grid.py:137-145`) caches geometry forever
and only `GEOM_EVENTS` invalidate it. At login, `exec-once` starts the daemon
before waybar registers its exclusive zone, so the first query caches
`reserved=[0,0,0,0]` — and **Hyprland emits no event when reserved space
changes** (waybar mapping, waybar restart, bar height change), so the cache
never heals. Drift-skip then *perpetuates* the wrong position: every new
window gets pre-placed correctly at (10,60) by the windowrule, then the
daemon "corrects" it to (10,10) under the bar — a visible jiggle plus a
wrongly placed window, every single open. This is both the "error" and a
chunk of the perceived lag.

**Fix (keep the cache, add a TTL):** record a timestamp when the cache is
filled; in `monitors()`, treat entries older than ~1–2 s as stale and
re-query. A monitors+workspaces query is two sub-millisecond unix-socket
round trips — one extra pair per settle burst is nothing, and correctness
beats a micro-optimization that caused a real bug. Keep the existing
event-driven `invalidate_monitors()` too (it gives instant refresh on real
monitor events; the TTL is the safety net for reserved-area changes that
have no event).

**Verify:** with the daemon running, `pkill waybar; waybar &` (reserved goes
0 → 50), wait 2 s, open a window on ws 1 → it must land at (10,60) 945×500.
Check with `hyprctl -j clients | jq '.[] | select(.workspace.id==1) | {at, size}'`.

## Step 3 — Resolve the windowrule ⇄ `should_manage` conflict

**Evidence:** `~/.config/hypr/conf/windowrules.conf:41-46` force-floats and
pre-places **every** window opening on ws 1/2 at hardcoded (10,60) 945×500
("instant open" hack, documented in README). Meanwhile the repo daemon's new
`should_manage()` (`snake-grid.py:175-181`) **rejects any window that maps
floating** (the dialog heuristic). These two are mutually exclusive:

- With the rules active, the *new* daemon manages **nothing** — every grid
  window maps floating, is rejected, and just stacks at the TL slot forever.
  The moment Step 1 deploys the new daemon, the grid goes dead. This is the
  regression Ryan would hit next.
- Independently, the rules are already broken today: they force *dialogs*
  (file pickers, pavucontrol) on ws 1/2 to 945×500 at TL, destroying their
  natural size.

**Important history — do NOT just delete the rules.** The open-shrink flash
they prevent is real and was *measured* on this box on 2026-06-27: a grid
window maps tiled at 1892×1002 at +0.7 ms, the daemon corrects it at
+5.7 ms, and Hyprland's open animation renders that shrink visibly over
~200 ms. Pre-placement is the right idea; the current *implementation*
(hardcoded coords, floats everything, fights `should_manage`) is what's
broken. Rebuild it properly:

1. **Make the daemon own the rules — no hardcoded coordinates.** On startup
   and whenever the monitor cache refreshes with *changed* geometry, compute
   the current TL slot per grid workspace and push session-scoped rules
   through the command socket (`keyword windowrule …` works over the same
   `.socket.sock`; verify the exact keyword syntax against
   `hyprctl keyword` docs for 0.55 before relying on it). Remove them on
   clean exit. This fixes Step 2's stale-coordinate variant of the bug
   permanently (rules always match what the daemon will compute), removes
   the machine-specific block from Ryan's config, and makes the feature
   shippable in the public repo instead of a README footnote. If dynamic
   `keyword windowrule` turns out not to be supported on 0.55, fall back to
   the daemon writing a small `source`d conf file + `reload`, or keep static
   rules in his config but have the daemon *log a warning* when its computed
   slot disagrees with where pre-placed windows land.
2. **Tag what the rules touch, and teach `should_manage()` the tag.** Add
   `tag +snakegrid` to the pushed rules. Then in `should_manage()`: a
   floating window carrying the `snakegrid` tag was floated by *our* rule —
   manageable; a floating window *without* the tag is a genuine dialog/popup
   — leave it alone. `c["tags"]` is exposed in clients JSON on 0.55. This
   resolves the mutual exclusion cleanly.
3. **Keep dialogs out of the rules' blast radius.** The tag alone doesn't
   stop the `size`/`move` rules from mangling dialogs at map time. Test live
   whether `match:float false` (evaluated against the window's natural
   floating state) excludes natively-floating xdg dialogs on this version:
   open pavucontrol and a browser file picker on ws 1 — they must keep their
   natural size. If that match isn't usable, note that the tag no longer
   discriminates either (the rules would tag dialogs too), so fall back to a
   daemon-side heuristic: on openwindow, treat a floating grid-workspace
   window as a dialog (unmanage + dispatch it back to its natural size via
   `resizewindowpixel`) when its `initialClass` matches a small configurable
   dialog list (`SNAKE_DIALOGS`, e.g. pavucontrol, xdg-desktop-portal-*), or
   simply document that dialogs on grid workspaces get pre-placed — pick
   whichever Ryan prefers after seeing both. Do not ship untested: the
   acceptance test is "dialogs keep their size AND normal windows are
   managed AND no visible open-flash."

Update `~/.config/hypr/conf/windowrules.conf` (delete the six static lines
plus their comment block once the daemon pushes rules itself) and rewrite
the README's "instant opens" section to describe the new built-in behavior.

**Verify:** on ws 1 — open 3 terminals (all tile into the snake), open
pavucontrol (keeps natural size, unmanaged), open a Zen download dialog
(unmanaged). On ws 3 — everything tiles normally, untouched.

## Step 4 — Robustness bugs (the remaining "errors")

Fix all of these in `snake-grid.py`:

1. **`_ipc()` has no socket timeout** (`snake-grid.py:100-115`). If the
   compositor ever stalls mid-response, the daemon hangs forever holding
   `LOCK`, and every subsequent event queues behind it — perceived as the
   grid "freezing". `s.settimeout(2.0)` and treat timeout like the existing
   `OSError` path (log, return `b""`).
2. **A bad byte in a window title kills the daemon.** `event_loop()`
   (`snake-grid.py:332-354`) reads events via `s.makefile("r")` with strict
   UTF-8 decoding, and `openwindow`/`windowtitle` payloads contain
   user-controlled titles. A single invalid byte raises
   `UnicodeDecodeError`, which is a `ValueError` — **not** caught by the
   `except OSError` — so it propagates out of `event_loop()` and the daemon
   dies silently. That matches "it randomly just stops working."
   Fix: `s.makefile("r", errors="replace")`, and broaden the loop's guard so
   no per-event exception can escape (there's a per-event try in
   `handle_event`, but the file-iteration itself must survive too).
3. **`openwindow` trusts the event's workspace field** (`snake-grid.py:283`),
   which is the workspace **name**, not id. Renamed workspaces (`"web"`)
   make `int()` fail → window silently never managed even though its
   workspace id is 1. The handler queries `Hj("clients")` anyway — use the
   authoritative `c["workspace"]["id"]` from that data for the
   `in GRID_WS` gate; use the event field only as a cheap pre-filter for
   obvious non-grid opens (digit and not in GRID_WS → skip early, else
   query and decide from clients JSON).
4. **Config parse crash:** `GRID_WS` (`snake-grid.py:31`) raises an
   unhandled `ValueError` at import on malformed `SNAKE_WS` (e.g. `"1, 2"`
   with a space). Parse defensively like `parse_grid()` does — fall back to
   `[1, 2]` and log.
5. **`relayout()` does a full clients query even with nothing to manage.**
   Every `fullscreen` event anywhere (any workspace) currently costs a full
   query + relayout. Early-out at the top: if `order` is empty and no
   clients dict was passed in, there is nothing to prune or place — return.

## Step 5 — Performance ("god-tier", in impact order)

The daemon is already close to optimal in architecture (direct socket IPC,
batched dispatches, drift-skip, geometry cache). Do **not** rewrite it. The
real perceived lag on Ryan's box came from Steps 2–3 (every window open =
misplace + correct + browser-resize fight + 3 resettle passes re-moving it).
After those land, apply these:

1. **Replace per-open `threading.Timer` fan-out with one settle scheduler.**
   `resettle()` (`snake-grid.py:258-260`) spawns 3 timer threads per window
   open; a 5-window session restore spawns 15 threads contending on `LOCK`.
   Keep one daemon thread with a `threading.Event`+deadline list (or
   `sched`): `resettle()` just pushes deadlines `now+0.15/0.45/1.0`,
   coalescing duplicates within a few ms. Same behavior, ~zero threads,
   no thundering herd. (Drift-skip already makes each pass cheap — this is
   about thread churn and lock contention, not IPC volume.)
2. **Debounce `GEOM_EVENTS`** (`snake-grid.py:324-327`). Monitor
   hotplug/`configreloaded` arrive in bursts; each currently triggers an
   immediate full relayout. Route them through the same scheduler with a
   ~150 ms debounce so a burst costs one relayout.
3. **Keep the TTL'd monitor cache from Step 2** — that *is* the right
   balance; don't return to query-every-time or cache-forever.
4. **Optional, only if measurements justify it:** a local client-state
   mirror maintained from events (open/close/move/floating), so relayout
   needs zero queries in steady state and reconciles against a real query
   only on resettle passes. This is the theoretical endgame, but a clients
   query is ~1 ms on this box — implement only if SNAKE_DEBUG numbers show
   the query dominating, and keep it out if it complicates the code beyond
   what Ryan can follow. Simplicity is a feature of this repo.
5. **Add timing to the log** so future "it's slow" reports are measurable:
   under `SNAKE_DEBUG=1`, log per-phase ms for openwindow handling
   (event→query→batch→done). Acceptance: event→batch-complete **< 10 ms**,
   settle passes with nothing to fix log **0 dispatches**.

## Step 6 — End-to-end acceptance (run every scenario)

Run with the *repo* daemon under `SNAKE_DEBUG=1` (`tail -f /tmp/snakegrid.log`):

1. Open 5 terminals on ws 1 → first 4 snake TL→TR→BR→BL at (10,60)/(965,60)/
   (965,570)/(10,570) all 945×500; 5th overflows: oldest hops to ws 2 TL.
2. Close one → remaining windows slide back along the snake.
3. `pkill waybar; waybar &`, wait 2 s, open a window → lands at (10,60), not
   (10,10) [Step 2 regression test].
4. Open pavucontrol + a browser save-dialog on ws 1 → both keep natural
   size, never enter `order` [Step 3].
5. Drag a managed window to ws 3 → released (stays where you put it); drag
   it back to ws 1 → adopted into the next free slot.
6. Fullscreen a managed window, then un-fullscreen → snaps back to its slot;
   fullscreen toggles on ws 4 cause **zero** clients queries in the log
   [Step 4.5].
7. Toggle `snakegrid` off → managed windows re-tile; ws 3+ untouched.
8. `kill -TERM` the daemon and restart → re-adopts existing grid windows in
   focus order without visible jiggle (drift-skip: log shows 0 dispatches if
   nothing moved).
9. Login race: `snakegrid` off, then
   `pkill waybar && snakegrid && sleep 1 && waybar &` — after waybar is up,
   the next opened window must still land below the bar [TTL test].

## Step 7 — Ship

1. `HYPRLAND_INSTANCE_SIGNATURE= python3 -m pytest tests/ -q` (20 pass today;
   add tests for anything you made testable — `parse_grid`-style pure
   functions, the SNAKE_WS fallback, settle-coalescing logic).
2. `python3 -m ruff check .` and `shellcheck snakegrid install.sh`
   (both already used in this repo; keep them clean).
3. Update README.md: remove/replace the "instant opens" windowrule tip per
   Step 3's outcome; document `SNAKE_DEBUG` timing fields if you changed
   them.
4. `./install.sh` (now restarts the daemon per Step 1), confirm one process,
   re-run scenario 1.
5. Commit in logical units (deploy fix / staleness fix / rule conflict /
   robustness / perf), imperative messages, no secrets, nothing
   machine-specific in the repo. Do **not** commit this guide file.
