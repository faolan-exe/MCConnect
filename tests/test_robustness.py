"""Hostile input: no endpoint may fail with a server error, whatever a browser or a plugin sends. Every JSON API gets
a valid body with one field replaced by odd values (and bodies that are no object at all); the socket server gets
broken messages and must keep the connection."""
import io
import json
import select
import time
import uuid as uuid_mod
from datetime import timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extensions
import pytest

from database import config
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_ingame import until
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_security_matrix import MAIN_POSTS, SERVER_POSTS, path_for
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import login_player, on
from web.main import create_app

S = on("testdomain")
MAIN = on(None)

ODD_VALUES = [None, "", " ", "1e400", "nan", "-inf", 10 ** 30, -10 ** 30, -1, 0, 1.5, True, [], {}, [1, "a"],
              {"a": 1}, "x" * 20000, "<script>alert(1)</script>", "Notch|~&§\n", "2026-02-30", "ä" * 70, "a\x00b"]
ODD_BODIES = [[], "text", 42, None, [{"id": 1}]]


def today(db, days=0):
    return (db.get_today() + timedelta(days=days)).isoformat()


# a valid body per endpoint (the fields the endpoint reads); endpoints without one get only ODD_BODIES and {}
VALID = {
    "/api/badges": lambda db: {"keys": []},
    "/api/builds/delete": lambda db: {"id": 1},
    "/api/builds/like": lambda db: {"id": 1},
    "/api/duels": lambda db: {"name": "Notch", "metric": "blocks_mined", "days": 3},
    "/api/duels/cancel": lambda db: {"id": 1},
    "/api/duels/respond": lambda db: {"id": 1, "accept": True},
    "/api/events/signup": lambda db: {"id": 1},
    "/api/favorites/toggle": lambda db: {"name": "Notch"},
    "/api/guestbook": lambda db: {"name": "Notch", "text": "Schöne Burg!"},
    "/api/guestbook/delete": lambda db: {"id": 1},
    "/api/guestbook/report": lambda db: {"id": 1},
    "/api/joinmutes": lambda db: {"name": "Notch", "muted": True},
    "/api/joinstyle": lambda db: {"field": "color", "value": "gold"},
    "/api/polls/vote": lambda db: {"id": 1, "option": 0},
    "/api/prefix/join": lambda db: {"prefix_id": 1, "password": "p" * 4},
    "/api/prefix/save": lambda db: {"text": "Team", "color": "red", "password": "p" * 4, "remove_password": False},
    "/api/profile": lambda db: {"bio": "Hallo", "hide_stats": False},
    "/api/reports": lambda db: {"name": "Notch", "reason": "baut Lavafallen"},
    "/api/sidebar": lambda db: {"mode": "playtime"},
    "/api/mod/applications": lambda db: {"id": 1, "accept": True},
    "/api/mod/ban": lambda db: {"name": "Notch", "reason_id": 1, "days": 3, "comment": "x", "reason_text": "Griefing"},
    "/api/mod/unban": lambda db: {"player_id": PLAYER_UUID},
    "/api/mod/codes": lambda db: {"code": "WILLKOMMEN", "max_uses": 5, "days": 7},
    "/api/mod/competitions": lambda db: {"title": "Bauwoche", "metric": "blocks_placed", "starts_on": today(db),
                                         "ends_on": today(db, 6)},
    "/api/mod/events": lambda db: {"title": "Bootsrennen", "starts_at": today(db, 2) + "T20:00", "place": "Hafen",
                                   "description": "Mit Preisen"},
    "/api/mod/goals": lambda db: {"title": "Eine Million", "metric": "blocks_mined", "amount": "1000",
                                  "starts_on": today(db), "ends_on": today(db, 30)},
    "/api/mod/icons": lambda db: {"name": "herz", "fallback": "♥", "pixels": ["#ff0000"] * 81},
    "/api/mod/mute": lambda db: {"name": "Notch", "minutes": 30, "reason": "Spam"},
    "/api/mod/notes": lambda db: {"name": "Notch", "text": "beobachten"},
    "/api/mod/polls": lambda db: {"question": "Was bauen wir?", "options": ["Burg", "Hafen"], "days": 3},
    "/api/mod/rewards": lambda db: {"enabled": True, "add_text": {"kind": "join", "text": "{name} ist da", "level": 0}},
    "/api/mod/settings": lambda db: {"access_mode": "both", "warn_threshold": 3, "warn_ban_days": 7,
                                     "rules_enabled": True, "rules": "Sei nett", "faq": "Frage?\nAntwort"},
    "/api/mod/warn": lambda db: {"name": "Notch", "reason": "Spam im Chat"},
    "/api/mod/whitelist/remove": lambda db: {"names": ["Notch"]},
    "/api/mod/addons/<addon_id>/config": lambda db: {"config": {}},
    "/api/servers": lambda db: {"subdomain": "neu", "server_name": "Neu", "mc_server_domain": "neu.example.com",
                                "server_description_short": "kurz", "server_description_long": "lang",
                                "discord_url": "https://discord.gg/abc", "whitelist": False},
    "/api/servers/<int:server_id>/update": lambda db: {"server_name": "Neu", "server_description_short": "kurz",
                                                       "discord_url": "https://discord.gg/abc", "listed": True},
    "/api/servers/<int:server_id>/moderators": lambda db: {"name": "Notch", "moderator": True},
    "/api/servers/<int:server_id>/ban": lambda db: {"name": "Notch", "reason_id": 2, "days": 0},
    "/api/servers/<int:server_id>/unban": lambda db: {"player_id": OTHER_UUID},
    "/api/servers/<int:server_id>/delete": lambda db: {"confirm": "nicht-diese"},
    "/api/servers/<int:server_id>/addons/rights": lambda db: {"moderators": True, "auto_update": False},
    "/api/servers/<int:server_id>/addons/<addon_id>/config": lambda db: {"config": {}},
    "/api/servers/<int:server_id>/addons/<addon_id>/approve": lambda db: {"approved": True},
    "/api/servers/<int:server_id>/addons/<addon_id>/enabled": lambda db: {"enabled": True},
}
# endpoints that stop the session or the server, and uploads (multipart, tested in test_web)
SKIPPED = {"main.logout", "main.upload_server_image_api", "server.build_upload_api", "server.player_login"}


