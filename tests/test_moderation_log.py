"""Moderation log, notes about players and the server health reported by the plugin."""
import json
from datetime import timedelta

import pytest

from mc_socket.main import parse_health
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import admin_client, app, client, login_player, on  # noqa: F401 (fixtures)

HEALTH = {"tps": 19.84, "mem_used_mb": 2048, "mem_max_mb": 4096, "players": 3, "chunks": 812, "entities": 1400,
          "uptime_s": 93784, "mc_version": "1.21.1-R0.1-SNAPSHOT", "plugin_version": "3.2"}


@pytest.fixture
def mod(client, db, server):
    """Client logged in as the moderator _Tobias4444; Notch is another player."""
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.ensure_player_on_server(server["id"], OTHER_UUID, "Notch")
    db.set_plugin_connected(server["id"], True)
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, player_id).json["status"] == "success"
    return client


def actions(db, server):
    return [(e["actor"], e["action"], e["target_name"]) for e in reversed(db.get_mod_log(server["id"]))]


# ------------------------------------------------------------------ log

def test_ban_and_unban_are_logged(mod, db, server):
    assert mod.post("/api/mod/ban", json={"name": "Notch", "days": 3, "comment": "Griefing"},
                    **on("testdomain")).status_code == 200
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(OTHER_UUID, server["id"])
    assert mod.post("/api/mod/unban", json={"player_id": str(player_id)}, **on("testdomain")).status_code == 200
    assert actions(db, server) == [("_Tobias4444", "ban", "Notch"), ("_Tobias4444", "unban", "Notch")]
    assert "Griefing" in db.get_mod_log(server["id"])[1]["details"]
    html = mod.get("/users/protokoll", **on("testdomain")).get_data(as_text=True)
    assert "Protokoll" in html and "hat gebannt" in html and "hat entbannt" in html


def test_admin_actions_are_logged(admin_client, db, server):
    db.ensure_player_on_server(server["id"], PLAYER_UUID, "_Tobias4444")
    url = f"/api/servers/{server['id']}"
    assert admin_client.post(f"{url}/moderators", json={"name": "_tobias4444", "moderator": True}, **on(None)).status_code == 200
    assert admin_client.post(f"{url}/ban", json={"name": "_Tobias4444", "days": 0}, **on(None)).status_code == 200
    assert actions(db, server) == [("Admin tobi", "mod_add", "_Tobias4444"), ("Admin tobi", "ban", "_Tobias4444")]


def test_competitions_are_logged(mod, db, server):
    today = db.get_today()
    body = {"title": "Angel-Cup", "metric": "fish_caught", "starts_on": today.isoformat(),
            "ends_on": (today + timedelta(days=3)).isoformat()}
    cid = mod.post("/api/mod/competitions", json=body, **on("testdomain")).json["id"]
    mod.post("/api/mod/competitions/delete", json={"id": cid}, **on("testdomain"))
    log = db.get_mod_log(server["id"])
    assert [e["action"] for e in reversed(log)] == ["competition_create", "competition_delete"]
    assert log[1]["details"].startswith("Angel-Cup · Fische gefangen")


def test_ingame_bans_are_logged(db, server):
    db.ensure_player_on_server(server["id"], OTHER_UUID, "Notch")
    entry = {"name": "Notch", "reason": "Hacks", "source": "Console", "created": 0, "expires": 0}
    db.sync_ingame_bans(server["id"], [entry])
    db.sync_ingame_bans(server["id"], [entry])  # unchanged: no new entry
    db.sync_ingame_bans(server["id"], [])
    assert actions(db, server) == [("Console", "ingame_ban", "Notch"), (None, "ingame_unban", "Notch")]


def test_log_filter_by_player(db, server):
    db.add_mod_log(server["id"], "a", "ban", "Notch")
    db.add_mod_log(server["id"], "a", "ban", "jeb_")
    assert [e["target_name"] for e in db.get_mod_log(server["id"], target_name="notch")] == ["Notch"]


# ------------------------------------------------------------------ notes

