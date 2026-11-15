"""The addon Tickets: /ticket in the game, answers of the moderators on the page /tickets (database/tickets.py)."""
import json
import os

import pytest

from database import addons, tickets
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_addons import connect
from tests.test_ingame import until
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_mobile import browser, live_site, phone  # noqa: F401 (fixtures)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

MANIFEST = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "java plugin", "addons", "Tickets", "src", "main", "resources", "mcconnect-addon.json")
WHERE = ("world", 10, 64, -20)


def request_id(n):
    return f"{n:032x}"


@pytest.fixture
def addon(db, server):
    """Tickets installed and approved, categories bug, grief, hilfe; at most 2 open tickets per player."""
    with open(MANIFEST, encoding="utf-8") as file:
        manifest = addons.check_manifest(json.load(file))
    db.record_addon_releases([("t" * 64, tickets.ADDON_ID, "1.0")])
    db.sync_reported_addons(server["id"], [{"id": tickets.ADDON_ID, "name": "Tickets", "plugin": "Tickets",
                                            "version": "1.0", "file": "Tickets-1.0.jar", "sha256": "t" * 64,
                                            "manifest": manifest}])
    config = addons.normalize(manifest["settings"], {"categories": ["bug", "grief", "hilfe"], "max_open": 2,
                                                     "notify_mods": True})
    db.set_addon_config(server["id"], tickets.ADDON_ID, config[0] if isinstance(config, tuple) else config)
    assert db.is_addon_active(server["id"], tickets.ADDON_ID)
    return db.get_addon(server["id"], tickets.ADDON_ID)


@pytest.fixture
def mod_client(client, db, server, two_players):  # noqa: F811
    """_Tobias4444 is a moderator and logged in; both players are online."""
    db.set_plugin_connected(server["id"], True)
    db._execute("UPDATE player_server_info SET online = true")
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    return client


# ------------------------------------------------------------------ rules

def test_categories_follow_the_rules_of_the_plugin():
    addon = {"active": True, "config": {"categories": ["Bug", "bug", "mit leerzeichen", " hilfe ", "", "x" * 21]}}
    assert tickets.categories(addon) == ["bug", "hilfe"]  # lower case, no repeats, invalid ones left out
    assert tickets.categories(dict(addon, active=False)) == []
    assert tickets.categories(None) == []


def test_limits_and_texts():
    assert tickets.max_open({"config": {"max_open": 5}}) == 5
    assert tickets.max_open({"config": {}}) == tickets.DEFAULT_MAX_OPEN
    assert tickets.max_open({"config": {"max_open": "kaputt"}}) == tickets.DEFAULT_MAX_OPEN
    assert tickets.notify_mods({"config": {}}) is True and tickets.notify_mods({"config": {"notify_mods": False}}) is False
    assert tickets.clean_text("  zwei\n  Zeilen\x07 ", 100) == "zwei Zeilen"
    assert tickets.clean_text("abcdef", 3) == "abc"


def test_the_manifest_is_valid():
    with open(MANIFEST, encoding="utf-8") as file:
        manifest = addons.check_manifest(json.load(file))
    assert manifest["id"] == tickets.ADDON_ID and tickets.ADDON_ID in addons.STORE
    defaults = addons.normalize(manifest["settings"], {})
    defaults = defaults[0] if isinstance(defaults, tuple) else defaults
    assert defaults["categories"] == ["bug", "grief", "hilfe", "sonstiges"] and defaults["max_open"] == 3


# ------------------------------------------------------------------ database

def test_numbers_per_server_repeats_and_the_limit(db, server, other_server, two_players):  # noqa: F811
    add = lambda n, sid=server["id"], uuid=PLAYER_UUID: db.add_ticket(sid, request_id(n), uuid, "bug", "Kaputt", WHERE, 2)
    assert add(1)["status"] == "new" and add(1)["number"] == 1
    assert add(1) == {"status": "repeat", "number": 1, "player_id": two_players[0]}  # sent again: stored once
    assert add(2)["number"] == 2
    assert add(3)["status"] == "full"  # two open tickets
    assert add(4, uuid=OTHER_UUID)["number"] == 3  # the limit is per player, the numbers per server
    assert add(1, sid=other_server["id"])["number"] == 1  # the same request id on another server is another ticket
    ticket = db.list_tickets(server["id"])[-1]
    db.set_ticket_open(server["id"], ticket["id"], False, None)
    assert add(5)["number"] == 4  # a closed ticket does not count


