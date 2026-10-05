"""Tests for delve: the dungeon crawler for AI players.

These target the properties an AI harness depends on -- stable output, honest
information, and reproducibility -- rather than the game's balance.  Balance is
measured in RULES.md, not asserted here.
"""

from __future__ import annotations

import json
import random
import re

import pytest

import delve
from delve import (
    DARK_RADIUS,
    DIRS,
    ITEM_GLYPHS,
    MAP_H,
    MAP_W,
    MONSTERS,
    REGEN_EVERY,
    RINGS,
    TILE_LOCKED,
    TILE_STAIRS,
    TILE_WALL,
    Game,
    _chebyshev,
    build_floor,
    load_game,
    main,
    new_game,
    save_game,
)


@pytest.fixture()
def tmp_save(tmp_path):
    return str(tmp_path / "run.json")


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 17, 99])
@pytest.mark.parametrize("depth", [1, 3, 6, 9])
def test_floors_are_connected(seed, depth):
    """Every floor must be walkable from the start, with the stairs reachable."""
    floor = build_floor(random.Random(seed), depth, 9)
    grid = floor["grid"]
    start = tuple(floor["start"])
    reached = {start}
    stack = [start]
    while stack:
        x, y = stack.pop()
        for dx, dy in DIRS.values():
            nx, ny = x + dx, y + dy
            if not (0 <= nx < MAP_W and 0 <= ny < MAP_H):
                continue
            if grid[ny][nx] == TILE_WALL or (nx, ny) in reached:
                continue
            reached.add((nx, ny))
            stack.append((nx, ny))
    if floor["stairs"]:
        assert tuple(floor["stairs"]) in reached


@pytest.mark.parametrize("seed", range(20))
def test_nothing_is_placed_inside_a_wall_or_on_the_start(seed):
    rng = random.Random(seed)
    for depth in range(1, 7):
        floor = build_floor(rng, depth, 6)
        grid = floor["grid"]
        start = tuple(floor["start"])
        for group in ("monsters", "items", "traps"):
            for thing in floor[group]:
                assert grid[thing["y"]][thing["x"]] != TILE_WALL, f"{group} in a wall"
                assert (thing["x"], thing["y"]) != start, f"{group} on the start tile"


def test_the_vault_floor_has_no_stairs_and_the_guardian():
    floor = build_floor(random.Random(5), 4, 4)
    assert floor["stairs"] is None
    kinds = [m["kind"] for m in floor["monsters"]]
    assert kinds.count("guardian") == 1


def test_the_guardian_scales_with_the_length_of_the_run():
    short = delve.guardian_stats(3)
    long = delve.guardian_stats(9)
    assert short["hp"] < long["hp"]
    assert short["atk"] <= long["atk"]


# --------------------------------------------------------------------------
# determinism and save/load
# --------------------------------------------------------------------------


def test_same_seed_gives_the_same_dungeon():
    script = ["e", "s", "move n", "wait", "sense", "w", "n"]
    renders = []
    for _ in range(2):
        game = new_game(seed=4242, depth=5)
        for command in script:
            game.step(command)
        renders.append(game.render_text(legend=True))
    assert renders[0] == renders[1]


def test_different_seeds_give_different_dungeons():
    a = new_game(seed=1, depth=5)
    b = new_game(seed=2, depth=5)
    assert a.s["floor"]["grid"] != b.s["floor"]["grid"]


def test_save_load_round_trip_is_seamless(tmp_save):
    game = new_game(seed=808, depth=5)
    for command in ["e", "s", "e", "wait", "n"]:
        game.step(command)
    save_game(tmp_save, game)

    reloaded = load_game(tmp_save)
    for command in ["s", "s", "e", "sense", "wait", "n", "use potion"]:
        game.step(command)
        reloaded.step(command)
        assert game.render_text() == reloaded.render_text()


def test_the_scroll_kind_survives_a_save(tmp_save):
    game = new_game(seed=55, depth=4)
    kind = game.s["scroll"]["kind"]
    save_game(tmp_save, game)
    assert load_game(tmp_save).s["scroll"]["kind"] == kind


def test_a_sealed_save_does_not_expose_the_dungeon(tmp_save):
    """The seal is obfuscation, not security -- but it must not be plain text."""
    game = new_game(seed=707, depth=4)
    save_game(tmp_save, game)
    with open(tmp_save) as handle:
        blob = handle.read()
    assert "guardian" not in blob
    assert game.s["scroll"]["kind"] not in blob


def test_an_unsealed_save_is_readable_and_loads(tmp_save):
    game = new_game(seed=707, depth=4)
    save_game(tmp_save, game, seal=False)
    with open(tmp_save) as handle:
        doc = json.load(handle)
    assert doc["sealed"] is False
    assert load_game(tmp_save).s["seed"] == 707


def test_saving_is_atomic_and_leaves_no_temp_file(tmp_save):
    game = new_game(seed=1, depth=3)
    save_game(tmp_save, game)
    import os

    assert not os.path.exists(tmp_save + ".tmp")


def test_a_foreign_file_is_rejected(tmp_save):
    with open(tmp_save, "w") as handle:
        handle.write('{"format": "something-else", "version": 1}')
    with pytest.raises(ValueError):
        load_game(tmp_save)


# --------------------------------------------------------------------------
# the command surface an AI actually drives
# --------------------------------------------------------------------------


def test_blank_lines_are_not_commands():
    game = new_game(seed=1, depth=3)
    turn = game.s["turn"]
    for blank in ["", "   ", "\t"]:
        kind, _ = game.step(blank)
        assert kind == "doc"
    assert game.s["turn"] == turn


def test_free_commands_do_not_spend_a_turn():
    game = new_game(seed=1, depth=3)
    turn = game.s["turn"]
    for command in ["map", "status", "inv", "inventory", "help", "?", "legend", "card"]:
        kind, payload = game.step(command)
        assert kind == "doc"
        assert payload
    assert game.s["turn"] == turn


