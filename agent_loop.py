#!/usr/bin/env python3
"""An example agent that plays delve through its terminal output only.

The point of this file is to show the intended shape of an AI player:

  * one **new process** per turn (`delve.py play --save ... --cmd ...`), so the agent
    holds no state the game does not also keep in the save file
  * the agent reads **the same text block a language model would be shown**, and
    nothing else -- no importing the game, no opening the save, no access to the
    generator, no ground truth about what is behind a wall
  * `parse_observation()` turns that block into fields; for a model you would normally
    paste the block into the prompt instead, but the parse is what makes "the interface
    is machine-readable" a checkable claim rather than a hope

`--policy parse` is a small agent that plans with nothing but the printed board: attack
the weakest adjacent monster, explore the floor, then take the stairs, with a sticky
target and a written-down note for the relic (items are only drawn while visible).
Measured on seeds 1..30 at depth 5 it wins 14 and dies 16, never stalling -- roughly
half the game's own greedy bot, which is the headroom a real model should be claiming.
`--policy bot` runs that in-process bot for a baseline, and `--policy random` is the
control that shows what a genuinely bad agent looks like (it stalls at the turn limit).

To play with a real model, replace `decide()`; keep the rule that the model sees only
what the game printed.  Every loop this agent fell into while being written is
documented in the comments -- they are all ways of reading the board wrong, and a model
that has the same habits will lose the same way.
"""

from __future__ import annotations

import argparse
import collections
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DELVE = os.path.join(HERE, "delve.py")

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
OPPOSITE = {"n": "s", "s": "n", "e": "w", "w": "e", "ne": "sw", "sw": "ne", "nw": "se", "se": "nw"}

MONSTER_GLYPHS = set("rbjswG")
# Only walls and never-seen space block movement.  Everything else on the board is a
# tile the player can walk onto: floor, remembered floor, stairs, and the item glyphs
# ($ ! ? ) [), which sit *on* floor.  Treating an item as a wall is not a cosmetic bug
# -- a single coin in a one-tile corridor makes the stairs look unreachable, and then
# the agent's plan flips between "go to the stairs" and "explore somewhere else" every
# time it steps, which is an infinite loop that looks like progress.
BLOCKING = set("# ")

HEADER_RE = re.compile(
    r"floor (?P<floor>\d+)/(?P<max_depth>\d+)\s+turn (?P<turn>\d+)\s+seed (?P<seed>\d+)"
)
VITALS_RE = re.compile(
    r"HP (?P<hp>\d+)/(?P<max_hp>\d+)\s+"
    r"ATK (?P<atk>\d+)\((?P<atk_base>\d+)\+(?P<atk_gear>\d+)\)\s+"
    r"DEF (?P<def>\d+)\((?P<def_base>\d+)\+(?P<def_gear>\d+)\)\s+"
    r"LV (?P<level>\d+)\s+xp (?P<xp>\d+)/(?P<xp_next>\d+)\s+GOLD (?P<gold>\d+)"
)
TARGET_RE = re.compile(r"(?P<name>[a-z]+)\((?P<dir>[a-z]{1,2}) hp (?P<hp>\d+)/(?P<max_hp>\d+)\)")
STAIRS_RE = re.compile(r"(?m)^STAIRS\s+(here|seen|unknown|none)\s*$")
GROUND_RE = re.compile(r"(?m)^GROUND\s+(?P<ground>.+?)\s*$")
INV_RE = re.compile(r"\bINV\s+(?P<inv>.+?)\s*$", re.MULTILINE)
RESULT_RE = re.compile(r"(?m)^RESULT (?P<result>ok|win|dead|stalled|quit)\s*$")
ROW_RE = re.compile(r"(?m)^  (?P<row>[#.:>@a-zA-Z!?$()\[\]*^ ]+)$")


