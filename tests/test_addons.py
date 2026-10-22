"""Addons (database/addons.py, !ADDONS & co. in mc_socket/main.py, the store on /manage/<id>#addons)."""
import hashlib
import json
import zipfile

import pytest

from database import addons, config
from tests.conftest import PLAYER_UUID, wait_for
from tests.test_ingame import until
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import admin_client, app, client, on  # noqa: F401 (fixtures)

SETTINGS = [
    {"title": "Allgemein", "fields": [
        {"key": "enabled", "type": "toggle", "default": True, "label": "An"},
        {"key": "radius", "type": "number", "default": 20, "min": 2, "max": 500, "step": 1, "label": "Radius"},
        {"key": "boost", "type": "number", "default": 1.0, "min": 0, "max": 5, "step": 0.1, "label": "Schub"},
        {"key": "text", "type": "text", "default": "&aHallo", "max": 10, "colors": True, "label": "Text"},
        {"key": "mode", "type": "select", "default": "spawn", "label": "Modus",
         "options": [{"value": "spawn", "label": "Spawn"}, {"value": "custom", "label": "Eigener"}]},
        {"key": "modes", "type": "multiselect", "default": ["a"], "label": "Modi",
         "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
        {"key": "item", "type": "material", "default": "", "label": "Item"},
        {"key": "grid", "type": "recipe", "label": "Raster"},
        {"key": "center", "type": "position", "default": {"x": 1, "y": 2, "z": 3}, "label": "Mitte",
         "show_if": {"key": "mode", "in": ["custom"]}},
        {"key": "regions", "type": "list", "max_items": 2, "label": "Bereiche", "default": [], "fields": [
            {"key": "name", "type": "text", "required": True, "default": "x", "max": 32, "label": "Name"},
            {"key": "lines", "type": "lines", "max_items": 2, "max": 5, "default": [], "label": "Zeilen"}]},
    ]},
]


def manifest(addon_id="spawnelytra", name="SpawnElytra", settings=SETTINGS):
    return {"id": addon_id, "name": name, "summary": "Gleiten", "settings": settings}


# ------------------------------------------------------------------ the settings description

def test_manifest_is_checked():
    checked = addons.check_manifest(manifest())
    assert checked["settings"][0]["fields"][1] == {"key": "radius", "type": "number", "label": "Radius", "help": "",
                                                    "min": 2.0, "max": 500.0, "step": 1.0, "unit": "", "default": 20}
    assert checked["settings"][0]["fields"][7]["default"] == [""] * 9  # a missing default: the empty value
    for broken in ({"id": "Bad Id", "name": "x"}, {"id": "ok", "name": ""}, {"id": "ok", "name": "x", "settings": "no"},
                   manifest(settings=[{"fields": [{"key": "a", "type": "script"}]}]),
                   manifest(settings=[{"fields": [{"key": "a", "type": "list", "fields": [{"key": "b", "type": "list", "fields": []}]}]}]),
                   manifest(settings=[{"fields": [{"key": "a", "type": "toggle"}, {"key": "a", "type": "toggle"}]}]),
                   manifest(settings=[{"fields": [{"key": "a", "type": "number", "min": 5, "max": 1}]}]),
                   manifest(settings=[{"fields": [{"key": "a", "type": "select", "options": []}]}]),
                   dict(manifest(), description="x" * 70000)):
        with pytest.raises(addons.ManifestError):
            addons.check_manifest(broken)


def test_manifest_texts_are_cut_and_cleaned():
    checked = addons.check_manifest(dict(manifest(settings=[{"fields": [
        {"key": "a", "type": "toggle", "label": "⟦klick⇒/op⟧ §cRot\x00", "show_if": {"key": "missing", "is": True}}]}]),
        name="N" * 80))
    field = checked["settings"][0]["fields"][0]
    assert checked["name"] == "N" * 40 and field["label"] == "klick/op cRot"
    assert "show_if" not in field  # points to no sibling


def test_defaults_pass_unchanged():
    sections = addons.check_manifest(manifest())["settings"]
    defaults = addons.defaults(sections)
    assert addons.normalize(sections, defaults) == (defaults, [])
    assert addons.normalize(sections, {}) == (defaults, [])  # missing keys: the defaults, no error


def test_normalize_reports_and_repairs_values():
    sections = addons.check_manifest(manifest())["settings"]
    data = {"enabled": "yes", "radius": 9999, "boost": 0.333333, "text": "&aviel zu lang", "mode": "nope",
            "modes": ["b", "zz"], "item": "minecraft:Glass_Pane", "grid": ["stone"] * 8, "center": {"x": 1, "y": 99999, "z": 0},
            "regions": [{"name": " ", "lines": ["123456", "a", "b"]}, {"name": "b"}, {"name": "c"}], "unknown": 1}
    settings, errors = addons.normalize(sections, data)
    assert settings["enabled"] is True and settings["radius"] == 500 and settings["boost"] == 0.3333
    assert settings["text"] == "&aviel zu " and settings["mode"] == "spawn" and settings["modes"] == ["b"]
    assert settings["item"] == "glass_pane" and settings["grid"] == [""] * 9 and settings["center"] == {"x": 1, "y": 2, "z": 3}
    assert len(settings["regions"]) == 2 and settings["regions"][0] == {"name": "x", "lines": ["12345", "a"]}
    assert "unknown" not in settings
    paths = {e["path"] for e in errors}
    assert paths == {"enabled", "radius", "text", "mode", "modes", "grid", "center", "regions", "regions.0.name", "regions.0.lines"}


def test_normalize_edges():
    sections = addons.check_manifest(manifest())["settings"]
    ok = lambda **values: addons.normalize(sections, values)[1] == []
    assert ok(radius=2) and ok(radius=500) and not ok(radius=1) and not ok(radius=501)
    assert not ok(radius=True) and not ok(radius=float("nan"))  # bool and NaN are no numbers
    assert ok(text="x" * 10) and not ok(text="x" * 11)
    assert ok(item="") and ok(item="light") and not ok(item="glass pane") and not ok(item=5)
    assert ok(regions=[{}, {}]) and not ok(regions=[{}, {}, {}])
    assert ok(center={"x": 30_000_000, "y": -64, "z": -30_000_000}) and not ok(center={"x": 30_000_001, "y": 0, "z": 0})
    assert addons.normalize(sections, "no object")[1][0]["path"] == ""


def test_store_reads_the_jars(tmp_path, monkeypatch):
    jar = make_jar(tmp_path, "SpawnElytra", "2.0")
    make_jar(tmp_path, "SpawnElytra", "1.9")  # an older build: the newest wins
    (tmp_path / "broken.jar").write_bytes(b"no zip")
    monkeypatch.setattr(config, "ADDON_DIR", str(tmp_path))
    info = addons.catalog()["spawnelytra"]
    assert info["version"] == "2.0" and info["sha256"] == hashlib.sha256(jar.read_bytes()).hexdigest()
    assert info["file"] == "SpawnElytra.jar" and info["commands"][0]["aliases"] == ["se"]
    assert addons.install_message(info) == (f"!addoninstall~spawnelytra|2.0|{config.PUBLIC_SCHEME}://{config.BASE_DOMAIN}"
                                            f"/download/addons/spawnelytra.jar|{info['sha256']}|SpawnElytra.jar")
    assert addons.remove_message("x", "../evil.jar") == "!addonremove~x|"


def make_jar(folder, name, version, settings=SETTINGS, addon_id="spawnelytra"):
    path = folder / f"{name}-{version}.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.yml", f"name: {name}\nversion: '{version}'\nmain: x.Y\ncommands:\n  {addon_id}:\n"
                                       f"    description: Einstellen\n    usage: /<command> info\n    aliases: [se]\n")
        archive.writestr("mcconnect-addon.json", json.dumps(manifest(addon_id, name, settings)))
    return path


