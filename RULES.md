# DELVE — full rulebook

This file is the **operator's** copy of the rules. It is deliberately separate from
the player card (`delve card`), because the card is the *player's* manual and the
interesting part of the game is a player that has to infer mechanics from what the
dungeon actually does.

Print this with `delve rulebook`. Hand the card to the AI. Keep this one.

## The run

- A run is `--depth` floors (default 5, allowed 2..12). You start on floor 1 at level 1.
- **Floor `N` = max depth is the vault floor.** It has no stairs. It contains the
  guardian `G` and, once the guardian dies, the relic `*`.
- **Win:** kill the guardian and step onto the relic. **Lose:** HP reaches 0.
  **Stalled:** the turn counter hits `TURN_LIMIT` (2000) — the run is called off.
- Only descending (and the teleport scroll) change floors. You can never go back up.

`--depth` is a **length** dial, not a difficulty dial: the guardian's stats are scaled
to the run length (`hp = 20 + 3*depth`, `atk = 6 + depth//5`, `def = 3 + depth//6`)
because a longer run also means more XP before the boss. Measured with the built-in
bot: 18/30 wins at depth 3 and 21/30 wins at depth 8 — a flat ~70% either way, at
~200 turns versus ~670 turns. Verified in `delve selftest` and in the test suite.

## Turn economy

Exactly one command per line, one status block back. Every command spends one turn
**except** the free information commands:

| free | effect |
|---|---|
| `map`, `status` | re-print the board / vitals |
| `inv`, `inventory` | equipped gear + carried items |
| `legend` | map glyph key |
| `help`, `?`, `card` | the player card |
| blank line | ignored, not a turn |

Everything else — including a typo like `move sideways` or `zzz` — **costs a turn**.
That is intentional: `EVENT` will literally say `?? unknown command: zzz  (the turn is
spent anyway)`. A player that spams nonsense loses to the clock and to wandering
monsters. This is the main pressure on a sloppy agent.

`--cmd 's'` runs exactly one command and exits, so a stateless harness can drive the
game one turn per process. State lives in the save file, not the process.

## Attributes

```
HP   hit points. 0 = dead.
ATK  damage you deal.  displayed as ATK total(base+gear)
DEF  damage you absorb. displayed as DEF total(base+gear)
LV   level.  xp_next = 10 + 6*(LV-1)
GOLD currency. Currently only a score.
```

Starting stats: 26 HP, 4 ATK, 1 DEF, level 1.

### Damage formula

```
damage = max(1, attacker_atk + randint(0,2) - defender_def)
```

Both directions use the same formula. A high-DEF player takes 1 damage from a rat,
which is a real strategy and not a bug. Armour matters a lot; it is the single
biggest lever on survival.

### Levelling

Each monster is worth its `xp`. On level up: **+4 max HP, +4 current HP, +1 ATK**,
and **+1 DEF on every even level**. Gear bonuses stack on top of base stats and are
never lost.

### Regeneration

**Every 14th turn** (turn 14, 28, 42, …) you regain **1 HP** if wounded. Slow by
design: it rewards not being in a fight rather than turtling mid-fight, because the
monster keeps swinging while you wait.

## Map and visibility

- Board is 40×14, drawn 2 spaces in, rows top-to-bottom, columns west-to-east.
- **FOV radius is 8 tiles, Euclidean, with line-of-sight through Bresenham lines.**
  Walls block sight and therefore hide whatever is behind them. Rooms are rectangles
  joined by L-shaped corridors, so corners genuinely block.
- **Remembered terrain is drawn as `:`** once seen and left. So `.` = in sight right
  now, `:` = remembered, ` ` = never seen.
- **Monsters and items are only drawn while actually in sight.** A remembered tile
  does not remember a monster. This is why the card says the map lies to you later:
  re-looking at a `:` tile tells you nothing about what is there now.
- Known traps are drawn as `^` and stay known forever after they fire.

## Directions

`n s e w ne nw se sw`. `move <dir>` / `go <dir>` / `walk <dir>` are synonyms, and
full words work: `move north`, `move northeast`.

Walking **into** a monster attacks it. `attack <dir>` swings even at empty air (wasted
turn, but it is a legal move). Walking into a wall is blocked and costs the turn.

Standing next to a monster and pressing `n`/`s`/etc. in its direction attacks it —
there is no separate close-combat verb beyond this.

## Monsters

