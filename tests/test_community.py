"""Phase 7: events, polls, build gallery and guestbook."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from database import config
from mc_socket import commands
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_ingame import cmd, tells, until
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)


def png():
    data = io.BytesIO()
    Image.new("RGB", (64, 40), (40, 160, 90)).save(data, "PNG")
    data.seek(0)
    return data


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def player_client(client, db, server, two_players):
    db.set_plugin_connected(server["id"], True)
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET online = true")
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    return client


@pytest.fixture
def mod_client(player_client, db, server):
    db.set_moderator(server["id"], "_Tobias4444", True)
    return player_client


@pytest.fixture
def game(db, server, plugin, two_players):
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    assert connection.request(f"!JOIN~{OTHER_UUID}|Notch") == "success|101"
    return connection


def soon(minutes):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


# ------------------------------------------------------------------ events

def test_event_signup(db, server, two_players):
    a, b = two_players
    event_id = db.create_event(server["id"], "Bauwettbewerb", soon(120), place="Spawn")
    assert db.toggle_event_signup(server["id"], event_id, a) is True
    assert db.toggle_event_signup(server["id"], event_id, b, True) is True
    assert db.toggle_event_signup(server["id"], event_id, b, True) is True  # stays signed up
    assert [p["name"] for p in db.get_event_signups(event_id)] == ["_Tobias4444", "Notch"]
    assert db.toggle_event_signup(server["id"], event_id, a) is False
    assert db.list_events(server["id"])[0]["signups"] == 1


def test_past_events_cannot_be_joined(db, server, two_players):
    event_id = db.create_event(server["id"], "Alt", soon(-60 * 5))
    assert db.toggle_event_signup(server["id"], event_id, two_players[0]) is None
    assert db.list_events(server["id"]) == [] and db.list_events(server["id"], upcoming=False)[0]["title"] == "Alt"


def test_event_reminder_and_start(db, server, game, socket_server):
    db.create_event(server["id"], "Bauwettbewerb", soon(20), place="Spawn")
    socket_server.periodic_checks()
    assert until(game, "!broadcast~")[-1].startswith("!broadcast~gold|★ In Kürze: »Bauwettbewerb« um ")
    with db._cursor() as cur:
        cur.execute("UPDATE events SET starts_at = now() - interval '1 minute'")
    socket_server.periodic_checks()
    assert until(game, "!broadcast~")[-1] == "!broadcast~gold|★ Jetzt geht's los: »Bauwettbewerb«! Treffpunkt: Spawn."
    assert db.take_due_event_announcements([server["id"]]) == []


def test_events_command(db, server, game):
    db.create_event(server["id"], "Bauwettbewerb", soon(120))
    cmd(game, OTHER_UUID, "events", "anmelden 1")
    assert tells(game, 1) == ["&aDu bist für »Bauwettbewerb« angemeldet."]
    cmd(game, OTHER_UUID, "events")
    lines = tells(game, 3)
    assert "(1 dabei) &a(angemeldet)" in lines[1]


def test_events_page_and_api(player_client, mod_client, db, server):
    local = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT20:00")
    response = mod_client.post("/api/mod/events", json={"title": "Bauwettbewerb", "starts_at": local, "place": "Spawn"},
                               **on("testdomain"))
    assert response.status_code == 200
    assert mod_client.post("/api/mod/events", json={"title": "Alt", "starts_at": "2020-01-01T10:00"},
                           **on("testdomain")).status_code == 400
    assert db.list_events(server["id"])[0]["starts_at"].strftime("%H:%M") == "20:00"
    assert mod_client.post("/api/events/signup", json={"id": response.json["id"]}, **on("testdomain")).json["signed_up"]
    html = mod_client.get("/events", **on("testdomain")).get_data(as_text=True)
    assert "Bauwettbewerb" in html and "1 Anmeldung" in html and "Abmelden" in html
    assert "Bauwettbewerb" in mod_client.get("/", **on("testdomain")).get_data(as_text=True)
    assert mod_client.post("/api/mod/events/delete", json={"id": response.json["id"]}, **on("testdomain")).status_code == 200
    assert [e["action"] for e in db.get_mod_log(server["id"])][:2] == ["event_delete", "event_create"]


# ------------------------------------------------------------------ polls

def test_poll_votes(db, server, two_players):
    a, b = two_players
    poll_id = db.create_poll(server["id"], "Was bauen wir?", ["Hafen", "Burg"], soon(60))
    assert db.vote(server["id"], poll_id, a, 1) == "ok"
    assert db.vote(server["id"], poll_id, a, 0) == "ok"  # changed
    assert db.vote(server["id"], poll_id, b, 0) == "ok"
    assert db.vote(server["id"], poll_id, b, 5) == "invalid"
    poll = db.get_poll(poll_id)
    assert (poll["votes"], poll["total"], poll["open"]) == ([2, 0], 2, True)
    with db._cursor() as cur:
        cur.execute("UPDATE polls SET ends_at = now() - interval '1 minute'")
    assert db.vote(server["id"], poll_id, a, 1) == "closed"
    finished = db.take_finished_polls([server["id"]])
    assert commands.poll_result(finished[0]) == ("gold", "★ Umfrage »Was bauen wir?«: Hafen (100 %, 2 Stimmen)")
    assert db.take_finished_polls([server["id"]]) == []


def test_vote_command(db, server, game, socket_server):
    db.create_poll(server["id"], "Was bauen wir?", ["Hafen", "Burg"], soon(60))
    cmd(game, PLAYER_UUID, "vote")
    assert tells(game, 4)[1:3] == ["&7  1. &fHafen", "&7  2. &fBurg"]
    cmd(game, PLAYER_UUID, "vote", "1 2")
    assert tells(game, 1) == ["&aDanke! Deine Stimme: Burg"]
    cmd(game, PLAYER_UUID, "vote")
    assert tells(game, 4)[2] == "&7  2. &fBurg &a← deine Stimme"
    with db._cursor() as cur:
        cur.execute("UPDATE polls SET ends_at = now() - interval '1 minute'")
    socket_server.periodic_checks()
    assert until(game, "!broadcast~")[-1] == "!broadcast~gold|★ Umfrage »Was bauen wir?«: Burg (100 %, 1 Stimme)"


def test_polls_page_and_api(mod_client, db, server):
    body = {"question": "Was bauen wir?", "options": ["Hafen", "Burg", "hafen"], "days": 3}
    assert mod_client.post("/api/mod/polls", json=body, **on("testdomain")).status_code == 400  # duplicate answer
    body["options"] = ["Hafen", "Burg"]
    poll_id = mod_client.post("/api/mod/polls", json=body, **on("testdomain")).json["id"]
    assert mod_client.post("/api/polls/vote", json={"id": poll_id, "option": 1}, **on("testdomain")).status_code == 200
    html = mod_client.get("/umfragen", **on("testdomain")).get_data(as_text=True)
    assert "Was bauen wir?" in html and "100 %" in html and "du kannst deine Stimme noch ändern" in html
    assert "Was bauen wir?" in mod_client.get("/", **on("testdomain")).get_data(as_text=True)


# ------------------------------------------------------------------ build gallery

def test_build_upload_approve_like(player_client, client, db, server, two_players, upload_dir):
    response = player_client.post("/api/builds", data={"title": "Leuchtturm", "coordinates": "X 1 Z 2", "image": (png(), "a.png")},
                                  content_type="multipart/form-data", **on("testdomain"))
    assert response.status_code == 201
    build = db.get_build(response.json["id"])
    assert build["status"] == "pending" and (upload_dir / build["filename"]).exists()
    assert db.list_builds(server["id"]) == []  # not in the gallery before the approval

    db.set_moderator(server["id"], "_Tobias4444", True)
    assert "Galerie freigeben · 1" in player_client.get("/users", **on("testdomain")).get_data(as_text=True)
    assert player_client.post("/api/mod/builds/approve", json={"id": build["id"]}, **on("testdomain")).status_code == 200
    assert "Leuchtturm" in client.get("/galerie", **on("testdomain")).get_data(as_text=True)
    assert "Bauten in der Galerie" in client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)

    assert player_client.post("/api/builds/like", json={"id": build["id"]}, **on("testdomain")).status_code == 409  # own
    assert db.toggle_build_like(server["id"], build["id"], two_players[1]) is True
    assert db.get_build(build["id"])["likes"] == 1


def test_build_upload_limits(player_client, db, server):
    upload = lambda: player_client.post("/api/builds", data={"title": "Haus", "image": (png(), "a.png")},
                                        content_type="multipart/form-data", **on("testdomain"))
    assert [upload().status_code for _ in range(4)] == [201, 201, 201, 429]
    bad = player_client.post("/api/builds", data={"title": "Haus", "image": (io.BytesIO(b"nope"), "a.png")},
                             content_type="multipart/form-data", **on("testdomain"))
    assert bad.status_code == 400
    assert player_client.post("/api/builds", data={"title": "Haus", "image": (png(), "a.png")}, content_type="multipart/form-data",
                              headers={"Origin": "https://evil.example"}, **on("testdomain")).status_code == 403


def test_delete_own_build(player_client, db, server, upload_dir):
    build_id = player_client.post("/api/builds", data={"title": "Haus", "image": (png(), "a.png")},
                                  content_type="multipart/form-data", **on("testdomain")).json["id"]
    filename = db.get_build(build_id)["filename"]
    assert player_client.post("/api/builds/delete", json={"id": build_id}, **on("testdomain")).status_code == 200
    assert db.get_build(build_id) is None and not (upload_dir / filename).exists()


# ------------------------------------------------------------------ guestbook

def test_guestbook(player_client, client, db, server, two_players):
    a, b = two_players
    assert player_client.post("/api/guestbook", json={"name": "Notch", "text": "Coole Burg!"}, **on("testdomain")).status_code == 200
    assert player_client.post("/api/guestbook", json={"name": "Notch", "text": "x"}, **on("testdomain")).status_code == 400
    html = client.get("/spieler?player=Notch", **on("testdomain")).get_data(as_text=True)
    assert "Gästebuch · 1" in html and "Coole Burg!" in html
    entry = db.get_guestbook(b)[0]
    # Notch reports it, a moderator keeps it; Notch as page owner can delete it
    assert db.report_guestbook_entry(server["id"], entry["id"])
    assert db.get_reported_guestbook_entries(server["id"])[0]["text"] == "Coole Burg!"
    assert db.keep_guestbook_entry(server["id"], entry["id"])
    assert db.get_reported_guestbook_entries(server["id"]) == []
    assert db.delete_guestbook_entry(server["id"], entry["id"], player_id=b)["author"] == "_Tobias4444"


def test_guestbook_delete_rights(player_client, db, server, two_players):
    a, b = two_players
    entry_id = db.add_guestbook_entry(a, b, "Hallo")  # Notch writes on the page of _Tobias4444
    other_id = db.add_guestbook_entry(b, b, "Selbst")  # an entry on Notch's page by Notch
    assert player_client.post("/api/guestbook/delete", json={"id": other_id}, **on("testdomain")).status_code == 404
    assert player_client.post("/api/guestbook/delete", json={"id": entry_id}, **on("testdomain")).status_code == 200  # own page
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert player_client.post("/api/guestbook/delete", json={"id": other_id}, **on("testdomain")).status_code == 200
    assert db.get_mod_log(server["id"])[0]["action"] == "guestbook_delete"


def test_community_pages_render(client, server):
    for path in ("/events", "/umfragen", "/galerie"):
        assert client.get(path, **on("testdomain")).status_code == 200
