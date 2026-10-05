# delve

A small dungeon crawler in one Python file, designed to be played by an **AI agent**
rather than by a person at a keyboard.

Roguelike rules — generated floors, fog of war, bump-to-attack, hidden traps,
unidentified scrolls, permadeath — with an interface built for a model driving a shell:
one command per line, one stable status block back, everything reproducible from a
seed, and a save file that lets a stateless harness play one turn per process call.

```
python3 delve.py new --seed 42 --save run.json
python3 delve.py play --save run.json --cmd "se"
```

No dependencies. Python 3.10+ (developed on 3.14). One file for the game
(`delve.py`), plus a test suite, a full rulebook, and this README.

---

## Quick start

```bash
python3 delve.py card                 # the player's manual (safe to hand to the AI)
python3 delve.py new --seed 42        # start a run, prints the opening board
python3 delve.py play --cmd "e"       # one turn
python3 delve.py play                # or feed commands on stdin, one per line
python3 delve.py show                 # re-print the board, does not spend a turn
python3 delve.py autoplay --seed 3    # let the built-in bot play the whole run
python3 delve.py selftest             # internal checks
python3 delve.py rulebook             # full mechanics -- operator's copy, NOT for the player
```

Options that matter:

| flag | meaning |
|---|---|
| `--seed N` | fixes the entire run; same seed + same commands ⇒ identical output |
| `--depth N` | floors in the run (default 5). Length dial, not difficulty — see RULES.md |
| `--save PATH` | which run file to use (default `delve-save.json`) |
| `--format text\|json` | status block or one JSON object per turn |
| `--view full\|window\|none` | how much board to draw; affects rendering only, never the game |
| `--legend` | print the glyph legend after every turn |
| `--no-seal` | write the save as readable JSON (for debugging) |
| `--strict-exit` | `10` win / `11` dead / `12` stalled / `13` quit instead of `0` |
| `--log PATH` | append one JSONL record per turn, for auditing an agent's play |

## What a turn looks like

```
-- DELVE  floor 1/3   turn 1   seed 42 --
HP 26/26  ATK 4(4+0)  DEF 1(1+0)  LV 1  xp 0/10  GOLD 0
GEAR  weapon none   armor none   INV empty
MAP
               ########
                .......
                 #....#
                 #....#
             ........@#
              .........r
               ######.##..
                    #.#  ##
                    ...
                    ...
                    ...
                    ###
TARGETS  rat(se hp 4/4)
GROUND   none
STAIRS   unknown
EVENT
  - (nothing)
RESULT ok
```

Then one command, and the next block:

```
> se
-- DELVE  floor 1/3   turn 2   seed 42 --
HP 24/26  ATK 4(4+0)  DEF 1(1+0)  LV 1  xp 0/10  GOLD 0
...
TARGETS  rat(e hp 4/4)
EVENT
  - The rat hits you for 2.
RESULT ok
```

The blocks are always in that order, with the same labels, and **the last line is always
`RESULT <word>`** — so a harness can read the outcome with one regex and a model can read
the board positionally instead of guessing at prose. `--format json` carries the identical
information as data.

## Using it as a test bed for an agent

The intended loop is: **one process per turn, no hidden state in the process.**

```
new  ──► save file ──► [agent sees the block] ──► play --cmd "..." ──► updated block ──► ...
```

`agent_loop.py` is a worked example of exactly that shape: a new process per turn, and a
policy that reads nothing but the printed block (no importing the game, no opening the
save). It ships three policies worth comparing on one seed:

| policy | what it is | seeds 1..30, depth 5 |
|---|---|---|
| `parse` | a small text-only agent: parse the block, plan on the parsed board | 14 wins / 16 deaths / 0 stalls, ~370 turns |
| `bot` | the game's own greedy bot, in-process | 21 wins / 9 deaths, ~340 turns |
| `random` | the control | stalls at the turn limit on nearly every seed |

```bash
python3 agent_loop.py --seed 5 --depth 5 --policy parse
python3 agent_loop.py --seed 5 --depth 5 --policy bot
```

The gap between `parse` and `bot` is the interesting number: it is the headroom a real
model is supposed to claim, and it is mostly about *planning quality*, not about access
to information — both see only what they have walked past.

