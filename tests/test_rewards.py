"""Join/leave messages and reward levels (database/rewards.py, /profil, /users/belohnungen, /joinmessage)."""
import copy

import pytest

from database import rewards
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_ingame import cmd, tells, until
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

TEN_HOURS = 20 * 3600 * 10


def facts(**values):
    return dict({"streak": 0, "tiers": 0, "play_hours": 0, "days": 0, "trophies": 0, "metrics": {}}, **values)


@pytest.fixture
def me(client, db, server, two_players):
    db.set_plugin_connected(server["id"], True)
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    return client


@pytest.fixture
def game(db, server, plugin, two_players):
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    assert connection.request(f"!JOIN~{OTHER_UUID}|Notch") == "success|101"
    return connection


# ------------------------------------------------------------------ levels

def test_a_level_needs_any_of_its_conditions():
    levels = rewards.DEFAULT_LEVELS
    assert rewards.reached_level(levels, facts()) == 0
    assert rewards.reached_level(levels, facts(play_hours=10)) == 1
    assert rewards.reached_level(levels, facts(streak=30)) == 2
    # the top level by another path: 500 Ancient Debris instead of a 365 day streak
    assert rewards.reached_level(levels, facts(metrics={"ancient_debris": 500})) == 5
    assert rewards.reached_level(levels, facts(metrics={"ancient_debris": 499})) == 0


def test_unlocks_add_up_and_moderators_get_their_colors():
    two = rewards.unlocked(rewards.DEFAULT_LEVELS, 2)
    assert two["colors"] == ["white", "gray", "green", "aqua", "blue", "yellow"]
    assert two["sounds"] == ["chime"] and "rainbow" not in two["styles"]
    assert rewards.unlocked(rewards.DEFAULT_LEVELS, 0, moderator=True)["colors"][-3:] == ["gold", "red", "dark_red"]
    assert not set(rewards.MOD_COLORS) & set(rewards.unlocked(rewards.DEFAULT_LEVELS, 5)["colors"])


def test_render_join_and_leave_lines():
    available = rewards.unlocked(rewards.DEFAULT_LEVELS, 3)
    style = dict(rewards.DEFAULT_STYLE, join_text="back", color="aqua", symbol="★", style="frame")
    assert rewards.render("Notch", style, available) == "&b★ &7&bNotch&r&7 ist zurück! &b★"
    assert rewards.render("Notch", dict(style, style="bold", symbol=""), available, "leave") == "&7&b&lNotch&r&7 ist weg."
    rainbow = rewards.render("Ab", dict(style, style="rainbow", symbol=""), rewards.unlocked(rewards.DEFAULT_LEVELS, 4))
    assert rainbow.startswith("&7&aA&bb")  # letters in the unlocked colors (without white and gray)


def test_choices_are_checked_against_the_unlocked_options():
    available = rewards.unlocked(rewards.DEFAULT_LEVELS, 1)
    assert rewards.check_choice("color", "aqua", available) is None
    assert rewards.check_choice("color", "blue", available) == "Das ist noch nicht freigeschaltet."
    assert rewards.check_choice("color", "gold", available) == "Das ist noch nicht freigeschaltet."  # moderators only
    assert rewards.check_choice("color", "pink", available) == "Unbekannte Auswahl."
    assert rewards.check_choice("sound", "", available) is None  # no sound is always fine
    assert rewards.check_choice("nonsense", "x", available) == "Unbekannte Einstellung."
    # a choice that is no longer available (the moderators changed the levels) falls back
    style = rewards.normalized_style({"color": "dark_purple", "sound": "fanfare"}, available)
    assert style["color"] == "white" and style["sound"] == ""