def parse_observation(text: str) -> dict:
    """Turn one status block into fields.  No game internals, just the printing."""
    observation: dict = {"targets": [], "ground": [], "map": [], "result": None, "inventory": {}}
    header = HEADER_RE.search(text)
    if header:
        observation.update({k: int(v) for k, v in header.groupdict().items()})
    vitals = VITALS_RE.search(text)
    if vitals:
        observation.update({k: int(v) for k, v in vitals.groupdict().items()})
    for match in TARGET_RE.finditer(text):
        group = match.groupdict()
        observation["targets"].append(
            {
                "name": group["name"],
                "dir": group["dir"],
                "hp": int(group["hp"]),
                "max_hp": int(group["max_hp"]),
            }
        )
    stairs = STAIRS_RE.search(text)
    if stairs:
        observation["stairs"] = stairs.group(1)
    ground = GROUND_RE.search(text)
    if ground and ground.group("ground") != "none":
        observation["ground"] = [item.strip() for item in ground.group("ground").split(",")]
    inventory = INV_RE.search(text)
    if inventory and inventory.group("inv") != "empty":
        for raw in inventory.group("inv").split(","):
            entry = raw.strip()
            if " x" in entry:
                name, _, count = entry.rpartition(" x")
                observation["inventory"][name] = int(count)
    result = RESULT_RE.search(text)
    if result:
        observation["result"] = result.group("result")
    observation["map"] = ROW_RE.findall(text)
    return observation


def decide(observation: dict, state: dict) -> str:
    """Choose the next command from the observation alone.

    This is where a model call belongs: put `observation` (or the raw block) plus
    `delve card` output in the prompt, take one line back, return it.
    """
    if observation.get("result") not in (None, "ok"):
        return "quit"

    grid = [list(row) for row in observation.get("map") or []]
    start = _find(grid, "@")
    if start is None:
        return "wait"

    # 1. something next to us: hit the weakest thing we can reach
    neighbours = {}
    for name, (dx, dy) in DIRS.items():
        x, y = start[0] + dx, start[1] + dy
        if 0 <= y < len(grid) and 0 <= x < len(grid[y]) and grid[y][x] in MONSTER_GLYPHS:
            neighbours[grid[y][x]] = name
    if neighbours:
        by_glyph_hp = {}
        for target in observation["targets"]:
            by_glyph_hp.setdefault(target["name"][0], target["hp"])
        glyph = min(neighbours, key=lambda g: by_glyph_hp.get(g, 99))
        return "move " + neighbours[glyph]

    # 2. hurt with a potion in hand
    if (
        observation.get("hp", 1) * 100 <= observation.get("max_hp", 1) * 40
        and "potion" in observation.get("inventory", {})
    ):
        state["last"] = "use potion"
        return "use potion"

    stairs = _find(grid, ">")
    reachable = _reachable(grid, start)
    frontier = [spot for spot in reachable if _is_frontier(grid, spot)]

    # 3. the relic: take it and the run is won.  Items are only drawn while they are in
    # sight, so the agent writes the position down the moment it sees it -- otherwise it
    # clears the vault floor, stands next to the relic it can no longer see, and waits
    # out the turn limit.  This is what "take notes" in the card is for.
    relic = _find(grid, "*")
    if relic:
        state["relic"] = relic
    elif state.get("relic"):
        relic = tuple(state["relic"])

    ban = OPPOSITE.get(state.get("last_dir", ""))
    if relic and tuple(relic) in reachable:
        target = tuple(relic)
    elif observation.get("stairs") == "here" and not frontier:
        # 4. standing on the stairs, with nothing left to explore: go down
        state.pop("target", None)
        state["last"] = "descend"
        return "descend"
    elif frontier:
        # 5. explore the floor before leaving it.  The first version of this agent
        # beelined for the stairs the moment they were known, arrived at the bottom
        # under-levelled, and died -- the XP and the potions are in the side rooms.
        # The target is sticky: re-deriving "the nearest unexplored edge" every turn
        # moves the target as terrain is revealed, and the agent then walks a small
        # circuit forever while believing it is making progress.
        target = state.get("target")
        if not _still_good_target(grid, target, reachable):
            target = _choose_target(grid, start, reachable, stairs)
    elif stairs and stairs in reachable:
        target = stairs
    else:
        target = None
    state["target"] = target

    if target:
        # Prefer a route that avoids the monsters we can see; fall back to walking
        # through one (which means attacking it) only when there is no way around.
        monsters = {
            (x, y)
            for y, row in enumerate(grid)
            for x, char in enumerate(row)
            if char in MONSTER_GLYPHS
        }
        step = (
            _bfs_step(grid, start, {target}, monsters, ban)
            or _bfs_step(grid, start, {target}, None, ban)
            or _bfs_step(grid, start, {target})
        )
        if step:
            return _committed(step, state, start)

    # 6. nowhere to go: any walkable move that is not a straight reversal
    for name, (dx, dy) in DIRS.items():
        nx, ny = start[0] + dx, start[1] + dy
        inside = 0 <= ny < len(grid) and 0 <= nx < len(grid[ny])
        if not inside or grid[ny][nx] in BLOCKING:
            continue
        if name != ban:
            return _committed(name, state, start)
    return "wait"