# ------------------------------------------------------------------ approval (fachliche Regeln)

def entry(addon_id="spawnelytra", sha256="a" * 64, version="2.0"):
    return {"id": addon_id, "name": "SpawnElytra", "plugin": "SpawnElytra", "version": version, "file": "SpawnElytra.jar",
            "sha256": sha256, "manifest": addons.check_manifest(manifest(addon_id))}


def test_official_jars_are_approved_others_wait(db, server):
    db.record_addon_releases([("a" * 64, "spawnelytra", "2.0")])
    rows = db.sync_reported_addons(server["id"], [entry(), entry("chatgames", "c" * 64)])
    official, other = sorted(rows, key=lambda row: row["addon_id"] != "spawnelytra")
    assert official["active"] and not other["active"] and not other["approved"]
    assert db.is_addon_active(server["id"], "spawnelytra") and not db.is_addon_active(server["id"], "chatgames")
    # a release of another addon id with the same checksum does not count
    assert not db.is_official_addon("chatgames", "a" * 64)
    db.set_addon_approved(server["id"], "chatgames", True)
    assert db.is_addon_active(server["id"], "chatgames")


def test_a_changed_jar_needs_a_new_approval(db, server):
    db.sync_reported_addons(server["id"], [entry(sha256="c" * 64)])
    db.set_addon_approved(server["id"], "spawnelytra", True)
    assert db.is_addon_active(server["id"], "spawnelytra")
    row, = db.sync_reported_addons(server["id"], [entry(sha256="d" * 64)])  # someone swapped the jar
    assert not row["active"] and row["status"] == "changed"
    db.set_addon_approved(server["id"], "spawnelytra", True)
    assert db.get_addon(server["id"], "spawnelytra")["status"] is None and db.is_addon_active(server["id"], "spawnelytra")