def bodies(rule, db):
    yield from ODD_BODIES
    yield {}
    if rule not in VALID:
        for value in ODD_VALUES:
            yield {"id": value, "name": value, "text": value}
        return
    valid = VALID[rule](db)
    yield valid
    for key in valid:
        for value in ODD_VALUES:
            yield dict(valid, **{key: value})


@pytest.fixture
def lenient_app(db):
    """Errors become 500 responses instead of exceptions in the test."""
    return create_app(db, {"TESTING": True, "SECRET_KEY": "test", "SERVER_NAME": "mc.test", "PROPAGATE_EXCEPTIONS": False})


@pytest.fixture
def mod_client(lenient_app, db, server, two_players):
    client = lenient_app.test_client()
    db.set_plugin_connected(server["id"], True)
    db.set_plugin_features(server["id"], {"unwhitelist", "addons"})
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET online = true")
    db.set_moderator(server["id"], "_Tobias4444", True)
    db.update_server(server["id"], db.get_servers_by_owner(1)[0]["owner_id"], addon_mods=True)
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    return client


@pytest.fixture
def owner_client(lenient_app, admin_id, server, two_players):
    client = lenient_app.test_client()
    assert client.post("/api/login", json={"username": "tobi", "password": "testPassword"}, **MAIN).status_code == 200
    return client


def server_errors(client, path, rule, db, **where):
    """The bodies (cut) that the endpoint answered with a server error."""
    failures = []
    for body in bodies(rule, db):
        response = client.post(path, data=json.dumps(body), content_type="application/json", **where)
        if response.status_code >= 500:
            failures.append((path, body if len(json.dumps(body)) < 200 else "(long)", response.status_code))
    return failures


# All endpoints of a group in one test (one login; the state the earlier calls leave behind is part of the test).

