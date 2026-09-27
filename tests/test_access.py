"""Phase 8: whitelist access, warnings and mutes, X-ray hints, alerts, rules and FAQ."""
import json

import pytest

from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_ingame import cmd, tells, until
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import admin_client, app, client, login_player, on  # noqa: F401 (fixtures)


@pytest.fixture
def mod_client(client, db, server, two_players):
    db.set_plugin_connected(server["id"], True)
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET online = true")
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    return client


@pytest.fixture
def game(db, server, plugin, two_players):
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    assert connection.request(f"!JOIN~{OTHER_UUID}|Notch") == "success|101"
    return connection


# ------------------------------------------------------------------ whitelist access

def test_access_page_is_off_by_default(client, server):
    assert client.get("/mitmachen", **on("testdomain")).status_code == 404
    assert client.post("/api/access/apply", json={}, **on("testdomain")).status_code == 404


def test_application_flow(client, mod_client, db, server, plugin):
    db.update_server_settings(server["id"], access_mode="application")
    assert "Bewerben" in client.get("/mitmachen", **on("testdomain")).get_data(as_text=True)
    body = {"name": "Neuling_1", "message": "Ich baue gern Häfen und Brücken."}
    assert client.post("/api/access/apply", json=dict(body, name="a b"), **on("testdomain")).status_code == 400
    assert client.post("/api/access/apply", json=body, **on("testdomain")).status_code == 200
    assert client.post("/api/access/apply", json=body, **on("testdomain")).status_code == 409  # already open
    request_id = db.list_access_requests(server["id"])[0]["id"]
    assert "Neuling_1" in mod_client.get("/users", **on("testdomain")).get_data(as_text=True)

    assert mod_client.post("/api/mod/applications", json={"id": request_id, "accept": True},
                           **on("testdomain")).status_code == 200
    assert db.get_unsynced_whitelist(server["id"]) == ["Neuling_1"]
    connection = plugin()  # the plugin gets the name when it connects
    connection.send(f"!AUTH~{server['key']}")
    messages = until(connection, "!whitelist~")
    assert "!joininfo~http" in "\n".join(messages) and messages[-1] == "!whitelist~add|Neuling_1"
    assert db.get_unsynced_whitelist(server["id"]) == []


def test_invite_codes(client, mod_client, db, server):
    db.update_server_settings(server["id"], access_mode="code")
    response = mod_client.post("/api/mod/codes", json={"code": "sommer-26", "max_uses": 1, "days": 7}, **on("testdomain"))
    assert response.json["code"] == "SOMMER-26"
    assert mod_client.post("/api/mod/codes", json={"code": "SOMMER-26"}, **on("testdomain")).status_code == 409
    redeem = lambda name, code: client.post("/api/access/redeem", json={"name": name, "code": code}, **on("testdomain"))
    assert redeem("Neuling_1", "falsch").status_code == 400
    assert redeem("Neuling_1", "sommer-26").status_code == 200
    assert redeem("Neuling_2", "SOMMER-26").status_code == 400  # used up
    assert db.list_invite_codes(server["id"])[0]["uses"] == 1
    assert db.get_unsynced_whitelist(server["id"]) == ["Neuling_1"]


def test_code_guessing_is_limited(client, db, server):
    db.update_server_settings(server["id"], access_mode="code")
    codes = [client.post("/api/access/redeem", json={"name": "Neuling_1", "code": f"X{i}"}, **on("testdomain")).status_code
             for i in range(11)]
    assert codes[-1] == 429


def test_join_info_follows_the_setting(mod_client, db, server, plugin, socket_server):
    connection = plugin().auth(server["key"])
    assert mod_client.post("/api/mod/settings", json={"access_mode": "both"}, **on("testdomain")).status_code == 200
    socket_server.handle_server_event({"server_id": server["id"], "type": "joininfo"})
    from mc_socket.commands import page_url
    assert connection.recv() == "!joininfo~" + page_url(db, server["id"], "/mitmachen")


# ------------------------------------------------------------------ warnings and mutes