- The run lives in `--save`, so a harness that shells out per turn works, a long-lived
  process works, and stdin piping works. All three are covered by tests.
- `--seed` makes two agents comparable: hand agent A and agent B the same seed and the
  turn count, HP, gold and result are directly comparable.
- `--log` writes `{turn, floor, cmd, hp, status, events}` per turn, so you can replay or
  score an episode afterwards without scraping stdout.
- `--strict-exit` lets a shell harness branch on the outcome without parsing text.
- Cost per turn is roughly 200 tokens for the text block at the default `--view full`
  (a 40×14 board is 14 lines of 40 characters). `--view window` draws 25×11 around the
  player for about half that; `--view none` drops the board entirely and leaves the
  events, which is the right setting for a very token-frugal agent that keeps its own
  map from the event stream.

`card` is the piece of documentation meant to be handed to the player: commands, how to
read the block, the map legend, what the letters mean — and an explicit note that the
*causes* are not on the card. `RULES.md` (printed by `rulebook`) is the operator's copy
and holds the actual mechanics. Whether the player gets to read it is the operator's
call; that is the one design decision that changes the game most.

## Design notes (why it looks like this)

- **Text first, no curses, no ANSI.** A terminal UI is a bad interface for a model:
  escapes cost tokens, cursor addressing hides state, and screenshots are unparseable.
  Fixed-width, fixed-order text is cheap and unambiguous.
- **Information is honest.** The render only ever draws what the player can currently
  see or remembers seeing. A monster on a remembered `:` tile is *not* drawn. There is a
  test asserting that no unseen monster ever leaks into the map.
- **Free reading, costly acting.** `map`, `status`, `inv`, `legend`, `help` cost nothing,
  so there is no incentive to avoid looking around. Every other command costs a turn,
  **including a typo** — the pressure on a sloppy agent is the turn counter, not a
  hidden rule.
- **Unidentified scrolls.** The scroll's kind is rolled once per run from the seed and is
  the same for every scroll in that run. Reading one identifies all of them, forever.
  That turns a "use item" into a real information decision, and it is the mechanic the
  built-in bot is worst at.
- **Determinism is a feature, not a nicety.** It makes the game testable, makes save/load
  exact, and makes two agents comparable on one seed.
- **The save file is obfuscated, not encrypted**, and says so in the source. It stops a
  player from casually reading the floor layout or the scroll kind out of the save. It is
  not security, and the honest fix for a determined player is to run the game somewhere
  the player cannot read. Same for `delve.py` itself: an agent with repo access can read
  the source and learn the mechanics. Assume the player knows how to read code and design
  around it anyway.

## Files

| file | what it is |
|---|---|
| `delve.py` | the whole game: generator, FOV, monsters, items, the CLI, the baseline bot, the self test |
| `test_delve.py` | pytest suite (117 tests) covering the output contract, determinism, save/load, information honesty, mechanics and the CLI |
| `RULES.md` | full mechanics — for the operator, printable with `delve rulebook` |
| `pyproject.toml` | ruff + pytest configuration |

## Testing

```bash
python3 delve.py selftest                 # built in, no dependencies
pytest -q                                 # the full suite
ruff check delve.py test_delve.py         # lint
```

The self test covers floor connectivity, determinism, sealed and plain save round trips,
garbage commands, free commands, the no-leak property, reachable win and death, and that
the baseline bot wins at least one seed. The pytest suite adds the CLI surface (JSON mode,
stdin piping, atomic saves, strict exits, log records) and the balance-independent
mechanics.

## Balance snapshot

The built-in greedy bot (`autoplay`), seeds 1..30, on this machine:

| depth | wins | deaths | stalls | turns (avg) |
|---|---|---|---|---|
| 3 | 18/30 | 12 | 0 | 196 |
| 5 (default) | 21/30 | 9 | 0 | 336 |
| 8 | 21/30 | 9 | 0 | 554 |

Roughly 70% for a bot that never uses `sense`, never identifies scrolls deliberately and
never retreats. A competent model should beat that comfortably, which is the intended
shape: the interesting cost is the turns it spends, not whether it can win at all.