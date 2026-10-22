"""Access rules of every endpoint, derived from the URL map (so new endpoints are checked as well): logins, moderator
and owner rights, JSON for state changes (CSRF), separation of the servers and the session cookie."""
import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_web import admin_client, app, client, login_player, on  # noqa: F401 (fixtures)
from web.main import create_app

S = on("testdomain")
MAIN = on(None)
THIRD_UUID = "853c80ef-3c37-49fd-aa49-938b674adae6"

# POST endpoints anyone may call (logins, signup, password reset, whitelist applications)
PUBLIC_POSTS = {
    "main.server_login_api", "main.logout", "main.signup", "main.resend_verification",
    "main.password_reset_request", "main.password_reset_confirm",
    "server.minecraft_login_api", "server.player_login", "server.access_apply_api", "server.access_redeem_api",
}
# pages and GET APIs of the moderators
MOD_GETS = {"server.mod_addons_api", "server.mod_bans_api"} | {
    "server.moderation_page", "server.moderation_players_page", "server.moderation_content_page",
    "server.moderation_access_page", "server.moderation_icons_page", "server.moderation_rewards_page",
    "server.moderation_log_page", "server.moderation_addons_page"}
UPLOADS = {"main.upload_server_image_api", "server.build_upload_api"}


def rules(subdomain, method):
    """(endpoint, path) of every rule of the main domain ("") or the server pages ("<subdomain>") with this method."""
    app = create_app(_NoDB(), {"TESTING": True, "SECRET_KEY": "test", "SERVER_NAME": "mc.test"})
    return sorted((r.endpoint, r.rule) for r in app.url_map.iter_rules()
                  if r.subdomain == subdomain and method in r.methods and r.endpoint != "static")


class _NoDB:
    """create_app only stores the manager; the URL map needs no database."""


def path_for(rule, server_id=1):
    values = {"server_id": server_id, "image_id": 1, "addon_id": "betterjoinmessages", "player_name": "Notch",
              "token": "x", "username": "x", "sha1": "x", "codepoint": 65, "filename": "x"}
    return re.sub(r"<(?:[^:>]+:)?([^>]+)>", lambda m: str(values[m.group(1)]), rule)


SERVER_POSTS = [r for r in rules("<subdomain>", "POST") if r[0] not in PUBLIC_POSTS]
MAIN_POSTS = [r for r in rules("", "POST") if r[0] not in PUBLIC_POSTS]
MOD_POSTS = [r for r in SERVER_POSTS if r[1].startswith("/api/mod/")]
MOD_PAGES = [r for r in rules("<subdomain>", "GET") if r[0] in MOD_GETS]
ADMIN_SERVER_ROUTES = [(e, r, method) for method in ("POST", "GET") for e, r in rules("", method) if "<int:server_id>" in r]


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


def test_the_url_map_is_read():
    assert len(SERVER_POSTS) > 40 and len(MAIN_POSTS) > 10 and len(MOD_POSTS) > 20


# ------------------------------------------------------------------ logins
# One test per group with all endpoints (the setup costs more than the requests); failures list every endpoint.

def test_server_posts_need_a_logged_in_player(client, server):
    wrong = [(e, s) for e, rule in SERVER_POSTS if (s := client.post(path_for(rule), json={}, **S).status_code) != 401]
    assert not wrong


def test_admin_posts_need_a_logged_in_admin(client, server):
    wrong = [(e, s) for e, rule in MAIN_POSTS
             if (s := client.post(path_for(rule, server["id"]), json={}, **MAIN).status_code) != 401]
    assert not wrong


def test_moderation_pages_need_a_login(client, server):
    wrong = [(e, s) for e, rule in MOD_PAGES if (s := client.get(path_for(rule), **S).status_code) not in (302, 401)]
    assert not wrong


def test_a_player_login_is_valid_on_its_server_only(player_client, db, server, other_server):
    db.register_player_join(other_server["id"], PLAYER_UUID, "_Tobias4444")
    assert player_client.post("/api/sidebar", json={"mode": "off"}, **S).status_code == 200
    # the same browser on another server's page: not logged in there (the cookie is also bound to the host)
    with player_client.session_transaction(base_url="http://testdomain.mc.test") as session:
        cookie = dict(session)
    with player_client.session_transaction(base_url="http://other.mc.test") as session:
        session.update(cookie)
    assert player_client.post("/api/sidebar", json={"mode": "off"}, **on("other")).status_code == 401