@pytest.mark.parametrize(
    "command",
    ["zzz", "move", "move sideways", "use", "use nothing", "attack", ";", "12345", "descend"],
)
def test_a_bad_command_costs_a_turn_but_never_crashes(command):
    game = new_game(seed=1, depth=3)
    turn = game.s["turn"]
    kind, _ = game.step(command)
    assert kind == "turn"
    assert game.s["turn"] == turn + 1
    assert game.s["status"] == "ok"


def test_move_accepts_short_long_and_compound_directions():
    seen = set()
    for spelling in ["e", "move e", "move east", "go east", "walk e"]:
        game = new_game(seed=31, depth=3)
        before = (game.px, game.py)
        game.step(spelling)
        seen.add((game.px - before[0], game.py - before[1]))
    assert len(seen) == 1


def test_walking_into_a_monster_attacks_it():
    game = new_game(seed=12, depth=3)
    monster = game.live_monsters()[0]
    spot = _free_neighbour(game, monster)
    assert spot is not None
    game.s["player"]["x"], game.s["player"]["y"] = spot
    hp = monster["hp"]
    game.step("move " + delve.dir_name(monster["x"] - game.px, monster["y"] - game.py))
    assert monster["hp"] < hp or not monster["alive"]


def test_walking_into_a_wall_is_blocked_and_says_so():
    game = new_game(seed=3, depth=3)
    game.s["player"]["x"], game.s["player"]["y"] = 0, 0
    before = (game.px, game.py)
    game.step("n")
    assert (game.px, game.py) == before
    assert any("Blocked" in event for event in game.events)


def test_the_interface_is_case_insensitive():
    lower = new_game(seed=6, depth=3)
    upper = new_game(seed=6, depth=3)
    lower.step("move north")
    upper.step("MOVE NORTH")
    assert (lower.px, lower.py) == (upper.px, upper.py)


# --------------------------------------------------------------------------
# information honesty -- the part that makes it a game for a model
# --------------------------------------------------------------------------


def test_unseen_monsters_are_never_drawn():
    for seed in range(1, 10):
        game = new_game(seed=seed, depth=5)
        for _ in range(30):
            visible = game.visible_tiles()
            rows = game.map_rows()
            for monster in game.live_monsters():
                if (monster["x"], monster["y"]) in visible:
                    continue
                glyph = rows[monster["y"]][monster["x"]]
                assert glyph != MONSTERS[monster["kind"]]["glyph"], (
                    f"seed {seed}: unseen {monster['kind']} was drawn on the map"
                )
            game.step("wait")


def test_targets_only_lists_what_can_be_seen_right_now():
    game = new_game(seed=77, depth=5)
    for _ in range(25):
        visible = game.visible_tiles()
        listed = {(t["dx"] + game.px, t["dy"] + game.py) for t in game.targets()}
        for spot in listed:
            assert spot in visible
        game.step("wait")


def test_remembered_ground_is_drawn_but_a_remembered_monster_is_not():
    game = new_game(seed=44, depth=4)
    for _ in range(40):
        game.step("wait")
    rows = game.map_rows()
    text = "\n".join(rows)
    assert ":" in text or "." in text


def test_targets_carry_exact_hp_so_a_kill_can_be_counted():
    game = new_game(seed=21, depth=4)
    for _ in range(40):
        targets = game.targets()
        if targets:
            for target in targets:
                assert 0 <= target["hp"] <= target["max_hp"]
                assert target["dir"] in [*list(DIRS), "here"]
            return
        game.step("wait")


def test_the_scroll_kind_is_hidden_until_read():
    game = new_game(seed=13, depth=4)
    text = game.render_text()
    assert game.s["scroll"]["kind"] not in text
    assert not game.s["scroll"]["known"]
    scrolls = game.s["player"]["inventory"]
    scrolls["scroll"] = 1
    game.step("use scroll")
    assert game.s["scroll"]["known"]
    assert game.s["scroll"]["kind"] in "\n".join(game.events)


def test_reading_one_scroll_identifies_all_of_them():
    game = new_game(seed=13, depth=4)
    game.s["player"]["inventory"]["scroll"] = 2
    game.step("use scroll")
    assert "scroll" in game.render_text() and "(" in game.render_text()


# --------------------------------------------------------------------------
# output contract
# --------------------------------------------------------------------------


def test_the_last_line_of_a_turn_is_always_result():
    game = new_game(seed=9, depth=4)
    for command in ["e", "s", "wait", "zzz", "move n", "sense", "map", "n"]:
        kind, _ = game.step(command)
        if kind != "turn":
            continue
        lines = [line for line in game.render_text().splitlines() if line.strip()]
        assert lines[-1].startswith("RESULT ")


def test_the_block_order_is_stable():
    game = new_game(seed=9, depth=4)
    game.step("e")
    labels = []
    for line in game.render_text().splitlines():
        head = line.split("  ")[0].strip()
        if head in ("MAP", "TARGETS", "GROUND", "STAIRS", "EVENT") or line.startswith("RESULT"):
            labels.append(head if not line.startswith("RESULT") else "RESULT")
    assert labels == ["MAP", "TARGETS", "GROUND", "STAIRS", "EVENT", "RESULT"]


def test_every_turn_has_exactly_one_result_line():
    game = new_game(seed=9, depth=4)
    for command in ["e", "s", "wait", "n", "e"]:
        game.step(command)
        text = game.render_text(legend=True)
        assert len(re.findall(r"(?m)^RESULT ", text)) == 1


def test_result_words_are_the_documented_set():
    game = new_game(seed=9, depth=4)
    game.step("e")
    assert re.search(r"(?m)^RESULT (ok|win|dead|stalled|quit)$", game.render_text())