def test_the_approved_or_an_official_jar_lifts_the_lock(db, server):
    db.sync_reported_addons(server["id"], [entry(sha256="c" * 64)])
    db.set_addon_approved(server["id"], "spawnelytra", True)
    assert not db.sync_reported_addons(server["id"], [entry(sha256="d" * 64)])[0]["active"]
    assert db.sync_reported_addons(server["id"], [entry(sha256="c" * 64)])[0]["active"]  # the old jar is back
    db.sync_reported_addons(server["id"], [entry(sha256="d" * 64)])
    db.record_addon_releases([("e" * 64, "spawnelytra", "2.1")])
    row, = db.sync_reported_addons(server["id"], [entry(sha256="e" * 64)])  # replaced with an official release
    assert row["active"] and row["status"] is None


def test_an_official_update_keeps_the_approval(db, server):
    db.record_addon_releases([("a" * 64, "spawnelytra", "2.0"), ("b" * 64, "spawnelytra", "2.1")])
    db.sync_reported_addons(server["id"], [entry()])
    row, = db.sync_reported_addons(server["id"], [entry(sha256="b" * 64, version="2.1")])
    assert row["active"] and row["approved_sha256"] == "b" * 64


def test_paused_missing_and_removed_addons_are_off(db, server):
    db.record_addon_releases([("a" * 64, "spawnelytra", "2.0")])
    db.sync_reported_addons(server["id"], [entry()])
    db.set_addon_enabled(server["id"], "spawnelytra", False)
    assert not db.is_addon_active(server["id"], "spawnelytra")
    db.set_addon_enabled(server["id"], "spawnelytra", True)
    db.sync_reported_addons(server["id"], [])  # the plugin does not report it any more
    assert not db.get_addon(server["id"], "spawnelytra")["present"] and not db.is_addon_active(server["id"], "spawnelytra")
    db.sync_reported_addons(server["id"], [entry()])
    db.set_addon_result(server["id"], "spawnelytra", "removed")
    row = db.get_addon(server["id"], "spawnelytra")
    assert not row["present"] and not row["approved"]
    # added again by hand after a removal: the owner has to approve it again
    assert not db.sync_reported_addons(server["id"], [entry()])[0]["active"]


def test_store_install_approves_and_finishes(db, server):
    info = {"id": "spawnelytra", "plugin": "SpawnElytra", "version": "2.0", "sha256": "a" * 64,
            "manifest": addons.check_manifest(manifest())}
    db.request_addon_install(server["id"], info)
    assert db.get_pending_addon_actions(server["id"]) == [("spawnelytra", "install", None)]
    row, = db.sync_reported_addons(server["id"], [entry()])  # not in addon_releases, but the owner installed exactly it
    assert row["active"] and row["status"] is None and db.get_pending_addon_actions(server["id"]) == []
    db.set_addon_result(server["id"], "spawnelytra", "restart", "2.1")
    assert db.get_addon(server["id"], "spawnelytra")["status"] == "restart"
    assert db.sync_reported_addons(server["id"], [entry(version="2.1")])[0]["status"] is None  # restarted with 2.1


# ------------------------------------------------------------------ plugin protocol