def test_level_editor_validation():
    levels, error = rewards.validate_levels(copy.deepcopy(rewards.DEFAULT_LEVELS))
    assert error is None and levels == rewards.DEFAULT_LEVELS
    broken = copy.deepcopy(rewards.DEFAULT_LEVELS)
    broken[1]["conditions"] = []
    assert "mindestens eine Bedingung" in rewards.validate_levels(broken)[1]
    broken = copy.deepcopy(rewards.DEFAULT_LEVELS)
    broken[2]["unlocks"]["colors"] = ["gold"]  # moderator colors are not a reward
    assert rewards.validate_levels(broken)[1] == "Stufe 3: unbekannte Freischaltung."
    broken = copy.deepcopy(rewards.DEFAULT_LEVELS)
    broken[5]["conditions"] = [{"type": "metric", "metric": "nope", "value": 5}]
    assert rewards.validate_levels(broken)[1] == "Stufe 6: unbekannte Kennzahl."
    assert rewards.validate_levels([])[1] and rewards.validate_levels("x")[1]


def test_a_reached_level_stays_when_the_facts_drop(db, server, two_players):
    a, _ = two_players
    db.update_player_stats(a, {"stats": {"minecraft:custom": {"minecraft:play_time": TEN_HOURS}}})
    state = rewards.player_state(db, a)
    assert (state["level"], state["new_level"]) == (1, 1)
    assert rewards.player_state(db, a)["new_level"] is None  # announced once
    db.set_reward_settings(server["id"], levels=[dict(level, conditions=[{"type": "streak", "value": 999}] if i else [])
                                                 for i, level in enumerate(rewards.DEFAULT_LEVELS)])
    assert rewards.player_state(db, a)["level"] == 1  # harder levels do not take it away


# ------------------------------------------------------------------ website

def test_profile_shows_the_join_message(me, db):
    html = me.get("/profil", **on("testdomain")).get_data(as_text=True)
    assert "Join-Nachricht" in html and "1. Neuling" in html and "_Tobias4444 ist da." in html
    assert "🔒" in html and "ab Stufe 2 »Stammgast«" in html


def test_choose_a_join_style(me, db, server, two_players):
    a, _ = two_players
    events = []
    db_notify = db.notify_server_event
    db.notify_server_event = lambda server_id, kind, **fields: events.append((kind, fields))
    try:
        assert me.post("/api/joinstyle", json={"field": "color", "value": "blue"}, **on("testdomain")).status_code == 400
        response = me.post("/api/joinstyle", json={"field": "join_text", "value": "hello"}, **on("testdomain"))
        assert response.status_code == 200 and "sagt Hallo!" in response.json["join"]
        assert db.get_join_settings(a)["style"]["join_text"] == "hello"
        assert events == [("joinstyle", {"player_id": str(a)})]
        # no way back to the game's own message
        assert me.post("/api/joinstyle", json={"custom": False}, **on("testdomain")).status_code == 400
    finally:
        db.notify_server_event = db_notify


def test_preview_is_escaped():
    from web.main import mc_html
    assert str(mc_html("&b<b>x</b>")) == '<span style="color:#55FFFF">&lt;b&gt;x&lt;/b&gt;</span>'


def test_mute_other_players(me, db, two_players):
    a, _ = two_players
    assert me.post("/api/joinmutes", json={"name": "Notch", "muted": True}, **on("testdomain")).json["mutes"][0]["name"] == "Notch"
    assert me.post("/api/joinmutes", json={"name": "_Tobias4444"}, **on("testdomain")).status_code == 404  # not oneself
    assert me.post("/api/joinmutes", json={"sounds_off": True}, **on("testdomain")).status_code == 200
    assert db.get_join_settings(a)["sounds_off"] is True
    assert db.get_join_sync(db.get_server_id_from_player_id(a))[1] == {PLAYER_UUID: (True, [OTHER_UUID])}
    assert me.post("/api/joinmutes", json={"name": "notch", "muted": False}, **on("testdomain")).json["mutes"] == []