# ------------------------------------------------------------------ rights

def test_moderator_apis_refuse_normal_players(player_client):
    wrong = [(e, s) for e, rule in MOD_POSTS if (s := player_client.post(path_for(rule), json={}, **S).status_code) != 403]
    wrong += [(e, s) for e, rule in MOD_PAGES if (s := player_client.get(path_for(rule), **S).status_code) != 403]
    assert not wrong


def test_removed_moderator_loses_the_rights_at_once(mod_client, db, server):
    assert mod_client.get("/users", **S).status_code == 200
    db.set_moderator(server["id"], "_Tobias4444", False)
    assert mod_client.get("/users", **S).status_code == 403
    assert mod_client.post("/api/mod/ban", json={"name": "Notch"}, **S).status_code == 403
    assert db.list_active_bans(server["id"]) == []


def test_admins_cannot_touch_servers_of_other_admins(admin_client, db):
    stranger = db.add_server_admin("eve", "evePassword", "eve@example.com", email_verified=True)
    foreign = db.add_server(stranger, "foreign", "foreign.example.com", "Foreign", server_key="f" * 64)
    wrong = []
    for endpoint, rule, method in ADMIN_SERVER_ROUTES:
        path = path_for(rule, foreign)
        response = admin_client.post(path, json={"confirm": "foreign"}, **MAIN) if method == "POST" \
            else admin_client.get(path, **MAIN)
        if response.status_code not in (400, 404):
            wrong.append((endpoint, response.status_code))
    assert not wrong
    assert db.get_server_id_by_auth_key("f" * 64) == foreign  # neither deleted nor given a new key
    assert db.list_moderators(foreign) == [] and db.get_server_images(foreign)["banner"] is None


# ------------------------------------------------------------------ CSRF: state changes only as JSON

def test_player_apis_refuse_forms(mod_client):
    """Browsers send forms cross-site without asking; JSON needs a CORS preflight that never succeeds."""
    wrong = [(e, s) for e, rule in SERVER_POSTS if e not in UPLOADS
             if (s := mod_client.post(path_for(rule), data={"id": "1", "name": "Notch"}, **S).status_code) != 415]
    assert not wrong


def test_admin_apis_refuse_forms(admin_client, server):
    wrong = [(e, s) for e, rule in MAIN_POSTS if e not in UPLOADS
             if (s := admin_client.post(path_for(rule, server["id"]), data={"confirm": "testdomain"},
                                        **MAIN).status_code) != 415]
    assert not wrong
    assert admin_client.get(f"/manage/{server['id']}", **MAIN).status_code == 200  # still logged in, server still there


def test_uploads_from_other_sites_are_refused(mod_client, admin_client, server):
    form = lambda: {"title": "Burg am See", "kind": "gallery", "image": (io.BytesIO(b"GIF89a"), "a.gif")}
    assert mod_client.post("/api/builds", data=form(), headers={"Origin": "https://evil.example"},
                           **S).status_code == 403
    assert admin_client.post(f"/api/servers/{server['id']}/images", data=form(),
                             headers={"Origin": "https://evil.example"}, **MAIN).status_code == 403


# ------------------------------------------------------------------ servers are separated