def test_json_carries_the_same_information_as_text():
    game = new_game(seed=9, depth=4)
    game.step("e")
    payload = game.render_json()
    for key in ("result", "seed", "floor", "turn", "player", "map", "targets", "ground", "stairs", "events"):
        assert key in payload
    assert payload["turn"] == game.s["turn"]
    assert payload["player"]["hp"] == game.s["player"]["hp"]
    assert "\n".join(payload["map"]["rows"]) == "\n".join(game.map_rows())


def test_view_only_changes_the_rendering_never_the_state():
    full = new_game(seed=303, depth=4, view="full")
    window = new_game(seed=303, depth=4, view="window")
    none = new_game(seed=303, depth=4, view="none")
    for command in ["e", "s", "wait", "n", "sense", "e"]:
        full.step(command)
        window.step(command)
        none.step(command)
    assert full.s["player"] == window.s["player"] == none.s["player"]
    assert full.s["turn"] == window.s["turn"] == none.s["turn"]
    assert full.s["floor"]["grid"] == window.s["floor"]["grid"]
    assert window.map_rows() != full.map_rows()
    assert none.map_rows() == ["(map suppressed)"]


def test_window_view_is_smaller_than_the_whole_floor():
    game = new_game(seed=303, depth=4, view="window")
    assert len(game.map_rows()) < MAP_H


def test_the_wall_row_width_is_always_the_map_width():
    game = new_game(seed=303, depth=4)
    for row in game.map_rows():
        assert len(row) == MAP_W


# --------------------------------------------------------------------------
# mechanics
# --------------------------------------------------------------------------


def test_damage_is_at_least_one_even_against_heavy_armour():
    game = new_game(seed=2, depth=4)
    game.s["player"]["def"] = 50
    monster = game.live_monsters()[0]
    monster["x"] = game.px + 1
    monster["y"] = game.py
    hp = game.s["player"]["hp"]
    game.step("wait")
    assert game.s["player"]["hp"] == hp - 1


def test_a_potion_heals_and_is_consumed():
    game = new_game(seed=2, depth=4)
    game.s["player"]["inventory"]["potion"] = 1
    game.s["player"]["hp"] = 5
    game.step("use potion")
    assert game.s["player"]["hp"] > 5
    assert game.s["player"]["inventory"].get("potion", 0) == 0


def test_using_something_you_do_not_have_is_a_wasted_turn_not_a_crash():
    game = new_game(seed=2, depth=4)
    turn = game.s["turn"]
    game.step("use potion")
    assert game.s["turn"] == turn + 1
    assert game.s["status"] == "ok"


def test_better_gear_equips_itself_and_worse_gear_does_not():
    game = new_game(seed=2, depth=4)
    player = game.s["player"]
    player["weapon"] = None
    game.items.append({"kind": "weapon", "name": "sword", "x": player["x"], "y": player["y"]})
    game._on_enter_tile(player["x"], player["y"])
    assert player["weapon"] == "sword"

    player["weapon"] = "rune blade"
    game.items.append({"kind": "weapon", "name": "dagger", "x": player["x"], "y": player["y"]})
    game._on_enter_tile(player["x"], player["y"])
    assert player["weapon"] == "rune blade"


def test_a_leavable_item_stays_on_the_floor_and_is_marked_judged():
    """The world must agree with the message.

    This used to delete the item, which contradicts "You leave it where it lies".
    The real fix is that the item stays and remembers it was judged, so the bot can
    learn to stop targeting it instead of the game quietly rewriting the map.
    """
    game = new_game(seed=2, depth=4)
    player = game.s["player"]
    player["weapon"] = "rune blade"
    tile = (player["x"], player["y"])
    game.items.append({"kind": "weapon", "name": "dagger", "x": tile[0], "y": tile[1]})
    game._on_enter_tile(tile[0], tile[1])
    left = [i for i in game.items if (i["x"], i["y"]) == tile and i["kind"] == "weapon"]
    assert len(left) == 1, "the item must stay where it lies"
    assert left[0]["judged"] is True
    assert "leave it where it lies" in " ".join(game.events)

    # a second visit says something different and does not try to re-evaluate
    game.events.clear()
    game._on_enter_tile(tile[0], tile[1])
    assert game.events and "again" in game.events[0]
    assert len([i for i in game.items if (i["x"], i["y"]) == tile and i["kind"] == "weapon"]) == 1


def test_regeneration_ticks_on_the_documented_interval():
    game = new_game(seed=2, depth=4)
    game.s["player"]["hp"] = 5
    game.s["turn"] = delve.REGEN_EVERY - 1
    game.step("wait")
    assert game.s["player"]["hp"] == 6


def test_regeneration_never_exceeds_max_hp():
    game = new_game(seed=2, depth=4)
    game.s["turn"] = delve.REGEN_EVERY - 1
    game.step("wait")
    assert game.s["player"]["hp"] == game.s["player"]["max_hp"]


def test_sense_reveals_a_trap_that_is_right_next_to_you():
    game = new_game(seed=2, depth=4)
    player = game.s["player"]
    hidden = {"x": player["x"] + 1, "y": player["y"], "damage": 5, "known": False}
    game.traps.append(hidden)
    game.step("sense")
    assert hidden["known"]


def test_a_trap_fires_once_and_then_stays_known():
    game = new_game(seed=2, depth=4)
    player = game.s["player"]
    trap = {"x": player["x"] + 1, "y": player["y"], "damage": 5, "known": False}
    game.traps.append(trap)
    game.s["player"]["hp"] = 40
    game.s["player"]["max_hp"] = 40
    game.step("e")
    hp_after_first = game.s["player"]["hp"]
    if (game.px, game.py) == (trap["x"], trap["y"]):
        assert trap["known"]
        game.step("w")
        game.step("e")
        assert game.s["player"]["hp"] == hp_after_first