def _committed(direction: str, state: dict, start: tuple[int, int]) -> str:
    state["last"] = direction
    state["last_dir"] = direction
    return "move " + direction


def _find(grid: list[list[str]], glyph: str) -> tuple[int, int] | None:
    for y, row in enumerate(grid):
        for x, char in enumerate(row):
            if char == glyph:
                return (x, y)
    return None


def _reachable(grid: list[list[str]], start: tuple[int, int]) -> set:
    """Every walkable tile connected to `start` on the printed board."""
    seen = {start}
    queue = collections.deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in DIRS.values():
            nxt = (x + dx, y + dy)
            if nxt in seen:
                continue
            if not (0 <= nxt[1] < len(grid) and 0 <= nxt[0] < len(grid[nxt[1]])):
                continue
            if grid[nxt[1]][nxt[0]] in BLOCKING:
                continue
            seen.add(nxt)
            queue.append(nxt)
    return seen


def _still_good_target(grid: list[list[str]], target, reachable: set) -> bool:
    """A sticky target is dropped once it is reached: standing on it stops it being a
    frontier, and its neighbourhood is now revealed."""
    return bool(target) and tuple(target) in reachable and _is_frontier(grid, target)


def _choose_target(grid: list[list[str]], start: tuple[int, int], reachable: set, stairs):
    """Explore toward the stairs if they are remembered but not yet reachable,
    otherwise the nearest unexplored edge the agent can actually walk to."""
    frontier = [spot for spot in reachable if _is_frontier(grid, spot)]
    if not frontier:
        return None
    if stairs:
        return min(
            frontier,
            key=lambda spot: (
                max(abs(spot[0] - stairs[0]), abs(spot[1] - stairs[1])),
                spot[1],
                spot[0],
            ),
        )
    return _nearest_frontier(grid, start)


def _is_frontier(grid: list[list[str]], spot) -> bool:
    """Is `spot` a walkable tile that still touches never-seen space?"""
    if not spot:
        return False
    x, y = spot
    if not (0 <= y < len(grid) and 0 <= x < len(grid[y])):
        return False
    if grid[y][x] in BLOCKING:
        return False
    for dx, dy in DIRS.values():
        nx, ny = x + dx, y + dy
        inside = 0 <= ny < len(grid) and 0 <= nx < len(grid[ny])
        if inside and grid[ny][nx] == " ":
            return True
    return False


def _nearest_frontier(grid: list[list[str]], start: tuple[int, int]):
    """Nearest reachable tile that touches never-seen space, by BFS distance.

    Restricted to the region connected to the agent, so an unexplored tile behind a
    wall cannot become an unreachable target.
    """
    previous = {start: None}
    queue = collections.deque([start])
    while queue:
        node = queue.popleft()
        if node != start and _is_frontier(grid, node):
            return node
        x, y = node
        for dx, dy in DIRS.values():
            nxt = (x + dx, y + dy)
            if nxt in previous:
                continue
            if not (0 <= nxt[1] < len(grid) and 0 <= nxt[0] < len(grid[nxt[1]])):
                continue
            if grid[nxt[1]][nxt[0]] in BLOCKING:
                continue
            previous[nxt] = node
            queue.append(nxt)
    return None