@pytest.fixture
def foreign(db, other_server):
    """Objects of another server (the moderator of testdomain must not reach them by id)."""
    sid = other_server["id"]
    jeb = db.register_player_join(sid, THIRD_UUID, "jeb_")
    alex = db.register_player_join(sid, OTHER_UUID, "Notch")
    today = db.get_today()
    with db._cursor() as cur:
        cur.execute("INSERT INTO reports (server_id, reporter_id, target_name, reason) VALUES (%s, %s, 'Notch', 'griefing') "
                    "RETURNING id", (sid, jeb))
        report = cur.fetchone()[0]
        cur.execute("INSERT INTO access_requests (server_id, name, message, kind) VALUES (%s, 'Neuling', 'hallo', "
                    "'application') RETURNING id", (sid,))
        application = cur.fetchone()[0]
        cur.execute("INSERT INTO warnings (server_id, player_id, reason) VALUES (%s, %s, 'spam') RETURNING id", (sid, jeb))
        warning = cur.fetchone()[0]
    entry = db.add_guestbook_entry(jeb, alex, "schöne Burg")
    db.report_guestbook_entry(sid, entry)
    db.ban_player(sid, "jeb_", "mod", days=0)
    return {
        "player": str(jeb), "report": report, "application": application, "warning": warning, "entry": entry,
        "event": db.create_event(sid, "Fremdes Fest", datetime.now(timezone.utc) + timedelta(days=1)),
        "poll": db.create_poll(sid, "Fremde Frage?", ["a", "b"], datetime.now(timezone.utc) + timedelta(days=1)),
        "build": db.add_build(sid, jeb, "Fremde Burg", "a" * 32 + ".webp"),
        "code": db.create_invite_code(sid, "FREMD-1"),
        "competition": db.create_competition(sid, "Fremder Wettbewerb", "blocks_mined", today, today + timedelta(days=3)),
        "goal": db.create_goal(sid, "Fremdes Ziel", "blocks_mined", 1000, today),
        "note": db.add_player_note(jeb, "mod", "beobachten"),
        "icon": db.add_server_icon(sid, "fremd", "x", ["#ffffff"] * 81, 0xE100),
        "duel": db.create_duel(sid, jeb, alex, "blocks_mined", 3)[0],
        "prefix": (db.save_own_prefix(jeb, "FREMD", "red") and db.get_owned_prefix(jeb)["prefix_id"]),
    }


def foreign_state(db, ids):
    """Everything that must stay as it is."""
    sid = db.get_server_id_from_player_id(ids["player"])
    return {
        "event": db.get_event(ids["event"]) is not None, "poll": db.get_poll(ids["poll"]) is not None,
        "build": (db.get_build(ids["build"]) or {}).get("status"),
        "code": [c["uses"] for c in db.list_invite_codes(sid)],
        "competition": db.get_competition(ids["competition"]) is not None,
        "goal": [g["id"] for g in db.list_goals(sid)], "notes": len(db.get_player_notes(ids["player"])),
        "warnings": len(db.get_warnings(ids["player"])), "icons": len(db.get_server_icons(sid)),
        "reports": [r["handled_at"] for r in db.list_reports(sid, include_handled=True)],
        "requests": [r["status"] for r in db.list_access_requests(sid)],
        "guestbook": [(e["id"], e["reported_at"] is not None) for e in db.get_guestbook(ids["player"])],
        "bans": len(db.list_active_bans(sid)), "duel": db.get_duel(ids["duel"])["status"],
        "mute": db.get_player_info_by_player_id(ids["player"])["muted_until"],
    }


FOREIGN_CALLS = [
    ("/api/mod/events/delete", lambda ids: {"id": ids["event"]}),
    ("/api/mod/polls/delete", lambda ids: {"id": ids["poll"]}),
    ("/api/mod/builds/approve", lambda ids: {"id": ids["build"]}),
    ("/api/mod/builds/delete", lambda ids: {"id": ids["build"]}),
    ("/api/mod/codes/delete", lambda ids: {"id": ids["code"]}),
    ("/api/mod/competitions/delete", lambda ids: {"id": ids["competition"]}),
    ("/api/mod/goals/delete", lambda ids: {"id": ids["goal"]}),
    ("/api/mod/notes/delete", lambda ids: {"id": ids["note"]}),
    ("/api/mod/warnings/delete", lambda ids: {"id": ids["warning"]}),
    ("/api/mod/reports/resolve", lambda ids: {"id": ids["report"]}),
    ("/api/mod/applications", lambda ids: {"id": ids["application"], "accept": True}),
    ("/api/mod/guestbook/keep", lambda ids: {"id": ids["entry"]}),
    ("/api/mod/icons", lambda ids: {"delete": ids["icon"]}),
    ("/api/mod/icons", lambda ids: {"id": ids["icon"], "name": "meins", "fallback": "y", "pixels": ["#000000"] * 81}),
    ("/api/mod/unban", lambda ids: {"player_id": ids["player"]}),
    ("/api/mod/ban", lambda ids: {"name": "jeb_"}),
    ("/api/mod/warn", lambda ids: {"name": "jeb_", "reason": "fremd"}),
    ("/api/mod/mute", lambda ids: {"name": "jeb_", "minutes": 10}),
    ("/api/mod/notes", lambda ids: {"name": "jeb_", "text": "fremd"}),
    ("/api/guestbook/delete", lambda ids: {"id": ids["entry"]}),
    ("/api/guestbook/report", lambda ids: {"id": ids["entry"]}),
    ("/api/builds/like", lambda ids: {"id": ids["build"]}),
    ("/api/builds/delete", lambda ids: {"id": ids["build"]}),
    ("/api/events/signup", lambda ids: {"id": ids["event"]}),
    ("/api/polls/vote", lambda ids: {"id": ids["poll"], "option": 0}),
    ("/api/duels/respond", lambda ids: {"id": ids["duel"], "accept": True}),
    ("/api/duels/cancel", lambda ids: {"id": ids["duel"]}),
    ("/api/prefix/join", lambda ids: {"prefix_id": ids["prefix"]}),
    ("/api/guestbook", lambda ids: {"name": "jeb_", "text": "fremd"}),
    ("/api/favorites/toggle", lambda ids: {"name": "jeb_"}),
    ("/api/duels", lambda ids: {"name": "jeb_", "metric": "blocks_mined", "days": 3}),
]