def test_server_apis_survive_odd_input(mod_client, db):
    assert not [f for e, rule in SERVER_POSTS if e not in SKIPPED for f in server_errors(mod_client, path_for(rule), rule, db, **S)]


def test_admin_apis_survive_odd_input(owner_client, db, server):
    assert not [f for e, rule in MAIN_POSTS if e not in SKIPPED
                for f in server_errors(owner_client, path_for(rule, server["id"]), rule, db, **MAIN)]
    assert db.get_server_id_from_subdomain("testdomain") == server["id"]


@pytest.mark.parametrize("path", ["/api/login", "/api/signup", "/api/resend_verification", "/api/password_reset/request",
                                  "/api/password_reset/confirm"])
def test_public_main_apis_survive_odd_input(lenient_app, db, path):
    client = lenient_app.test_client()
    for value in ODD_VALUES:
        body = {"username": value, "password": value, "email": value, "token": value}
        assert client.post(path, json=body, **MAIN).status_code < 500, value
    for body in ODD_BODIES:
        assert client.post(path, data=json.dumps(body), content_type="application/json", **MAIN).status_code < 500


@pytest.mark.parametrize("path", ["/api/login", "/api/access/apply", "/api/access/redeem"])
def test_public_server_apis_survive_odd_input(lenient_app, db, server, two_players, path):
    db.update_server_settings(server["id"], access_mode="both")
    client = lenient_app.test_client()
    for value in ODD_VALUES:
        body = {"username": value, "pin": value, "name": value, "message": value, "code": value}
        assert client.post(path, json=body, **S).status_code < 500, value
    for body in ODD_BODIES:
        assert client.post(path, data=json.dumps(body), content_type="application/json", **S).status_code < 500


QUERIES = ["zeitraum=%00", "zeitraum=9999", "spieler=" + ",".join(["Notch"] * 50),
                                   "kennzahl=__class__", "jahr=99999999999999999999", "jahr=-1", "player=%00",
                                   "seit=not-a-date", "seit=9999-12-31T23:59:59%2B14:00", "sortierung=beliebt"]
PAGES = ["/rangliste", "/vergleich", "/teams", "/rueckblick/Notch", "/spieler", "/galerie", "/api/meine-erfolge", "/login",
         "/server-statistik", "/ruhmeshalle", "/wettbewerbe", "/duelle", "/events", "/umfragen", "/"]


def test_pages_survive_odd_query_strings(lenient_app, server, two_players):
    client = lenient_app.test_client()
    assert not [(page, query, s) for page in PAGES for query in QUERIES
                if (s := client.get(f"{page}?{query}", **S).status_code) >= 500]


@pytest.mark.parametrize("path", ["/api/player_info/%00", "/api/player_info/" + "a" * 3000, "/spieler/..%2f..%2fetc/karte.png",
                                  "/icons/1114112.svg", "/icons/55296.svg", "/rueckblick/%27%20OR%201%3D1--"])
def test_paths_survive_odd_names(lenient_app, server, two_players, path):
    assert lenient_app.test_client().get(path, **S).status_code in (404, 400)


# ------------------------------------------------------------------ plugin protocol

BROKEN_MESSAGES = [
    "!JOIN~", "!JOIN~not-a-uuid|x", f"!JOIN~{PLAYER_UUID}|x|2|99999999999999999999999",
    f"!JOIN~{PLAYER_UUID}|x|1|-5", "!QUIT~", f"!STATS~{PLAYER_UUID}", f"!STATS~{PLAYER_UUID}|[]",
    f"!STATS~{PLAYER_UUID}|{{\"stats\": {{\"minecraft:mined\": {{\"minecraft:stone\": 1e400}}}}}}",
    f"!STATS~{PLAYER_UUID}|{{\"stats\": {{\"minecraft:mined\": {{\"<b>\": 5, \"minecraft:stone\": \"5\"}}}}}}",
    "!BANS~{}", "!BANS~[1]", '!BANS~[{"name": "_Tobias4444", "created": "x", "expires": 1e30}]',
    "!WEBBANS~{}", "!HEALTH~[]", '!HEALTH~{"tps": "fast", "players": 1e400}',
    f"!CMD~{PLAYER_UUID}", f"!CMD~{PLAYER_UUID}|world|1e400|nan|-inf|stats|",
    f"!CMD~{PLAYER_UUID}|world|99999999999|0|0|report|- Lava an der Basis",
    f"!CMD~{OTHER_UUID}|world|0|0|0|duell|Notch blocks 99999999999999999999",
    "!ADDONS~{}", '!ADDONS~[{"id": "x"}, 5]', "!ADDON~", "!ADDONCONFIG~nope|x|{}", "!ADDONRESULT~x|exploded|",
    "!ADDONPROBLEMS~x|{}", "!REGISTRY~[]", '!REGISTRY~{"blocks": "stone"}', "!FEATURES~" + ",".join(["x"] * 1000),
    "!UNKNOWN~x", "no tilde at all",
]