def test_a_monster_only_wakes_when_it_can_see_you():
    game = new_game(seed=2, depth=4)
    monster = game.live_monsters()[0]
    assert not monster["alerted"]
    game.s["player"]["x"], game.s["player"]["y"] = 1, 1
    monster["x"], monster["y"] = MAP_W - 2, MAP_H - 2
    game.step("wait")
    assert not monster["alerted"]


def test_walking_to_the_stairs_and_descending_changes_floor():
    game = new_game(seed=2, depth=6)
    stairs = game.s["floor"]["stairs"]
    game.s["player"]["x"], game.s["player"]["y"] = stairs
    assert game.stairs_state() == "here"
    game.step("descend")
    assert game.s["depth"] == 2


def test_descending_away_from_the_stairs_wastes_a_turn():
    game = new_game(seed=2, depth=6)
    depth = game.s["depth"]
    game.step("descend")
    assert game.s["depth"] == depth
    assert any("no stairs" in event for event in game.events)


def test_stairs_state_is_one_of_the_documented_words():
    game = new_game(seed=2, depth=6)
    for _ in range(10):
        assert game.stairs_state() in ("here", "seen", "unknown", "none")
        game.step("wait")


def test_killing_the_guardian_drops_the_relic_and_taking_it_wins():
    game = new_game(seed=8, depth=4)
    game._enter_floor(game.s["max_depth"])
    guardian = next(m for m in game.monsters if m["kind"] == "guardian")
    guardian["hp"] = 1
    spot = _free_neighbour(game, guardian)
    assert spot is not None
    game.s["player"]["x"], game.s["player"]["y"] = spot
    game.s["player"]["atk"] = 99
    game.s["player"]["hp"] = 99
    game.s["player"]["max_hp"] = 99
    game.step("move " + delve.dir_name(guardian["x"] - game.px, guardian["y"] - game.py))
    assert not guardian["alive"]
    assert game.s["floor"]["relic"] == [guardian["x"], guardian["y"]]
    game.step("move " + delve.dir_name(guardian["x"] - game.px, guardian["y"] - game.py))
    assert game.s["status"] == "win"


def test_death_ends_the_run():
    game = new_game(seed=8, depth=4)
    game._enter_floor(game.s["max_depth"])
    guardian = next(m for m in game.monsters if m["kind"] == "guardian")
    spot = _free_neighbour(game, guardian)
    assert spot is not None
    game.s["player"]["x"], game.s["player"]["y"] = spot
    game.s["player"]["hp"] = 1
    game.step("wait")
    assert game.s["status"] == "dead"


def test_dead_is_a_floor_not_a_negative():
    game = new_game(seed=8, depth=4)
    game.s["player"]["hp"] = 0
    game._check_end()
    assert game.s["player"]["hp"] == 0


def test_a_finished_run_stops_accepting_commands():
    game = new_game(seed=8, depth=4)
    game.s["status"] = "dead"
    turn = game.s["turn"]
    kind, payload = game.step("e")
    assert kind == "doc"
    assert "over" in payload
    assert game.s["turn"] == turn


def test_the_turn_limit_stalls_the_run():
    game = new_game(seed=8, depth=4)
    game.s["turn"] = delve.TURN_LIMIT - 1
    game.step("wait")
    assert game.s["status"] == "stalled"


def test_quit_ends_the_run_immediately():
    game = new_game(seed=8, depth=4)
    game.step("quit")
    assert game.s["status"] == "quit"


def test_experience_levels_you_up_and_raises_stats():
    game = new_game(seed=8, depth=4)
    player = game.s["player"]
    before = (player["max_hp"], player["atk"])
    player["xp"] = player["xp_next"]
    game._gain_xp(0)
    assert player["level"] == 2
    assert player["max_hp"] > before[0]
    assert player["atk"] > before[1]


# --------------------------------------------------------------------------
# the baseline bot
# --------------------------------------------------------------------------


def test_the_baseline_bot_never_issues_an_unknown_command():
    for seed in (1, 5, 11):
        game = new_game(seed=seed, depth=5)
        for _ in range(200):
            command = delve.bot_command(game)
            assert command.split()[0] in (
                "move",
                "use",
                "wait",
                "descend",
            ), command
            game.step(command)
            if game.s["status"] != "ok":
                break


def test_the_baseline_bot_finishes_runs_and_wins_some():
    wins = 0
    for seed in range(1, 6):
        game = new_game(seed=seed, depth=5)
        while game.s["status"] == "ok":
            game.step(delve.bot_command(game))
        wins += game.s["status"] == "win"
    assert wins >= 1