| glyph | name | HP | ATK | DEF | xp | floors |
|---|---|---|---|---|---|---|
| `r` | rat | 4 | 2 | 0 | 3 | 1+ |
| `b` | bat | 3 | 2 | 0 | 4 | 1–3 |
| `j` | slime | 9 | 3 | 1 | 7 | 2–5 |
| `s` | skeleton | 11 | 4 | 2 | 9 | 2+ |
| `w` | wraith | 15 | 6 | 3 | 16 | 3+ |
| `G` | guardian | scaled | scaled | scaled | 60 | vault |

Population is `3 + depth` per floor, drawn from the species whose floor range covers
the depth. The guardian appears only on the vault floor, plus 2 normal guards.

### Behaviour

- **Idle** until `chebyshev_distance <= 7` **and** line of sight. Being seen is what
  wakes a monster; standing still behind a wall is safe.
- Once aware, a monster steps toward you using **8-way movement through walkable
  tiles it can see around**, and attacks if it ends up adjacent.
- Monsters do not path through each other or through you.
- **Bats are erratic**: 50% of their moves are a random walk instead of a pursuit.
  Their chase is therefore not reliably predictable — do not assume a bat will
  come to you on the line you calculated.
- Monsters never flee, never open doors (there are none), and never regenerate.

## Items

| glyph | item | effect |
|---|---|---|
| `!` | potion | carried; `use potion` heals `max(6, 2/5 of max HP)`. Not consumed on pickup. |
| `?` | scroll | carried; one of four kinds, **unidentified until read** |
| `$` | gold | picked up on step, score only |
| `)` | weapon | **auto-equipped on step if strictly better** |
| `[` | armor | same for DEF |

Gear is auto-compared on step: walking onto a weapon/armour with a strictly higher
bonus equips it and the event line says so; otherwise the event says it is no better
and **the item is consumed/left behind** — it does not linger to be re-evaluated, so
a player cannot farm the same tile for decisions. Gear tables:

| weapon | atk | floors | | armor | def | floors |
|---|---|---|---|---|---|---|
| dagger | 1 | 1–2 | | leather | 1 | 1–3 |
| sword | 2 | 2–4 | | chain | 2 | 2+ |
| axe | 3 | 3+ | | plate | 3 | 4+ |
| rune blade | 4 | 4+ | | | | |

### Scrolls (the interesting ones)

The scroll's kind is rolled **once at run creation** from `random.Random(seed)` and is
then fixed for the whole run — every scroll you find is the same kind, and reading one
identifies all of them retroactively. It is hidden from the player until the first
read. The four kinds:

| kind | effect |
|---|---|
| `mapping` | reveals the **entire floor**, walls and all, permanently |
| `teleport` | moves you to a random walkable tile with no monster on it |
| `blessing` | permanent **+1 base ATK** |
| `ward` | permanent **+1 base DEF** |

This is the purest information problem in the game: the first scroll is a gamble.
`blessing`/`ward` are permanent stats, so **reading a scroll you cannot afford to
waste is a genuine decision**, and a player who tracks "scroll read on turn N gave
X" can infer what all future scrolls in this run will do.

Kinds are not distributed evenly across seeds — that is the point. A player who
assumes scroll #1 is always mapping will eventually be wrong.

## Traps

- 1–3 per normal floor, 0–1 on the vault floor, never on the starting tile.
- **Hidden until triggered.** Stepping on one reveals it, marks it `^` permanently,
  and deals `3 + depth` damage. It does not fire twice.
- Known traps are still walkable — they are just no longer a surprise.
- **`sense`** is a real action: it spends a turn and reveals any hidden trap within
  Chebyshev distance 2. It is the only way to find traps other than eating them.
- Traps are placed on tiles at least 4 steps from the start, so the opening is safe.

## Rendering contract

Text mode is **exact and stable**, in this order, every turn:

```
-- DELVE  floor N/M   turn T   seed S --
HP h/max  ATK t(b+g)  DEF t(b+g)  LV L  xp x/xn  GOLD g
GEAR  weapon X   armor Y   INV item xN, ...
MAP
  <40 columns, 2-space indent>
TARGETS  name(dir hp h/m)  ...   |  none
GROUND   gold(7), potion, ...    |  none
STAIRS   here | seen | unknown | none
EVENT
  - one line per thing that happened this turn
RESULT ok | win | dead | stalled | quit
```

- **The last line of a turn is always `RESULT <status>`**, so a harness can regex the
  outcome without parsing anything else. With `--legend` the legend line comes after
  `RESULT`; with `--strict-exit` the process exit code mirrors it
  (`10` win / `11` dead / `12` stalled / `13` quit, else `0`).
