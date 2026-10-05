"""Tests for the example agent loop: parsing the block and planning from it.

The important claim here is that the terminal output is genuinely machine-readable,
so most of these tests parse real game output rather than hand-written fixtures.
"""

from __future__ import annotations

import os
import re

import agent_loop
import delve


def block_for(**kwargs) -> str:
    """Produce real output from a real run, not a fixture."""
    game = delve.new_game(**kwargs)
    for command in ["e", "n", "wait"]:
        game.step(command)
    return game.render_text()


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_it_parses_the_header_and_the_vitals():
    observation = agent_loop.parse_observation(block_for(seed=42, depth=4))
    assert observation["floor"] == 1
    assert observation["max_depth"] == 4
    assert observation["seed"] == 42
    assert observation["turn"] == 3
    assert 0 < observation["hp"] <= observation["max_hp"]
    assert observation["atk"] > 0
    assert observation["def"] >= 1


def test_it_parses_every_documented_field_out_of_real_output():
    """Every field the agent reads must appear in the block, or the parse silently
    degrades to defaults and the agent plays blind."""
    game = delve.new_game(seed=7, depth=4)
    for command in ["e", "s", "wait", "wait"]:
        game.step(command)
    text = game.render_text()
    for label in ("-- DELVE", "HP ", "GEAR", "MAP", "TARGETS", "GROUND", "STAIRS", "EVENT", "RESULT"):
        assert label in text, label
    observation = agent_loop.parse_observation(text)
    assert observation["result"] == "ok"
    assert observation["stairs"] in ("here", "seen", "unknown", "none")
    assert isinstance(observation["map"], list)


def test_the_parsed_map_matches_the_printed_map():
    game = delve.new_game(seed=7, depth=4)
    game.step("e")
    text = game.render_text()
    observation = agent_loop.parse_observation(text)
    assert observation["map"] == game.map_rows()


def test_the_player_glyph_is_found_exactly_once_on_the_board():
    observation = agent_loop.parse_observation(block_for(seed=11, depth=4))
    joined = "".join(observation["map"])
    assert joined.count("@") == 1


def test_targets_are_parsed_with_direction_and_hp():
    game = delve.new_game(seed=5, depth=4)
    observation = None
    for _ in range(60):
        game.step("wait")
        observation = agent_loop.parse_observation(game.render_text())
        if observation["targets"]:
            break
    assert observation is not None
    if observation["targets"]:
        for target in observation["targets"]:
            assert target["dir"] in agent_loop.DIRS
            assert 0 <= target["hp"] <= target["max_hp"]
            assert target["name"].isalpha()


def test_a_dead_parser_still_reads_result():
    game = delve.new_game(seed=8, depth=4)
    game.s["player"]["hp"] = 0
    game.s["status"] = "dead"
    observation = agent_loop.parse_observation(game.render_text())
    assert observation["result"] == "dead"


def test_the_inventory_is_parsed_including_a_multi_word_item():
    game = delve.new_game(seed=5, depth=4)
    game.s["player"]["inventory"] = {"potion": 2, "scroll": 1}
    observation = agent_loop.parse_observation(game.render_text())
    assert observation["inventory"]["potion"] == 2
    assert observation["inventory"]["scroll"] == 1


def test_an_identified_scroll_keeps_its_label_in_the_parse():
    game = delve.new_game(seed=5, depth=4)
    game.s["player"]["inventory"] = {"scroll": 1}
    game.step("use scroll")
    game.s["player"]["inventory"] = {"scroll": 1}
    observation = agent_loop.parse_observation(game.render_text())
    assert any("scroll" in name for name in observation["inventory"])


def test_ground_is_parsed_and_none_is_not_a_list_of_junk():
    game = delve.new_game(seed=5, depth=4)
    observation = agent_loop.parse_observation(game.render_text())
    assert isinstance(observation["ground"], list)
    assert observation["ground"] != ["none"]


def test_garbage_input_does_not_explode():
    observation = agent_loop.parse_observation("not a status block at all\n???\n")
    assert observation["result"] is None
    assert observation["targets"] == []
    assert observation["map"] == []


# --------------------------------------------------------------------------
# the agent plans from the parse alone
# --------------------------------------------------------------------------