def test_a_moderator_cannot_reach_other_servers(mod_client, db, server, foreign):
    me = db.get_player_id_from_player_name_and_server_id("_Tobias4444", server["id"])
    wrong = []
    for path, body in FOREIGN_CALLS:
        before = foreign_state(db, foreign)
        response = mod_client.post(path, json=body(foreign), **S)
        if response.status_code not in (400, 404, 409) or foreign_state(db, foreign) != before:
            wrong.append((path, body(foreign), response.status_code))
    assert not wrong
    assert db.get_player_prefix(me) is None and db.get_favorites(me) == [] and db.get_player_duels(me) == []


def test_admin_moderator_and_ban_apis_stay_on_the_own_server(admin_client, db, server, other_server):
    db.register_player_join(other_server["id"], THIRD_UUID, "jeb_")
    sid = server["id"]
    assert admin_client.post(f"/api/servers/{sid}/moderators", json={"name": "jeb_", "moderator": True},
                             **MAIN).status_code == 404
    assert admin_client.post(f"/api/servers/{sid}/ban", json={"name": "jeb_"}, **MAIN).status_code == 404
    assert db.list_moderators(other_server["id"]) == [] and db.list_active_bans(other_server["id"]) == []


# ------------------------------------------------------------------ session cookie

def test_session_cookie_is_protected(player_client, admin_client):
    cookies = [c for c in admin_client.get("/manage", **MAIN).headers.getlist("Set-Cookie")] + \
        [admin_client.post("/api/login", json={"username": "tobi", "password": "testPassword"},
                           **MAIN).headers["Set-Cookie"]]
    for cookie in cookies:
        attributes = cookie.lower()
        assert "httponly" in attributes and "samesite=lax" in attributes
        # bound to the host: a server page never gets the admin session (and the other way round)
        assert "domain=" not in attributes


def test_admin_session_does_not_log_in_a_player(admin_client, server, two_players):
    assert admin_client.post("/api/sidebar", json={"mode": "off"}, **S).status_code == 401


# ------------------------------------------------------------------ hidden players

def test_duels_of_hidden_players_are_not_public(app, player_client, db, server, two_players):
    me, notch = two_players
    duel_id, _ = db.create_duel(server["id"], me, notch, "blocks_mined", 3)
    db.respond_duel(duel_id, notch, True)
    db.save_profile(notch, None, True)
    assert "Notch" not in app.test_client().get("/duelle", **S).get_data(as_text=True)
    assert "Notch" in player_client.get("/duelle", **S).get_data(as_text=True)  # still visible to the participant


@pytest.mark.parametrize("path", ["/api/player_info/Notch", "/api/spieler/Notch/erfolge", "/spieler/Notch/karte.png",
                                  "/rueckblick/Notch"])
def test_player_values_of_hidden_players_are_not_public(player_client, db, server, two_players, path):
    db.save_profile(two_players[1], None, True)
    assert player_client.get(path, **S).status_code == 404
