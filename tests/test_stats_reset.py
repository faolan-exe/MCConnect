"""Setting stats back to 0 (docs/adr/0005-stats-reset.md): the database, the order with the plugin, the website."""
import json

import pytest

from tests.conftest import OTHER_UUID, PLAYER_UUID, wait_for
from tests.test_addons import connect
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import admin_client, app, client, on  # noqa: F401 (fixtures)

STATS = json.dumps({"stats": {"minecraft:mined": {"minecraft:stone": 12}, "minecraft:custom": {"minecraft:play_time": 72000}}})
CLEARED = ("actions", "stat_snapshots", "player_achievements", "trophies", "record_history")


def earn(db, server, uuid, reward_level=2):
    player_id = db.ensure_player_on_server(server["id"], uuid)
    db.update_player_stats(player_id, STATS)
    db._execute("INSERT INTO player_achievements (player_id, achievement, tier) VALUES (%s, 'miner', 0)", (player_id,))
    db._execute("""INSERT INTO trophies (server_id, player_id, kind, ref, title)
                   VALUES (%s, %s, 'player_of_week', 'w1', 'Spieler der Woche')""", (server["id"], player_id))
    db._execute("INSERT INTO record_history (server_id, metric, player_id, value) VALUES (%s, 'blocks_mined', %s, 12)",
                (server["id"], player_id))
    db._execute("INSERT INTO player_milestones (player_id, kind, value) VALUES (%s, 'streak', 7), (%s, 'anniversary', 1)",
                (player_id, player_id))
    db._execute("""INSERT INTO player_sessions (player_id, started_at, ended_at)
                   VALUES (%s, now() - interval '2 hours', now() - interval '1 hour'), (%s, now(), NULL)""",
                (player_id, player_id))
    db._execute("UPDATE player_server_info SET reward_level = %s WHERE player_id = %s", (reward_level, player_id))
    return player_id


def rows(db, table, player_id):
    return db._fetchvalue(f"SELECT count(*) FROM {table} WHERE player_id = %s", (player_id,))


def assert_earned(db, player_id):
    assert all(rows(db, table, player_id) > 0 for table in CLEARED)
    assert rows(db, "player_milestones", player_id) == 2 and rows(db, "player_sessions", player_id) == 2
    assert db.get_join_settings(player_id)["level"] == 2


def assert_cleared(db, player_id):
    assert all(rows(db, table, player_id) == 0 for table in CLEARED)
    assert db.get_join_settings(player_id)["level"] == 0
    # the anniversary and the session of who is online right now stay
    assert db._fetchvalue("SELECT array_agg(kind) FROM player_milestones WHERE player_id = %s", (player_id,)) == ["anniversary"]
    assert db._fetchvalue("SELECT count(*) FROM player_sessions WHERE player_id = %s AND ended_at IS NULL", (player_id,)) == 1
    assert rows(db, "player_sessions", player_id) == 1


def logged(db, server, action):
    return [entry for entry in db.get_mod_log(server["id"]) if entry["action"] == action]


def test_one_player_leaves_the_others_alone(db, server):
    player, other = earn(db, server, PLAYER_UUID), earn(db, server, OTHER_UUID)
    assert db.reset_stats(server["id"], player) == 1
    assert_cleared(db, player)
    assert_earned(db, other)


def test_whole_server_clears_every_player(db, server):
    player, other = earn(db, server, PLAYER_UUID), earn(db, server, OTHER_UUID)
    assert db.reset_stats(server["id"]) == 2
    assert_cleared(db, player)
    assert_cleared(db, other)


def test_player_of_another_server_is_not_touched(db, server, other_server):
    player = earn(db, server, PLAYER_UUID)
    assert db.reset_stats(other_server["id"], player) == 0
    assert db.reset_stats(other_server["id"]) == 0
    assert_earned(db, player)


def test_stats_start_at_zero_after_a_reset(db, server):
    player = earn(db, server, PLAYER_UUID)
    db.reset_stats(server["id"], player)
    db.update_player_stats(player, json.dumps({"stats": {"minecraft:mined": {"minecraft:stone": 1}}}))
    assert db.get_player_metrics(player)["blocks_mined"] == 1


def event(player_id=None, target="all", name=None):
    return {"type": "stats_reset", "target": target, "player_id": player_id, "name": name, "actor": "Admin Tobi"}