def test_closing_reopening_and_delivery(db, server, two_players):  # noqa: F811
    player = two_players[0]
    db.add_ticket(server["id"], request_id(1), PLAYER_UUID, "grief", "Haus kaputt", WHERE, 3)
    ticket = db.list_tickets(server["id"])[0]
    assert db.add_ticket_reply(server["id"], ticket["id"], "Mod", "Schaue ich mir an") == player
    assert db.set_ticket_open(server["id"], ticket["id"], False, "Mod") == player
    assert db.set_ticket_open(server["id"], ticket["id"], False, "Mod") is None  # already closed
    assert db.set_ticket_open(server["id"], ticket["id"], True, "Mod") == player
    assert db.set_ticket_open(server["id"], ticket["id"], False, None) == player  # by the player: not told again
    assert [e["kind"] for e in db.take_undelivered_ticket_events(player)] == ["reply", "closed", "reopened"]
    assert db.take_undelivered_ticket_events(player) == []
    assert [e["kind"] for e in db.get_ticket(server["id"], ticket["id"])["events"]] == ["reply", "closed", "reopened", "closed"]
    assert db.add_ticket_reply(server["id"], 999999, "Mod", "x") is None
    assert db.list_tickets(server["id"], "open") == [] and len(db.list_tickets(server["id"], "closed")) == 1


# ------------------------------------------------------------------ socket

def send_add(connection, n, uuid=PLAYER_UUID, category="bug", text="Die Tür geht nicht auf"):
    connection.send(f"!ADDON~tickets|add|{request_id(n)}|{uuid}|{category}|world|10|64|-20|0|{text}")


def test_a_new_ticket_is_confirmed_and_the_moderators_are_told(db, server, plugin, addon, two_players):  # noqa: F811
    db.set_moderator(server["id"], "Notch", True)
    connection = connect(plugin, server)
    db._execute("UPDATE player_server_info SET online = true")  # the plugin's login sets everyone offline
    send_add(connection, 1, text="Die Tür | geht &cnicht auf")
    messages = until(connection, "!addon~tickets|done|")
    assert messages[-1] == f"!addon~tickets|done|{request_id(1)}"
    assert messages[0].startswith(f"!tell~{PLAYER_UUID}|&aTicket #1 ist bei den Moderatoren angekommen.")
    assert messages[1].startswith(f"!tell~{OTHER_UUID}|&6[Ticket #1] &f_Tobias4444 &7(bug): &fDie Tür / geht +cnicht auf")
    ticket = db.list_tickets(server["id"])[0]
    assert ticket["message"] == "Die Tür | geht &cnicht auf" and (ticket["world"], ticket["x"], ticket["z"]) == ("world", 10, -20)

    send_add(connection, 1)  # sent again (the answer got lost): only confirmed
    assert until(connection, "!addon~tickets|") == [f"!addon~tickets|done|{request_id(1)}"]


def test_refused_tickets_are_confirmed_too(db, server, plugin, addon, two_players):  # noqa: F811
    connection = connect(plugin, server)
    send_add(connection, 1, category="weg")  # removed from the settings while it waited in the plugin
    messages = until(connection, "!addon~tickets|done|")
    assert "die Kategorie »weg« gibt es nicht mehr" in messages[0] and len(messages) == 2
    send_add(connection, 2)
    send_add(connection, 3)
    until(connection, "!addon~tickets|done|")
    until(connection, "!addon~tickets|done|")
    send_add(connection, 4)
    messages = until(connection, "!addon~tickets|done|")
    assert "&cDu hast schon 2 offene Tickets." in messages[0]
    assert len(db.list_tickets(server["id"])) == 2


