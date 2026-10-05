#!/usr/bin/env python3
"""delve -- a small dungeon crawler built to be played by an AI agent.

Design goal: the *player* is a language model driving a shell, not a human at a
keyboard.  So the interface is:

  * one command per line, one status block out (pipe friendly, no curses, no ANSI)
  * a fixed block order every turn (HEADER / VITALS / GEAR / MAP / TARGETS /
    GROUND / STAIRS / EVENT / RESULT) so a model can read it positionally
  * the last line is always `RESULT <word>`, so a harness can regex the outcome
  * `--format json` gives the exact same information as machine data
  * the state lives in a save file, so a stateless `--cmd` call per turn works
  * everything is derived from `--seed`, and the RNG state is carried in the
    save, so a run is byte-for-byte reproducible

The mechanics are deliberately NOT in `card` (see RULES.md).  `card` teaches the
player how to *talk* to the game; working out how the world behaves is the game.
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import random
import sys
import tempfile

GAME_VERSION = 2

SAVE_FORMAT = "delve-save"
SAVE_VERSION = 2
SAVE_XOR_KEY = b"delve/v1/obfuscation-not-encryption"

MAP_W = 40
MAP_H = 14
FOV_RADIUS = 8
DEFAULT_DEPTH = 8
MAX_DEPTH_ALLOWED = 20
TURN_LIMIT = 6000
REGEN_EVERY = 14
DEFAULT_SAVE = "delve-save.json"
MONSTER_SIGHT = 7

TILE_WALL = "#"
TILE_FLOOR = "."
TILE_STAIRS = ">"
TILE_LOCKED = "+"

GLYPH_REMEMBERED_FLOOR = ":"
GLYPH_TRAP = "^"
GLYPH_PLAYER = "@"
GLYPH_RELIC = "*"

# One place that decides what a thing looks like.  `selftest` asserts every item
# kind has an entry, so adding a kind without a glyph fails loudly instead of
# rendering a mystery character.
ITEM_GLYPHS = {
    "gold": "$",
    "potion": "!",
    "scroll": "?",
    "weapon": ")",
    "armor": "[",
    "ring": "=",
    "key": "k",
}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
STRICT_EXITS = {"win": 10, "dead": 11, "stalled": 12, "quit": 13}

MONSTERS = {
    "rat": {"glyph": "r", "hp": 4, "atk": 2, "def": 0, "xp": 3, "floors": (1, 99)},
    "bat": {"glyph": "b", "hp": 3, "atk": 2, "def": 0, "xp": 4, "floors": (1, 3), "erratic": 0.5},
    "slime": {"glyph": "j", "hp": 9, "atk": 3, "def": 1, "xp": 7, "floors": (2, 5)},
    "skeleton": {"glyph": "s", "hp": 11, "atk": 4, "def": 2, "xp": 9, "floors": (2, 99)},
    "wraith": {"glyph": "w", "hp": 15, "atk": 6, "def": 3, "xp": 16, "floors": (3, 99), "weakens": 3},
    # The deep floors are not just bigger numbers.  Each of these changes what you
    # should do, not only how long you can stand there.
    "archer": {
        "glyph": "a",
        "hp": 10,
        "atk": 4,
        "def": 1,
        "xp": 15,
        "floors": (3, 99),
        "ranged": 4,
        "reload": 2,
    },
    "ghoul": {"glyph": "g", "hp": 13, "atk": 5, "def": 1, "xp": 14, "floors": (4, 99), "poisons": 4},
    "shaman": {"glyph": "h", "hp": 14, "atk": 4, "def": 2, "xp": 18, "floors": (5, 99), "heals": 5},
    "wisp": {"glyph": "W", "hp": 9, "atk": 5, "def": 2, "xp": 17, "floors": (5, 99), "speed": 2},
    "golem": {"glyph": "o", "hp": 30, "atk": 8, "def": 5, "xp": 30, "floors": (6, 99), "slow": 2},
    "guardian": {"glyph": "G", "hp": 44, "atk": 9, "def": 4, "xp": 60, "floors": ("boss",)},
}

WEAPONS = {
    "dagger": {"atk": 1, "floors": (1, 2)},
    "sword": {"atk": 2, "floors": (2, 4)},
    "axe": {"atk": 3, "floors": (3, 99)},
    "rune blade": {"atk": 4, "floors": (4, 99)},
}

ARMORS = {
    "leather": {"def": 1, "floors": (1, 3)},
    "chain": {"def": 2, "floors": (2, 99)},
    "plate": {"def": 3, "floors": (4, 99)},
}

SCROLL_KINDS = ("mapping", "teleport", "blessing", "ward")

# Rings trade against each other instead of stacking, so wearing one is a decision
# rather than an upgrade.  `insight` and `greed` in particular are not "worse" than
# `might`, they answer a different question.
RINGS = {
    "might": {"note": "+2 ATK", "atk": 2},
    "warding": {"note": "+2 DEF", "def": 2},
    "vigor": {"note": "+8 max HP", "max_hp": 8},
    "insight": {"note": "+2 sight radius", "sight": 2},
    "greed": {"note": "double gold", "gold": 2},
}

# Temporary effects on the player.  They exist to make *when* you act matter.
POISON_DAMAGE = 1
WEAK_PENALTY = 2
DARK_RADIUS = 2

PLAYER_BASE_HP = 26
PLAYER_BASE_ATK = 4
PLAYER_BASE_DEF = 1

DIRS = {
    "n": (0, -1),
    "ne": (1, -1),
    "e": (1, 0),
    "se": (1, 1),
    "s": (0, 1),
    "sw": (-1, 1),
    "w": (-1, 0),
    "nw": (-1, -1),
}
DIR_WORDS = {
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
}
FREE_COMMANDS = {"map", "status", "inv", "inventory", "help", "?", "legend", "card"}

LEGEND = {
    "@": "you",
    "#": "wall",
    ".": "floor in sight",
    ":": "floor remembered, not in sight",
    ">": "stairs down",
    "+": "stairs down, locked",
    "r": "rat",
    "b": "bat",
    "j": "slime",
    "s": "skeleton",
    "w": "wraith",
    "a": "archer (shoots at range 4, must reload)",
    "g": "ghoul (its hits poison)",
    "h": "shaman (heals other monsters)",
    "W": "wisp (acts twice per turn)",
    "o": "golem (acts every other turn)",
    "G": "guardian",
    "!": "potion",
    "?": "scroll",
    "$": "gold",
    ")": "weapon",
    "[": "armor",
    "=": "ring",
    "k": "key (opens the locked stairs)",
    "*": "relic",
    "^": "trap you already know about",
    " ": "never seen",
}

CARD_TEXT = """\
PLAYER CARD -- DELVE, a dungeon crawler you play from the command line.

GOAL
  Go down. The bottom floor holds the guardian and the relic.
  Kill the guardian, take the relic (*), and the run is won.
  Your HP reaching 0 ends the run.

HOW TO TALK TO THE GAME
  One command per line, one status block back.
  Every command advances one turn EXCEPT the free ones listed below.
  An unknown or impossible command still costs a turn. Commands are case-insensitive.

COMMANDS
  n  s  e  w  ne  nw  se  sw      move one step; walking into a monster attacks it
  move <dir>                     same thing, longer spelling (move n / move north)
  attack <dir>                   swing even at empty air
  wait  (or rest)                let one turn pass
  descend  (or >)                take the stairs you are standing on
  use <item>                     quaff/read something you carry: use potion, use scroll, use 1
  sense                          concentrate and check your immediate surroundings
  map  status  inv  legend       re-print information (FREE, no turn)
  help                           this card (FREE)
  quit                           abandon the run

READING THE STATUS BLOCK
  -- DELVE  floor 2/6   turn 118   seed 42 --   where you are and how long you played
  HP 19/26  ATK 5(4+1)  DEF 2(1+1)  LV 2  xp 14/20  GOLD 41
                                                numbers; (base+gear), LV is your level
  GEAR ...  INV ...                             equipped gear and what you carry
  FLOOR   nothing unusual                       traits of this floor; announced on arrival
  EFFECTS poison 3, weak 2 (-2 ATK)             only printed while something is on you
  MAP                                           the board, 2-space indented
  TARGETS  rat(e hp 3/4)                        monsters you can currently see, direction + HP
  GROUND   gold(7), potion                      items lying at your feet
  LOOT     potion(ne)  armor(nw, judged)        other visible items, by direction
  STAIRS   here | seen | unknown (+ (locked))   where the way down is
  EVENT                                         what just happened
  CAUSE    slain by a wraith on floor 3         printed once the run is over
  RESULT ok | win | dead | stalled | quit       always the FINAL line; `stalled` = turn limit hit

WHAT THE CARD DOES NOT TELL YOU
  How the world actually behaves: what each monster does, how much a potion really
  heals, what the unidentified scroll does this run, what is hiding where you cannot
  see it, and what the map means when you look again later. The card tells you how to
  act. Finding out how the dungeon works is the game. Take notes.