def test_notes(mod, db, server):
    assert mod.post("/api/mod/notes", json={"name": "notch", "text": "Hat sich entschuldigt."},
                    **on("testdomain")).status_code == 200
    html = mod.get("/spieler?player=Notch", **on("testdomain")).get_data(as_text=True)
    assert "nur für Moderatoren sichtbar" in html and "Hat sich entschuldigt." in html
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(OTHER_UUID, server["id"])
    note = db.get_player_notes(player_id)[0]
    assert note["author"] == "_Tobias4444"
    assert mod.post("/api/mod/notes/delete", json={"id": note["id"]}, **on("testdomain")).status_code == 200
    assert db.get_player_notes(player_id) == []
    assert [e["action"] for e in reversed(db.get_mod_log(server["id"]))] == ["note_add", "note_delete"]


@pytest.mark.parametrize("body, status", [({"name": "Notch", "text": ""}, 400), ({"name": "Notch", "text": "x" * 1001}, 400),
                                          ({"name": "nobody", "text": "hi"}, 404)])
def test_note_validation(mod, body, status):
    assert mod.post("/api/mod/notes", json=body, **on("testdomain")).status_code == status


def test_notes_only_for_moderators(client, db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.set_plugin_connected(server["id"], True)
    db.add_player_note(player_id, "x", "geheim")
    assert login_player(client, db, player_id).json["status"] == "success"
    assert "geheim" not in client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert client.post("/api/mod/notes", json={"name": "_Tobias4444", "text": "x"}, **on("testdomain")).status_code == 403


def test_notes_of_other_servers_cannot_be_deleted(mod, db, other_server):
    other = db.ensure_player_on_server(other_server["id"], OTHER_UUID, "Notch")
    note_id = db.add_player_note(other, "x", "fremd")
    assert mod.post("/api/mod/notes/delete", json={"id": note_id}, **on("testdomain")).status_code == 404


# ------------------------------------------------------------------ server health

def test_plugin_reports_health(db, server, plugin):
    client = plugin().auth(server["key"])
    assert client.request("!HEALTH~" + json.dumps(HEALTH)) == "success|104"
    latest = db.get_latest_health(server["id"])
    assert latest["tps"] == pytest.approx(19.84) and latest["chunks"] == 812 and latest["plugin_version"] == "3.2"
    assert client.request("!HEALTH~[1, 2]") == "error|005"


def test_parse_health_drops_invalid_values():
    sample = parse_health(json.dumps({"tps": "fast", "mem_used_mb": -5, "players": 2, "mc_version": "x" * 300}))
    assert sample["tps"] is None and sample["mem_used_mb"] is None and sample["players"] == 2
    assert len(sample["mc_version"]) == 100


def test_health_on_moderation_page(mod, db, server):
    db.add_health_sample(server["id"], HEALTH)
    html = mod.get("/users", **on("testdomain")).get_data(as_text=True)
    assert "Serverzustand" in html and "19,8" in html and "✓ flüssig" in html
    assert "1 Tag 2 Std." in html  # 93784 s uptime
    assert 'data-line-chart="tps-data"' in html
    assert db.get_health_availability(server["id"]) == pytest.approx(1.0)


def test_health_offline_warning(mod, db, server):
    db.set_plugin_connected(server["id"], False)
    html = mod.get("/users", **on("testdomain")).get_data(as_text=True)
    assert "nicht mit MCConnect verbunden" in html and "Plugin 3.2 oder neuer" in html


def test_old_health_samples_are_deleted(db, server):
    db.add_health_sample(server["id"], HEALTH)
    with db._cursor() as cur:
        cur.execute("UPDATE server_health SET at = now() - interval '8 days'")
    db.add_health_sample(server["id"], HEALTH)
    assert db._fetchvalue("SELECT count(*) FROM server_health") == 1


def test_health_hint_when_plugin_sends_nothing(mod, db, server):
    html = mod.get("/users", **on("testdomain")).get_data(as_text=True)
    assert "schickt aber keinen Serverzustand" in html
    db.add_health_sample(server["id"], HEALTH)
    with db._cursor() as cur:
        cur.execute("UPDATE server_health SET at = now() - interval '1 hour'")
    html = mod.get("/users", **on("testdomain")).get_data(as_text=True)
    assert "schickt aber keinen Serverzustand mehr (zuletzt" in html