def test_status_and_closing_in_the_game(db, server, plugin, addon, two_players):  # noqa: F811
    db.add_ticket(server["id"], request_id(1), PLAYER_UUID, "bug", "Die Tür geht nicht auf", WHERE, 3)
    ticket = db.list_tickets(server["id"])[0]
    db.add_ticket_reply(server["id"], ticket["id"], "Notch", "Ist repariert")
    connection = connect(plugin, server)

    connection.send(f"!ADDON~tickets|status|{PLAYER_UUID}|")
    assert connection.recv() == f"!tell~{PLAYER_UUID}|&6Deine Tickets"
    assert "bug · &aoffen &7– &fDie Tür geht nicht auf" in connection.recv()

    connection.send(f"!ADDON~tickets|status|{PLAYER_UUID}|1")
    lines = [connection.recv() for _ in range(4)]
    assert lines[0].startswith(f"!tell~{PLAYER_UUID}|&6Ticket #1 &7(bug, offen, vom ")
    assert lines[2] == f"!tell~{PLAYER_UUID}|&eNotch&7: &fIst repariert"
    assert "/ticket close 1" in lines[3]

    connection.send(f"!ADDON~tickets|close|{OTHER_UUID}|1")  # not Notch's ticket
    assert connection.recv().startswith(f"!tell~{OTHER_UUID}|&cDu hast kein Ticket #1.")
    connection.send(f"!ADDON~tickets|close|{PLAYER_UUID}|1")
    assert connection.recv() == f"!tell~{PLAYER_UUID}|&aTicket #1 ist geschlossen. &7Danke!"
    connection.send(f"!ADDON~tickets|close|{PLAYER_UUID}|1")
    assert connection.recv() == f"!tell~{PLAYER_UUID}|&7Ticket #1 ist schon geschlossen."
    assert db.list_tickets(server["id"], "open") == []


def test_bad_messages_and_inactive_addons(db, server, plugin, addon, two_players):  # noqa: F811
    connection = connect(plugin, server)
    assert connection.request("!ADDON~tickets|add|kurz|x") == "error|005"
    assert connection.request(f"!ADDON~tickets|close|{PLAYER_UUID}|eins") == "error|005"
    assert connection.request("!ADDON~tickets|weg|x") == "error|005"
    db.set_addon_enabled(server["id"], tickets.ADDON_ID, False)  # paused: no confirmation, the plugin keeps it
    send_add(connection, 1)
    assert connection.request("!BEAT_X~x") == "error|004"
    assert db.list_tickets(server["id"], None) == []


def test_answers_reach_the_player_now_or_with_the_next_join(db, server, plugin, socket_server, addon, two_players):  # noqa: F811
    player = two_players[0]
    db.add_ticket(server["id"], request_id(1), PLAYER_UUID, "bug", "Die Tür geht nicht auf", WHERE, 3)
    ticket = db.list_tickets(server["id"])[0]
    connection = connect(plugin, server)
    db._execute("UPDATE player_server_info SET online = false")
    db.add_ticket_reply(server["id"], ticket["id"], "Notch", "Ist repariert")
    socket_server.handle_server_event({"server_id": server["id"], "type": "tickets", "player_id": player})
    assert connection.request("!BEAT_X~x") == "error|004"  # offline: kept for the next join

    connection.send(f"!JOIN~{PLAYER_UUID}|_Tobias4444")
    line = until(connection, f"!tell~{PLAYER_UUID}|&6[Ticket #1]")[-1]
    assert line.startswith(f"!tell~{PLAYER_UUID}|&6[Ticket #1] &eNotch&7: &fIst repariert")

    db.set_ticket_open(server["id"], ticket["id"], False, "Notch")  # online now: told at once
    socket_server.handle_server_event({"server_id": server["id"], "type": "tickets", "player_id": player})
    assert connection.recv().startswith(f"!tell~{PLAYER_UUID}|&6[Ticket #1] &7Geschlossen von &eNotch")
    assert db.take_undelivered_ticket_events(player) == []


# ------------------------------------------------------------------ website