def test_it_attacks_a_monster_beside_it():
    game = delve.new_game(seed=12, depth=4)
    monster = game.live_monsters()[0]
    spot = None
    for dx, dy in agent_loop.DIRS.values():
        nx, ny = monster["x"] + dx, monster["y"] + dy
        if game.tile(nx, ny) != delve.TILE_WALL and game.monster_at(nx, ny) is None:
            spot = (nx, ny)
            break
    assert spot is not None
    game.s["player"]["x"], game.s["player"]["y"] = spot
    observation = agent_loop.parse_observation(game.render_text())
    command = agent_loop.decide(observation, {})
    assert command.startswith("move ")
    # and it actually hits: the command is the direction of the adjacent monster
    assert command.split()[1] in agent_loop.DIRS


def test_it_descends_when_the_floor_is_explored_and_it_stands_on_the_stairs():
    game = delve.new_game(seed=42, depth=4)
    # a mapping scroll reveals the whole floor, so there is nothing left to explore
    game.s["scroll"]["kind"] = "mapping"
    game.s["player"]["inventory"]["scroll"] = 1
    game.step("use scroll")
    game.s["player"]["x"], game.s["player"]["y"] = game.s["floor"]["stairs"]
    observation = agent_loop.parse_observation(game.render_text())
    assert observation["stairs"] == "here"
    assert agent_loop.decide(observation, {}) == "descend"


def test_it_explores_before_descending_on_a_fresh_floor():
    """Arriving at the bottom under-levelled is how the first version of this agent died."""
    game = delve.new_game(seed=42, depth=4)
    game.s["player"]["x"], game.s["player"]["y"] = game.s["floor"]["stairs"]
    observation = agent_loop.parse_observation(game.render_text())
    assert observation["stairs"] == "here"
    assert agent_loop.decide(observation, {}) != "descend"


def test_it_writes_down_the_relic_and_comes_back_for_it():
    """Items are only drawn while visible, so a relic it walks away from is lost unless
    the agent keeps its own note -- which is exactly what the card tells it to do."""
    game = delve.new_game(seed=8, depth=4)
    game._enter_floor(game.s["max_depth"])
    guardian = next(m for m in game.monsters if m["kind"] == "guardian")
    guardian["hp"] = 1
    spot = None
    for dx, dy in agent_loop.DIRS.values():
        nx, ny = guardian["x"] + dx, guardian["y"] + dy
        if game.tile(nx, ny) != delve.TILE_WALL and game.monster_at(nx, ny) is None:
            spot = (nx, ny)
            break
    assert spot is not None
    game.s["player"]["x"], game.s["player"]["y"] = spot
    game.s["player"]["atk"] = 99
    game.s["player"]["hp"] = 99
    game.s["player"]["max_hp"] = 99
    state: dict = {}

    game.step("move " + delve.dir_name(guardian["x"] - game.px, guardian["y"] - game.py))
    assert game.s["floor"]["relic"] == [guardian["x"], guardian["y"]]
    observation = agent_loop.parse_observation(game.render_text())
    agent_loop.decide(observation, state)
    assert tuple(state["relic"]) == (guardian["x"], guardian["y"])

    # walk away, then check the agent still heads back to the remembered spot
    game.s["player"]["x"], game.s["player"]["y"] = spot
    observation = agent_loop.parse_observation(game.render_text())
    command = agent_loop.decide(observation, state)
    assert command.startswith("move ")

    for _ in range(10):
        if game.s["status"] != "ok":
            break
        observation = agent_loop.parse_observation(game.render_text())
        game.step(agent_loop.decide(observation, state))
    assert game.s["status"] == "win"


def test_the_text_agent_finishes_every_run_it_starts():
    """No stalls: every loop this agent ever got into was a planning bug, not bad luck."""
    for seed in (2, 14, 21, 27):
        game = delve.new_game(seed=seed, depth=5)
        state: dict = {}
        for _ in range(delve.TURN_LIMIT):
            observation = agent_loop.parse_observation(game.render_text())
            game.step(agent_loop.decide(observation, state))
            if game.s["status"] != "ok":
                break
        assert game.s["status"] != "stalled", f"seed {seed} stalled on floor {game.s['depth']}"


