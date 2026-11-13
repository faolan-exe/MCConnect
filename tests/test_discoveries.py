"""The addon Discoveries: places found by entering them (database/discoveries.py, plugin side in java plugin/addons)."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from database import addons, discoveries
from tests.conftest import OTHER_UUID, PLAYER_UUID, wait_for
from tests.test_addons import connect
from tests.test_ingame import until
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_mobile import browser, live_site, phone  # noqa: F401 (fixtures)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

MANIFEST = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "java plugin", "addons", "Discoveries", "src", "main", "resources", "mcconnect-addon.json")
AREAS = [
    {"name": "Hafen", "title": "Der alte Hafen", "description": "Wo die Boote schlafen", "icon": "oak_boat"},
    {"name": "Mine", "title": "Tiefe Mine", "icon": "unknown_thing"},
    {"name": "Turm", "title": "Turm", "enabled": False},
]


def place_row(**fields):
    return dict({"shape": "circle", "center": {"x": 0, "y": 64, "z": 0}, "radius": 20, "enabled": True}, **fields)


@pytest.fixture
def addon(db, server):
    """Discoveries installed, approved and with three places (one switched off)."""
    with open(MANIFEST, encoding="utf-8") as file:
        manifest = addons.check_manifest(json.load(file))
    db.record_addon_releases([("d" * 64, discoveries.ADDON_ID, "1.0")])
    db.sync_reported_addons(server["id"], [{"id": discoveries.ADDON_ID, "name": "Discoveries", "plugin": "Discoveries",
                                            "version": "1.0", "file": "Discoveries-1.0.jar", "sha256": "d" * 64,
                                            "manifest": manifest}])
    config = addons.normalize(manifest["settings"], {"areas": [place_row(**a) for a in AREAS], "first_broadcast": True})
    db.set_addon_config(server["id"], discoveries.ADDON_ID, config[0] if isinstance(config, tuple) else config)
    assert db.is_addon_active(server["id"], discoveries.ADDON_ID)
    return db.get_addon(server["id"], discoveries.ADDON_ID)


# ------------------------------------------------------------------ places (the rules of the plugin)

def test_places_follow_the_rules_of_the_plugin():
    addon = {"active": True, "config": {"areas": [
        {"name": "Hafen", "title": "Der alte Hafen", "icon": "oak_boat"},
        {"name": "hafen", "title": "Doppelt"},              # the same id: only the first counts
        {"name": "mit leerzeichen", "title": "Ungültig"},   # not a valid name
        {"name": "Aus", "title": "Aus", "enabled": False},  # switched off
        {"name": "Ohne", "title": "  ", "icon": ""},        # no title: the name; no icon: a map
    ]}}
    assert discoveries.places(addon) == [
        {"id": "hafen", "title": "Der alte Hafen", "description": "", "icon": "oak_boat"},
        {"id": "ohne", "title": "Ohne", "description": "", "icon": "filled_map"}]
    assert discoveries.places(dict(addon, active=False)) == []
    assert discoveries.places(None) == []


def test_the_manifest_is_valid():
    with open(MANIFEST, encoding="utf-8") as file:
        manifest = addons.check_manifest(json.load(file))
    assert manifest["id"] == discoveries.ADDON_ID
    assert discoveries.ADDON_ID in addons.STORE


# ------------------------------------------------------------------ database

def test_a_place_is_found_once_and_the_first_one_counts(db, server, two_players):  # noqa: F811
    a, b = two_players
    now = datetime.now(timezone.utc)
    assert db.add_discovery(server["id"], PLAYER_UUID, "hafen", now - timedelta(minutes=5)) == \
        {"new": True, "first": True, "player_id": a}
    assert db.add_discovery(server["id"], PLAYER_UUID, "hafen", now) == {"new": False, "first": False, "player_id": a}
    assert db.add_discovery(server["id"], OTHER_UUID, "hafen", now)["first"] is False
    assert db.add_discovery(server["id"], OTHER_UUID, "mine", now + timedelta(days=1))["first"] is True
    assert db.get_player_discoveries(a)["hafen"]["first"] is True
    assert db.get_player_discoveries(b)["hafen"]["first"] is False
    assert db.get_player_discoveries(b)["mine"]["at"] <= datetime.now(timezone.utc)  # never in the future
    overview = db.get_discovery_overview(server["id"])
    assert overview["hafen"]["count"] == 2 and overview["hafen"]["first_name"] == "_Tobias4444"
    assert overview["mine"]["count"] == 1 and overview["mine"]["first_name"] == "Notch"


def test_a_late_report_of_an_earlier_visit_is_the_first_everywhere(db, server, two_players):  # noqa: F811
    """The plugin keeps discoveries while MCConnect is away: the earliest visit is the first one on every page."""
    a, b = two_players
    now = datetime.now(timezone.utc)
    db.add_discovery(server["id"], OTHER_UUID, "hafen", now)
    db.add_discovery(server["id"], PLAYER_UUID, "hafen", now - timedelta(hours=1))  # reported later
    assert db.get_player_discoveries(a)["hafen"]["first"] and not db.get_player_discoveries(b)["hafen"]["first"]
    assert db.get_discovery_overview(server["id"])["hafen"]["first_name"] == "_Tobias4444"
    kinds = {e["name"]: e["kind"] for e in db.get_feed(server["id"]) if e["kind"].startswith("discovery")}
    assert kinds == {"_Tobias4444": "discovery_first", "Notch": "discovery"}


def test_hidden_players_count_but_are_not_named(db, server, two_players):  # noqa: F811
    a, _ = two_players
    db.save_profile(a, None, True)
    db.add_discovery(server["id"], PLAYER_UUID, "hafen", datetime.now(timezone.utc))
    overview = db.get_discovery_overview(server["id"])["hafen"]
    assert overview["count"] == 1 and overview["first_name"] is None and overview["first_uuid"] is None
    assert not [e for e in db.get_feed(server["id"]) if e["kind"].startswith("discovery")]


# ------------------------------------------------------------------ socket

def found(connection, uuid, place, at=None):
    at = at or datetime.now(timezone.utc)
    connection.send(f"!ADDON~discoveries|found|{uuid}|{place}|{int(at.timestamp() * 1000)}")


def test_discoveries_are_confirmed_and_the_first_one_is_announced(db, server, plugin, addon, two_players):  # noqa: F811
    connection = connect(plugin, server)
    found(connection, PLAYER_UUID, "hafen")
    messages = until(connection, "!addon~discoveries|ok|")
    assert messages[-1] == f"!addon~discoveries|ok|{PLAYER_UUID}|hafen"
    assert until(connection, "!broadcast~")[-1] == "!broadcast~gold|&6✦ &e_Tobias4444 &7hat &f»Der alte Hafen« &7als Erster entdeckt!"
    found(connection, OTHER_UUID, "hafen")  # second: confirmed, not announced by MCConnect (the game shows the advancement)
    found(connection, PLAYER_UUID, "hafen")  # repeated report (the confirmation got lost): confirmed again
    assert until(connection, "!addon~discoveries|ok|")[-1] == f"!addon~discoveries|ok|{OTHER_UUID}|hafen"
    assert until(connection, "!addon~discoveries|ok|") == [f"!addon~discoveries|ok|{PLAYER_UUID}|hafen"]
    assert db.get_discovery_overview(server["id"])["hafen"]["count"] == 2


def test_no_announcement_when_switched_off_or_for_unknown_places(db, server, plugin, addon, two_players):  # noqa: F811
    config = dict(addon["config"], first_broadcast=False)
    db.set_addon_config(server["id"], discoveries.ADDON_ID, config)
    connection = connect(plugin, server)
    found(connection, PLAYER_UUID, "hafen")
    found(connection, PLAYER_UUID, "geloescht")  # deleted meanwhile: stored and confirmed all the same
    assert until(connection, "!addon~discoveries|ok|")[-1].endswith("|hafen")
    assert until(connection, "!addon~discoveries|ok|") == [f"!addon~discoveries|ok|{PLAYER_UUID}|geloescht"]
    assert connection.request("!BEAT_X~x") == "error|004"  # nothing else was sent
    assert set(db.get_discovery_overview(server["id"])) == {"hafen", "geloescht"}


def test_bad_reports_and_inactive_addons_are_ignored(db, server, plugin, addon, two_players):  # noqa: F811
    connection = connect(plugin, server)
    assert connection.request("!ADDON~discoveries|found|kein-uuid|hafen|0") == "error|005"
    assert connection.request(f"!ADDON~discoveries|found|{PLAYER_UUID}|Hafen Groß|0") == "error|005"
    db.set_addon_enabled(server["id"], discoveries.ADDON_ID, False)  # paused: no confirmation, the plugin keeps it
    found(connection, PLAYER_UUID, "hafen")
    assert connection.request("!BEAT_X~x") == "error|004"
    wait_for(lambda: db.get_discovery_overview(server["id"]) == {})


# ------------------------------------------------------------------ website

def test_player_page_overview_and_feed(client, db, server, addon, two_players):  # noqa: F811
    db.add_discovery(server["id"], PLAYER_UUID, "hafen", datetime.now(timezone.utc))
    page = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Entdeckungen · 1 von 2" in page  # the switched off place does not count
    assert "Der alte Hafen" in page and "Als Erster entdeckt am" in page and "Tiefe Mine" not in page
    assert "icon-minecraft-oak-boat" in page

    overview = client.get("/entdeckungen", **on("testdomain")).get_data(as_text=True)
    assert "Der alte Hafen" in overview and "1 Spieler hat ihn entdeckt" in overview
    assert "Tiefe Mine" not in overview and "Wer ist der Erste?" in overview  # nobody found it: still secret
    assert "Turm" not in overview
    assert 'href="/entdeckungen"' in overview  # in the menu

    events = client.get("/api/feed", **on("testdomain")).json["events"]
    assert any(e["text"] == "hat »Der alte Hafen« als Erster entdeckt" for e in events)


def test_logged_in_players_see_their_count(client, db, server, addon, two_players):  # noqa: F811
    db.add_discovery(server["id"], OTHER_UUID, "mine", datetime.now(timezone.utc))
    db.set_plugin_connected(server["id"], True)  # the login pin goes to the game
    db._execute("UPDATE player_server_info SET online = true")
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    page = client.get("/entdeckungen", **on("testdomain")).get_data(as_text=True)
    assert "Du hast <b>0 von 2</b> entdeckt" in page
    assert "Tiefe Mine" in page and "Notch" in page  # found by someone: named, with the first discoverer


def test_without_the_addon_there_is_nothing(client, db, server, two_players):  # noqa: F811
    assert client.get("/entdeckungen", **on("testdomain")).status_code == 404
    page = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Entdeckungen" not in page


def test_discoveries_fit_a_phone(addon, db, server, two_players, phone, live_site):  # noqa: F811
    from tests.test_mobile import OVERFLOW
    db.add_discovery(server["id"], OTHER_UUID, "hafen", datetime.now(timezone.utc))
    db.add_discovery(server["id"], PLAYER_UUID, "hafen", datetime.now(timezone.utc))
    for path in ("/entdeckungen", "/spieler?player=_Tobias4444"):
        phone.goto(live_site + path, wait_until="domcontentloaded")
        phone.wait_for_timeout(300)
        result = phone.evaluate(OVERFLOW)
        assert result["body"] <= result["width"] and not result["out"], (path, result)