"""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _line(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Bresenham line, endpoints included."""
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    points = []
    x, y = x0, y0
    while True:
        points.append((x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy
    return points


def _chebyshev(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _with_article(word: str) -> str:
    """'wraith' -> 'a wraith', 'archer' -> 'an archer'.  Death causes are read by humans."""
    return f"{'an' if word[:1].lower() in 'aeiou' else 'a'} {word}"


def dir_name(dx: int, dy: int) -> str:
    ns = "" if dy == 0 else ("s" if dy > 0 else "n")
    ew = "" if dx == 0 else ("e" if dx > 0 else "w")
    return (ns + ew) or "here"


def _rng_to_json(rng: random.Random) -> list:
    version, internal, gauss = rng.getstate()
    return [version, list(internal), gauss]


def _rng_from_json(blob: list) -> tuple:
    return (blob[0], tuple(blob[1]), blob[2])


def _rooms_overlap(a: tuple, b: tuple, pad: int = 1) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax - pad < bx + bw and bx - pad < ax + aw and ay - pad < by + bh and by - pad < ay + ah


def _center(room: tuple) -> tuple[int, int]:
    x, y, w, h = room
    return (x + w // 2, y + h // 2)


def _carve(rng: random.Random, grid: list[list[str]], a: tuple[int, int], b: tuple[int, int]) -> None:
    ax, ay = a
    bx, by = b
    if rng.random() < 0.5:
        for x in range(min(ax, bx), max(ax, bx) + 1):
            grid[ay][x] = TILE_FLOOR
        for y in range(min(ay, by), max(ay, by) + 1):
            grid[y][bx] = TILE_FLOOR
    else:
        for y in range(min(ay, by), max(ay, by) + 1):
            grid[y][ax] = TILE_FLOOR
        for x in range(min(ax, bx), max(ax, bx) + 1):
            grid[by][x] = TILE_FLOOR


# --------------------------------------------------------------------------
# floor generation
# --------------------------------------------------------------------------


def _pick_monsters(rng: random.Random, depth: int, count: int, mechanical_only: bool = False) -> list[str]:
    pool = [
        name
        for name, spec in MONSTERS.items()
        if spec["floors"] != ("boss",) and spec["floors"][0] <= depth <= spec["floors"][1]
    ]
    if mechanical_only:
        pool = [name for name in pool if has_mechanic(MONSTERS[name])] or pool
    if not pool:
        pool = ["rat"]
    return [rng.choice(pool) for _ in range(count)]


def has_mechanic(spec: dict) -> bool:
    """True for anything that is more than a pile of stats."""
    return bool(
        spec.get("erratic")
        or spec.get("ranged")
        or spec.get("poisons")
        or spec.get("heals")
        or spec.get("weakens")
        or spec.get("speed", 1) > 1
        or spec.get("slow", 1) > 1
        or spec.get("reload", 1) > 1
    )


def _pick_gear(rng: random.Random, table: dict, depth: int) -> str | None:
    pool = [name for name, spec in table.items() if spec["floors"][0] <= depth <= spec["floors"][1]]
    if not pool:
        return None
    return rng.choice(pool)


def build_floor(rng: random.Random, depth: int, max_depth: int) -> dict:
    """Generate one floor.  Deterministic given `rng` state + depth."""
    is_boss = depth == max_depth
    grid = [[TILE_WALL] * MAP_W for _ in range(MAP_H)]

    rooms: list[tuple[int, int, int, int]] = []
    want_rooms = 5 if is_boss else 7
    for _ in range(600):
        if len(rooms) >= want_rooms:
            break
        w = rng.randint(4, 9)
        h = rng.randint(3, 4)
        x = rng.randint(1, MAP_W - w - 1)
        y = rng.randint(1, MAP_H - h - 1)
        cand = (x, y, w, h)
        if any(_rooms_overlap(cand, room) for room in rooms):
            continue
        rooms.append(cand)

    for x, y, w, h in rooms:
        for iy in range(y, y + h):
            for ix in range(x, x + w):
                grid[iy][ix] = TILE_FLOOR

    for i in range(1, len(rooms)):
        _carve(rng, grid, _center(rooms[i - 1]), _center(rooms[i]))

    start = _center(rooms[0])
    far_room = rooms[-1]
    if len(rooms) > 1:
        far_room = max(
            rooms[1:],
            key=lambda r: abs(_center(r)[0] - start[0]) + abs(_center(r)[1] - start[1]),
        )
    special = _center(far_room)

    stairs = None
    if not is_boss:
        grid[special[1]][special[0]] = TILE_STAIRS
        stairs = [special[0], special[1]]

    floor = {
        "grid": ["".join(row) for row in grid],
        "start": [start[0], start[1]],
        "stairs": stairs,
        "monsters": [],
        "items": [],
        "traps": [],
        "relic": None,
    }

    used = {start, special}

    def free_tiles(min_from_start: int) -> list[tuple[int, int]]:
        out = []
        for y in range(MAP_H):
            for x in range(MAP_W):
                if grid[y][x] == TILE_WALL:
                    continue
                if (x, y) in used:
                    continue
                if abs(x - start[0]) + abs(y - start[1]) <= min_from_start:
                    continue
                out.append((x, y))
        return out

    def take(min_from_start: int = 3) -> tuple[int, int] | None:
        pool = free_tiles(min_from_start)
        if not pool:
            return None
        spot = pool[rng.randrange(len(pool))]
        used.add(spot)
        return spot

    # floor features -------------------------------------------------------
    # Both are announced in the FLOOR block the moment you arrive.  Nothing in
    # this game is allowed to be a rule the player could not have known.
    dark = (not is_boss) and depth >= 3 and rng.random() < 0.3
    locked = (not is_boss) and depth >= 2 and rng.random() < 0.35
    floor["dark"] = dark
    floor["locked"] = locked
    if locked:
        _set_grid_tile(floor, floor["stairs"][0], floor["stairs"][1], TILE_LOCKED)

    # monsters
    if is_boss:
        floor["monsters"].append(_make_monster("guardian", special, guardian_stats(max_depth)))
        names = _pick_monsters(rng, depth, 2)
    else:
        names = _pick_monsters(rng, depth, min(3 + depth, 9))
        # Past the early floors, guarantee that a floor poses a *tactical* problem and
        # not just an arithmetic one: at least one monster that changes how you fight.
        if depth >= 7:
            tricky = _pick_monsters(rng, depth, 3, mechanical_only=True)
            for name in tricky[: 1 + depth // 10]:
                if names:
                    names[rng.randrange(len(names))] = name
                else:
                    names.append(name)
    scale = None if is_boss else threat_scale(depth)
    for name in names:
        spot = take(4)
        if spot is None:
            break
        floor["monsters"].append(_make_monster(name, spot, scale=scale))

    # loot
    gold_piles = 2 if is_boss else min(3 + depth, 9)
    for _ in range(gold_piles):
        spot = take(3)
        if spot is None:
            break
        floor["items"].append(
            {"kind": "gold", "x": spot[0], "y": spot[1], "amount": rng.randint(3, 9) + depth}
        )

    if locked:
        spot = take(3)
        if spot is not None:
            floor["items"].append({"kind": "key", "x": spot[0], "y": spot[1]})
        else:
            floor["locked"] = False

    potions = 1 + (1 if depth >= 3 else 0) + (1 if depth >= 9 else 0)
    for _ in range(potions):
        spot = take(3)
        if spot is None:
            break
        floor["items"].append({"kind": "potion", "x": spot[0], "y": spot[1]})

    scrolls = 1 + (1 if depth >= 6 else 0)
    for _ in range(scrolls):
        spot = take(3)
        if spot is None:
            break
        floor["items"].append({"kind": "scroll", "x": spot[0], "y": spot[1]})

    if rng.random() < 0.75:
        name = _pick_gear(rng, WEAPONS, depth)
        spot = take(3)
        if name and spot is not None:
            floor["items"].append({"kind": "weapon", "name": name, "x": spot[0], "y": spot[1]})
    if rng.random() < 0.65:
        name = _pick_gear(rng, ARMORS, depth)
        spot = take(3)
        if name and spot is not None:
            floor["items"].append({"kind": "armor", "name": name, "x": spot[0], "y": spot[1]})
    if rng.random() < 0.35:
        ring = rng.choice(sorted(RINGS))
        spot = take(3)
        if spot is not None:
            floor["items"].append({"kind": "ring", "name": ring, "x": spot[0], "y": spot[1]})

    # traps
    trap_count = rng.randint(0, 1) if is_boss else rng.randint(1, min(1 + depth // 3, 4))
    for _ in range(trap_count):
        spot = take(4)
        if spot is None:
            break
        floor["traps"].append({"x": spot[0], "y": spot[1], "damage": 3 + depth, "known": False})

    return floor


def _set_grid_tile(floor: dict, x: int, y: int, glyph: str) -> None:
    """The grid is stored as a list of strings, so a one-character edit rebuilds the row."""
    row = floor["grid"][y]
    floor["grid"][y] = row[:x] + glyph + row[x + 1 :]


def threat_scale(depth: int) -> dict:
    """A gently growing threat for floors past the early game.

    Mechanics are what make a floor interesting; this exists only so that floor 15 is
    not a victory lap.  The overall win rate is meant to stay roughly flat across
    --depth (that is what `guardian_stats` is for) -- but without this, all the danger
    piles up on floors 2-5 while the player out-levels everything after that, and the
    back half of a long run stops being a game.

    Deliberately mild: HP x1.00 at floor 5 rising to x1.75 at floor 20, +3 ATK and
    +2 DEF at floor 20.
    """
    past = max(0, depth - 5)
    return {"hp": 1 + 0.07 * past, "atk": past // 3, "def": past // 5}


def _make_monster(
    name: str, spot: tuple[int, int], stats: dict | None = None, scale: dict | None = None
) -> dict:
    spec = dict(MONSTERS[name])
    if stats:
        # An explicitly-statted monster (the guardian) is already scaled to the run.
        spec.update(stats)
    elif scale:
        spec["hp"] = max(1, round(spec["hp"] * scale["hp"]))
        spec["atk"] += scale["atk"]
        spec["def"] += scale["def"]
    return {
        "kind": name,
        "x": spot[0],
        "y": spot[1],
        "hp": spec["hp"],
        "max_hp": spec["hp"],
        "atk": spec["atk"],
        "def": spec["def"],
        "xp": spec["xp"],
        "erratic": spec.get("erratic", 0.0),
        "ranged": spec.get("ranged", 0),
        "reload": spec.get("reload", 1),
        "poisons": spec.get("poisons", 0),
        "heals": spec.get("heals", 0),
        "weakens": spec.get("weakens", 0),
        "speed": spec.get("speed", 1),
        "slow": spec.get("slow", 1),
        "tick": 0,
        "alerted": False,
        "alive": True,
    }


def guardian_stats(max_depth: int) -> dict:
    """The guardian is scaled to the length of the run.

    A longer run means more XP before the boss, so a fixed boss would make
    `--depth 3` brutal and `--depth 8` trivial.  Scaling it keeps the difficulty
    roughly flat and lets `--depth` mean "how long", not "how hard".
    """
    return {"hp": 20 + 3 * max_depth, "atk": 6 + max_depth // 5, "def": 3 + max_depth // 6}


# --------------------------------------------------------------------------
# the game
# --------------------------------------------------------------------------


class Game:
    def __init__(self, state: dict, view: str = "full", legend: bool = False, seed: int | None = None):
        self.s = state
        self.view = view
        self.show_legend = legend
        self.events: list[str] = list(self.s.get("log") or [])
        self._hit_by = "something in the dark"
        self.rng = random.Random()
        if state.get("rng"):
            self.rng.setstate(_rng_from_json(state["rng"]))
        else:
            self.rng.seed(state["seed"] if seed is None else seed)
        self._bind_floor(
            {(int(x), int(y)) for x, y in state["floor"].get("seen", [])} if state.get("floor") else set()
        )

    # -- state plumbing ----------------------------------------------------

    def _bind_floor(self, seen: set | None = None) -> None:
        floor = self.s["floor"]
        if not floor:
            floor.update({"grid": [], "monsters": [], "items": [], "traps": [], "stairs": None})
        self.grid: list[str] = floor["grid"]
        self.monsters: list[dict] = floor["monsters"]
        self.items: list[dict] = floor["items"]
        self.traps: list[dict] = floor["traps"]
        self.seen: set[tuple[int, int]] = seen if seen is not None else set()

    def sync(self) -> None:
        self.s["rng"] = _rng_to_json(self.rng)
        self.s["floor"]["seen"] = sorted([list(t) for t in self.seen])

    @property
    def px(self) -> int:
        return self.s["player"]["x"]

    @property
    def py(self) -> int:
        return self.s["player"]["y"]

    def tile(self, x: int, y: int) -> str:
        if 0 <= x < MAP_W and 0 <= y < MAP_H:
            return self.grid[y][x]
        return TILE_WALL

    def ring_bonus(self, key: str) -> int:
        ring = self.s["player"].get("ring")
        return RINGS[ring].get(key, 0) if ring else 0

    def p_atk(self) -> int:
        player = self.s["player"]
        bonus = WEAPONS[player["weapon"]]["atk"] if player["weapon"] else 0
        weak = WEAK_PENALTY if player.get("effects", {}).get("weak") else 0
        return max(1, player["atk"] + bonus + self.ring_bonus("atk") - weak)

    def p_def(self) -> int:
        player = self.s["player"]
        bonus = ARMORS[player["armor"]]["def"] if player["armor"] else 0
        return player["def"] + bonus + self.ring_bonus("def")

    def sight(self) -> int:
        """How far the torch reaches.  Dark floors really change what you can know."""
        base = DARK_RADIUS if self.s["floor"].get("dark") else FOV_RADIUS
        return base + self.ring_bonus("sight")

    def live_monsters(self) -> list[dict]:
        return [m for m in self.monsters if m["alive"]]

    def monster_at(self, x: int, y: int) -> dict | None:
        for m in self.live_monsters():
            if m["x"] == x and m["y"] == y:
                return m
        return None

    def item_at(self, x: int, y: int) -> dict | None:
        for it in self.items:
            if it["x"] == x and it["y"] == y:
                return it
        return None

    def trap_at(self, x: int, y: int) -> dict | None:
        for t in self.traps:
            if t["x"] == x and t["y"] == y:
                return t
        return None

    def rel_name(self) -> str:
        kind = self.s["scroll"]["kind"]
        return "scroll" if not self.s["scroll"]["known"] else f"scroll ({kind})"

    def inv_label(self, key: str, count: int) -> str:
        return f"{self.rel_name()} x{count}" if key == "scroll" else f"{key} x{count}"

    # -- geometry ----------------------------------------------------------

    def _los(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        return all(self.tile(x, y) != TILE_WALL for x, y in _line(a[0], a[1], b[0], b[1])[1:-1])

    def visible_tiles(self) -> set[tuple[int, int]]:
        px, py = self.px, self.py
        vis = {(px, py)}
        r = self.sight()
        for y in range(max(0, py - r), min(MAP_H - 1, py + r) + 1):
            for x in range(max(0, px - r), min(MAP_W - 1, px + r) + 1):
                if (x - px) ** 2 + (y - py) ** 2 > r * r:
                    continue
                if (x, y) == (px, py):
                    continue
                for tx, ty in _line(px, py, x, y):
                    if (tx, ty) == (px, py):
                        continue
                    vis.add((tx, ty))
                    if self.tile(tx, ty) == TILE_WALL:
                        break
        self.seen |= vis
        return vis

    # -- lifecycle ---------------------------------------------------------

    def _enter_floor(self, depth: int) -> None:
        floor = build_floor(self.rng, depth, self.s["max_depth"])
        self.s["floor"] = floor
        self.s["depth"] = depth
        self._bind_floor(set())
        player = self.s["player"]
        player["x"], player["y"] = floor["start"][0], floor["start"][1]

    def _tick_effects(self) -> None:
        effects = self.s["player"].setdefault("effects", {"poison": 0, "weak": 0})
        if effects.get("poison", 0) > 0:
            effects["poison"] -= 1
            self._hurt(POISON_DAMAGE, "poison")
            self.events.append(f"Poison burns for {POISON_DAMAGE} ({effects['poison']} turns left).")
        if effects.get("weak", 0) > 0:
            effects["weak"] -= 1

    def _hurt(self, amount: int, source: str) -> None:
        """All damage to the player funnels through here so the run can name its killer."""
        self.s["player"]["hp"] -= amount
        self._hit_by = source

    def _regen(self) -> None:
        turn = self.s["turn"]
        if turn <= 0 or turn % REGEN_EVERY != 0:
            return
        player = self.s["player"]
        # Poison stops you knitting back together, which is what makes it a threat
        # rather than chip damage.
        if player.get("effects", {}).get("poison"):
            return
        if player["hp"] < player["max_hp"]:
            player["hp"] += 1
            self.events.append("You feel slightly better.")

    def _gain_xp(self, amount: int) -> None:
        player = self.s["player"]
        player["xp"] += amount
        while player["xp"] >= player["xp_next"]:
            player["xp"] -= player["xp_next"]
            player["level"] += 1
            player["max_hp"] += 4
            player["hp"] += 4
            player["atk"] += 1
            if player["level"] % 2 == 0:
                player["def"] += 1
            player["xp_next"] = 10 + 6 * (player["level"] - 1)
            self.events.append(
                f"You reach level {player['level']}. (max HP {player['max_hp']}, ATK {player['atk']})"
            )

    def _kill_monster(self, monster: dict) -> None:
        monster["alive"] = False
        self.events.append(f"The {monster['kind']} dies.")
        if monster["kind"] == "guardian":
            self.s["floor"]["relic"] = [monster["x"], monster["y"]]
            self.events.append("The guardian collapses. Something clatters onto the floor.")
        self._gain_xp(monster["xp"])

    # -- commands ----------------------------------------------------------

    def step(self, raw: str) -> tuple[str, object]:
        """Run one command.  Returns (kind, payload) with kind in {doc, turn}."""
        text = raw.strip()
        if not text:
            return ("doc", "")
        if self.s["status"] != "ok":
            return ("doc", f"the run is already over: {self.s['status']}")

        tokens = text.lower().split()
        verb = tokens[0]
        args = tokens[1:]

        if verb in ("help", "?") or (verb == "card" and not args):
            return ("doc", CARD_TEXT.rstrip())
        if verb == "legend":
            return ("doc", self.legend_block())
        if verb in ("map", "status"):
            return ("doc", self.render_text(full_map=True))
        if verb in ("inv", "inventory"):
            return ("doc", self.inventory_block())
        if verb == "card":
            return ("doc", CARD_TEXT.rstrip())

        self.events = []
        self.s["turn"] += 1
        self._apply(verb, args)
        if self.s["status"] == "ok":
            self._monster_turn()
        if self.s["status"] == "ok":
            self._tick_effects()
        self._check_end()
        # A dead run does not keep ticking: no regeneration, no further attacks on a corpse.
        if self.s["status"] == "ok":
            self._regen()
            self._check_end()
        self.s["log"] = list(self.events)
        return ("turn", list(self.events))

    def _apply(self, verb: str, args: list[str]) -> None:
        if verb in ("move", "go", "walk", "step"):
            if not args:
                self.events.append("move where? give a direction (n s e w ne nw se sw)")
                return
            word = args[0]
            # "down" means exactly one thing everywhere in this game: go down the stairs.
            if word in ("down", "descend", "stairs", ">"):
                self._descend()
                return
            if word in ("up", "ascend", "<"):
                self.events.append(
                    "?? there is no way up: the entrance collapsed behind you. use `down` to take the stairs."
                )
                return
            self._try_move(word)
            return
        if verb in ("attack", "hit", "strike"):
            direction = args[0] if args else None
            if direction is None:
                self.events.append("attack where? give a direction")
                return
            self._try_move(direction, force_attack=True)
            return
        if verb in DIRS:
            self._try_move(verb)
            return
        if verb in ("wait", "rest", ".", "5", "z"):
            self.events.append("You wait.")
            return
        if verb in ("descend", "down", ">", "stairs"):
            self._descend()
            return
        if verb in ("use", "quaff", "read", "drink"):
            self._use(args[0] if args else None)
            return
        if verb in ("sense", "search", "detect", "listen"):
            self._sense()
            return
        if verb == "quit":
            self.s["status"] = "quit"
            self.s["cause"] = f"abandoned the run on floor {self.s['depth']}"
            self.events.append("You abandon the run.")
            return
        self.events.append(f"?? unknown command: {verb}  (the turn is spent anyway)")
        return

    def _try_move(self, direction: str, force_attack: bool = False) -> None:
        direction = DIR_WORDS.get(direction, direction)
        if direction not in DIRS:
            self.events.append(f"?? not a direction: {direction}  (the turn is spent anyway)")
            return
        dx, dy = DIRS[direction]
        nx, ny = self.px + dx, self.py + dy

        target = self.monster_at(nx, ny)
        if target is not None:
            self._player_attack(target)
            return
        if force_attack:
            self.events.append(f"You swing {direction} at empty air.")
            return
        if self.tile(nx, ny) == TILE_WALL:
            self.events.append(f"Blocked: wall to the {direction}.")
            return
        if not (0 <= nx < MAP_W and 0 <= ny < MAP_H):
            self.events.append(f"Blocked: edge of the floor to the {direction}.")
            return

        self.s["player"]["x"], self.s["player"]["y"] = nx, ny
        self._on_enter_tile(nx, ny)

    def _player_attack(self, monster: dict) -> None:
        damage = max(1, self.p_atk() + self.rng.randint(0, 2) - monster["def"])
        monster["hp"] -= damage
        monster["alerted"] = True
        self.events.append(f"You hit the {monster['kind']} for {damage}. (it has {max(0, monster['hp'])} hp)")
        if monster["hp"] <= 0:
            self._kill_monster(monster)

    def _on_enter_tile(self, x: int, y: int) -> None:
        floor = self.s["floor"]
        player = self.s["player"]

        if floor.get("relic") and [x, y] == list(floor["relic"]):
            floor["relic"] = None
            player["gold"] += 25
            self.events.append("You lift the relic. It is warm, and the dungeon goes quiet.")
            self.s["status"] = "win"
            self.s["cause"] = f"took the relic on floor {self.s['depth']}"
            self.events.append(f"You escape with the relic after {self.s['turn']} turns.")
            return

        # Prefer something we have not already looked at.  An item we passed on stays
        # on the floor by design, and it must not shadow a second item on the same tile
        # forever -- that used to make whatever sat underneath it unreachable.
        here = [it for it in self.items if (it["x"], it["y"]) == (x, y)]
        item = next((it for it in here if not it.get("judged")), here[0] if here else None)
        if item is not None:
            if item["kind"] == "gold":
                player["gold"] += item["amount"]
                self.events.append(f"You pick up {item['amount']} gold.")
                self.items.remove(item)
            elif item["kind"] == "potion":
                player["inventory"]["potion"] = player["inventory"].get("potion", 0) + 1
                self.events.append("You pick up a potion.")
                self.items.remove(item)
            elif item["kind"] == "scroll":
                player["inventory"]["scroll"] = player["inventory"].get("scroll", 0) + 1
                self.events.append("You pick up an unlabelled scroll.")
                self.items.remove(item)
            elif item["kind"] == "weapon":
                bonus = WEAPONS[item["name"]]["atk"]
                current = WEAPONS[player["weapon"]]["atk"] if player["weapon"] else 0
                if bonus > current:
                    player["weapon"] = item["name"]
                    self.events.append(f"You are now wielding the {item['name']} (+{bonus} ATK).")
                    self.items.remove(item)
                elif item.get("judged"):
                    self.events.append(
                        f"You step over the {item['name']} again — still no better than "
                        f"your {player['weapon']}."
                    )
                else:
                    item["judged"] = True
                    self.events.append(
                        f"The {item['name']} (+{bonus} ATK) is no better than your current weapon. "
                        "You leave it where it lies."
                    )
            elif item["kind"] == "armor":
                bonus = ARMORS[item["name"]]["def"]
                current = ARMORS[player["armor"]]["def"] if player["armor"] else 0
                if bonus > current:
                    player["armor"] = item["name"]
                    self.events.append(f"You are now wearing the {item['name']} (+{bonus} DEF).")
                    self.items.remove(item)
                elif item.get("judged"):
                    self.events.append(
                        f"You step over the {item['name']} again — still no better than what you wear."
                    )
                else:
                    item["judged"] = True
                    self.events.append(
                        f"The {item['name']} (+{bonus} DEF) is no better than what you wear. "
                        "You leave it where it lies."
                    )
            elif item["kind"] == "ring":
                self.items.remove(item)
                worn = player.get("ring")
                if worn:
                    player["max_hp"] -= RINGS[worn].get("max_hp", 0)
                player["ring"] = item["name"]
                player["max_hp"] += RINGS[item["name"]].get("max_hp", 0)
                player["hp"] = min(player["hp"], player["max_hp"])
                self.events.append(f"You slip on the ring of {item['name']} ({RINGS[item['name']]['note']}).")
                if worn:
                    # Dropped where you stand, so stepping off and back swaps it again.
                    self.items.append(
                        {
                            "kind": "ring",
                            "name": worn,
                            "x": x,
                            "y": y,
                            "judged": True,
                        }
                    )
                    self.events.append(f"You leave the ring of {worn} on the floor.")
            elif item["kind"] == "key":
                player["keys"] = player.get("keys", 0) + 1
                self.items.remove(item)
                self.events.append("You pick up a small iron key.")
                if floor.get("locked"):
                    floor["locked"] = False
                    _set_grid_tile(floor, floor["stairs"][0], floor["stairs"][1], TILE_STAIRS)
                    self.events.append("Something heavy unlatches below: the stairs are open.")

        trap = self.trap_at(x, y)
        if trap is not None and not trap["known"]:
            trap["known"] = True
            player["hp"] -= trap["damage"]
            self._hit_by = "a trap"
            self.events.append(f"A hidden trap goes off. You take {trap['damage']} damage.")

        if self.tile(x, y) == TILE_STAIRS:
            self.events.append("There are stairs leading down here.")
        elif self.tile(x, y) == TILE_LOCKED:
            self.events.append("The stairs down are sealed by a lock.")

    def _descend(self) -> None:
        if not self.on_stairs():
            self.events.append("There are no stairs here.")
            return
        floor = self.s["floor"]
        if floor.get("locked"):
            # A key you are already carrying opens the lock.  Refusing purely because
            # this floor's own key has not been stepped on yet would strand a player who
            # arrived holding one -- an unwinnable state, not a puzzle.
            if self.s["player"].get("keys", 0) > 0:
                self.s["player"]["keys"] -= 1
                floor["locked"] = False
                _set_grid_tile(floor, floor["stairs"][0], floor["stairs"][1], TILE_STAIRS)
                self.events.append("You fit the iron key into the lock and the stairs swing open.")
            else:
                self.events.append("The stairs are locked. Somewhere on this floor is the key.")
                return
        depth = self.s["depth"] + 1
        if depth > self.s["max_depth"]:
            self.events.append("There is nothing below. (this should not happen)")
            return
        self._enter_floor(depth)
        if depth == self.s["max_depth"]:
            self.events.append(f"You descend to floor {depth}. The air is heavy, and something is awake.")
        else:
            self.events.append(f"You descend to floor {depth}.")

    def _use(self, key: str | None) -> None:
        player = self.s["player"]
        inventory = player["inventory"]
        if key is None:
            self.events.append("use what? (use potion / use scroll / use 1)")
            return
        key = key.lower()
        if key in ("p", "potion", "potions"):
            key = "potion"
        elif key in ("s", "scroll", "scrolls"):
            key = "scroll"
        elif key.isdigit():
            names = sorted(inventory)
            index = int(key) - 1
            if 0 <= index < len(names):
                key = names[index]
            else:
                self.events.append(f"There is no item {key}.")
                return

        if inventory.get(key, 0) <= 0:
            self.events.append(f"You have no {key}.")
            return

        if key == "potion":
            inventory[key] -= 1
            heal = max(6, player["max_hp"] * 2 // 5)
            before = player["hp"]
            player["hp"] = min(player["max_hp"], player["hp"] + heal)
            self.events.append(f"You drink a potion. HP {before} -> {player['hp']}.")
        elif key == "scroll":
            inventory[key] -= 1
            kind = self.s["scroll"]["kind"]
            self.s["scroll"]["known"] = True
            self.events.append(f"The scroll reads: {kind}.")
            if kind == "mapping":
                for y in range(MAP_H):
                    self.seen |= {(x, y) for x in range(MAP_W)}
                self.events.append("The whole floor plan floods into your head.")
            elif kind == "teleport":
                spots = [
                    (x, y)
                    for y in range(MAP_H)
                    for x in range(MAP_W)
                    if self.tile(x, y) != TILE_WALL and self.monster_at(x, y) is None
                ]
                spot = spots[self.rng.randrange(len(spots))]
                player["x"], player["y"] = spot
                self.events.append("The floor drops away and you are somewhere else.")
            elif kind == "blessing":
                player["atk"] += 1
                self.events.append("A warm surge. Your attack is permanently stronger.")
            elif kind == "ward":
                player["def"] += 1
                self.events.append("A cold shell settles on your skin. Your defence is permanently higher.")
        else:
            self.events.append(f"You cannot use the {key} like that.")
            return
        if inventory.get(key, 0) <= 0:
            inventory.pop(key, None)

    def _sense(self) -> None:
        found = 0
        for trap in self.traps:
            if trap["known"]:
                continue
            if _chebyshev((trap["x"], trap["y"]), (self.px, self.py)) <= 2:
                trap["known"] = True
                found += 1
        if found:
            self.events.append(f"You find {found} trap{'s' if found > 1 else ''} right next to you.")
        else:
            self.events.append("You find nothing hidden nearby.")

    # -- monsters ----------------------------------------------------------

    def _monster_turn(self) -> None:
        player_pos = (self.px, self.py)
        for monster in self.monsters:
            if not monster["alive"]:
                continue
            monster["tick"] = monster.get("tick", 0) + 1
            # Heavy things do not act every turn, so walking away is a real option.
            slow = monster.get("slow", 1)
            if slow > 1 and monster["tick"] % slow != 0:
                continue
            # Fast things act more than once, so standing in the open is a real cost.
            for _ in range(max(1, monster.get("speed", 1))):
                if not monster["alive"] or self.s["status"] != "ok":
                    break
                self._monster_act(monster, player_pos)

    def _monster_act(self, monster: dict, player_pos: tuple[int, int]) -> None:
        here = (monster["x"], monster["y"])
        distance = _chebyshev(here, (self.px, self.py))
        if distance <= 1:
            self._monster_attack(monster)
            return
        if not monster["alerted"]:
            if distance <= MONSTER_SIGHT and self._los(here, player_pos):
                monster["alerted"] = True
            else:
                # A shaman tends its friends whether or not it has seen you yet.
                if monster.get("heals", 0):
                    self._monster_heal(monster)
                return
        # Archers do not close in: if they can see you, they shoot.  They also have to
        # spend a turn drawing the next arrow, which is what turns "you will take four
        # hits walking at it" into a rhythm you can actually read and time.
        if monster.get("ranged", 0) >= distance and self._los(here, player_pos):
            if monster.get("reload", 1) <= 1 or monster["tick"] % monster["reload"] == 0:
                self._monster_shoot(monster, distance)
            else:
                self.events.append(f"The {monster['kind']} draws its bow.")
            return
        if monster.get("heals", 0) and self._monster_heal(monster):
            return
        self._monster_move(monster)
        if monster["alive"] and _chebyshev((monster["x"], monster["y"]), (self.px, self.py)) <= 1:
            self._monster_attack(monster)

    def _monster_heal(self, monster: dict) -> bool:
        wounded = None
        worst = 0.8
        for other in self.monsters:
            if other is monster or not other["alive"]:
                continue
            if _chebyshev((other["x"], other["y"]), (monster["x"], monster["y"])) > monster.get("heals", 0):
                continue
            ratio = other["hp"] / other["max_hp"] if other["max_hp"] else 1.0
            if ratio < worst:
                worst = ratio
                wounded = other
        if wounded is None:
            return False
        amount = min(wounded["max_hp"] - wounded["hp"], 3 + wounded["max_hp"] // 4)
        wounded["hp"] += amount
        wounded["alerted"] = wounded["alerted"] or True
        self.events.append(f"The {monster['kind']} mends the {wounded['kind']} for {amount}.")
        return True

    def _monster_shoot(self, monster: dict, distance: int) -> None:
        damage = max(1, monster["atk"] + self.rng.randint(0, 2) - self.p_def() // 2)
        self._hurt(damage, _with_article(monster["kind"]))
        self.events.append(f"The {monster['kind']} shoots you for {damage} from {distance} tiles away.")

    def _monster_attack(self, monster: dict) -> None:
        player = self.s["player"]
        damage = max(1, monster["atk"] + self.rng.randint(0, 2) - self.p_def())
        self._hurt(damage, _with_article(monster["kind"]))
        self.events.append(f"The {monster['kind']} hits you for {damage}.")
        if monster.get("poisons", 0):
            effects = player.setdefault("effects", {"poison": 0, "weak": 0})
            if monster["poisons"] > effects.get("poison", 0):
                effects["poison"] = monster["poisons"]
                self.events.append(f"Venom works into the wound ({monster['poisons']} turns of poison).")
        if monster.get("weakens", 0):
            effects = player.setdefault("effects", {"poison": 0, "weak": 0})
            if monster["weakens"] > effects.get("weak", 0):
                effects["weak"] = monster["weakens"]
                self.events.append(f"A cold grip saps your strength ({monster['weakens']} turns).")

    def _monster_move(self, monster: dict) -> None:
        here = (monster["x"], monster["y"])
        if monster["erratic"] and self.rng.random() < monster["erratic"]:
            options = [
                (dx, dy)
                for dx, dy in DIRS.values()
                if self._walkable_for_monster(monster["x"] + dx, monster["y"] + dy)
            ]
            if not options:
                return
            dx, dy = options[self.rng.randrange(len(options))]
        else:
            best = None
            best_distance = _chebyshev(here, (self.px, self.py))
            options = []
            for dx, dy in DIRS.values():
                nx, ny = monster["x"] + dx, monster["y"] + dy
                if not self._walkable_for_monster(nx, ny):
                    continue
                options.append((dx, dy))
                distance = _chebyshev((nx, ny), (self.px, self.py))
                if distance < best_distance:
                    best_distance = distance
                    best = (dx, dy)
            if best is None:
                if not options:
                    return
                if not monster["alerted"]:
                    return
                best = options[self.rng.randrange(len(options))]
            dx, dy = best
        monster["x"] += dx
        monster["y"] += dy

    def _walkable_for_monster(self, x: int, y: int) -> bool:
        if self.tile(x, y) == TILE_WALL:
            return False
        if (x, y) == (self.px, self.py):
            return False
        return self.monster_at(x, y) is None

    # -- endings -----------------------------------------------------------

    def _check_end(self) -> None:
        if self.s["status"] != "ok":
            return
        player = self.s["player"]
        if player["hp"] <= 0:
            player["hp"] = 0
            self.s["status"] = "dead"
            self.s["cause"] = f"slain by {self._hit_by} on floor {self.s['depth']}"
            self.events.append(f"You die on floor {self.s['depth']}.")
            return
        if self.s["turn"] >= TURN_LIMIT:
            self.s["status"] = "stalled"
            self.s["cause"] = f"ran out of turns on floor {self.s['depth']}"
            self.events.append("The dungeon has gone on too long. The run ends here.")

    # -- rendering ---------------------------------------------------------

    def legend_block(self) -> str:
        parts = [f"{glyph} {text}" for glyph, text in LEGEND.items() if glyph != " "]
        parts.append("(space) never seen")
        return "LEGEND  " + "   ".join(parts)

    def inventory_block(self) -> str:
        player = self.s["player"]
        gear = f"weapon {player['weapon'] or 'none'}   armor {player['armor'] or 'none'}"
        if not player["inventory"]:
            carried = "empty"
        else:
            carried = ", ".join(
                self.inv_label(key, player["inventory"][key]) for key in sorted(player["inventory"])
            )
        return f"GEAR  {gear}\nINV   {carried}\nGOLD  {player['gold']}"

    def _window_bounds(self) -> tuple[int, int, int, int]:
        half_w, half_h = 12, 5
        x0 = max(0, min(self.px - half_w, MAP_W - (half_w * 2 + 1)))
        y0 = max(0, min(self.py - half_h, MAP_H - (half_h * 2 + 1)))
        return x0, y0, min(MAP_W, x0 + half_w * 2 + 1), min(MAP_H, y0 + half_h * 2 + 1)

    def map_rows(self, view: str | None = None) -> list[str]:
        view = view or self.view
        if view == "none":
            return ["(map suppressed)"]
        vis = self.visible_tiles()
        if view == "window":
            x0, y0, x1, y1 = self._window_bounds()
        else:
            x0, y0, x1, y1 = 0, 0, MAP_W, MAP_H
        rows = []
        for y in range(y0, y1):
            row = []
            for x in range(x0, x1):
                row.append(self._glyph(x, y, vis))
            rows.append("".join(row))
        return rows

    def _glyph(self, x: int, y: int, vis: set) -> str:
        seen = (x, y) in vis or (x, y) in self.seen
        if not seen:
            return " "
        if (x, y) == (self.px, self.py):
            return GLYPH_PLAYER
        if (x, y) in vis:
            monster = self.monster_at(x, y)
            if monster is not None:
                return MONSTERS[monster["kind"]]["glyph"]
            relic = self.s["floor"].get("relic")
            if relic and [x, y] == list(relic):
                return GLYPH_RELIC
            item = self.item_at(x, y)
            if item is not None:
                return ITEM_GLYPHS[item["kind"]]
        for trap in self.traps:
            if trap["known"] and trap["x"] == x and trap["y"] == y:
                return GLYPH_TRAP
        if self.tile(x, y) == TILE_WALL:
            return TILE_WALL
        if self.tile(x, y) in (TILE_STAIRS, TILE_LOCKED):
            return self.tile(x, y)
        return TILE_FLOOR if (x, y) in vis else GLYPH_REMEMBERED_FLOOR

    def targets(self) -> list[dict]:
        vis = getattr(self, "_vis_cache", None)
        if vis is None:
            vis = self.visible_tiles()
        out = []
        for monster in self.live_monsters():
            if (monster["x"], monster["y"]) not in vis:
                continue
            out.append(
                {
                    "name": monster["kind"],
                    "dir": dir_name(monster["x"] - self.px, monster["y"] - self.py),
                    "dx": monster["x"] - self.px,
                    "dy": monster["y"] - self.py,
                    "hp": max(0, monster["hp"]),
                    "max_hp": monster["max_hp"],
                }
            )
        out.sort(key=lambda t: (t["dir"], t["name"]))
        return out

    def ground(self) -> list[dict]:
        out = []
        for item in self.items:
            if item["x"] == self.px and item["y"] == self.py:
                if item["kind"] == "gold":
                    out.append({"kind": "gold", "amount": item["amount"]})
                else:
                    out.append({"kind": item["kind"], "name": item.get("name")})
        return out

    def floor_traits(self) -> str:
        traits = []
        if self.s["floor"].get("dark"):
            traits.append(f"dark (sight {DARK_RADIUS})")
        if self.s["floor"].get("locked"):
            traits.append("stairs locked")
        return ", ".join(traits) if traits else "nothing unusual"

    def effect_line(self) -> str:
        """Empty when nothing is active, so the line only ever appears as a warning."""
        effects = self.s["player"].get("effects") or {}
        parts = []
        if effects.get("poison"):
            parts.append(f"poison {effects['poison']}")
        if effects.get("weak"):
            parts.append(f"weak {effects['weak']} (-{WEAK_PENALTY} ATK)")
        return ", ".join(parts)

    def loot(self) -> list[dict]:
        """Everything visible that is not already underfoot, with a bearing."""
        out = []
        vis = self._vis_cache if self._vis_cache is not None else self.visible_tiles()
        for item in self.items:
            if (item["x"], item["y"]) not in vis:
                continue
            if (item["x"], item["y"]) == (self.px, self.py):
                continue
            out.append(
                {
                    "dir": dir_name(item["x"] - self.px, item["y"] - self.py),
                    "kind": item["kind"],
                    "name": item.get("name"),
                    "x": item["x"],
                    "y": item["y"],
                    "judged": bool(item.get("judged")),
                }
            )
        out.sort(key=lambda entry: (entry["dir"], entry["kind"], entry["name"] or ""))
        return out

    def on_stairs(self) -> bool:
        """True when the player is standing on the way down (locked or not)."""
        return list(self.s["floor"].get("stairs") or []) == [self.px, self.py]

    def stairs_state(self) -> str:
        stairs = self.s["floor"].get("stairs")
        if not stairs:
            return "none"
        locked = " (locked)" if self.s["floor"].get("locked") else ""
        if list(stairs) == [self.px, self.py]:
            return "here" + locked
        if tuple(stairs) in self.seen:
            return "seen" + locked
        return "unknown" + locked

    def render_text(self, full_map: bool = False, legend: bool | None = None) -> str:
        vis = self.visible_tiles()
        self._vis_cache = vis
        player = self.s["player"]
        view = "full" if full_map else self.view
        lines = []
        lines.append(
            f"-- DELVE  floor {self.s['depth']}/{self.s['max_depth']}   "
            f"turn {self.s['turn']}   seed {self.s['seed']} --"
        )
        lines.append(
            f"HP {player['hp']}/{player['max_hp']}  "
            f"ATK {self.p_atk()}({player['atk']}+{self.p_atk() - player['atk']})  "
            f"DEF {self.p_def()}({player['def']}+{self.p_def() - player['def']})  "
            f"LV {player['level']}  xp {player['xp']}/{player['xp_next']}  GOLD {player['gold']}"
        )
        ring = player.get("ring")
        gear = (
            f"GEAR  weapon {player['weapon'] or 'none'}   armor {player['armor'] or 'none'}"
            f"   ring {ring or 'none'}"
        )
        if player["inventory"]:
            carried = ", ".join(
                self.inv_label(key, player["inventory"][key]) for key in sorted(player["inventory"])
            )
        else:
            carried = "empty"
        # The key shows up here so an agent can see it, but deliberately does *not* go
        # into the numbered inventory: `use 1` must keep meaning the same thing.
        if player.get("keys"):
            carried = f"key x{player['keys']}, {carried}" if carried != "empty" else f"key x{player['keys']}"
        lines.append(f"{gear}   INV {carried}")

        lines.append(f"FLOOR   {self.floor_traits()}")
        effects = self.effect_line()
        if effects:
            lines.append(f"EFFECTS {effects}")

        lines.append("MAP")
        for row in self.map_rows(view):
            lines.append("  " + row)

        targets = self.targets()
        if targets:
            joined = "  ".join(f"{t['name']}({t['dir']} hp {t['hp']}/{t['max_hp']})" for t in targets)
            lines.append("TARGETS  " + joined)
        else:
            lines.append("TARGETS  none")

        ground = self.ground()
        if ground:
            lines.append(
                "GROUND   "
                + ", ".join(
                    f"gold({g['amount']})" if g["kind"] == "gold" else (g.get("name") or g["kind"])
                    for g in ground
                )
            )
        else:
            lines.append("GROUND   none")

        loot = self.loot()
        if loot:
            lines.append(
                "LOOT     "
                + "  ".join(
                    f"{entry['name'] or entry['kind']}({entry['dir']}"
                    + (", judged" if entry["judged"] else "")
                    + ")"
                    for entry in loot[:8]
                )
            )
            if len(loot) > 8:
                lines.append(f"         ... and {len(loot) - 8} more")
        else:
            lines.append("LOOT     none")

        lines.append(f"STAIRS   {self.stairs_state()}")

        lines.append("EVENT")
        events = self.events
        if events:
            for event in events:
                lines.append("  - " + event)
        else:
            lines.append("  - (nothing)")

        if self.s.get("cause"):
            lines.append("CAUSE    " + self.s["cause"])
        if legend if legend is not None else self.show_legend:
            lines.append(self.legend_block())
        # RESULT is always the final line: a harness that runs `tail -1` must never read anything else.
        lines.append(f"RESULT {self.s['status']}")
        return "\n".join(lines)

    def render_json(self) -> dict:
        vis = self.visible_tiles()
        self._vis_cache = vis
        player = self.s["player"]
        return {
            "result": self.s["status"],
            "cause": self.s.get("cause"),
            "game_version": GAME_VERSION,
            "seed": self.s["seed"],
            "floor": self.s["depth"],
            "max_depth": self.s["max_depth"],
            "turn": self.s["turn"],
            "player": {
                "hp": player["hp"],
                "max_hp": player["max_hp"],
                "atk": self.p_atk(),
                "base_atk": player["atk"],
                "def": self.p_def(),
                "base_def": player["def"],
                "level": player["level"],
                "xp": player["xp"],
                "xp_next": player["xp_next"],
                "gold": player["gold"],
                "weapon": player["weapon"],
                "armor": player["armor"],
                "ring": player.get("ring"),
                "keys": player.get("keys", 0),
                "effects": dict(player.get("effects") or {}),
                "inventory": dict(player["inventory"]),
                "scroll_identified": self.s["scroll"]["known"],
            },
            "map": {"width": MAP_W, "height": MAP_H, "view": self.view, "rows": self.map_rows()},
            "legend": {glyph: text for glyph, text in LEGEND.items() if glyph != " "},
            "targets": self.targets(),
            "ground": self.ground(),
            "loot": self.loot(),
            "floor_traits": self.floor_traits(),
            "stairs": self.stairs_state(),
            "events": list(self.events),
        }


# --------------------------------------------------------------------------
# new game / save / load
# --------------------------------------------------------------------------


def new_game(
    seed: int | None = None, depth: int = DEFAULT_DEPTH, view: str = "full", legend: bool = False
) -> Game:
    if seed is None:
        seed = random.SystemRandom().randrange(1, 2**31)
    state = {
        "format": SAVE_FORMAT,
        "version": SAVE_VERSION,
        "game_version": GAME_VERSION,
        "seed": int(seed),
        "depth": 1,
        "max_depth": int(depth),
        "turn": 0,
        "status": "ok",
        "cause": None,
        "log": [],
        "player": {
            "x": 0,
            "y": 0,
            "hp": PLAYER_BASE_HP,
            "max_hp": PLAYER_BASE_HP,
            "atk": PLAYER_BASE_ATK,
            "def": PLAYER_BASE_DEF,
            "level": 1,
            "xp": 0,
            "xp_next": 10,
            "gold": 0,
            "weapon": None,
            "armor": None,
            "ring": None,
            "keys": 0,
            "effects": {"poison": 0, "weak": 0},
            "inventory": {},
        },
        "scroll": {"kind": None, "known": False},
        "floor": {},
        "rng": None,
    }
    game = Game(state, view=view, legend=legend, seed=int(seed))
    game.rng = random.Random(int(seed))
    state["scroll"]["kind"] = game.rng.choice(SCROLL_KINDS)
    game._enter_floor(1)
    game.sync()
    return game


def _xor(data: bytes) -> bytes:
    key = SAVE_XOR_KEY
    return bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))


def save_game(path: str, game: Game, seal: bool = True) -> None:
    game.sync()
    payload = json.dumps(game.s, sort_keys=True, separators=(",", ":"))
    if seal:
        encoded = base64.b64encode(_xor(payload.encode("utf-8"))).decode("ascii")
        doc = {"format": SAVE_FORMAT, "version": SAVE_VERSION, "sealed": True, "data": encoded}
    else:
        doc = {"format": SAVE_FORMAT, "version": SAVE_VERSION, "sealed": False, "data": json.loads(payload)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    os.replace(tmp, path)


def load_game(path: str, view: str = "full", legend: bool = False) -> Game:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if doc.get("format") != SAVE_FORMAT:
        raise ValueError(f"not a {SAVE_FORMAT} file: {path}")
    if doc.get("version") != SAVE_VERSION:
        # Fail here, clearly, rather than halfway through constructing a Game from a
        # state that is missing fields this version expects.
        raise ValueError(
            f"save format v{doc.get('version')} is not readable by this build (v{SAVE_VERSION}): {path}"
        )
    if doc.get("sealed"):
        raw = _xor(base64.b64decode(doc["data"])).decode("utf-8")
        state = json.loads(raw)
    else:
        state = doc["data"]
    return Game(state, view=view, legend=legend)


# --------------------------------------------------------------------------
# a built-in greedy bot (baseline + demo, only uses what is visible/seen)
# --------------------------------------------------------------------------


def _bfs_step(game: Game, targets: set[tuple[int, int]], known: set) -> str | None:
    """One step along the shortest path through `known` walkable tiles to `targets`."""
    start = (game.px, game.py)
    if start in targets:
        return None
    previous = {start: None}
    queue = [start]
    head = 0
    found = None
    while head < len(queue):
        x, y = queue[head]
        head += 1
        if (x, y) in targets:
            found = (x, y)
            break
        for dx, dy in DIRS.values():
            nx, ny = x + dx, y + dy
            if (nx, ny) in previous or (nx, ny) not in known:
                continue
            if game.tile(nx, ny) == TILE_WALL:
                continue
            previous[(nx, ny)] = (x, y)
            queue.append((nx, ny))
    if found is None:
        return None
    node = found
    while previous[node] is not None and previous[previous[node]] is not None:
        node = previous[node]
    dx, dy = node[0] - start[0], node[1] - start[1]
    for name, (ddx, ddy) in DIRS.items():
        if (ddx, ddy) == (dx, dy):
            return name
    return None


_OPPOSITE = {"n": "s", "s": "n", "e": "w", "w": "e", "ne": "sw", "sw": "ne", "nw": "se", "se": "nw"}


def _bot_pick(game: Game, ban: str | None = None, avoid: set | None = None) -> str:
    """Choose one command for the built-in bot.

    `ban` forbids a single step direction and `avoid` forbids stepping onto a
    recently visited tile; the wrapper relaxes both if nothing else is available.
    """
    player = game.s["player"]
    vis = game.visible_tiles()
    known = set(game.seen) | vis
    walk_known = {(x, y) for x, y in known if game.tile(x, y) != TILE_WALL}
    inventory = player["inventory"]

    # The bot's notebook.  Keeping it here (rather than in the save) is deliberate:
    # remembering where you saw things is the *player's* job, and this is a sample
    # player showing how to do it.  Anything you only ever see once and never walk
    # over -- a key on the far side of the floor -- is lost forever without notes.
    notes = getattr(game, "_bot_notes", None)
    if notes is None:
        notes = {}
        game._bot_notes = notes
    present = {(it["x"], it["y"]) for it in game.items}
    for pos in [pos for pos in notes if pos not in present]:
        del notes[pos]
    for item in game.items:
        pos = (item["x"], item["y"])
        if item.get("judged"):
            notes.pop(pos, None)
        elif pos in vis:
            notes[pos] = item["kind"]

    def step_ok(step: str | None) -> bool:
        if step is None or step == ban:
            return False
        if avoid and step in DIRS:
            dx, dy = DIRS[step]
            if (game.px + dx, game.py + dy) in avoid:
                return False
        return True

    adjacent = [m for m in game.live_monsters() if _chebyshev((m["x"], m["y"]), (game.px, game.py)) == 1]
    hurt = player["hp"] * 100 <= player["max_hp"] * 40
    if hurt and inventory.get("potion", 0) > 0 and not adjacent:
        return "use potion"

    if adjacent:
        target = min(adjacent, key=lambda m: m["hp"])
        return "move " + dir_name(target["x"] - game.px, target["y"] - game.py)

    seen_monsters = [m for m in game.live_monsters() if (m["x"], m["y"]) in vis]
    if seen_monsters and player["hp"] * 100 > player["max_hp"] * 35:
        target = min(seen_monsters, key=lambda m: _chebyshev((m["x"], m["y"]), (game.px, game.py)))
        step = _bfs_step(game, {(target["x"], target["y"])}, walk_known | {(target["x"], target["y"])})
        if step_ok(step):
            return "move " + step

    # A locked staircase is a hard gate: without the key nothing else on the floor
    # matters, so the key outranks exploring and looting.
    if game.s["floor"].get("locked") and not player.get("keys", 0):
        keys = [(it["x"], it["y"]) for it in game.items if it["kind"] == "key"]
        if keys:
            step = _bfs_step(game, set(keys), walk_known)
            if step_ok(step):
                return "move " + step

    relic = game.s["floor"].get("relic")
    if relic:
        step = _bfs_step(game, {(relic[0], relic[1])}, walk_known | {(relic[0], relic[1])})
        if step_ok(step):
            return "move " + step

    frontier = set()
    for x, y in walk_known:
        for dx, dy in DIRS.values():
            nx, ny = x + dx, y + dy
            if (nx, ny) not in known and 0 <= nx < MAP_W and 0 <= ny < MAP_H:
                frontier.add((nx, ny))
    frontier = {t for t in frontier if game.tile(t[0], t[1]) != TILE_WALL}
    if frontier:
        step = _bfs_step(game, frontier, walk_known | frontier)
        if step_ok(step):
            return "move " + step

    visible_items = [
        (it["x"], it["y"]) for it in game.items if (it["x"], it["y"]) in vis and not it.get("judged")
    ]
    if visible_items:
        step = _bfs_step(game, set(visible_items), walk_known)
        if step_ok(step):
            return "move " + step

    # Nothing in sight that we want: fall back on what we wrote down earlier.
    noted = [pos for pos in notes if pos not in vis and game.tile(pos[0], pos[1]) != TILE_WALL]
    if noted:
        step = _bfs_step(game, set(noted), walk_known)
        if step_ok(step):
            return "move " + step

    if game.on_stairs():
        return "descend"

    stairs = game.s["floor"].get("stairs")
    if stairs and tuple(stairs) in walk_known:
        step = _bfs_step(game, {tuple(stairs)}, walk_known)
        if step_ok(step):
            return "move " + step

    if hurt and inventory.get("potion", 0) > 0:
        return "use potion"
    return "wait"


def bot_command(game: Game) -> str:
    """The bot refuses to step straight back where it came from, and avoids the
    tiles it just walked through.

    Without that it can lock into a short cycle: its goal is always "the nearest
    unexplored edge", and that changes under it as it reveals terrain, so it can
    loop over four tiles for a whole run.  Both guards are soft -- if every other
    move is impossible the bot falls back to a normal answer rather than freezing.
    """
    visits = getattr(game, "_bot_visits", None)
    if visits is None:
        visits = collections.deque(maxlen=8)
        game._bot_visits = visits
    previous = getattr(game, "_bot_last", "")
    ban = _OPPOSITE.get(previous.split()[1]) if previous.startswith("move ") else None
    avoid = set(visits)

    command = _bot_pick(game, ban, avoid)
    if command == "wait":
        command = _bot_pick(game, ban, None)
    if command == "wait":
        command = _bot_pick(game, None, None)
    game._bot_last = command
    visits.append((game.px, game.py))
    return command


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------


def selftest(verbose: bool = True) -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        results.append((name, bool(condition), detail))

    # 1. floors are connected and populated sanely
    ok = True
    detail = ""
    for seed in range(1, 16):
        rng = random.Random(seed)
        for depth in range(1, 7):
            floor = build_floor(rng, depth, 6)
            grid = floor["grid"]
            start = tuple(floor["start"])
            goal = tuple(floor["stairs"]) if floor["stairs"] else None
            reached = {start}
            queue = [start]
            while queue:
                x, y = queue.pop()
                for dx, dy in DIRS.values():
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < MAP_W and 0 <= ny < MAP_H):
                        continue
                    if grid[ny][nx] == TILE_WALL or (nx, ny) in reached:
                        continue
                    reached.add((nx, ny))
                    queue.append((nx, ny))
            if goal and goal not in reached:
                ok, detail = False, f"seed {seed} depth {depth}: stairs unreachable"
            for group in ("monsters", "items", "traps"):
                for thing in floor[group]:
                    if grid[thing["y"]][thing["x"]] == TILE_WALL:
                        ok, detail = False, f"seed {seed} depth {depth}: {group} on a wall"
            for monster in floor["monsters"]:
                if (monster["x"], monster["y"]) == start:
                    ok, detail = False, f"seed {seed} depth {depth}: monster on the start tile"
            if not (reached >= {tuple(floor["start"])}):
                ok = False
    check("floors connected and populated", ok, detail)

    # 2. same seed -> identical text
    script = ["e", "s", "move n", "wait", "sense", "w", "n", "use potion", "map", "se"]
    runs = []
    for _ in range(2):
        game = new_game(seed=1234, depth=4)
        for command in script:
            game.step(command)
            game.render_text(legend=True)
        runs.append(game.render_text(legend=True))
    check("same seed reproduces the same board", runs[0] == runs[1])

    # 3. save / load round trip keeps the run identical

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "save.json")
        game = new_game(seed=99, depth=4)
        for command in ["e", "s", "e", "wait"]:
            game.step(command)
        save_game(path, game)
        reloaded = load_game(path)
        after_a = []
        after_b = []
        for command in ["n", "e", "s", "sense", "wait"]:
            game.step(command)
            reloaded.step(command)
            after_a.append(game.render_text())
            after_b.append(reloaded.render_text())
        check("save/load round trip", after_a == after_b)
        plain = os.path.join(tmp, "plain.json")
        save_game(plain, game, seal=False)
        check("plain save loads too", load_game(plain).s["seed"] == game.s["seed"])

    # 4. garbage never crashes and always costs a turn
    game = new_game(seed=7, depth=3)
    before = game.s["turn"]
    garbage = ["zzz", "move", "move sideways", "use", "use nothing", "descend", "attack", ";", "12345"]
    for command in garbage:
        game.step(command)
    check("garbage commands do not crash", game.s["status"] == "ok")
    check(
        "every command spends a turn",
        game.s["turn"] == before + len(garbage),
        f"turn={game.s['turn']}",
    )
    game.step("")
    game.step("   ")
    check("a blank line is not a command", game.s["turn"] == before + len(garbage))

    # 5. free commands are free
    game = new_game(seed=7, depth=3)
    turn = game.s["turn"]
    for command in ["map", "status", "inv", "help", "legend", "card"]:
        kind, _ = game.step(command)
        if kind != "doc":
            check("free commands return docs", False, command)
            break
    check("free commands do not spend a turn", game.s["turn"] == turn)

    # 6. no leaking of monsters the player cannot see
    leaked = ""
    for seed in range(1, 8):
        game = new_game(seed=seed, depth=4)
        for _ in range(40):
            kind, _ = game.step("map")
            vis = game.visible_tiles()
            rows = game.map_rows()
            hidden = [m for m in game.live_monsters() if (m["x"], m["y"]) not in vis]
            for monster in hidden:
                glyph = rows[monster["y"]][monster["x"]]
                if glyph == MONSTERS[monster["kind"]]["glyph"]:
                    leaked = f"seed {seed}: {monster['kind']} drawn while unseen"
            if leaked:
                break
        if leaked:
            break
    check("unseen monsters are not drawn", not leaked, leaked)

    # 7. death is reachable
    game = new_game(seed=3, depth=6)
    game._enter_floor(game.s["max_depth"])
    guardian = next(m for m in game.monsters if m["kind"] == "guardian")
    spot = None
    for dx, dy in DIRS.values():
        nx, ny = guardian["x"] + dx, guardian["y"] + dy
        if game.tile(nx, ny) != TILE_WALL and game.monster_at(nx, ny) is None:
            spot = (nx, ny)
            break
    check("the guardian has room to be reached", spot is not None)
    if spot:
        game.s["player"]["x"], game.s["player"]["y"] = spot
        game.s["player"]["hp"] = 1
        game.step("wait")
        check("death is reachable", game.s["status"] == "dead", f"status={game.s['status']}")

    # 7b. win is reachable by hand
    game = new_game(seed=5, depth=4)
    game._enter_floor(game.s["max_depth"])
    guardian = next(m for m in game.monsters if m["kind"] == "guardian")
    guardian["hp"] = 1
    spot = None
    for dx, dy in DIRS.values():
        nx, ny = guardian["x"] + dx, guardian["y"] + dy
        if game.tile(nx, ny) != TILE_WALL and game.monster_at(nx, ny) is None:
            spot = (nx, ny)
            break
    if spot:
        game.s["player"]["x"], game.s["player"]["y"] = spot
        game.s["player"]["hp"] = 99
        game.s["player"]["max_hp"] = 99
        game.step("attack " + dir_name(guardian["x"] - spot[0], guardian["y"] - spot[1]))
        check(
            "killing the guardian drops the relic",
            game.s["floor"].get("relic") == [guardian["x"], guardian["y"]],
        )
        game.step("move " + dir_name(guardian["x"] - game.px, guardian["y"] - game.py))
        check("taking the relic wins", game.s["status"] == "win", f"status={game.s['status']}")

    # 8. the greedy bot can actually win
    wins = 0
    best = ""
    for seed in (1, 2, 3, 11, 17):
        game = new_game(seed=seed, depth=6)
        for _ in range(TURN_LIMIT):
            kind, _ = game.step(bot_command(game))
            if game.s["status"] != "ok":
                break
        if game.s["status"] == "win":
            wins += 1
        else:
            best = f"seed {seed}: {game.s['status']} floor {game.s['depth']} turn {game.s['turn']}"
    check("the baseline bot wins some seeds", wins >= 1, f"wins={wins}/5 last={best}")

    # 9. content tables are self-consistent
    sample = build_floor(random.Random(5), 6, 8)
    generated = {item["kind"] for item in sample["items"]}
    missing = sorted(generated - set(ITEM_GLYPHS))
    check("every generated item kind has a glyph", not missing, f"missing={missing}")
    bad_ring = [name for name, spec in RINGS.items() if not spec.get("note")]
    check("every ring carries a note for the player", not bad_ring, f"missing={bad_ring}")
    unknown = [kind for kind, spec in MONSTERS.items() if "glyph" not in spec]
    check("every monster has a glyph", not unknown, f"missing={unknown}")
    for glyph, name in [(spec["glyph"], kind) for kind, spec in MONSTERS.items()]:
        if LEGEND.get(glyph) is None:
            check("every monster glyph is in the legend", False, f"{name} ({glyph}) missing")
            break
    else:
        check("every monster glyph is in the legend", True)
    undoc = [
        kind
        for kind, spec in MONSTERS.items()
        if spec.get("ranged")
        or spec.get("poisons")
        or spec.get("heals")
        or spec.get("weakens")
        or spec.get("speed", 1) > 1
        or spec.get("slow", 1) > 1
    ]
    check("the deep monsters have mechanics, not just stats", len(undoc) >= 4, f"found={undoc}")

    # 10. a locked floor always has a reachable key, and dark floors are playable
    locked_failures = 0
    dark_seen = 0
    dark_sight_ok = True
    for seed in range(1, 40):
        game = new_game(seed=seed, depth=8)
        for depth in range(1, 9):
            game._enter_floor(depth)
            floor = game.s["floor"]
            if floor.get("locked") and not [i for i in floor["items"] if i["kind"] == "key"]:
                locked_failures += 1
            if floor.get("dark"):
                dark_seen += 1
                if game.sight() != DARK_RADIUS:
                    dark_sight_ok = False
            if floor.get("locked") and floor["grid"][floor["stairs"][1]][floor["stairs"][0]] != TILE_LOCKED:
                locked_failures += 1
    check("every locked floor has a key", locked_failures == 0, f"failures={locked_failures}")
    check("dark floors actually occur", dark_seen > 0, f"seen={dark_seen}")
    check("dark floors shrink sight to the dark radius", dark_sight_ok)

    # 11. the ring's max_hp is added and removed symmetrically
    game = new_game(seed=12, depth=3)
    player = game.s["player"]
    base = player["max_hp"]
    game.items.append({"kind": "ring", "name": "vigor", "x": player["x"], "y": player["y"]})
    game._on_enter_tile(player["x"], player["y"])
    check("a vigor ring raises max_hp", player["max_hp"] == base + 8, f"max_hp={player['max_hp']}")
    # walk off and back: the ring we took off is dropped, then re-worn
    spot = None
    for dx, dy in DIRS.values():
        if game.tile(player["x"] + dx, player["y"] + dy) != TILE_WALL:
            spot = (player["x"] + dx, player["y"] + dy)
            break
    if spot:
        game.step("move " + dir_name(spot[0] - player["x"], spot[1] - player["y"]))
        game.step("move " + dir_name(player["x"] - game.px, player["y"] - game.py))
        check(
            "max_hp comes back off with the ring",
            player["max_hp"] == base + 8,
            f"max_hp={player['max_hp']} ring={player['ring']}",
        )

    # 12. poison suppresses regeneration
    game = new_game(seed=12, depth=3)
    player = game.s["player"]
    player["max_hp"] = 400
    player["hp"] = 300
    player["effects"]["poison"] = 60
    climbed = 0
    dropped = 0
    for _ in range(40):
        before = player["hp"]
        game.step("wait")
        if player["effects"]["poison"] > 0:
            if player["hp"] > before:
                climbed += 1
            dropped += 1
        assert game.s["status"] == "ok", "a poisoned player should not die in this window"
    check("poison stops regeneration restoring HP", climbed == 0, f"healed {climbed} times")
    check("poison itself damages every turn", dropped == 40, f"ticked {dropped}/40")

    failed = [name for name, ok_, _ in results if not ok_]
    if verbose:
        for name, ok_, detail in results:
            mark = "ok  " if ok_ else "FAIL"
            line = f"  [{mark}] {name}"
            if detail and not ok_:
                line += f"  -- {detail}"
            print(line)
        print(f"selftest: {len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--save", default=DEFAULT_SAVE, help=f"save file (default {DEFAULT_SAVE})")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="output format")
    parser.add_argument(
        "--view",
        choices=("full", "window", "none"),
        default="full",
        help="map rendering: full board, local window, or suppressed",
    )
    parser.add_argument("--legend", action="store_true", help="print the map legend every turn")
    parser.add_argument("--no-seal", action="store_true", help="write the save file as readable JSON")
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="exit 10 win / 11 dead / 12 stalled / 13 quit instead of 0",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delve",
        description="a small dungeon crawler for AI players (one command per line)",
    )
    sub = parser.add_subparsers(dest="command")

    new = sub.add_parser("new", help="start a new run")
    new.add_argument("--seed", type=int, default=None, help="fix the run (default: random)")
    new.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help=f"floors (default {DEFAULT_DEPTH})")
    _add_common(new)

    play = sub.add_parser("play", help="send one command (--cmd) or a stream of them on stdin")
    play.add_argument("--cmd", default=None, help="a single command to run")
    play.add_argument("--log", default=None, help="append one JSONL record per turn to this file")
    _add_common(play)

    show = sub.add_parser("show", help="re-print the current board without spending a turn")
    _add_common(show)

    autoplay = sub.add_parser("autoplay", help="let the built-in greedy bot play (demo/baseline)")
    autoplay.add_argument("--seed", type=int, default=None)
    autoplay.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    autoplay.add_argument("--steps", type=int, default=TURN_LIMIT)
    autoplay.add_argument("--verbose", action="store_true", help="print every turn")
    _add_common(autoplay)

    sub.add_parser("card", help="print the player card (the public part of the rules)")
    sub.add_parser("rulebook", help="print RULES.md, the full mechanics (for the operator)")
    st = sub.add_parser("selftest", help="run internal checks")
    st.add_argument("--quiet", action="store_true")
    return parser


def _view_of(args) -> str:
    return getattr(args, "view", "full")


def _legend_of(args) -> bool:
    return bool(getattr(args, "legend", False))


def _finish(status: str, strict: bool) -> int:
    if strict and status in STRICT_EXITS:
        return STRICT_EXITS[status]
    return EXIT_OK


def _print_doc(args, text: str) -> None:
    if args.format == "json":
        print(json.dumps({"result": "ok", "kind": "doc", "doc": text}))
    else:
        print(text)


def _print_turn(args, game: Game) -> None:
    if args.format == "json":
        print(json.dumps(game.render_json(), ensure_ascii=False))
    else:
        print(game.render_text())


def _log_turn(path: str | None, game: Game, command: str, events: list[str]) -> None:
    if not path:
        return
    record = {
        "turn": game.s["turn"],
        "floor": game.s["depth"],
        "cmd": command.strip(),
        "hp": game.s["player"]["hp"],
        "status": game.s["status"],
        "events": list(events),
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        print("\nquick start:  delve new --seed 42 --save run.json && delve play --save run.json --cmd 'e'")
        return EXIT_USAGE

    if args.command == "card":
        print(CARD_TEXT.rstrip())
        return EXIT_OK

    if args.command == "rulebook":
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "RULES.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                print(handle.read().rstrip())
        else:
            print("RULES.md is not next to this script. Card only:")
            print(CARD_TEXT.rstrip())
        return EXIT_OK

    if args.command == "selftest":
        return selftest(verbose=not args.quiet)

    if args.command == "new":
        if not 2 <= args.depth <= MAX_DEPTH_ALLOWED:
            print(f"depth must be between 2 and {MAX_DEPTH_ALLOWED}", file=sys.stderr)
            return EXIT_USAGE
        game = new_game(seed=args.seed, depth=args.depth, view=_view_of(args), legend=True)
        save_game(args.save, game, seal=not args.no_seal)
        legend = game.legend_block()
        if args.format == "json":
            payload = game.render_json()
            payload["legend_text"] = legend
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(game.render_text(legend=True))
        return _finish(game.s["status"], args.strict_exit)

    if args.command == "autoplay":
        game = new_game(seed=args.seed, depth=args.depth, view=_view_of(args), legend=_legend_of(args))
        steps = 0
        while game.s["status"] == "ok" and steps < args.steps:
            command = bot_command(game)
            _kind, _ = game.step(command)
            _log_turn(getattr(args, "log", None), game, command, game.events)
            steps += 1
            if args.verbose and args.format == "text":
                print(f"> {command}")
                print(game.render_text())
        save_game(args.save, game, seal=not args.no_seal)
        player = game.s["player"]
        summary = (
            f"RESULT {game.s['status']}  seed {game.s['seed']}  "
            f"floor {game.s['depth']}/{game.s['max_depth']}  "
            f"turn {game.s['turn']}  steps {steps}  hp {player['hp']}/{player['max_hp']}  "
            f"level {player['level']}  gold {player['gold']}"
        )
        if args.format == "json":
            payload = game.render_json()
            payload["summary"] = summary
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(summary)
            print(game.render_text())
        return _finish(game.s["status"], args.strict_exit)

    # play / show
    try:
        game = load_game(args.save, view=_view_of(args), legend=_legend_of(args))
    except FileNotFoundError:
        print(f"no save at {args.save}; run `delve new` first", file=sys.stderr)
        return EXIT_ERROR
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.save}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.command == "show":
        _print_turn(args, game)
        return _finish(game.s["status"], args.strict_exit)

    def run(command: str) -> None:
        kind, payload = game.step(command)
        if kind == "doc":
            if payload:
                _print_doc(args, str(payload))
            return
        _log_turn(args.log, game, command, game.events)
        _print_turn(args, game)

    if args.cmd is not None:
        run(args.cmd)
        save_game(args.save, game, seal=not args.no_seal)
        return _finish(game.s["status"], args.strict_exit)

    interactive = sys.stdin.isatty()
    try:
        while True:
            if interactive:
                print("> ", end="", file=sys.stderr, flush=True)
            line = sys.stdin.readline()
            if not line:
                break
            if not line.strip():
                continue
            run(line.rstrip("\n"))
            if interactive:
                save_game(args.save, game, seal=not args.no_seal)
            if game.s["status"] != "ok":
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        save_game(args.save, game, seal=not args.no_seal)
    return _finish(game.s["status"], args.strict_exit)


if __name__ == "__main__":
    sys.exit(main())