def test_moderators_edit_the_levels(me, db, server):
    assert me.get("/users/belohnungen", **on("testdomain")).status_code == 403
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert "Stufen speichern" in me.get("/users/belohnungen", **on("testdomain")).get_data(as_text=True)
    levels = copy.deepcopy(rewards.DEFAULT_LEVELS)[:3]
    levels[2]["name"] = "Profi"
    response = me.post("/api/mod/rewards", json={"levels": levels}, **on("testdomain"))
    assert response.status_code == 200 and [l["name"] for l in response.json["levels"]] == ["Neuling", "Stammgast", "Profi"]
    levels[1]["conditions"] = []
    assert me.post("/api/mod/rewards", json={"levels": levels}, **on("testdomain")).status_code == 400
    assert me.post("/api/mod/rewards", json={"levels": "default"}, **on("testdomain")).json["custom"] is False
    assert me.post("/api/mod/rewards", json={"enabled": False}, **on("testdomain")).json["enabled"] is False
    assert me.post("/api/joinstyle", json={"field": "join_text", "value": "hello"}, **on("testdomain")).status_code == 409
    assert "Eigene Join-Nachrichten erlauben" in me.get("/users/belohnungen", **on("testdomain")).get_data(as_text=True)
    assert [e["action"] for e in db.get_mod_log(server["id"])][:3] == ["rewards"] * 3


# ------------------------------------------------------------------ in the game

def test_joinmessage_command(db, server, game, two_players):
    a, _ = two_players
    cmd(game, PLAYER_UUID, "joinmessage")
    lines = until(game, "!tell~" + PLAYER_UUID + "|&7Vorschau")
    assert any("Stufe Neuling (1/6)" in line for line in lines)
    assert any("/joinmessage farbe gray" in line for line in lines)  # a button (older plugin: the command as text)
    cmd(game, PLAYER_UUID, "joinmessage", "farbe blue")
    assert tells(game, 1) == ["&cDas ist noch nicht freigeschaltet."]
    cmd(game, PLAYER_UUID, "joinmessage", "farbe gray")
    style_message, answer = game.recv(), game.recv()
    assert style_message == f"!joinstyle~{PLAYER_UUID}|&7&7_Tobias4444&r&7 ist da.|&7&7_Tobias4444&r&7 ist weg.||"
    assert answer.startswith(f"!tell~{PLAYER_UUID}|&aGespeichert:")
    cmd(game, PLAYER_UUID, "joinmessage", "aus")  # gone: the MCConnect message is always used
    assert tells(game, 1)[0].startswith("&7Benutzung")


def test_styles_and_mutes_are_sent_on_connect(db, server, plugin, two_players):
    a, b = two_players
    db.set_join_style(a, dict(rewards.DEFAULT_STYLE, join_text="hello"))
    db.set_join_mute(b, "_Tobias4444")
    connection = plugin()
    connection.send(f"!AUTH~{server['key']}")
    messages = until(connection, "!joinmutes~")
    assert connection.join_default == "!joindefault~&7&f{name}&r&7 ist da.|&7&f{name}&r&7 ist weg."
    assert f"!joinstyle~{PLAYER_UUID}|&7&f_Tobias4444&r&7 sagt Hallo!|&7&f_Tobias4444&r&7 ist weg.||" in messages
    assert messages[-1] == f"!joinmutes~{OTHER_UUID}|0|{PLAYER_UUID}"


def test_new_level_is_told_in_the_game(db, server, game, two_players, socket_server):
    a, _ = two_players
    game.send(f"!STATS~{PLAYER_UUID}|" + '{"stats": {"minecraft:custom": {"minecraft:play_time": %d}}}' % TEN_HOURS)
    lines = until(game, f"!tell~{PLAYER_UUID}|&6★ Neue Stufe")
    assert "»Stammgast«" in lines[-1] and "/joinmessage" in lines[-1]
    assert db.get_join_settings(a)["level"] == 1


def test_custom_text_check():
    assert rewards.check_custom_text("  {name}   kommt  aus dem Nether ")[0] == "{name} kommt aus dem Nether"
    assert "genau einmal" in rewards.check_custom_text("ohne Namen")[1]
    assert "genau einmal" in rewards.check_custom_text("{name} und {name}")[1]
    assert rewards.check_custom_text("&c{name} rot")[1] and rewards.check_custom_text("{name} ⟦x⇒/op⟧")[1]