def test_warnings_ban_at_the_threshold(db, server, game):
    db.set_moderator(server["id"], "_Tobias4444", True)
    db.update_server_settings(server["id"], warn_threshold=2, warn_ban_days=3)
    cmd(game, PLAYER_UUID, "verwarnen", "Notch Spam im Chat")
    assert tells(game, 2) == ["&c&lVerwarnung (1/2): &fSpam im Chat", "&aNotch ist verwarnt (1/2)."]
    cmd(game, PLAYER_UUID, "verwarnen", "Notch schon wieder")
    messages = [game.recv() for _ in range(3)]
    assert messages[1].startswith(f"!ban~{OTHER_UUID}|Notch|")
    assert messages[2] == f"!tell~{PLAYER_UUID}|&aNotch ist verwarnt (2/2) und für 3 Tage gebannt."
    notch = db.get_player_id_from_mojang_uuid_and_server_id(OTHER_UUID, server["id"])
    assert len(db.get_warnings(notch)) == 2 and db.get_ban_reason_from_player_id(notch)
    assert [e["action"] for e in db.get_mod_log(server["id"])][:2] == ["ban", "warn"]


def test_only_moderators_warn_in_game(game):
    cmd(game, OTHER_UUID, "verwarnen", "_Tobias4444 Test")
    assert tells(game, 1) == ["&cDas dürfen nur Moderatoren."]


def test_mute_in_game(db, server, game):
    db.set_moderator(server["id"], "_Tobias4444", True)
    cmd(game, PLAYER_UUID, "stumm", "Notch 10 Caps")
    mute, told, answer = game.recv(), game.recv(), game.recv()
    assert mute.startswith(f"!mute~{OTHER_UUID}|") and mute.endswith("|Caps")
    assert told.startswith(f"!tell~{OTHER_UUID}|&cDu bist stummgeschaltet bis ")
    assert answer.startswith(f"!tell~{PLAYER_UUID}|&aNotch ist stummgeschaltet bis ")
    assert OTHER_UUID in db.get_mutes(server["id"])
    cmd(game, PLAYER_UUID, "entstummen", "Notch")
    assert game.recv() == f"!mute~{OTHER_UUID}|0|"
    assert db.get_mutes(server["id"]) == {}


def test_mutes_are_sent_on_connect(db, server, plugin, two_players):
    from datetime import datetime, timedelta, timezone
    db.set_mute(two_players[1], datetime.now(timezone.utc) + timedelta(minutes=5), "Caps")
    connection = plugin()
    connection.send(f"!AUTH~{server['key']}")
    assert until(connection, "!mute~")[-1].startswith(f"!mute~{OTHER_UUID}|")


def test_warn_and_mute_on_the_website(mod_client, db, server, two_players):
    assert mod_client.post("/api/mod/warn", json={"name": "Notch", "reason": "Griefing"}, **on("testdomain")).status_code == 200
    assert mod_client.post("/api/mod/mute", json={"name": "Notch", "minutes": 60}, **on("testdomain")).status_code == 200
    html = mod_client.get("/spieler?player=Notch", **on("testdomain")).get_data(as_text=True)
    assert "Verwarnungen: 1" in html and "Griefing" in html and "stumm bis" in html
    warning_id = db.get_warnings(two_players[1])[0]["id"]
    assert mod_client.post("/api/mod/warnings/delete", json={"id": warning_id}, **on("testdomain")).status_code == 200
    assert mod_client.post("/api/mod/mute", json={"name": "Notch", "minutes": 0}, **on("testdomain")).status_code == 200
    assert db.get_warnings(two_players[1]) == [] and db.get_mutes(server["id"]) == {}


# ------------------------------------------------------------------ x-ray, rules, alerts

def test_xray_hints(mod_client, db, server, two_players):
    a, b = two_players
    db.update_player_stats(b, {"stats": {"minecraft:mined": {"minecraft:stone": 2500, "minecraft:deepslate_diamond_ore": 40}}})
    db.update_player_stats(a, {"stats": {"minecraft:mined": {"minecraft:stone": 9000, "minecraft:diamond_ore": 12}}})
    html = mod_client.get("/users", **on("testdomain")).get_data(as_text=True)
    section = html.split('id="xray"')[1].split("</section>")[0]
    assert "Notch" in section and "16,0" in section and "_Tobias4444" not in section