def _bfs_step(
    grid: list[list[str]],
    start: tuple[int, int],
    targets: set,
    blocked: set | None = None,
    ban: str | None = None,
) -> str | None:
    """One step along the shortest seen-board path to any of `targets`.

    Only tiles already printed as walkable are used, so the agent cannot plan through
    terrain it has not seen -- unlike the game's internal bot, which is standing on the
    real map.  `blocked` tiles are routed around but never used as targets, and `ban`
    refuses the first step in one direction, which is how the caller stops a reversal.
    """
    if start in targets:
        return None
    blocked = blocked or set()
    previous = {start: None}
    queue = collections.deque([start])
    found = None
    while queue:
        node = queue.popleft()
        if node in targets and node != start:
            found = node
            break
        x, y = node
        for name, (dx, dy) in DIRS.items():
            if node == start and name == ban:
                continue
            nxt = (x + dx, y + dy)
            if nxt in previous or nxt in blocked:
                continue
            if not (0 <= nxt[1] < len(grid) and 0 <= nxt[0] < len(grid[nxt[1]])):
                continue
            if grid[nxt[1]][nxt[0]] in BLOCKING:
                continue
            previous[nxt] = node
            queue.append(nxt)
    if found is None:
        return None
    node = found
    while previous[node] is not None and previous[previous[node]] is not None:
        node = previous[node]
    delta = (node[0] - start[0], node[1] - start[1])
    for name, offset in DIRS.items():
        if offset == delta:
            return name
    return None


def run_subprocess(save: str, command: str) -> str:
    """Exactly the call a harness would make: one process, one turn."""
    result = subprocess.run(
        [sys.executable, DELVE, "play", "--save", save, "--cmd", command],
        capture_output=True,
        text=True,
        cwd=HERE,
        check=False,
    )
    return result.stdout if result.stdout.strip() else result.stderr


def run_reference_bot(seed: int, depth: int, turns: int) -> dict:
    """The game's own in-process greedy bot, for a baseline number."""
    sys.path.insert(0, HERE)
    import delve

    game = delve.new_game(seed=seed, depth=depth)
    while game.s["status"] == "ok" and game.s["turn"] < turns:
        game.step(delve.bot_command(game))
    player = game.s["player"]
    return {
        "policy": "bot",
        "result": game.s["status"],
        "seed": seed,
        "floor": game.s["depth"],
        "max_depth": game.s["max_depth"],
        "turn": game.s["turn"],
        "hp": player["hp"],
        "max_hp": player["max_hp"],
        "level": player["level"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="an example agent that plays delve from its text output")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--save", default=None, help="run file (default: temp file, removed after)")
    parser.add_argument("--policy", choices=("parse", "random", "bot"), default="parse")
    parser.add_argument("--turns", type=int, default=2000)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--trace", action="store_true", help="print every command and result")
    args = parser.parse_args(argv)

    if args.policy == "bot":
        summary = run_reference_bot(args.seed, args.depth, args.turns)
        if not args.quiet:
            print(summary)
        return 0

    save = args.save or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"delve-agent-{args.seed}-{args.policy}.json"
    )
    if os.path.exists(save):
        os.remove(save)

    new = subprocess.run(
        [sys.executable, DELVE, "new", "--seed", str(args.seed), "--depth", str(args.depth), "--save", save],
        capture_output=True,
        text=True,
        cwd=HERE,
        check=False,
    )
    observation = parse_observation(new.stdout)

    rng = random.Random(args.seed)
    state: dict = {}
    turns = 0
    while observation.get("result") == "ok" and turns < args.turns:
        command = decide(observation, state) if args.policy == "parse" else "move " + rng.choice(list(DIRS))
        text = run_subprocess(save, command)
        observation = parse_observation(text)
        turns += 1
        if args.trace:
            print(f"turn {observation.get('turn')}: {command!r} -> {observation.get('result')}")

    summary = {
        "policy": args.policy,
        "result": observation.get("result"),
        "seed": args.seed,
        "floor": observation.get("floor"),
        "max_depth": observation.get("max_depth"),
        "turn": observation.get("turn"),
        "hp": observation.get("hp"),
        "max_hp": observation.get("max_hp"),
        "level": observation.get("level"),
        "processes": turns + 1,
    }
    if not args.quiet:
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