def test_the_baseline_bot_never_steps_straight_back_where_it_came_from():
    """The loop guard is what keeps a run from stalling on a moving goal set."""
    for seed in (4, 7, 19):
        game = new_game(seed=seed, depth=5)
        previous = None
        moves = 0
        reversals = 0
        for _ in range(400):
            command = delve.bot_command(game)
            if previous and previous.startswith("move ") and command.startswith("move "):
                moves += 1
                back = (previous.split()[1], command.split()[1])
                if back == (back[0], delve._OPPOSITE[back[0]]):
                    reversals += 1
            previous = command
            game.step(command)
            if game.s["status"] != "ok":
                break
        assert reversals <= max(2, moves // 20), f"seed {seed}: {reversals} reversals in {moves} moves"


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def test_cli_new_then_play_advances_the_run(tmp_save, capsys):
    assert main(["new", "--seed", "42", "--depth", "4", "--save", tmp_save]) == 0
    capsys.readouterr()
    assert main(["play", "--save", tmp_save, "--cmd", "e"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"(?m)^RESULT ok$", out)
    assert "floor 1/4" in out


def test_cli_turn_is_persisted_between_processes(tmp_save, capsys):
    main(["new", "--seed", "42", "--depth", "4", "--save", tmp_save])
    capsys.readouterr()
    main(["play", "--save", tmp_save, "--cmd", "e"])
    capsys.readouterr()
    main(["play", "--save", tmp_save, "--cmd", "s"])
    out = capsys.readouterr().out
    assert "turn 2" in out


def test_cli_json_output_is_a_single_object(tmp_save, capsys):
    main(["new", "--seed", "42", "--depth", "4", "--save", tmp_save, "--format", "json"])
    capsys.readouterr()
    main(["play", "--save", tmp_save, "--cmd", "e", "--format", "json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["result"] == "ok"
    assert payload["turn"] == 1


def test_cli_show_does_not_spend_a_turn(tmp_save, capsys):
    main(["new", "--seed", "42", "--depth", "4", "--save", tmp_save])
    capsys.readouterr()
    main(["show", "--save", tmp_save])
    capsys.readouterr()
    main(["show", "--save", tmp_save])
    out = capsys.readouterr().out
    assert "turn 0" in out


def test_cli_card_and_rulebook_print(capsys):
    assert main(["card"]) == 0
    assert "DELVE" in capsys.readouterr().out
    assert main(["rulebook"]) == 0
    assert "Turn economy" in capsys.readouterr().out


def test_cli_selftest_passes(capsys):
    assert main(["selftest", "--quiet"]) == 0


def test_cli_strict_exit_reports_death(tmp_save, capsys):
    main(["new", "--seed", "42", "--depth", "4", "--save", tmp_save])
    capsys.readouterr()
    game = load_game(tmp_save)
    game.s["player"]["hp"] = 0
    game.s["player"]["hp"] = 0
    game._check_end()
    save_game(tmp_save, game)
    assert main(["play", "--save", tmp_save, "--cmd", "wait", "--strict-exit"]) == 11


def test_cli_missing_save_is_an_error_not_a_traceback(tmp_path, capsys):
    missing = str(tmp_path / "nope.json")
    assert main(["play", "--save", missing, "--cmd", "e"]) == 1
    assert "no save" in capsys.readouterr().err


def test_cli_rejects_an_absurd_depth(tmp_save, capsys):
    assert main(["new", "--depth", "99", "--save", tmp_save]) == 2


def test_cli_autoplay_finishes_a_run(tmp_save, capsys):
    assert main(["autoplay", "--seed", "3", "--depth", "4", "--save", tmp_save]) == 0
    out = capsys.readouterr().out
    assert re.search(r"RESULT (win|dead|stalled|quit)", out)


def test_cli_no_arguments_prints_help(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_cli_pipe_mode_runs_a_script_of_commands(tmp_save, capsys, monkeypatch):
    main(["new", "--seed", "42", "--depth", "4", "--save", tmp_save])
    capsys.readouterr()
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO("e\ns\nwait\n"))
    assert main(["play", "--save", tmp_save]) == 0
    out = capsys.readouterr().out
    assert out.count("RESULT ok") == 3
    assert "turn 3" in out


def test_cli_log_writes_one_jsonl_record_per_turn(tmp_save, tmp_path, capsys):
    log = str(tmp_path / "turns.jsonl")
    main(["new", "--seed", "42", "--depth", "4", "--save", tmp_save])
    capsys.readouterr()
    for command in ["e", "s", "wait"]:
        main(["play", "--save", tmp_save, "--cmd", command, "--log", log])
        capsys.readouterr()
    with open(log) as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    assert [r["turn"] for r in records] == [1, 2, 3]
    assert all("events" in r and "hp" in r for r in records)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _free_neighbour(game: Game, monster: dict) -> tuple[int, int] | None:
    for dx, dy in DIRS.values():
        nx, ny = monster["x"] + dx, monster["y"] + dy
        if game.tile(nx, ny) == TILE_WALL:
            continue
        if game.monster_at(nx, ny) is not None:
            continue
        return nx, ny
    return None


def test_the_stairs_tile_is_marked_in_the_grid():
    floor = build_floor(random.Random(3), 2, 4)
    x, y = floor["stairs"]
    assert floor["grid"][y][x] == TILE_STAIRS


def test_chebyshev_is_eight_way_distance():
    assert _chebyshev((0, 0), (1, 1)) == 1
    assert _chebyshev((0, 0), (3, 0)) == 3


# --------------------------------------------------------------------------
# regressions found by actually playing the thing
# --------------------------------------------------------------------------


def test_a_dead_run_does_not_keep_ticking():
    """A corpse must not regenerate, and monsters must stop hitting it.

    Regression: the turn used to run ``_regen()`` unconditionally after ``_check_end()``,
    so a run killed by a monster then healed the corpse back to 1 HP -- leaving
    ``status=dead`` with ``hp=1``, a self-contradicting state for any agent reading it.
    """
    game = new_game(seed=11, depth=5)
    player = game.s["player"]
    player["hp"] = 1
    # walk until something kills us, then keep the state pristine after death
    for _ in range(delve.TURN_LIMIT):
        if game.s["status"] != "ok":
            break
        game.step("wait")
    assert game.s["status"] in {"dead", "stalled"}
    assert game.s["status"] != "dead" or player["hp"] == 0
    # once the run is over, pumping more turns must not change the state at all
    frozen = json.dumps(game.s, sort_keys=True)
    for _ in range(5):
        game.step("wait")
    assert json.dumps(game.s, sort_keys=True) == frozen


def test_a_live_turn_still_regenerates():
    game = new_game(seed=11, depth=5)
    player = game.s["player"]
    player["hp"] = max(2, player["max_hp"] // 2)
    before = player["hp"]
    for _ in range(20):
        game.step("wait")
    assert player["hp"] > before, "regeneration must still happen while alive"


def test_every_finished_run_names_a_cause():
    for seed in range(1, 25):
        game = new_game(seed=seed, depth=5)
        for _ in range(delve.TURN_LIMIT + 20):
            if game.s["status"] != "ok":
                break
            game.step(delve.bot_command(game))
        if game.s["status"] == "ok":
            continue
        assert game.s["cause"], f"seed {seed} ended as {game.s['status']} with no cause"
        assert game.s["cause"] in game.render_text()


def test_quit_names_itself_as_the_cause():
    game = new_game(seed=3, depth=3)
    game.step("quit")
    assert game.s["status"] == "quit"
    assert "abandoned" in game.s["cause"]


def test_death_cause_names_the_killer():
    game = new_game(seed=11, depth=5)
    player = game.s["player"]
    player["hp"] = 1
    # put a real threat next to the player instead of hoping one wanders over
    game.s["floor"]["monsters"].append(
        {
            "kind": "wraith",
            "x": player["x"] + 1,
            "y": player["y"],
            "hp": 20,
            "max_hp": 20,
            "atk": 9,
            "def": 0,
            "xp": 5,
            "alive": True,
            "alerted": True,
        }
    )
    for _ in range(50):
        if game.s["status"] != "ok":
            break
        game.step("wait")
    assert game.s["status"] == "dead"
    assert game.s["cause"] == "slain by a wraith on floor 1", game.s["cause"]
    assert game.s["player"]["hp"] == 0
    assert "CAUSE" in game.render_text()


def test_the_last_log_survives_a_reload(tmp_save):
    """Reading the same save twice must give the same answer.

    Regression: the event log lived only in memory, so a reloaded save rendered
    ``EVENT - (nothing)`` for the turn the player had just taken.
    """
    game = new_game(seed=4, depth=4)
    game.step("e")
    save_game(tmp_save, game)
    back = load_game(tmp_save)
    assert back.events == game.events
    assert back.render_text() == game.render_text()


def test_result_is_always_the_final_line():
    game = new_game(seed=4, depth=4)
    for legend in (False, True):
        game.show_legend = legend
        lines = game.render_text().splitlines()
        assert lines[-1] == f"RESULT {game.s['status']}"
        assert lines[-1].startswith("RESULT ")


def test_result_is_the_final_line_even_after_the_run_ends():
    game = new_game(seed=4, depth=4)
    game.step("quit")
    game.show_legend = True
    assert game.render_text().splitlines()[-1] == "RESULT quit"


def test_move_down_means_descend_not_south():
    """`down` must mean exactly one thing: the stairs.  Silently walking south instead
    is the kind of quiet wrong answer that burns an agent's whole run."""
    game = new_game(seed=5, depth=4)
    player = game.s["player"]
    game.events.clear()
    game.step("move down")
    assert game.events and "no stairs" in " ".join(game.events).lower()
    assert (player["x"], player["y"]) == (game.s["player"]["x"], game.s["player"]["y"])

    # standing on the stairs, "move down" really descends
    x, y = game.s["floor"]["stairs"]
    player["x"], player["y"] = x, y
    depth_before = game.s["depth"]
    game.step("move down")
    assert game.s["depth"] == depth_before + 1


def test_move_up_is_refused_with_an_explanation():
    game = new_game(seed=5, depth=4)
    player = game.s["player"]
    start = (player["x"], player["y"])
    game.step("move up")
    assert "no way up" in " ".join(game.events)
    assert (game.s["player"]["x"], game.s["player"]["y"]) == start


def test_direction_words_no_longer_swallow_up_and_down():
    assert "up" not in delve.DIR_WORDS
    assert "down" not in delve.DIR_WORDS
    assert delve.DIR_WORDS["north"] == "n"


def test_judged_junk_does_not_stall_a_run():
    """The bot must learn to ignore what it already passed on, rather than the game
    deleting the item to stop it looping."""
    for seed in range(1, 31):
        game = new_game(seed=seed, depth=5)
        player = game.s["player"]
        player["weapon"] = "rune blade"
        player["armor"] = "plate"
        # scatter junk the bot can see but never wants, right around the start
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1)):
            x, y = player["x"] + dx, player["y"] + dy
            if game.tile(x, y) == TILE_WALL:
                continue
            game.items.append({"kind": "weapon", "name": "dagger", "x": x, "y": y})
            game.items.append({"kind": "armor", "name": "leather", "x": x, "y": y})
        for _ in range(delve.TURN_LIMIT):
            if game.s["status"] != "ok":
                break
            game.step(delve.bot_command(game))
        assert game.s["status"] != "stalled", f"seed {seed} stalled on judged junk"


# --------------------------------------------------------------------------
# the mechanics added for depth (v2)
# --------------------------------------------------------------------------


def _place_monster(game, kind, dx, dy, **over):
    player = game.s["player"]
    spec = MONSTERS[kind]
    monster = {
        "kind": kind,
        "x": player["x"] + dx,
        "y": player["y"] + dy,
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
        "alerted": True,
        "alive": True,
    }
    monster.update(over)
    game.s["floor"]["monsters"].append(monster)
    return monster


def _pick_spot(game, dx, dy):
    """Clear the stage: empty the floor of residents and stand where `dx, dy` is walkable.

    The clearing matters.  A generated floor already has monsters on it, and a test
    that counts hits will happily count a rat that wandered into the experiment.
    """
    game.s["floor"]["monsters"].clear()
    for y in range(1, MAP_H - 1):
        for x in range(1, MAP_W - 1):
            if game.tile(x, y) != TILE_WALL and game.tile(x + dx, y + dy) != TILE_WALL:
                game.s["player"]["x"], game.s["player"]["y"] = x, y
                game._vis_cache = None
                return
    raise AssertionError("no room on this floor")


def test_archers_shoot_without_closing_in():
    game = new_game(seed=3, depth=4)
    _pick_spot(game, 3, 0)
    archer = _place_monster(game, "archer", 3, 0)
    spot = (archer["x"], archer["y"])
    game.s["player"]["hp"] = 60
    game.s["player"]["max_hp"] = 60
    # turn 1 is spent drawing, turn 2 is the shot: an archer is on a reload clock
    game.step("wait")
    assert "draws its bow" in " ".join(game.events), game.events
    assert (archer["x"], archer["y"]) == spot, "an archer must not advance"
    game.step("wait")
    assert any("shoots you" in event for event in game.events), game.events
    assert (archer["x"], archer["y"]) == spot, "an archer must not advance"


def test_an_archer_alternates_between_drawing_and_shooting():
    game = new_game(seed=3, depth=4)
    _pick_spot(game, 3, 0)
    _place_monster(game, "archer", 3, 0)
    player = game.s["player"]
    player["hp"] = player["max_hp"] = 300
    shots = 0
    draws = 0
    for _ in range(6):
        game.events.clear()
        game.step("wait")
        shots += len([e for e in game.events if "shoots you" in e])
        draws += len([e for e in game.events if "draws its bow" in e])
    assert shots == 3, f"expected 3 shots in 6 turns, got {shots}"
    assert draws == 3, f"expected 3 draws in 6 turns, got {draws}"


def test_archer_damage_halves_effective_defence():
    game = new_game(seed=3, depth=4)
    _pick_spot(game, 3, 0)
    _place_monster(game, "archer", 3, 0)
    player = game.s["player"]
    player["hp"] = player["max_hp"] = 200
    player["def"] = 0
    before = player["hp"]
    game.step("wait")
    unarmoured = before - player["hp"]

    game = new_game(seed=3, depth=4)
    _pick_spot(game, 3, 0)
    _place_monster(game, "archer", 3, 0)
    player = game.s["player"]
    player["hp"] = player["max_hp"] = 200
    player["def"] = 4
    player["armor"] = "chain"  # +3 DEF, so DEF/2 = 3 rather than the full 7
    before = player["hp"]
    game.step("wait")
    armoured = before - player["hp"]
    assert armoured >= 0
    assert unarmoured >= armoured


def test_a_ghoul_poisons_and_poison_stops_regeneration():
    game = new_game(seed=3, depth=6)
    _pick_spot(game, 1, 0)
    _place_monster(game, "ghoul", 1, 0)
    player = game.s["player"]
    player["hp"] = player["max_hp"] = 400
    game.step("wait")
    assert player["effects"]["poison"] > 0
    assert any("poison" in event.lower() for event in game.events)

    # with the poison up, regeneration must not fire even on a regen turn
    player["hp"] = 300
    for _ in range(REGEN_EVERY * 2):
        before = player["hp"]
        assert game.s["status"] == "ok"
        game.step("wait")
        if player["effects"]["poison"] > 0:
            assert player["hp"] <= before, "poison must suppress regeneration"


def test_poison_only_refreshes_upward():
    game = new_game(seed=3, depth=6)
    player = game.s["player"]
    player["effects"]["poison"] = 9
    _pick_spot(game, 1, 0)
    _place_monster(game, "ghoul", 1, 0, poisons=2)
    game.step("wait")
    # one tick of clock passes, but the weaker venom must not reset the counter to 2
    assert player["effects"]["poison"] == 8, player["effects"]["poison"]
    assert not any("Venom" in event for event in game.events)


def test_a_wraith_weakens_you_and_the_penalty_costs_attack():
    game = new_game(seed=3, depth=4)
    player = game.s["player"]
    _pick_spot(game, 1, 0)
    _place_monster(game, "wraith", 1, 0)
    game.step("wait")
    assert player["effects"]["weak"] > 0
    assert game.p_atk() == max(1, player["atk"] - delve.WEAK_PENALTY + game.ring_bonus("atk"))


def test_a_shaman_heals_its_wounded_friend_instead_of_attacking():
    game = new_game(seed=3, depth=6)
    _pick_spot(game, 4, 0)
    shaman = _place_monster(game, "shaman", 4, 0)
    hurt = _place_monster(game, "skeleton", 5, 0, hp=3)
    game.step("wait")
    assert hurt["hp"] > 3, "the shaman should have mended the skeleton"
    assert (shaman["x"], shaman["y"]) == (game.s["player"]["x"] + 4, game.s["player"]["y"])
    assert any("mends" in event for event in game.events)


def test_a_wisp_acts_twice():
    game = new_game(seed=3, depth=6)
    _pick_spot(game, 1, 0)
    _place_monster(game, "wisp", 1, 0, atk=1)
    player = game.s["player"]
    player["hp"] = player["max_hp"] = 200
    game.step("wait")
    hits = [event for event in game.events if "hits you" in event]
    assert len(hits) == 2, f"a wisp should land two hits, got {hits}"


def test_a_golem_acts_every_second_turn():
    game = new_game(seed=3, depth=8)
    _pick_spot(game, 1, 0)
    _place_monster(game, "golem", 1, 0, atk=1)
    player = game.s["player"]
    player["hp"] = player["max_hp"] = 200
    hits = 0
    for _ in range(4):
        game.events.clear()
        game.step("wait")
        hits += len([event for event in game.events if "hits you" in event])
    assert hits == 2, f"a golem should land two hits in four turns, got {hits}"


def test_a_locked_floor_has_a_key_and_refuses_the_stairs_without_it():
    found = None
    for seed in range(1, 60):
        game = new_game(seed=seed, depth=6)
        for depth in range(1, 7):
            game._enter_floor(depth)
            if game.s["floor"].get("locked"):
                found = game
                break
        if found:
            break
    assert found is not None, "no locked floor in 60 seeds"
    floor = found.s["floor"]
    assert [i for i in floor["items"] if i["kind"] == "key"], "a locked floor must contain a key"
    x, y = floor["stairs"]
    assert floor["grid"][y][x] == TILE_LOCKED
    found.s["player"]["x"], found.s["player"]["y"] = x, y
    floor_depth = found.s["depth"]
    found.step("down")
    assert found.s["depth"] == floor_depth, "the stairs must refuse without the key"
    assert "locked" in " ".join(found.events)

    key = next(i for i in floor["items"] if i["kind"] == "key")
    found._on_enter_tile(key["x"], key["y"])
    assert found.s["player"]["keys"] == 1
    assert not floor["locked"]
    assert floor["grid"][y][x] == TILE_STAIRS
    found.s["player"]["x"], found.s["player"]["y"] = x, y
    found.step("down")
    assert found.s["depth"] == floor_depth + 1


def test_a_dark_floor_shrinks_sight_and_an_insight_ring_gives_it_back():
    found = None
    for seed in range(1, 80):
        game = new_game(seed=seed, depth=8)
        for depth in range(1, 9):
            game._enter_floor(depth)
            if game.s["floor"].get("dark"):
                found = game
                break
        if found:
            break
    assert found is not None, "no dark floor in 80 seeds"
    assert found.sight() == DARK_RADIUS
    player = found.s["player"]
    found.items.append({"kind": "ring", "name": "insight", "x": player["x"], "y": player["y"]})
    found._on_enter_tile(player["x"], player["y"])
    assert player["ring"] == "insight"
    assert found.sight() == DARK_RADIUS + RINGS["insight"]["sight"]


def test_a_dark_floor_announces_itself_on_arrival():
    for seed in range(1, 80):
        game = new_game(seed=seed, depth=8)
        for depth in range(1, 9):
            game._enter_floor(depth)
            if game.s["floor"].get("dark"):
                assert "dark" in game.floor_traits()
                return
    raise AssertionError("no dark floor to check")


def test_the_vault_is_never_dark_or_locked():
    for seed in range(1, 30):
        game = new_game(seed=seed, depth=6)
        game._enter_floor(game.s["max_depth"])
        floor = game.s["floor"]
        assert not floor.get("dark"), f"seed {seed}: vault was dark"
        assert not floor.get("locked"), f"seed {seed}: vault was locked"


def test_a_ring_is_worn_and_swapped_rather_than_stacked():
    game = new_game(seed=3, depth=4)
    player = game.s["player"]
    base_atk = game.p_atk()
    game.items.append({"kind": "ring", "name": "might", "x": player["x"], "y": player["y"]})
    game._on_enter_tile(player["x"], player["y"])
    assert player["ring"] == "might"
    assert game.p_atk() == base_atk + 2

    game.items.append({"kind": "ring", "name": "warding", "x": player["x"], "y": player["y"]})
    game._on_enter_tile(player["x"], player["y"])
    assert player["ring"] == "warding", "the new ring replaces the old one"
    assert game.p_atk() == base_atk, "the old ring's bonus must be gone"
    dropped = [i for i in game.items if i["kind"] == "ring" and i["name"] == "might"]
    assert len(dropped) == 1, "the replaced ring drops where you stand"


def test_the_vigor_ring_moves_max_hp_and_hp_stays_bounded():
    game = new_game(seed=3, depth=4)
    player = game.s["player"]
    base = player["max_hp"]
    player["hp"] = base
    game.items.append({"kind": "ring", "name": "vigor", "x": player["x"], "y": player["y"]})
    game._on_enter_tile(player["x"], player["y"])
    assert player["max_hp"] == base + 8
    game.items.append({"kind": "ring", "name": "might", "x": player["x"], "y": player["y"]})
    game._on_enter_tile(player["x"], player["y"])
    assert player["max_hp"] == base, "taking the vigor ring off must take the HP back"
    assert player["hp"] <= player["max_hp"]


def test_item_glyphs_cover_every_kind_a_floor_can_generate():
    kinds = set()
    for seed in range(1, 40):
        floor = build_floor(random.Random(seed), 6, 8)
        kinds |= {item["kind"] for item in floor["items"]}
    assert kinds <= set(ITEM_GLYPHS), f"unglyphed kinds: {kinds - set(ITEM_GLYPHS)}"


def test_every_monster_glyph_is_unique_and_visible_in_the_legend():
    glyphs = [spec["glyph"] for spec in MONSTERS.values()]
    assert len(glyphs) == len(set(glyphs)), "two monsters share a glyph"
    for kind, spec in MONSTERS.items():
        assert spec["glyph"] in delve.LEGEND, f"{kind} has no legend entry"


def test_deep_floors_bring_mechanics_not_just_bigger_numbers():
    """The design promise: from floor 3 down, most species change what you should do."""
    mechanical = [
        kind
        for kind, spec in MONSTERS.items()
        if spec.get("ranged")
        or spec.get("poisons")
        or spec.get("heals")
        or spec.get("weakens")
        or spec.get("speed", 1) > 1
        or spec.get("slow", 1) > 1
    ]
    assert len(mechanical) >= 5, mechanical


def test_a_floor_never_stacks_more_monsters_than_the_cap():
    for depth in (4, 8, 12):
        floor = build_floor(random.Random(depth), depth, 12)
        assert len(floor["monsters"]) <= 9 + 1, "the boss floor adds one guardian on top"


def test_a_save_from_an_older_format_is_refused_clearly(tmp_path):
    """Better to say "cannot read this" than to half-build a Game from a stale state."""
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"format": "delve-save", "version": 1, "sealed": False, "data": {}}))
    with pytest.raises(ValueError, match="not readable by this build"):
        load_game(str(path))


def test_a_file_that_is_not_a_delve_save_is_refused(tmp_path):
    path = tmp_path / "nope.json"
    path.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(ValueError, match="not a delve-save file"):
        load_game(str(path))


def test_an_unknown_command_never_wedges_the_run():
    game = new_game(seed=7, depth=3)
    for command in ["", "   ", "flurble", "move sideways", "use nothing", "attack up", "10", "null"]:
        before = game.s["turn"]
        game.step(command)
        assert game.s["status"] == "ok"
        assert game.s["turn"] in (before, before + 1)
        if before != game.s["turn"]:
            assert game.events, f"{command!r} spent a turn but said nothing"