@pytest.fixture
def store(tmp_path, monkeypatch):
    jar = make_jar(tmp_path, "SpawnElytra", "2.0")
    monkeypatch.setattr(config, "ADDON_DIR", str(tmp_path))
    return hashlib.sha256(jar.read_bytes()).hexdigest()


def connect(plugin, server, features="click,addons"):
    connection = plugin().auth(server["key"])
    assert connection.request(f"!FEATURES~{features}") == "success|106"
    assert connection.request("!BEAT_X~x") == "error|004"  # everything of !FEATURES is done
    return connection


def report(connection, *entries):
    connection.send("!ADDONS~" + json.dumps(list(entries)))
    messages = until(connection, "success|107")
    return messages[:-1]


def test_reported_addons_get_their_state(db, server, plugin, store):
    connection = connect(plugin, server)
    report(connection, entry(sha256=store), entry("chatgames", "c" * 64), {"id": "bad"}, "no object")
    states = {s.split("|")[0]: s for s in connection.addon_states}
    assert states["!addonstate~chatgames"] == "!addonstate~chatgames|0|{}"  # waits for the approval
    active, settings = states["!addonstate~spawnelytra"].split("|", 2)[1:]
    assert active == "1" and json.loads(settings)["radius"] == 20  # official (the store's jar): approved
    assert {a["addon_id"] for a in db.get_addons(server["id"])} == {"spawnelytra", "chatgames"}


def test_settings_from_the_game_are_checked_stored_and_sent_back(db, server, plugin, store):
    connection = connect(plugin, server)
    report(connection, entry(sha256=store))
    connection.addon_states.clear()
    connection.send("!ADDONCONFIG~spawnelytra|_Tobias4444|" + json.dumps({"radius": 9999, "mode": "custom"}))
    wait_for(lambda: connection.request("!BEAT_X~x") and connection.addon_states)
    settings = json.loads(connection.addon_states[0].split("|", 2)[2])
    assert settings["radius"] == 500 and settings["mode"] == "custom"  # corrected, not refused
    assert db.get_addon(server["id"], "spawnelytra")["config"]["radius"] == 500
    assert db.get_mod_log(server["id"])[0]["actor"] == "_Tobias4444"
    # an addon that is not active may not change anything
    report(connection, entry(sha256=store), entry("chatgames", "c" * 64))
    assert connection.request("!ADDONCONFIG~chatgames||{}") == "error|005"


def test_install_and_remove_from_the_store(db, server, plugin, store, socket_server):
    connection = connect(plugin, server)
    db.request_addon_install(server["id"], addons.catalog()["spawnelytra"])
    socket_server.handle_server_event({"server_id": server["id"], "type": "addon_install", "addon_id": "spawnelytra"})
    assert connection.recv().startswith("!addoninstall~spawnelytra|2.0|")
    connection.send("!ADDONRESULT~spawnelytra|failed|Kein Platz")
    wait_for(lambda: db.get_addon(server["id"], "spawnelytra")["status"] == "failed")
    assert db.get_addon(server["id"], "spawnelytra")["status_text"] == "Kein Platz"
    assert connection.request("!ADDONRESULT~spawnelytra|weird|") == "error|005"
    report(connection, entry(sha256=store))
    db.request_addon_removal(server["id"], "spawnelytra")
    socket_server.handle_server_event({"server_id": server["id"], "type": "addon_remove", "addon_id": "spawnelytra"})
    assert connection.recv() == "!addonremove~spawnelytra|SpawnElytra.jar"


def test_actions_wait_for_the_plugin(db, server, plugin, store):
    db.request_addon_install(server["id"], addons.catalog()["spawnelytra"])
    old = plugin().auth(server["key"])  # plugins before 3.22 get nothing
    assert old.request("!FEATURES~click") == "success|106"
    old.close()
    connection = plugin().auth(server["key"])
    connection.send("!FEATURES~click,addons")
    assert until(connection, "!addoninstall~")[-1].startswith("!addoninstall~spawnelytra|")


def test_join_messages_need_the_addon(db, server, plugin, two_players):
    old = plugin().auth(server["key"])  # before 3.22: only told to forget the join messages
    old.send(f"!JOIN~{PLAYER_UUID}|_Tobias4444")
    assert until(old, "success|101")[-1] == "success|101"
    old.close()
    connection = connect(plugin, server)
    connection.addon_states.clear()
    report(connection)  # 3.22 without BetterJoinMessages: nothing for it
    connection.send("!CMD~" + PLAYER_UUID + "|world|0|0|0|joinmessage|")
    assert "BetterJoinMessages" in until(connection, "!tell~")[-1]