def test_rules_page_and_first_join(mod_client, client, db, server, plugin):
    assert client.get("/regeln", **on("testdomain")).status_code == 404
    body = {"rules_enabled": True, "rules": "Kein Griefing.\n\nSeid nett.", "faq": "Wie komme ich zum Spawn?\nMit /spawn.\n\nGibt es Claims?\nNein."}
    assert mod_client.post("/api/mod/settings", json=body, **on("testdomain")).status_code == 200
    html = client.get("/regeln", **on("testdomain")).get_data(as_text=True)
    assert "Kein Griefing." in html and "Gibt es Claims?" in html and "Mit /spawn." in html
    connection = plugin().auth(server["key"])
    new_uuid = "11111111-2222-3333-4444-555555555555"
    assert connection.request(f"!JOIN~{new_uuid}|Neuling") == "success|101"
    assert connection.recv().startswith(f"!tell~{new_uuid}|&6Willkommen auf dem Server! &7Bitte lies zuerst die Regeln:")


def test_offline_alert_is_sent_once(db, server, socket_server):
    class Mailer:
        sent = []

        def send_email(self, recipient, subject, html):
            self.sent.append((recipient, subject))
    socket_server.mailer = Mailer()
    with db._cursor() as cur:
        cur.execute("UPDATE servers SET plugin_connected = false, plugin_last_seen = now() - interval '10 minutes'")
    socket_server.send_alerts()
    socket_server.send_alerts()
    assert Mailer.sent == [("tobi@example.com", "MCConnect: Test Server ist nicht erreichbar")]
    db.update_server_settings(server["id"], alerts_enabled=False)
    with db._cursor() as cur:
        cur.execute("UPDATE servers SET alert_offline_at = NULL")
    socket_server.send_alerts()
    assert len(Mailer.sent) == 1


def test_tps_alert(db, server, socket_server):
    for _ in range(5):
        db.add_health_sample(server["id"], {"tps": 11.5})
    kinds = [a["kind"] for a in db.get_alert_candidates()]
    assert kinds == ["tps"]
    db.mark_alert_sent(server["id"], "tps")
    assert db.get_alert_candidates() == []


def test_admin_page_shows_health_and_alert_setting(admin_client, db, server):
    db.add_health_sample(server["id"], {"tps": 19.9, "mem_used_mb": 1024, "mem_max_mb": 4096, "players": 2})
    html = admin_client.get("/manage", **on(None)).get_data(as_text=True)
    assert "Serverzustand" in html and "19,9" in html and "E-Mail an mich" in html
    assert admin_client.post(f"/api/servers/{server['id']}/update", json={"alerts_enabled": False},
                             **on(None)).status_code == 200
    assert db.get_server_settings(server["id"])["alerts_enabled"] is False


# ------------------------------------------------------------------ website bans and /pardon in the game

def test_bans_made_while_offline_are_sent_on_connect(db, server, plugin, two_players):
    db.ban_player(server["id"], "Notch", "Admin tobi", days=3)
    assert len(db.get_undelivered_web_bans(server["id"])) == 1
    connection = plugin()
    connection.send(f"!AUTH~{server['key']}")
    assert until(connection, "!ban~")[-1].startswith(f"!ban~{OTHER_UUID}|Notch|")
    assert db.get_undelivered_web_bans(server["id"]) == []


def test_pardon_in_game_lifts_the_website_ban(db, server, plugin, two_players):
    connection = plugin().auth(server["key"])
    db.ban_player(server["id"], "Notch", "Admin tobi", days=3)
    db.mark_ban_delivered(server["id"], OTHER_UUID)
    notch = db.get_player_id_from_mojang_uuid_and_server_id(OTHER_UUID, server["id"])
    assert connection.request('!WEBBANS~[]') == "success|105"
    assert db.get_ban_reason_from_player_id(notch)  # just delivered: the plugin may not have applied it yet
    with db._cursor() as cur:
        cur.execute("UPDATE banned_players SET delivered_at = now() - interval '5 minutes'")
    assert connection.request('!WEBBANS~["notch"]') == "success|105"
    assert db.get_ban_reason_from_player_id(notch)  # still in the list
    assert connection.request('!WEBBANS~[]') == "success|105"
    assert db.get_ban_reason_from_player_id(notch) is None
    assert db.get_mod_log(server["id"])[0]["action"] == "ingame_pardon"


def test_undelivered_bans_are_not_pardoned(db, server, two_players):
    db.ban_player(server["id"], "Notch", "Admin tobi", days=3)
    assert db.sync_web_bans(server["id"], []) == []