def test_database_waits_for_the_plugin(db, server, plugin, socket_server):  # noqa: F811
    connection = connect(plugin, server, "click,statsreset")
    player = earn(db, server, PLAYER_UUID)
    socket_server.handle_server_event(dict(event(str(player), PLAYER_UUID, "_Tobias4444"), server_id=server["id"]))
    assert connection.recv() == f"!statsreset~{PLAYER_UUID}"
    assert_earned(db, player)
    connection.send(f"!STATSRESET~{PLAYER_UUID}|ok|")
    wait_for(lambda: logged(db, server, "stats_reset"))
    assert_cleared(db, player)
    assert logged(db, server, "stats_reset")[0]["target_name"] == "_Tobias4444"


def test_failure_in_the_game_keeps_the_database(db, server, plugin, socket_server):  # noqa: F811
    connection = connect(plugin, server, "click,statsreset")
    player = earn(db, server, PLAYER_UUID)
    socket_server.handle_server_event(dict(event(), server_id=server["id"]))
    assert connection.recv() == "!statsreset~all"
    connection.send("!STATSRESET~all|failed|2 Dateien")
    wait_for(lambda: logged(db, server, "stats_reset_failed"))
    assert_earned(db, player)


@pytest.mark.parametrize("message", ["!STATSRESET~all|ok|", "!STATSRESET~all|weird|"])
def test_answer_without_a_request_changes_nothing(db, server, plugin, message):  # noqa: F811
    connection = connect(plugin, server, "click,statsreset")
    player = earn(db, server, PLAYER_UUID)
    assert connection.request(message) == "error|005"
    assert_earned(db, player)


def test_old_plugin_gets_nothing_and_it_is_logged(db, server, plugin, socket_server):  # noqa: F811
    connect(plugin, server, "click")
    player = earn(db, server, PLAYER_UUID)
    socket_server.handle_server_event(dict(event(), server_id=server["id"]))
    assert logged(db, server, "stats_reset_failed")
    assert_earned(db, player)


@pytest.fixture
def events(db, monkeypatch):
    seen = []
    monkeypatch.setattr(db, "notify_server_event", lambda server_id, kind, **data: seen.append(dict(data, type=kind)))
    return seen


@pytest.fixture
def connected(db, server):
    db.set_plugin_connected(server["id"], True)
    db.set_plugin_features(server["id"], {"statsreset"})


def url(server):
    return f"/api/servers/{server['id']}/stats-reset"


def test_login_is_required(client, server):  # noqa: F811
    assert client.post(url(server), json={"confirm": server["subdomain"]}, **on(None)).status_code == 401


def test_only_the_owner_can_reset(admin_client, db, server, connected, events):  # noqa: F811
    stranger = db.add_server_admin("anna", "testPassword", "anna@example.com", email_verified=True)
    db._execute("UPDATE servers SET owner_id = %s WHERE id = %s", (stranger, server["id"]))
    assert admin_client.post(url(server), json={"confirm": server["subdomain"]}, **on(None)).status_code == 404
    assert not events


def test_refused_while_the_plugin_is_away(admin_client, db, server, events):  # noqa: F811
    db.set_plugin_features(server["id"], {"statsreset"})
    db.set_plugin_connected(server["id"], False)
    assert admin_client.post(url(server), json={"confirm": server["subdomain"]}, **on(None)).status_code == 409
    assert not events


def test_refused_for_a_plugin_without_the_feature(admin_client, db, server, events):  # noqa: F811
    db.set_plugin_connected(server["id"], True)
    db.set_plugin_features(server["id"], {"addons"})
    assert admin_client.post(url(server), json={"confirm": server["subdomain"]}, **on(None)).status_code == 409
    assert not events


@pytest.mark.parametrize("body, status", [
    ({}, 400), ({"confirm": "falsch"}, 400),
    ({"player": "_Tobias4444"}, 400), ({"player": "_Tobias4444", "confirm": "Notch"}, 400),
    ({"player": "Unbekannt", "confirm": "Unbekannt"}, 404),
])
def test_needs_the_confirmation(admin_client, db, server, connected, events, body, status):  # noqa: F811
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    assert admin_client.post(url(server), json=body, **on(None)).status_code == status
    assert not events


def test_whole_server_is_requested(admin_client, server, connected, events):  # noqa: F811
    assert admin_client.post(url(server), json={"confirm": server["subdomain"]}, **on(None)).status_code == 200
    assert events[0]["type"] == "stats_reset" and events[0]["target"] == "all" and "player_id" not in events[0]


def test_one_player_is_requested(admin_client, db, server, connected, events):  # noqa: F811
    player = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    body = {"player": "_Tobias4444", "confirm": "_Tobias4444"}
    assert admin_client.post(url(server), json=body, **on(None)).status_code == 200
    assert events[0]["target"] == PLAYER_UUID and events[0]["player_id"] == str(player) and events[0]["name"] == "_Tobias4444"