def test_broken_plugin_messages_keep_the_connection(db, server, plugin, two_players):
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    for message in BROKEN_MESSAGES:
        connection.send(message)
    # the connection still works: the answer to a last valid message arrives
    connection.send("!WEBBANS~[]")
    until(connection, "success|105")
    assert connection.request(f"!QUIT~{PLAYER_UUID}") == "success|101"


def test_messages_before_the_login_are_small(server, plugin):
    connection = plugin()
    connection.sock.sendall(b"%-10d" % 50_000 + b"x" * 100)
    assert connection.is_closed()


# ------------------------------------------------------------------ found by the tests above

@pytest.fixture
def strict_mod(db, server, two_players):
    """A logged in moderator; errors raise in the test."""
    client = create_app(db, {"TESTING": True, "SECRET_KEY": "test", "SERVER_NAME": "mc.test"}).test_client()
    db.set_plugin_connected(server["id"], True)
    db.set_plugin_features(server["id"], {"unwhitelist"})
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET online = true")
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    return client


def test_json_apis_need_an_object(strict_mod):
    for body in ("[1]", "42", '"text"', "{broken"):
        response = strict_mod.post("/api/sidebar", data=body, content_type="application/json", **S)
        assert response.status_code == 400 and response.json["error"], body
    assert strict_mod.post("/api/sidebar", json={"mode": "off"}, **S).status_code == 200


def test_nul_characters_are_refused(strict_mod):
    assert strict_mod.post("/api/profile", json={"bio": "a\x00b"}, **S).status_code == 400
    assert strict_mod.post("/api/profile", json={"bio\x00": "ab"}, **S).status_code == 400
    assert strict_mod.get("/spieler?player=a%00b", **S).status_code == 400
    assert strict_mod.get("/api/player_info/a%00b", **S).status_code == 400
    assert strict_mod.post("/api/builds", data={"title": "Burg\x00", "image": (io.BytesIO(b"x"), "a.png")},
                           **S).status_code == 400


@pytest.mark.parametrize("amount", ["1e400", "-1e400", "inf", "nan", "0", "-5"])
def test_goal_amount_must_be_a_positive_number(strict_mod, db, server, amount):
    body = dict(VALID["/api/mod/goals"](db), amount=amount)
    assert strict_mod.post("/api/mod/goals", json=body, **S).status_code == 400
    assert db.list_goals(server["id"]) == []


@pytest.mark.parametrize("reason_id", [9999, -1, 2 ** 40, "1e3"])
def test_unknown_ban_reason_is_refused(strict_mod, db, server, reason_id):
    assert strict_mod.post("/api/mod/ban", json={"name": "Notch", "reason_id": reason_id}, **S).status_code == 400
    assert db.list_active_bans(server["id"]) == []


def test_known_ban_reason_still_works(strict_mod, db, server):
    reason = db.get_ban_reasons()[1]
    assert strict_mod.post("/api/mod/ban", json={"name": "Notch", "reason_id": reason["id"]}, **S).status_code == 200
    assert db.list_active_bans(server["id"])[0]["reason"] == reason["reason"]