- `TARGETS` lists only monsters **currently visible**, with direction and exact HP —
  exact HP is deliberate: it lets an agent compute how many swings a kill takes.
- `STAIRS` is one word: `here` (you are on them), `seen` (they are inside your
  remembered map), `unknown` (not located yet), `none` (vault floor).
- `--format json` emits the identical information as one JSON object per turn
  (keys: `result, seed, floor, max_depth, turn, player{...}, map{rows[]}, legend,
  targets[], ground[], stairs, events[]`).

`--view full|window|none` controls how much board is drawn (`window` = 25×11 around
the player, `none` = suppressed). It changes rendering only — **never** the simulation.

## Reproducibility

- Everything is derived from `--seed`. The RNG state itself is carried in the save,
  so a run is reproducible **across processes and after save/load**, not just at
  generation time.
- Same seed + same command sequence ⇒ byte-identical output. This is asserted in the
  self test and is what makes the game testable and fair to compare agents on.
- `delve new --seed 42 --save a.json` then playing `a.json` is the standard way to
  hand two different agents the identical dungeon.

## Save file

- Default `delve-save.json`. Written atomically (temp file + rename).
- **Sealed by default**: base64 of the JSON XOR'd with a fixed key. The comment in the
  source says out loud that this is **obfuscation, not encryption** — it stops a player
  from casually `cat`-ing the save to read the scroll kind or the floor layout, without
  pretending to be security. `--no-seal` writes readable JSON for debugging and is the
  right choice when *you* are the one testing.
- `--save <path>` per invocation, so one directory can hold many parallel runs.

## Baseline bot

`delve autoplay` runs a built-in greedy bot (no LLM). It exists to (a) demo the game,
(b) prove the game is winnable without a model, and (c) give you a baseline number to
compare an AI player against. Its policy, in priority order:

1. `use potion` if HP is at/below 40% of max, a potion is held, and nothing is adjacent
2. attack the adjacent monster with the lowest current HP
3. if a monster is visible and HP > 35%: path to it
4. if the relic is on this floor: path to it
5. path to the nearest unexplored edge of the map
6. path to the nearest visible item
7. `descend` if standing on stairs; else path to known stairs
8. `wait`

It refuses to step straight back where it came from, and it will not walk onto a tile
it stood on in the last 8 moves, unless nothing else is possible. Both guards are
needed: its goal is "the nearest unexplored edge", that goal moves as terrain is
revealed, and without the guards it locks into short cycles. Four runs out of the first
30 stalled at the turn limit that way, including one that orbited a four-tile loop
right next to the stairs it was looking for.

**Measured on this machine, seeds 1..30 (the built-in bot):**

| depth | wins | deaths | stalls | turns (avg / median) |
|---|---|---|---|---|
| 3 | 18/30 | 12 | 0 | 196 / 203 |
| 4 | 22/30 | 8 | 0 | 273 / 285 |
| 5 (default) | 21/30 | 9 | 0 | 336 / 373 |
| 6 | 21/30 | 9 | 0 | 407 / 447 |
| 8 | 21/30 | 9 | 0 | 554 / 669 |

A flat ~70% win rate across every length, with depth buying turns rather than
difficulty — which is the property that makes the seed a fair comparison between two
different agents.

The bot is a demo, not a good player. A competent model should beat it: the bot ignores
scroll identification, never uses `sense`, never retreats to regenerate, refuses to
route around known danger, and does not fight tactically. It exists to prove the
dungeon is winnable without a language model and to give you a baseline number.

For comparison, `agent_loop.py --policy parse` is an example player that is allowed to
see *only* the printed status block (it is not allowed to import the game): 14 wins, 16
deaths, no stalls over the same seeds, at ~370 turns. Roughly two thirds of the bot's
win rate, using strictly less information -- which is the number a real model is
expected to beat.

## Verification

`delve selftest` runs the internal checks, including:

- every generated floor is fully connected and the stairs reachable; no entity is
  placed inside a wall or on the start tile
- the same seed reproduces the same board, byte for byte
- save → load → identical continuation, sealed and unsealed
- garbage commands neither crash nor skip a turn
- free commands really are free
- **an unseen monster is never drawn** (no information leak through the map)
- death is reachable, and a kill + relic step produces `win`
- the baseline bot wins at least one seed, never returns an unknown command, and
  does not step straight back and forth