def test_moderators_add_and_remove_own_texts(me, db, server, two_players, game):
    a, _ = two_players
    db.set_moderator(server["id"], "_Tobias4444", True)
    add = lambda kind, text, level=0: me.post("/api/mod/rewards", json={"add_text": {"kind": kind, "text": text, "level": level}},
                                              **on("testdomain"))
    assert add("join", "kein Name").status_code == 400
    response = add("join", "{name} ist aus dem Nether zurück!")
    assert response.status_code == 200 and response.json["texts"]["join"] == [
        {"key": "c1", "text": "{name} ist aus dem Nether zurück!", "level": 0}]
    assert add("leave", "{name} geht schlafen.", level=5).json["texts"]["leave"][0]["level"] == 5
    # everyone can choose the new join text (level 1); the leave text is locked
    assert me.post("/api/joinstyle", json={"field": "join_text", "value": "c1"}, **on("testdomain")).status_code == 200
    assert me.post("/api/joinstyle", json={"field": "leave_text", "value": "c1"}, **on("testdomain")).status_code == 400
    assert "_Tobias4444 ist aus dem Nether zurück!" in me.get("/profil", **on("testdomain")).get_data(as_text=True)
    cmd(game, PLAYER_UUID, "joinmessage")
    assert any("… ist aus dem Nether zurück!" in line for line in until(game, "!tell~" + PLAYER_UUID + "|&7Vorschau"))
    # removed: the player falls back to a default text, the levels forget it
    assert me.post("/api/mod/rewards", json={"remove_text": {"kind": "join", "key": "c1"}}, **on("testdomain")).json["texts"]["join"] == []
    state = rewards.player_state(db, a)
    assert state["style"]["join_text"] == "joined" and "c1" not in state["levels"][0]["unlocks"]["join_texts"]



def test_everyone_gets_the_default_message(db, server, plugin, two_players):
    """Players without an own choice: the default lines of the first level (the plugin fills in the name)."""
    a, _ = two_players
    assert rewards.join_style_message(db, a) == f"!joinstyle~{PLAYER_UUID}|&7&f_Tobias4444&r&7 ist da.|&7&f_Tobias4444&r&7 ist weg.||"
    db.set_reward_settings(server["id"], enabled=False)  # switched off: the game's own message
    assert rewards.join_style_message(db, a) == f"!joinstyle~{PLAYER_UUID}||||"
    connection = plugin()
    connection.send(f"!AUTH~{server['key']}")
    until(connection, "!sendAllPlayerStats")
    assert not hasattr(connection, "join_default")


def test_conditions_are_alternatives_of_groups():
    """(30 day streak and 50 hours) or 500 Ancient Debris."""
    levels = [{"conditions": [], "unlocks": {}}, {"conditions": [[{"type": "streak", "value": 30}, {"type": "play_hours", "value": 50}],
                                                                  [{"type": "metric", "metric": "ancient_debris", "value": 500}]], "unlocks": {}}]
    assert rewards.reached_level(levels, facts(streak=30, play_hours=10)) == 0  # only one of the "and"
    assert rewards.reached_level(levels, facts(streak=30, play_hours=50)) == 1
    assert rewards.reached_level(levels, facts(metrics={"ancient_debris": 500})) == 1
    assert rewards.level_hint(levels[1]) == "(30 Tage Serie und 50 Std. Spielzeit) oder 500 Ancient Debris"
    # the old flat format: every condition is an alternative of its own
    assert rewards.levels_of([{"name": "A", "conditions": [], "unlocks": {}},
                              {"name": "B", "conditions": [{"type": "streak", "value": 7}], "unlocks": {}}])[1]["conditions"] \
        == [[{"type": "streak", "value": 7}]]
    edited = copy.deepcopy(rewards.DEFAULT_LEVELS)
    edited[2]["conditions"] = [[{"type": "streak", "value": 30}, {"type": "play_hours", "value": "50"}], []]
    levels, error = rewards.validate_levels(edited)
    assert error is None and levels[2]["conditions"] == [[{"type": "streak", "value": 30}, {"type": "play_hours", "value": 50}]]
