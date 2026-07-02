"""Unit tests for snakegrid's pure geometry/config helpers.

The daemon file has a hyphen in its name (``snake-grid.py``), so we load it by
path. Importing it must have no side effects that need a live Hyprland session —
these tests double as a guard that the module stays importable in CI.
"""
import importlib.util
import pathlib

import pytest

DAEMON = pathlib.Path(__file__).resolve().parent.parent / "snake-grid.py"


def load():
    spec = importlib.util.spec_from_file_location("snakegrid_daemon", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sg = load()


# ── parse_grid ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("spec,expected", [
    ("2x2", (2, 2)),
    ("3x3", (3, 3)),
    ("3x2", (3, 2)),
    ("1x4", (1, 4)),
    ("", (2, 2)),          # empty → default
    ("junk", (2, 2)),      # malformed → default
    ("0x2", (2, 2)),       # non-positive → default
    (None, (2, 2)),        # wrong type → default
])
def test_parse_grid(spec, expected):
    assert sg.parse_grid(spec) == expected


# ── parse_ws ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("spec,expected", [
    ("1,2", [1, 2]),
    ("4,5", [4, 5]),
    ("1, 2", [1, 2]),        # stray space must not crash (it used to)
    (" 3 , 4 ", [3, 4]),
    ("7", [7]),
    ("", [1, 2]),            # empty → default
    ("a,b", [1, 2]),         # junk → default
    ("1,,2", [1, 2]),        # blank token skipped
])
def test_parse_ws(spec, expected):
    assert sg.parse_ws(spec) == expected


# ── _has_tag (Hyprland reports applied tags with a trailing '*') ────────────────
def test_has_tag_strips_trailing_star():
    assert sg._has_tag({"tags": ["snakegrid*"]}, "snakegrid") is True
    assert sg._has_tag({"tags": ["snakegrid"]}, "snakegrid") is True
    assert sg._has_tag({"tags": ["other*"]}, "snakegrid") is False
    assert sg._has_tag({"tags": []}, "snakegrid") is False
    assert sg._has_tag({}, "snakegrid") is False   # no 'tags' key at all


# ── make_snake (boustrophedon) ─────────────────────────────────────────────────
def test_make_snake_2x2_matches_tl_tr_br_bl():
    assert sg.make_snake(2, 2) == [(0, 0), (0, 1), (1, 1), (1, 0)]


def test_make_snake_alternates_direction_per_row():
    assert sg.make_snake(3, 2) == [(0, 0), (0, 1), (1, 1), (1, 0), (2, 0), (2, 1)]


def test_make_snake_length_is_rows_times_cols():
    assert len(sg.make_snake(4, 5)) == 20
    # every cell is visited exactly once
    assert len(set(sg.make_snake(4, 5))) == 20


# ── slots_for / slot_xy ─────────────────────────────────────────────────────────
def base_mon(**over):
    mon = {"width": 1920, "height": 1080, "x": 0, "y": 0, "scale": 1,
           "reserved": [0, 0, 0, 0]}
    mon.update(over)
    return mon


def test_slots_for_simple_1080p(monkeypatch):
    monkeypatch.setattr(sg, "GAP", 10)
    monkeypatch.setattr(sg, "ROWS", 2)
    monkeypatch.setattr(sg, "COLS", 2)
    x0, y0, cw, ch = sg.slots_for(base_mon())
    assert (x0, y0) == (10, 10)
    assert cw == (1920 - 3 * 10) // 2 == 945
    assert ch == (1080 - 3 * 10) // 2 == 525


def test_slots_for_respects_reserved_bar(monkeypatch):
    monkeypatch.setattr(sg, "GAP", 10)
    monkeypatch.setattr(sg, "ROWS", 2)
    monkeypatch.setattr(sg, "COLS", 2)
    # 60px top bar reserved
    x0, y0, cw, ch = sg.slots_for(base_mon(reserved=[0, 60, 0, 0]))
    assert (x0, y0) == (10, 70)
    assert ch == (1080 - 60 - 3 * 10) // 2 == 495


def test_slots_for_hidpi_uses_logical_pixels(monkeypatch):
    monkeypatch.setattr(sg, "GAP", 10)
    monkeypatch.setattr(sg, "ROWS", 2)
    monkeypatch.setattr(sg, "COLS", 2)
    # a 3840×2160 panel at scale 2 is logically 1920×1080
    hidpi = sg.slots_for(base_mon(width=3840, height=2160, scale=2))
    assert hidpi == sg.slots_for(base_mon())


def test_slots_for_applies_monitor_offset(monkeypatch):
    monkeypatch.setattr(sg, "GAP", 10)
    monkeypatch.setattr(sg, "ROWS", 2)
    monkeypatch.setattr(sg, "COLS", 2)
    x0, y0, _, _ = sg.slots_for(base_mon(x=1920, y=0))
    assert (x0, y0) == (1930, 10)


def test_slot_xy_places_cells_in_a_grid():
    x0, y0, cw, ch = 10, 10, 945, 525
    assert sg.slot_xy(x0, y0, cw, ch, (0, 0)) == (10, 10)                 # TL
    assert sg.slot_xy(x0, y0, cw, ch, (0, 1)) == (10 + 945 + 10, 10)      # TR
    assert sg.slot_xy(x0, y0, cw, ch, (1, 0)) == (10, 10 + 525 + 10)      # BL
    assert sg.slot_xy(x0, y0, cw, ch, (1, 1)) == (965, 545)              # BR


# ── should_manage ───────────────────────────────────────────────────────────────
def test_should_manage_normal_tiled_window(monkeypatch):
    monkeypatch.setattr(sg, "IGNORE", set())
    assert sg.should_manage({"floating": False}, "kitty") is True


def test_should_manage_skips_floating_dialogs(monkeypatch):
    monkeypatch.setattr(sg, "IGNORE", set())
    assert sg.should_manage({"floating": True}, "kitty") is False


def test_should_manage_skips_ignored_class(monkeypatch):
    monkeypatch.setattr(sg, "IGNORE", {"pavucontrol"})
    assert sg.should_manage({"floating": False}, "pavucontrol") is False


def test_should_manage_adopts_tagged_floating_window(monkeypatch):
    # a floating window carrying our tag was pre-floated by the 'instant opens'
    # rule — it must be managed despite being floating (only dialogs are skipped)
    monkeypatch.setattr(sg, "IGNORE", set())
    assert sg.should_manage({"floating": True, "tags": ["snakegrid*"]}, "kitty") is True


def test_should_manage_ignored_class_beats_tag(monkeypatch):
    # an explicitly-ignored class stays ignored even if something tagged it
    monkeypatch.setattr(sg, "IGNORE", {"pavucontrol"})
    assert sg.should_manage({"floating": True, "tags": ["snakegrid*"]}, "pavucontrol") is False


# ── event payload parsing ────────────────────────────────────────────────────────
def test_parse_addr_ws():
    assert sg._parse_addr_ws("55f0abcd,2,two") == ("0x55f0abcd", 2)
    assert sg._parse_addr_ws("55f0abcd,-99,special") == ("0x55f0abcd", -99)
    # named (non-numeric) workspace → ws is None, not a crash
    assert sg._parse_addr_ws("55f0abcd,name") == ("0x55f0abcd", None)
