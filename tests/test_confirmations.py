"""Plugin 3.27 ("confirm"): bans, unbans and removals from the whitelist count only when the plugin confirms them."""
from datetime import datetime, timezone

from tests.conftest import PLAYER_UUID, wait_for
from tests.test_addons import connect
from tests.test_ingame import until
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)


def delivered(db, server):
    return db._fetchvalue("""SELECT bp.delivered_at IS NOT NULL FROM banned_players bp
                             JOIN player_server_info psi ON psi.player_id = bp.banned_player_id
                             WHERE psi.server_id = %s AND bp.source = 'web'""", (server["id"],))


def web_ban(db, server, socket_server):  # noqa: F811
    ban = db.ban_player(server["id"], "_Tobias4444", "Admin", days=0)
    socket_server.handle_server_event({"type": "ban", "server_id": server["id"], "uuid": ban["uuid"],
                                       "name": ban["name"], "reason": ban["reason"], "end_ms": 0})


# ------------------------------------------------------------------ bans

def test_a_ban_counts_only_when_the_plugin_confirms_it(db, server, plugin, socket_server):  # noqa: F811
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    connection = connect(plugin, server, "click,confirm")
    web_ban(db, server, socket_server)
    assert connection.recv().startswith(f"!ban~{PLAYER_UUID}|_Tobias4444|0|")
    assert delivered(db, server) is False
    connection.send(f"!BANNED~{PLAYER_UUID}|0")  # Mojang down: not on the server's ban list
    socket_server.whitelist_retry_interval = 0.1
    assert until(connection, "!ban~")[-1].startswith(f"!ban~{PLAYER_UUID}|")  # sent again
    socket_server.whitelist_retry_interval = 3600
    connection.send(f"!BANNED~{PLAYER_UUID}|1")
    wait_for(lambda: delivered(db, server) is True)


def test_a_failed_ban_is_not_taken_for_a_pardon(db, server, plugin, socket_server):  # noqa: F811
    """The ban list sync used to lift website bans the server never had (they looked pardoned in the game)."""
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.ban_player(server["id"], "_Tobias4444", "Admin", days=0)
    connection = plugin().auth(server["key"])  # features not known yet: delivered when sent ...
    assert until(connection, "!ban~")[-1].startswith(f"!ban~{PLAYER_UUID}|")
    wait_for(lambda: delivered(db, server) is True)
    connection.send(f"!BANNED~{PLAYER_UUID}|0")  # ... until a 3.27 plugin says it failed
    wait_for(lambda: delivered(db, server) is False)
    assert db.sync_web_bans(server["id"], []) == []
    assert [b["name"] for b in db.list_active_bans(server["id"])] == ["_Tobias4444"]


# ------------------------------------------------------------------ unbans

def test_an_unban_while_the_server_is_offline_is_delivered_later(db, server, plugin, socket_server):  # noqa: F811
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.ban_player(server["id"], "_Tobias4444", "Admin", days=0)
    db.unban_player(server["id"], player_id)  # no plugin connected
    assert db.get_plugin_tasks(server["id"]) == [{"kind": "unban", "target": PLAYER_UUID, "name": "_Tobias4444"}]
    connection = plugin().auth(server["key"])
    connection.send("!FEATURES~click,confirm")
    assert until(connection, "!unban~")[-1] == f"!unban~{PLAYER_UUID}|_Tobias4444"
    connection.send(f"!UNBANNED~{PLAYER_UUID}|0|_Tobias4444")  # failed: kept and tried again
    socket_server.whitelist_retry_interval = 0.1
    assert until(connection, "!unban~")[-1] == f"!unban~{PLAYER_UUID}|_Tobias4444"
    socket_server.whitelist_retry_interval = 3600
    assert db.get_plugin_tasks(server["id"]) != []
    connection.send(f"!UNBANNED~{PLAYER_UUID}|1|_Tobias4444")
    wait_for(lambda: db.get_plugin_tasks(server["id"]) == [])


def test_a_new_ban_cancels_an_unban_that_is_not_done_yet(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.ban_player(server["id"], "_Tobias4444", "Admin", days=0)
    db.unban_player(server["id"], player_id)
    db.ban_player(server["id"], "_Tobias4444", "Admin", days=0)
    assert db.get_plugin_tasks(server["id"]) == []


# ------------------------------------------------------------------ removals from the whitelist

def test_a_removal_from_the_whitelist_is_kept_until_it_worked(db, server, plugin, socket_server):  # noqa: F811
    connection = connect(plugin, server, "click,unwhitelist,confirm")
    db.add_plugin_task(server["id"], "unwhitelist", "altspieler", "AltSpieler")
    socket_server.handle_server_event({"type": "whitelist_remove", "server_id": server["id"], "names": ["AltSpieler"]})
    assert connection.recv() == "!whitelist~remove|AltSpieler"
    connection.send("!UNWHITELISTED~AltSpieler|0")
    assert db.get_plugin_tasks(server["id"])[0]["target"] == "altspieler"
    connection.send("!UNWHITELISTED~AltSpieler|1")
    wait_for(lambda: db.get_plugin_tasks(server["id"]) == [])


def test_accepting_a_name_again_cancels_its_removal(db, server):
    db.create_invite_code(server["id"], "HERBST-26")
    db.add_plugin_task(server["id"], "unwhitelist", "neuling_1", "Neuling_1")
    assert db.redeem_invite_code(server["id"], "Neuling_1", "HERBST-26") == "ok"
    assert db.get_plugin_tasks(server["id"]) == []
    request_id, _ = db.add_application(server["id"], "Neuling_2", "Ich baue gern Häfen.")
    db.add_plugin_task(server["id"], "unwhitelist", "neuling_2", "Neuling_2")
    db.handle_application(server["id"], request_id, True, "Mod")
    assert db.get_plugin_tasks(server["id"]) == []


def test_older_plugins_and_bad_answers(db, server, plugin, socket_server):  # noqa: F811
    connection = connect(plugin, server, "click")  # without "unwhitelist": the removal waits for a newer plugin
    db.add_plugin_task(server["id"], "unwhitelist", "altspieler", "AltSpieler")
    socket_server.send_plugin_tasks(server["id"])
    assert connection.request("!UNWHITELISTED~AltSpieler|2") == "error|005"
    assert connection.request("!BANNED~kein-uuid|1") == "error|005"
    assert db.get_plugin_tasks(server["id"]) != []


def test_ban_messages_keep_their_end_and_reason(db, server, plugin, socket_server):  # noqa: F811
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    connection = connect(plugin, server, "click,confirm")
    end = datetime(2027, 1, 2, 3, 4, tzinfo=timezone.utc)
    socket_server.handle_server_event({"type": "ban", "server_id": server["id"], "uuid": PLAYER_UUID, "name": "_Tobias4444",
                                       "reason": "Griefing", "end_ms": int(end.timestamp() * 1000)})
    message = connection.recv()
    assert message.startswith(f"!ban~{PLAYER_UUID}|_Tobias4444|{int(end.timestamp() * 1000)}|Griefing|bis 02.01.2027")