def test_the_menu_entry_is_for_moderators_with_the_addon(client, mod_client, db, server, addon, two_players):  # noqa: F811
    db.add_ticket(server["id"], request_id(1), OTHER_UUID, "bug", "Die Tür geht nicht auf", WHERE, 3)
    page = mod_client.get("/", **on("testdomain")).get_data(as_text=True)
    assert 'href="/tickets">Tickets<b class="nav-count" data-tickets-open>1</b>' in page
    db.set_addon_enabled(server["id"], tickets.ADDON_ID, False)
    assert 'href="/tickets"' not in mod_client.get("/", **on("testdomain")).get_data(as_text=True)
    assert mod_client.get("/tickets", **on("testdomain")).status_code == 404


def test_players_do_not_see_tickets(client, db, server, addon, two_players):  # noqa: F811
    db._execute("UPDATE player_server_info SET online = true")
    db.set_plugin_connected(server["id"], True)
    assert login_player(client, db, two_players[0]).json["status"] == "success"  # not a moderator
    assert 'href="/tickets"' not in client.get("/", **on("testdomain")).get_data(as_text=True)
    assert client.get("/tickets", **on("testdomain")).status_code == 403
    assert client.post("/api/mod/tickets/1/state", json={"open": False}, **on("testdomain")).status_code == 403


def test_moderators_answer_close_and_reopen(mod_client, db, server, addon, two_players):  # noqa: F811
    db.add_ticket(server["id"], request_id(1), OTHER_UUID, "grief", "Mein Haus ist weg", WHERE, 3)
    db.add_ticket(server["id"], request_id(2), OTHER_UUID, "bug", "Die Tür geht nicht auf", WHERE, 3)
    page = mod_client.get("/tickets", **on("testdomain")).get_data(as_text=True)
    assert "Mein Haus ist weg" in page and "<code>world 10 64 -20</code>" in page and "Notch" in page
    assert "Mein Haus ist weg" not in mod_client.get("/tickets?kategorie=bug", **on("testdomain")).get_data(as_text=True)

    grief = next(t for t in db.list_tickets(server["id"]) if t["category"] == "grief")
    url = f"/api/mod/tickets/{grief['id']}"
    assert mod_client.post(url + "/reply", json={"text": "  "}, **on("testdomain")).status_code == 400
    response = mod_client.post(url + "/reply", json={"text": "Schaue ich mir an"}, **on("testdomain"))
    assert response.status_code == 200 and "<b>_Tobias4444</b>: Schaue ich mir an" in response.json["html"]
    response = mod_client.post(url + "/state", json={"open": False}, **on("testdomain"))
    assert response.json["open"] == 1 and "Geschlossen von _Tobias4444" in response.json["html"]
    assert mod_client.post(url + "/state", json={"open": False}, **on("testdomain")).status_code == 409
    assert mod_client.post(url + "/state", json={"open": "ja"}, **on("testdomain")).status_code == 400
    assert mod_client.post("/api/mod/tickets/999999/state", json={"open": True}, **on("testdomain")).status_code == 404
    assert "Mein Haus ist weg" not in mod_client.get("/tickets", **on("testdomain")).get_data(as_text=True)
    assert "Mein Haus ist weg" in mod_client.get("/tickets?status=geschlossen", **on("testdomain")).get_data(as_text=True)
    assert mod_client.post(url + "/state", json={"open": True}, **on("testdomain")).json["open"] == 2

    notch = two_players[1]
    assert [e["kind"] for e in db.take_undelivered_ticket_events(notch)] == ["reply", "closed", "reopened"]
    assert [e["action"] for e in db.get_mod_log(server["id"])][:3] == ["ticket_reopen", "ticket_close", "ticket_reply"]


def test_tickets_fit_a_phone(addon, db, server, two_players, phone, live_site):  # noqa: F811
    from tests.test_mobile import OVERFLOW
    db.add_ticket(server["id"], request_id(1), OTHER_UUID, "grief", "Mein Haus ist weg " * 20, WHERE, 3)
    phone.goto(live_site + "/tickets", wait_until="domcontentloaded")  # logged in as a moderator
    phone.wait_for_timeout(300)
    assert "Mein Haus ist weg" in phone.content()
    result = phone.evaluate(OVERFLOW)
    assert result["body"] <= result["width"] and not result["out"], result