@pytest.mark.parametrize("options", ["Burg,Hafen", 10 ** 30, {"a": "Burg", "b": "Hafen"}, None])
def test_poll_options_must_be_a_list(strict_mod, db, server, options):
    body = {"question": "Was bauen wir?", "options": options, "days": 3}
    assert strict_mod.post("/api/mod/polls", json=body, **S).status_code == 400
    assert db.list_polls(server["id"]) == []


def test_liking_an_unknown_or_waiting_build(strict_mod, db, server, two_players):
    waiting = db.add_build(server["id"], two_players[1], "Burg", "b" * 32 + ".webp")
    for build_id in (424242, waiting):
        response = strict_mod.post("/api/builds/like", json={"id": build_id}, **S)
        assert response.status_code == 404 and response.json["error"] == "Unbekanntes Bild."
    own = db.add_build(server["id"], two_players[0], "Meins", "c" * 32 + ".webp", approved=True)
    assert strict_mod.post("/api/builds/like", json={"id": own}, **S).status_code == 409


def test_event_time_with_offset_keeps_the_moment(strict_mod, db, server):
    from datetime import datetime, timezone
    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    assert strict_mod.post("/api/mod/events", json={"title": "Fest", "starts_at": when.isoformat()}, **S).status_code == 200
    assert db.list_events(server["id"])[0]["starts_at"] == when
    # the form of the moderation page sends local time without offset: the server's time zone
    local = (when + timedelta(days=1)).astimezone(ZoneInfo(config.TIMEZONE))
    assert strict_mod.post("/api/mod/events", json={"title": "Fest 2", "starts_at": local.strftime("%Y-%m-%dT%H:%M")},
                           **S).status_code == 200
    assert db.list_events(server["id"])[1]["starts_at"] == local.replace(second=0)


def received_events(db, action):
    """The payloads of the server events sent while action() runs."""
    conn = psycopg2.connect(**db.db_config)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute("LISTEN mcc_events")
        action()
        events, deadline = [], time.monotonic() + 3
        while time.monotonic() < deadline:
            if select.select([conn], [], [], 0.2)[0]:
                conn.poll()
                events += [json.loads(n.payload) for n in conn.notifies]
                conn.notifies.clear()
            elif events:
                break
        return events
    finally:
        conn.close()


def test_long_player_lists_are_split_into_several_events(db, server):
    uuids = [str(uuid_mod.UUID(int=i)) for i in range(600)]
    events = received_events(db, lambda: db.notify_server_event(server["id"], "prefix", uuids=uuids))
    assert len(events) > 1 and all(e["type"] == "prefix" and e["server_id"] == server["id"] for e in events)
    assert [u for e in events for u in e["uuids"]] == uuids


def test_removing_many_names_from_the_whitelist(strict_mod, db, server):
    names = [f"Spieler_{i:04d}xyz" for i in range(500)]
    events = received_events(db, lambda: strict_mod.post("/api/mod/whitelist/remove", json={"names": names}, **S))
    assert sorted(n for e in events if e["type"] == "whitelist_remove" for n in e["names"]) == names


@pytest.mark.parametrize("name", ["<b>x</b>", "A" * 33, "zwei Worte", "x~y", "§cRot", ""])
def test_odd_player_names_from_the_plugin_are_ignored(db, server, plugin, two_players, name):
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|{name}") == "success|101"
    assert db.get_player_name_from_mojang_uuid(PLAYER_UUID) == "_Tobias4444"


@pytest.mark.parametrize("name", ["Steve", ".BedrockSteve", "*Steve", "a_b", "Q" * 16])
def test_real_player_names_from_the_plugin_are_stored(db, server, plugin, name):
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|{name}") == "success|101"
    assert db.get_player_name_from_mojang_uuid(PLAYER_UUID) == name


@pytest.mark.parametrize("where", ["world|1e400|64|0", "world|99999999999|64|0", "world|nan|0|0"])
def test_commands_with_odd_positions_are_answered(db, server, plugin, two_players, where):
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    connection.send(f"!CMD~{PLAYER_UUID}|{where}|report|- Lava an der Basis")
    assert "Danke" in until(connection, "!tell~")[-1]
    assert db.list_reports(server["id"])[0]["x"] is None