# ------------------------------------------------------------------ the store on the admin page

def test_store_page_and_actions(admin_client, db, server, store):
    db.set_plugin_features(server["id"], {"click", "addons"})
    view = admin_client.get(f"/api/servers/{server['id']}/addons", **on(None)).json
    addon, = view["addons"]
    assert addon["state"] == "available" and addon["official"] and view["plugin"]["supports"]
    response = admin_client.post(f"/api/servers/{server['id']}/addons/spawnelytra/install", json={}, **on(None))
    assert response.status_code == 200 and response.json["addons"][0]["state"] == "install"
    assert admin_client.post(f"/api/servers/{server['id']}/addons/nothing/install", json={}, **on(None)).status_code == 404
    bad = admin_client.post(f"/api/servers/{server['id']}/addons/spawnelytra/config", json={"config": {"radius": 1}}, **on(None))
    assert bad.status_code == 400 and bad.json["errors"] == [{"path": "radius", "message": "Muss zwischen 2 und 500 liegen."}]
    good = admin_client.post(f"/api/servers/{server['id']}/addons/spawnelytra/config", json={"config": {"radius": 30}}, **on(None))
    assert good.json["addons"][0]["config"]["radius"] == 30
    assert admin_client.post(f"/api/servers/{server['id']}/addons/spawnelytra/enabled", json={"enabled": False},
                             **on(None)).status_code == 200
    assert [e["action"] for e in db.get_mod_log(server["id"])][:3] == ["addon_pause", "addon_config", "addon_install"]
    download = admin_client.get("/download/addons/spawnelytra.jar", **on(None))
    assert download.status_code == 200 and hashlib.sha256(download.data).hexdigest() == store
    assert admin_client.get("/download/addons/../x.jar", **on(None)).status_code == 404


def test_old_plugins_cannot_install(admin_client, db, server, store):
    db.set_plugin_features(server["id"], {"click", "glyphs"})
    response = admin_client.post(f"/api/servers/{server['id']}/addons/spawnelytra/install", json={}, **on(None))
    assert response.status_code == 409 and "3.22" in response.json["error"]


def test_store_is_for_the_owner_only(admin_client, db, server, other_server, store):
    other_admin = db.add_server_admin("eve", "testPassword", "eve@example.com", email_verified=True)
    foreign = db.add_server(other_admin, "foreign", "f.example.com", "Foreign", server_key="f" * 64)
    assert admin_client.get(f"/api/servers/{foreign}/addons", **on(None)).status_code == 404
    assert admin_client.post(f"/api/servers/{foreign}/addons/spawnelytra/install", json={}, **on(None)).status_code == 404
    admin_client.post("/api/logout", json={}, **on(None))
    assert admin_client.get(f"/api/servers/{server['id']}/addons", **on(None)).status_code == 401


def test_moderators_see_that_the_join_addon_is_missing(client, db, server, two_players):
    from tests.test_web import login_player
    db.set_plugin_connected(server["id"], True)
    login_player(client, db, two_players[0])
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert "BetterJoinMessages" in client.get("/users/belohnungen", **on("testdomain")).get_data(as_text=True)
    assert 'id="join"' not in client.get("/profil", **on("testdomain")).get_data(as_text=True)
    assert client.post("/api/joinstyle", json={"field": "color", "value": "white"}, **on("testdomain")).status_code == 409


# ------------------------------------------------------------------ owner rights: moderators, automatic updates

def test_automatic_updates_only_for_official_older_jars(db, server, admin_id, plugin, store):
    db.record_addon_releases([("a" * 64, "spawnelytra", "1.9")])  # the server runs the official 1.9
    connection = connect(plugin, server)
    report(connection, entry(version="1.9"))  # switch off: nothing
    assert db.get_addon(server["id"], "spawnelytra")["status"] is None
    assert db.update_server(server["id"], admin_id, addon_auto_update=True)
    assert report(connection, entry(version="1.9"))[-1].startswith("!addoninstall~spawnelytra|2.0|")
    assert db.get_mod_log(server["id"])[0]["actor"] == "Auto-Update"
    # an own build with the same id (not an official jar) or the newest version: left alone
    db.set_addon_result(server["id"], "spawnelytra", "installed")
    db._execute("UPDATE server_addons SET status = NULL")
    assert not any(m.startswith("!addoninstall") for m in report(connection, entry(sha256="f" * 64, version="1.9")))
    assert not any(m.startswith("!addoninstall") for m in report(connection, entry(sha256=store, version="2.0")))