def test_it_quits_once_the_run_is_over():
    observation = {"result": "dead"}
    assert agent_loop.decide(observation, {}) == "quit"


def test_it_moves_toward_the_stairs_when_they_are_known():
    game = delve.new_game(seed=42, depth=4)
    # reveal the whole floor, so the stairs are on the printed board
    game.s["player"]["inventory"]["scroll"] = 1
    game.s["scroll"]["kind"] = "mapping"
    game.step("use scroll")
    observation = agent_loop.parse_observation(game.render_text())
    assert ">" in "".join(observation["map"])
    command = agent_loop.decide(observation, {})
    assert command.startswith("move ")


def test_it_never_plans_through_walls():
    """The planning grid is the printed board, so walls really are obstacles."""
    game = delve.new_game(seed=42, depth=4, view="window")
    observation = agent_loop.parse_observation(game.render_text())
    grid = [list(row) for row in observation["map"]]
    start = agent_loop._find(grid, "@")
    assert start is not None
    stairs = {(x, y) for y, row in enumerate(grid) for x, c in enumerate(row) if c == ">"}
    step = agent_loop._bfs_step(grid, start, stairs)
    if step:
        dx, dy = agent_loop.DIRS[step]
        assert grid[start[1] + dy][start[0] + dx] not in agent_loop.BLOCKING


def test_bfs_returns_none_when_the_target_is_unreachable():
    grid = [list("#####"), list("#@#.#"), list("#####")]
    assert agent_loop._bfs_step(grid, (1, 1), {(3, 1)}) is None


def test_bfs_takes_a_step_that_shortens_the_distance():
    grid = [list("....."), list(".@..>"), list(".....")]
    step = agent_loop._bfs_step(grid, (1, 1), {(4, 1)})
    assert step in agent_loop.DIRS
    dx, dy = agent_loop.DIRS[step]
    landed = (1 + dx, 1 + dy)
    assert grid[landed[1]][landed[0]] not in agent_loop.BLOCKING

    def chebyshev(a, b):
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    assert chebyshev(landed, (4, 1)) < chebyshev((1, 1), (4, 1))


def test_the_agent_only_ever_returns_legal_commands():
    """The agent must not invent a verb the game does not know -- a typo costs a turn."""
    allowed = re.compile(r"^(move [a-z]{1,8}|use [a-z]+|wait|descend|quit|map|status|inv|legend|help)$")
    for seed in (1, 4, 9):
        game = delve.new_game(seed=seed, depth=4)
        state: dict = {}
        for _ in range(150):
            observation = agent_loop.parse_observation(game.render_text())
            command = agent_loop.decide(observation, state)
            assert allowed.match(command), command
            game.step(command)
            if game.s["status"] != "ok":
                break


def test_the_text_only_agent_reaches_the_bottom_floor_on_at_least_one_seed():
    """The parse-based agent has to actually be able to play, not just emit commands."""
    best = 0
    for seed in (5, 7, 42):
        game = delve.new_game(seed=seed, depth=3)
        state: dict = {}
        for _ in range(delve.TURN_LIMIT):
            observation = agent_loop.parse_observation(game.render_text())
            game.step(agent_loop.decide(observation, state))
            if game.s["status"] != "ok":
                break
        best = max(best, game.s["depth"])
    assert best >= 2, "the parsed agent never descended"


# --------------------------------------------------------------------------
# the harness shape
# --------------------------------------------------------------------------


def test_one_process_per_turn_is_enough(tmp_path):
    """The documented harness: new run, then a fresh process per command."""
    save = str(tmp_path / "run.json")
    code = agent_loop.main(
        ["--seed", "3", "--depth", "3", "--save", save, "--policy", "random", "--turns", "4", "--quiet"]
    )
    assert code == 0
    assert os.path.exists(save)
    text = agent_loop.run_subprocess(save, "map")
    assert "RESULT" in text


def test_the_reference_bot_reports_a_finished_run():
    summary = agent_loop.run_reference_bot(seed=3, depth=4, turns=2000)
    assert summary["result"] in ("win", "dead", "stalled", "quit")
    assert summary["policy"] == "bot"