def test_owner_switches(admin_client, db, server, store):
    response = admin_client.post(f"/api/servers/{server['id']}/addons/rights", json={"moderators": True}, **on(None))
    assert response.json["rights"] == {"moderators": True, "auto_update": False, "moderator": False}
    assert db.get_addon_rights(server["id"]) == (True, False)
    assert admin_client.post(f"/api/servers/{server['id']}/addons/rights", json={"auto_update": True},
                             **on(None)).json["rights"]["auto_update"] is True


@pytest.fixture
def mod(client, db, server, two_players):
    from tests.test_web import login_player
    db.set_plugin_connected(server["id"], True)
    login_player(client, db, two_players[0])
    db.set_moderator(server["id"], "_Tobias4444", True)
    return client


def test_moderators_edit_settings_only_if_allowed(mod, db, server, admin_id, store):
    db.record_addon_releases([(store, "spawnelytra", "2.0")])
    db.sync_reported_addons(server["id"], [entry(sha256=store)])
    assert mod.get("/users/erweiterungen", **on("testdomain")).status_code == 403
    assert mod.get("/api/mod/addons", **on("testdomain")).status_code == 403
    assert "/users/erweiterungen" not in mod.get("/users", **on("testdomain")).get_data(as_text=True)  # no tab
    assert db.update_server(server["id"], admin_id, addon_mods=True)
    assert "/users/erweiterungen" in mod.get("/users", **on("testdomain")).get_data(as_text=True)
    view = mod.get("/api/mod/addons", **on("testdomain")).json
    assert [a["id"] for a in view["addons"]] == ["spawnelytra"] and view["rights"]["moderator"]
    bad = mod.post("/api/mod/addons/spawnelytra/config", json={"config": {"radius": 1}}, **on("testdomain"))
    assert bad.status_code == 400
    assert mod.post("/api/mod/addons/spawnelytra/config", json={"config": {"radius": 40}},
                    **on("testdomain")).json["addons"][0]["config"]["radius"] == 40
    assert db.get_mod_log(server["id"])[0]["action"] == "addon_config"
    # installing, approving and removing stay with the owner
    assert mod.post("/api/mod/addons/spawnelytra/remove", json={}, **on("testdomain")).status_code in (404, 405)
    assert mod.post(f"/api/servers/{server['id']}/addons/spawnelytra/remove", json={}, **on(None)).status_code == 401


# ------------------------------------------------------------------ whitelist cleanup (plugin 3.23)

def test_moderators_take_players_off_the_whitelist(mod, db, server, plugin, socket_server):
    names = {"names": ["Notch", "_Tobias4444"]}
    assert mod.post("/api/mod/whitelist/remove", json={"names": ["bad name"]}, **on("testdomain")).status_code == 400
    assert mod.post("/api/mod/whitelist/remove", json={"names": []}, **on("testdomain")).status_code == 400
    db.set_plugin_features(server["id"], {"click"})  # an older plugin
    assert "3.23" in mod.post("/api/mod/whitelist/remove", json=names, **on("testdomain")).json["error"]
    connection = connect(plugin, server, features="click,addons,unwhitelist")
    assert mod.post("/api/mod/whitelist/remove", json=names, **on("testdomain")).json == {"removed": names["names"]}
    assert until(connection, "!whitelist~remove|_Tobias4444")[-2:] == ["!whitelist~remove|Notch", "!whitelist~remove|_Tobias4444"]
    assert db.get_mod_log(server["id"])[0]["action"] == "whitelist_remove"
    db.set_plugin_connected(server["id"], False)
    assert "nicht verbunden" in mod.post("/api/mod/whitelist/remove", json=names, **on("testdomain")).json["error"]


def test_only_moderators_take_players_off_the_whitelist(client, db, server, two_players):
    from tests.test_web import login_player
    db.set_plugin_connected(server["id"], True)
    login_player(client, db, two_players[0])
    assert client.post("/api/mod/whitelist/remove", json={"names": ["Notch"]}, **on("testdomain")).status_code == 403
