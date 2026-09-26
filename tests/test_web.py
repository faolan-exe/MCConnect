import json

import pytest

from tests.conftest import OTHER_UUID, PLAYER_UUID
from web.main import create_app, safe_next_path

BASE = "mc.test"


@pytest.fixture
def app(db):
    return create_app(db, {"TESTING": True, "SECRET_KEY": "test", "SERVER_NAME": BASE})


@pytest.fixture
def client(app):
    return app.test_client()


def on(subdomain):
    return {"base_url": f"http://{subdomain}.{BASE}" if subdomain else f"http://{BASE}"}


def first_event(response):
    chunk = next(response.response)
    response.close()
    chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert chunk.startswith("data: ")
    return json.loads(chunk[len("data: "):])


@pytest.fixture
def online_player(db, server):
    db.set_plugin_connected(server["id"], True)
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player_id, {"stats": {
        "minecraft:mined": {"minecraft:stone": 10},
        "minecraft:custom": {"minecraft:deaths": 2, "minecraft:play_time": 72000},
    }})
    return player_id


def login_player(client, db, player_id, subdomain="testdomain"):
    response = client.post("/api/login", json={"username": "_Tobias4444", "pin": None}, **on(subdomain))
    assert response.json["status"] == "success"
    pin = db._fetchvalue("SELECT pin FROM login WHERE player_id = %s", (player_id,))
    return client.post("/api/login", json={"username": None, "pin": str(pin)}, **on(subdomain))


# ------------------------------------------------------------------ pages

def test_main_page(client):
    assert client.get("/", **on(None)).status_code == 200


def test_healthz(client):
    assert client.get("/healthz", **on(None)).json == {"status": "ok"}


def test_unknown_subdomain_is_404(client, server):
    assert client.get("/", **on("nope")).status_code == 404
    assert client.get("/api/player_count", **on("nope")).status_code == 404


def test_server_page(client, server):
    response = client.get("/", **on("testdomain"))
    assert response.status_code == 200
    assert b"Test Server" in response.data


def test_subdomain_is_case_insensitive(client, server):
    assert client.get("/", **on("TestDomain")).status_code == 200


def test_player_list(client, server, online_player, db):
    db.ensure_player_on_server(server["id"], OTHER_UUID)
    response = client.get("/spieler", **on("testdomain"))
    assert response.status_code == 200
    body = response.data.decode()
    assert body.index("Notch") < body.index("_Tobias4444")


def test_player_page(client, online_player):
    response = client.get("/spieler?player=_tobias4444", **on("testdomain"))
    assert response.status_code == 200
    assert b"_Tobias4444" in response.data


def test_player_page_unknown_player(client, server):
    assert client.get("/spieler?player=nobody", **on("testdomain")).status_code == 404


def test_player_page_of_other_server_is_404(client, online_player, other_server):
    assert client.get("/spieler?player=_Tobias4444", **on("other")).status_code == 404


def test_banned_player_page(client, db, online_player):
    from datetime import datetime, timedelta, timezone
    reason_id = db._fetchvalue("SELECT id FROM ban_reasons WHERE reason = 'hacking'")
    db.add_banned_player(online_player, None, reason_id, datetime.now(timezone.utc) + timedelta(days=3))
    response = client.get("/spieler?player=_Tobias4444", **on("testdomain"))
    assert response.status_code == 200
    assert b'if ("True" == "True")' in response.data
    assert (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%d.%m.%Y").encode() in response.data


# ------------------------------------------------------------------ SSE

def test_player_count_stream(client, online_player):
    assert first_event(client.get("/api/player_count", buffered=False, **on("testdomain"))) == 1


def test_total_player_count_stream(client, online_player):
    assert first_event(client.get("/api/player_count", buffered=False, **on(None))) == 1


def test_status_stream(client, db, server, online_player):
    db.ensure_player_on_server(server["id"], OTHER_UUID)
    assert first_event(client.get("/api/status", buffered=False, **on("testdomain"))) == ["offline", "online"]


def test_player_info_stream(client, online_player):
    data = first_event(client.get("/api/player_info/_Tobias4444", buffered=False, **on("testdomain")))
    assert data[0] == PLAYER_UUID
    assert data[1] == "online"
    assert data[2] == 2
    assert data[6] == "1 Std."


def test_player_info_stream_unknown_player(client, server):
    assert client.get("/api/player_info/nobody", **on("testdomain")).status_code == 404


# ------------------------------------------------------------------ player login

def test_login_page_redirects_with_next(client, server):
    response = client.get("/login", **on("testdomain"))
    assert response.status_code == 302
    assert response.location.endswith("/login?next=/")


def test_login_page_rejects_foreign_next(client, server):
    response = client.get("/login?next=//evil.com", **on("testdomain"))
    assert response.status_code == 302
    assert "evil" not in response.location


@pytest.mark.parametrize("path, expected", [
    ("/spieler", "/spieler"), ("//evil.com", "/"), ("https://evil.com", "/"), ("", "/"), (None, "/"),
    ("/\\evil.com", "/"),
])
def test_safe_next_path(path, expected):
    assert safe_next_path(path) == expected


def test_login_unknown_player(client, server):
    response = client.post("/api/login", json={"username": "nobody", "pin": None}, **on("testdomain"))
    assert response.json["response"] == "Invalid username"


def test_login_offline_player(client, db, server):
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    response = client.post("/api/login", json={"username": "_Tobias4444", "pin": None}, **on("testdomain"))
    assert response.json["response"] == "You are offline"


def test_login_invalid_body(client, server):
    assert client.post("/api/login", data="x", **on("testdomain")).status_code == 400


def test_login_success(client, db, online_player):
    response = login_player(client, db, online_player)
    assert response.json["status"] == "success"
    # logged in: the header shows the name, the login page offers logout
    assert b"href=\"/spieler?player=" in client.get("/", **on("testdomain")).data
    assert client.get("/login?next=/", **on("testdomain")).status_code == 200


def test_login_wrong_pin(client, db, online_player):
    client.post("/api/login", json={"username": "_Tobias4444", "pin": None}, **on("testdomain"))
    response = client.post("/api/login", json={"username": None, "pin": "000000"}, **on("testdomain"))
    assert response.json["response"] == "Pin is incorrect"
    response = client.post("/api/login", json={"username": None, "pin": "abc"}, **on("testdomain"))
    assert response.json["response"] == "Pin is incorrect"


def test_login_pin_without_first_step(client, online_player):
    response = client.post("/api/login", json={"username": None, "pin": "123456"}, **on("testdomain"))
    assert response.json["status"] == "reset"


def test_login_is_scoped_to_server(client, db, server, other_server, online_player):
    login_player(client, db, online_player)
    # the session of testdomain must not log the player in on another server
    response = client.get("/", **on("other"))
    assert b"href=\"/spieler?player=" not in response.data


def test_player_logout(client, db, online_player):
    login_player(client, db, online_player)
    response = client.post("/login", data={"text_input": "logout"}, **on("testdomain"))
    assert response.status_code == 302
    assert b"href=\"/spieler?player=" not in client.get("/", **on("testdomain")).data


# ------------------------------------------------------------------ server admins

def test_signup_verify_login(client, db):
    response = client.post("/api/signup", json={"username": "alice", "email": "alice@example.com",
                                                "password": "secret123"}, **on(None))
    assert response.status_code == 200
    assert client.post("/api/login", json={"username": "alice", "password": "secret123"},
                       **on(None)).status_code == 400  # not verified yet

    token = db._fetchvalue("SELECT token FROM email_verification")
    assert client.get(f"/verify_email/alice/{token}", **on(None)).status_code == 200
    assert client.post("/api/login", json={"username": "alice", "password": "secret123"},
                       **on(None)).status_code == 200
    assert client.get("/manage", **on(None)).status_code == 200


@pytest.mark.parametrize("payload", [
    {"username": "a", "email": "a@example.com", "password": "secret123"},
    {"username": "alice", "email": "no-email", "password": "secret123"},
    {"username": "alice", "email": "a@example.com", "password": "short"},
    {},
])
def test_signup_validation(client, payload):
    assert client.post("/api/signup", json=payload, **on(None)).status_code == 400


def test_signup_duplicate(client, admin_id):
    response = client.post("/api/signup", json={"username": "tobi", "email": "new@example.com",
                                                "password": "secret123"}, **on(None))
    assert response.status_code == 409


def test_verify_email_invalid_token(client, admin_id):
    assert client.get("/verify_email/tobi/invalid", **on(None)).status_code == 400


def test_manage_requires_login(client):
    response = client.get("/manage", **on(None))
    assert response.status_code == 302
    assert response.location.endswith("/login")


# ------------------------------------------------------------------ plugin status in login

def test_login_requires_connected_plugin(client, db, server, online_player):
    db.set_plugin_connected(server["id"], False)
    response = client.post("/api/login", json={"username": "_Tobias4444", "pin": None}, **on("testdomain"))
    assert response.json["response"] == "Server not connected"


# ------------------------------------------------------------------ legal pages & menu

@pytest.mark.parametrize("subdomain", [None, "testdomain"])
@pytest.mark.parametrize("page", ["/impressum", "/datenschutz"])
def test_legal_pages(client, server, subdomain, page):
    assert client.get(page, **on(subdomain)).status_code == 200


def test_impressum_shows_configured_operator(client, monkeypatch):
    from database import config
    monkeypatch.setattr(config, "LEGAL_NAME", "Max Muster")
    monkeypatch.setattr(config, "LEGAL_ADDRESS", ["Weg 1", "12345 Stadt"])
    monkeypatch.setattr(config, "LEGAL_EMAIL", "max@example.com")
    body = client.get("/impressum", **on(None)).data.decode()
    assert "Max Muster" in body and "12345 Stadt" in body and "nicht konfiguriert" not in body


def test_prefix_menu_is_shown(client, server):
    body = client.get("/", **on("testdomain")).data.decode()
    assert "/add_pref" in body and "/users" not in body  # "Verwaltung" only for moderators


def test_menu_items_can_be_disabled(db, server):
    app = create_app(db, {"TESTING": True, "SECRET_KEY": "t", "SERVER_NAME": BASE, "FEATURE_PREFIXES": False})
    assert "/add_pref" not in app.test_client().get("/", **on("testdomain")).data.decode()


def test_server_description_is_escaped(client, db, admin_id):
    db.add_server(admin_id, "xss", "x.example.com", "X", server_description_long="<script>alert(1)</script>\nline2")
    body = client.get("/", **on("xss")).data.decode()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# ------------------------------------------------------------------ server admin area

@pytest.fixture
def admin_client(client, admin_id):
    assert client.post("/api/login", json={"username": "tobi", "password": "testPassword"}, **on(None)).status_code == 200
    return client


NEW_SERVER = {"subdomain": "survival", "server_name": "Survival", "mc_server_domain": "play.example.com",
              "server_description_short": "short", "server_description_long": "long",
              "discord_url": "https://discord.gg/abc"}


def test_admin_pages_require_login(client):
    assert client.get("/create", **on(None)).status_code == 302
    assert client.post("/api/servers", json=NEW_SERVER, **on(None)).status_code == 401


def test_admin_api_requires_json(admin_client):
    assert admin_client.post("/api/servers", data=NEW_SERVER, **on(None)).status_code == 415


def test_create_server(admin_client, db, admin_id):
    response = admin_client.post("/api/servers", json=dict(NEW_SERVER, subdomain="Survival"), **on(None))
    assert response.status_code == 201
    assert response.json["subdomain"] == "survival"
    info = db.get_server_information_dict("survival")
    assert info["owner_id"] == admin_id and info["discord_url"] == "https://discord.gg/abc"
    assert admin_client.get("/", **on("survival")).status_code == 200

    manage = admin_client.get("/manage", **on(None)).data.decode()
    key = db._fetchvalue("SELECT server_key FROM servers WHERE subdomain = 'survival'")
    assert key in manage and "Plugin nicht verbunden" in manage
    assert admin_client.post("/api/servers", json=NEW_SERVER, **on(None)).status_code == 409


@pytest.mark.parametrize("changes", [
    {"subdomain": "ab"}, {"subdomain": "-bad-"}, {"subdomain": "www"}, {"subdomain": "a.b.c"},
    {"server_name": ""}, {"mc_server_domain": "has space"},
    {"discord_url": "javascript:alert(1)"}, {"discord_url": "https://evil.com/x"},
    {"server_description_short": "x" * 201},
])
def test_create_server_validation(admin_client, changes):
    assert admin_client.post("/api/servers", json=dict(NEW_SERVER, **changes), **on(None)).status_code == 400


def test_update_server(admin_client, db, server):
    response = admin_client.post(f"/api/servers/{server['id']}/update",
                                 json={"server_name": "Renamed", "discord_url": ""}, **on(None))
    assert response.status_code == 200
    info = db.get_server_information_dict("testdomain")
    assert info["server_name"] == "Renamed" and info["discord_url"] is None
    assert admin_client.post(f"/api/servers/{server['id']}/update", json={"discord_url": "javascript:x"},
                             **on(None)).status_code == 400


def test_regenerate_key(admin_client, db, server):
    response = admin_client.post(f"/api/servers/{server['id']}/regenerate_key", json={}, **on(None))
    assert response.status_code == 200
    assert db.get_server_id_by_auth_key(response.json["server_key"]) == server["id"]


def test_delete_server_needs_confirmation(admin_client, db, server):
    url = f"/api/servers/{server['id']}/delete"
    assert admin_client.post(url, json={"confirm": "wrong"}, **on(None)).status_code == 400
    assert admin_client.post(url, json={"confirm": "testdomain"}, **on(None)).status_code == 200
    assert db.get_server_information_dict("testdomain") is None


def test_cannot_touch_servers_of_other_admins(client, db, server):
    db.add_server_admin("eve", "secret123", "eve@example.com", email_verified=True)
    client.post("/api/login", json={"username": "eve", "password": "secret123"}, **on(None))
    for action, body in [("update", {"server_name": "x"}), ("regenerate_key", {}), ("delete", {"confirm": "testdomain"})]:
        assert client.post(f"/api/servers/{server['id']}/{action}", json=body, **on(None)).status_code == 404
    assert server["key"] not in client.get("/manage", **on(None)).data.decode()


def test_plugin_download(client, monkeypatch, tmp_path):
    from database import config
    monkeypatch.setattr(config, "PLUGIN_JAR", str(tmp_path / "missing.jar"))
    assert client.get("/download/MCDataLink.jar", **on(None)).status_code == 404
    jar = tmp_path / "MCDataLink-3.0.jar"
    jar.write_bytes(b"PK jar")
    monkeypatch.setattr(config, "PLUGIN_JAR", str(jar))
    response = client.get("/download/MCDataLink.jar", **on(None))
    assert response.status_code == 200 and response.data == b"PK jar"
    assert "attachment" in response.headers["Content-Disposition"]


def test_proxy_fix_uses_forwarded_scheme(db, server):
    app = create_app(db, {"TESTING": True, "SECRET_KEY": "t", "SERVER_NAME": BASE, "PROXY_FIX": True})
    body = app.test_client().get("/", headers={"X-Forwarded-Proto": "https"}, **on("testdomain")).data.decode()
    assert f"https://{BASE}/static/" in body


# ------------------------------------------------------------------ signup corrections

class FakeMailer:
    def __init__(self):
        self.sent = []

    def send_email(self, recipient, subject, html):
        self.sent.append((recipient, html))


@pytest.fixture
def mailer(app):
    fake = FakeMailer()
    app.extensions["mcconnect_mailer"] = fake
    return fake


def test_signup_again_fixes_typo_in_email(client, db, mailer):
    body = {"username": "alice", "email": "alice@exmaple.com", "password": "secret123"}
    assert client.post("/api/signup", json=body, **on(None)).status_code == 200
    body["email"] = "alice@example.com"
    assert client.post("/api/signup", json=body, **on(None)).status_code == 200
    assert [recipient for recipient, _ in mailer.sent] == ["alice@exmaple.com", "alice@example.com"]
    token = db._fetchvalue("SELECT token FROM email_verification")
    assert token in mailer.sent[-1][1]
    assert client.get(f"/verify_email/alice/{token}", **on(None)).status_code == 200


def test_resend_verification(client, db, mailer):
    client.post("/api/signup", json={"username": "alice", "email": "alice@example.com",
                                     "password": "secret123"}, **on(None))
    with db._cursor() as cur:
        cur.execute("UPDATE email_verification SET created_at = now() - interval '2 minutes'")
    assert client.post("/api/resend_verification", json={"email": "alice@example.com"}, **on(None)).status_code == 200
    assert len(mailer.sent) == 2
    # rate limited, unknown and invalid addresses answer the same but send nothing
    for email in ["alice@example.com", "nobody@example.com", "not-an-email"]:
        assert client.post("/api/resend_verification", json={"email": email}, **on(None)).status_code == 200
    assert len(mailer.sent) == 2


# ------------------------------------------------------------------ password reset

def _reset_link_token(mailer):
    import re as _re
    return _re.search(r"/reset_password/([A-Za-z0-9]+)", mailer.sent[-1][1]).group(1)


def test_password_reset_flow(client, db, admin_id, mailer):
    assert client.get("/forgot_password", **on(None)).status_code == 200
    assert client.post("/api/password_reset/request", json={"email": "tobi@example.com"}, **on(None)).status_code == 200
    assert mailer.sent[-1][0] == "tobi@example.com"
    token = _reset_link_token(mailer)

    page = client.get(f"/reset_password/{token}", **on(None))
    assert page.status_code == 200 and b"tobi" in page.data
    assert client.post("/api/password_reset/confirm", json={"token": token, "password": "short"},
                       **on(None)).status_code == 400
    assert client.post("/api/password_reset/confirm", json={"token": token, "password": "brandNew123"},
                       **on(None)).status_code == 200
    assert client.post("/api/login", json={"username": "tobi", "password": "brandNew123"}, **on(None)).status_code == 200
    assert client.get(f"/reset_password/{token}", **on(None)).status_code == 400  # used


def test_password_reset_request_reveals_nothing(client, mailer):
    for email in ["nobody@example.com", "invalid"]:
        assert client.post("/api/password_reset/request", json={"email": email}, **on(None)).status_code == 200
    assert mailer.sent == []


def test_password_reset_logs_out_other_sessions(app, db, admin_client, mailer):
    assert admin_client.get("/manage", **on(None)).status_code == 200
    other = app.test_client()
    other.post("/api/password_reset/request", json={"email": "tobi@example.com"}, **on(None))
    other.post("/api/password_reset/confirm", json={"token": _reset_link_token(mailer), "password": "brandNew123"},
               **on(None))
    # the session from before the reset is no longer valid
    assert admin_client.get("/manage", **on(None)).status_code == 302
    assert admin_client.post("/api/servers", json=NEW_SERVER, **on(None)).status_code == 401


def test_session_of_deleted_admin_is_invalid(db, admin_client):
    with db._cursor() as cur:
        cur.execute("DELETE FROM server_admins")
    assert admin_client.get("/manage", **on(None)).status_code == 302


def test_session_without_login_time_is_invalid(client, admin_id):
    with client.session_transaction(base_url=f"http://{BASE}") as sess:  # e.g. created before this version
        sess["admin_id"] = admin_id
        sess["admin_username"] = "tobi"
    assert client.get("/manage", **on(None)).status_code == 302


def test_admin_login_with_email(client, admin_id):
    response = client.post("/api/login", json={"username": " Tobi@example.com ", "password": "testPassword"}, **on(None))
    assert response.status_code == 200
    manage = client.get("/manage", **on(None))
    assert manage.status_code == 200 and b"Logout (tobi)" in manage.data


def test_fonts_can_be_embedded_from_subdomains(client):
    response = client.get("/static/fonts/chakra-petch-700.woff2", headers={"Origin": f"http://testdomain.{BASE}"}, **on(None))
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Origin" not in client.get("/static/css/admin.css", **on(None)).headers


def test_servers_status_polling(admin_client, db, server, other_server):
    data = admin_client.get("/api/servers/status", **on(None)).json["servers"]
    assert {s["id"]: s["plugin_online"] for s in data} == {server["id"]: False, other_server["id"]: False}
    db.set_plugin_connected(server["id"], True)
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    data = {s["id"]: s for s in admin_client.get("/api/servers/status", **on(None)).json["servers"]}
    assert data[server["id"]]["plugin_online"] is True and data[server["id"]]["player_count"] == 1
    page = admin_client.get("/manage", **on(None)).data.decode()
    assert f'id="plugin-badge-{server["id"]}" data-online="true"' in page


def test_servers_status_requires_login(client):
    assert client.get("/api/servers/status", **on(None)).status_code == 401


def test_player_info_without_deaths_shows_dash(client, db, server):
    db.set_plugin_connected(server["id"], True)
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player_id, {"stats": {"minecraft:custom": {"minecraft:time_since_death": 72000}}})
    data = first_event(client.get("/api/player_info/_Tobias4444", buffered=False, **on("testdomain")))
    assert data[2] == 0 and data[5] == "-"


# ------------------------------------------------------------------ public / whitelist servers

def test_create_whitelist_server_without_address(admin_client, db):
    body = dict(NEW_SERVER, whitelist=True, mc_server_domain="")
    assert admin_client.post("/api/servers", json=body, **on(None)).status_code == 201
    info = db.get_server_information_dict("survival")
    assert info["whitelist"] is True and info["mc_server_domain"] is None


def test_public_server_needs_address(admin_client):
    body = dict(NEW_SERVER, whitelist=False, mc_server_domain="")
    response = admin_client.post("/api/servers", json=body, **on(None))
    assert response.status_code == 400 and "Adresse" in response.json["error"]


def test_switch_server_to_whitelist_and_back(admin_client, db, server):
    url = f"/api/servers/{server['id']}/update"
    assert admin_client.post(url, json={"whitelist": True, "mc_server_domain": ""}, **on(None)).status_code == 200
    assert db.get_server_information_dict("testdomain")["whitelist"] is True
    # back to public without an address is refused, with one it works
    assert admin_client.post(url, json={"whitelist": False}, **on(None)).status_code == 400
    assert admin_client.post(url, json={"whitelist": False, "mc_server_domain": "play.example.com"},
                             **on(None)).status_code == 200
    # updating other fields keeps the access mode
    assert admin_client.post(url, json={"server_name": "Renamed"}, **on(None)).status_code == 200
    assert db.get_server_information_dict("testdomain")["whitelist"] is False


def test_public_server_page_shows_address(client, server):
    body = client.get("/", **on("testdomain")).data.decode()
    assert "Öffentlich" in body and "mc.example.com" in body and "Willkommen" not in body


def test_whitelist_server_page_hides_address(client, db, admin_id):
    db.add_server(admin_id, "private", "secret.example.com", "Private", whitelist=True,
                  server_description_short="short", server_description_long="long")
    for path in ["/", "/spieler"]:
        body = client.get(path, **on("private")).data.decode()
        assert "secret.example.com" not in body
    assert "Whitelist" in client.get("/", **on("private")).data.decode()


# ------------------------------------------------------------------ server images

import io
import os


def png_bytes(size=(64, 32), color=(200, 30, 30)):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    from database import config
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path))
    return tmp_path


def upload(client, server_id, kind="gallery", data=None, filename="bild.png", headers=None):
    return client.post(f"/api/servers/{server_id}/images",
                       data={"kind": kind, "image": (io.BytesIO(data or png_bytes()), filename)},
                       content_type="multipart/form-data", headers=headers or {}, **on(None))


def test_upload_gallery_image_and_show_it(admin_client, db, server, upload_dir):
    response = upload(admin_client, server["id"])
    assert response.status_code == 201
    files = os.listdir(upload_dir)
    assert len(files) == 1 and files[0].endswith(".webp")
    assert response.json["url"] == f"http://{BASE}/uploads/{files[0]}"

    served = admin_client.get(f"/uploads/{files[0]}", **on(None))
    assert served.status_code == 200 and served.data[:4] == b"RIFF"  # re-encoded as WebP
    page = admin_client.get("/", **on("testdomain")).data.decode()
    assert f"/uploads/{files[0]}" in page and "Eindrücke" in page


def test_upload_strips_metadata(admin_client, server, upload_dir):
    from PIL import Image
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"  # Make
    Image.new("RGB", (40, 40)).save(buffer, "JPEG", exif=exif)
    assert upload(admin_client, server["id"], data=buffer.getvalue(), filename="x.jpg").status_code == 201
    stored = Image.open(upload_dir / os.listdir(upload_dir)[0])
    assert "SecretCamera" not in str(stored.getexif())


def test_banner_replaces_old_file_and_is_downscaled(admin_client, db, server, upload_dir):
    from PIL import Image
    upload(admin_client, server["id"], kind="banner", data=png_bytes((3000, 1000)))
    first = os.listdir(upload_dir)
    assert Image.open(upload_dir / first[0]).size == (2400, 800)
    upload(admin_client, server["id"], kind="banner")
    second = os.listdir(upload_dir)
    assert len(second) == 1 and second != first
    assert f"/uploads/{second[0]}" in admin_client.get("/", **on("testdomain")).data.decode()


def test_upload_rejects_non_images(admin_client, server, upload_dir):
    for data, name in [(b"<?php echo 1; ?>", "shell.png"), (b"GIF89a" + b"x" * 20, "broken.gif")]:
        response = upload(admin_client, server["id"], data=data, filename=name)
        assert response.status_code == 400
    assert os.listdir(upload_dir) == []


def test_upload_too_large(admin_client, server, upload_dir):
    response = upload(admin_client, server["id"], data=b"x" * (11 * 1024 * 1024))
    assert response.status_code == 413 and "zu groß" in response.json["error"]


def test_gallery_limit(admin_client, db, admin_id, server, upload_dir):
    from database.databaseManagerV2 import MAX_GALLERY_IMAGES
    for i in range(MAX_GALLERY_IMAGES):
        db.add_server_image(server["id"], admin_id, "gallery", f"{i:032x}.webp")
    response = upload(admin_client, server["id"])
    assert response.status_code == 400 and "voll" in response.json["error"]
    assert os.listdir(upload_dir) == []  # the processed file was removed again


def test_upload_security(client, admin_client, db, server, upload_dir):
    assert upload(admin_client, server["id"], headers={"Origin": "https://evil.example"}).status_code == 403
    assert upload(admin_client, server["id"], headers={"Origin": f"http://{BASE}"}).status_code == 201
    other = create_app(db, {"TESTING": True, "SECRET_KEY": "test", "SERVER_NAME": BASE}).test_client()
    assert upload(other, server["id"]).status_code == 401
    # other JSON APIs still refuse form posts
    assert admin_client.post(f"/api/servers/{server['id']}/update", data={"server_name": "x"},
                             **on(None)).status_code == 415


def test_upload_to_foreign_server(client, db, server, upload_dir):
    db.add_server_admin("eve", "secret123", "eve@example.com", email_verified=True)
    client.post("/api/login", json={"username": "eve", "password": "secret123"}, **on(None))
    assert upload(client, server["id"]).status_code == 404
    assert os.listdir(upload_dir) == []


def test_delete_image_and_server_removes_files(admin_client, db, server, upload_dir):
    image_id = upload(admin_client, server["id"]).json["id"]
    upload(admin_client, server["id"], kind="banner")
    assert admin_client.post(f"/api/servers/{server['id']}/images/{image_id}/delete", json={},
                             **on(None)).status_code == 200
    assert len(os.listdir(upload_dir)) == 1
    admin_client.post(f"/api/servers/{server['id']}/delete", json={"confirm": "testdomain"}, **on(None))
    assert os.listdir(upload_dir) == []


def test_uploads_route_only_serves_generated_names(client, upload_dir):
    (upload_dir / "secret.txt").write_text("x")
    assert client.get("/uploads/secret.txt", **on(None)).status_code == 404
    assert client.get("/uploads/..%2Fsecret.txt", **on(None)).status_code == 404



# ------------------------------------------------------------------ prefixes

@pytest.fixture
def player_client(client, db, online_player):
    """Client logged in as _Tobias4444 on testdomain."""
    assert login_player(client, db, online_player).json["status"] == "success"
    return client


def test_prefix_pages_require_login(client, server):
    response = client.get("/add_pref", **on("testdomain"))
    assert response.status_code == 302 and "next=/add_pref" in response.location
    assert client.post("/api/prefix/save", json={}, **on("testdomain")).status_code == 401


def test_create_prefix_and_show_it(player_client, db, server, online_player):
    assert player_client.get("/add_pref", **on("testdomain")).status_code == 200
    response = player_client.post("/api/prefix/save", json={"text": "Bauteam", "color": "aqua"}, **on("testdomain"))
    assert response.status_code == 200
    assert db.get_player_prefix(online_player)["text"] == "Bauteam"
    assert "[Bauteam]" in player_client.get("/spieler", **on("testdomain")).data.decode()
    assert "[Bauteam]" in player_client.get("/spieler?player=_Tobias4444", **on("testdomain")).data.decode()
    assert "[Bauteam]" in player_client.get("/join_pref", **on("testdomain")).data.decode()


@pytest.mark.parametrize("body", [
    {"text": "", "color": "aqua"}, {"text": "x" * 17, "color": "aqua"}, {"text": "§cRot", "color": "aqua"},
    {"text": "a|b", "color": "aqua"}, {"text": "100%", "color": "aqua"}, {"text": "ok", "color": "pink"},
    {"text": "ok", "color": "aqua", "password": "abc"},
])
def test_prefix_validation(player_client, body):
    assert player_client.post("/api/prefix/save", json=body, **on("testdomain")).status_code == 400


def test_prefix_requires_json(player_client):
    assert player_client.post("/api/prefix/save", data={"text": "x", "color": "aqua"},
                              **on("testdomain")).status_code == 415


def test_join_and_leave_prefix(player_client, db, server):
    owner = db.ensure_player_on_server(server["id"], OTHER_UUID)
    db.save_own_prefix(owner, "Clan", "red", password="geheim")
    prefix_id = db.get_owned_prefix(owner)["prefix_id"]
    url = "/api/prefix/join"
    assert player_client.post(url, json={"prefix_id": prefix_id, "password": "x"}, **on("testdomain")).status_code == 403
    assert player_client.post(url, json={"prefix_id": 99999}, **on("testdomain")).status_code == 404
    assert player_client.post(url, json={"prefix_id": prefix_id, "password": "geheim"}, **on("testdomain")).status_code == 200
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    assert db.get_player_prefix(player_id)["text"] == "Clan"
    assert player_client.post("/api/prefix/leave", json={}, **on("testdomain")).status_code == 200
    assert db.get_player_prefix(player_id) is None


def test_duplicate_prefix_text(player_client, db, server):
    owner = db.ensure_player_on_server(server["id"], OTHER_UUID)
    db.save_own_prefix(owner, "Clan", "red")
    response = player_client.post("/api/prefix/save", json={"text": "clan", "color": "aqua"}, **on("testdomain"))
    assert response.status_code == 409


def test_prefix_is_escaped_on_pages(player_client, db, server):
    owner = db.ensure_player_on_server(server["id"], OTHER_UUID)
    with db._cursor() as cur:  # bypasses the web validation on purpose
        cur.execute("INSERT INTO prefixes (prefix_owner_id, server_id, prefix_text) VALUES (%s, %s, '<b>x</b>')",
                    (owner, server["id"]))
    assert "<b>x</b>" not in player_client.get("/join_pref", **on("testdomain")).data.decode()


# ------------------------------------------------------------------ moderation

def test_moderation_page_only_for_moderators(player_client, db, server):
    assert player_client.get("/users", **on("testdomain")).status_code == 403
    assert player_client.get("/api/mod/bans", **on("testdomain")).status_code == 403
    db.set_moderator(server["id"], "_Tobias4444", True)
    page = player_client.get("/users", **on("testdomain"))
    assert page.status_code == 200 and "Spieler bannen" in page.data.decode()
    assert "/users" in player_client.get("/", **on("testdomain")).data.decode()  # menu entry


def test_op_becomes_moderator_only_with_setting(player_client, db, admin_id, server):
    db.set_player_op(server["id"], PLAYER_UUID, True)
    assert player_client.get("/users", **on("testdomain")).status_code == 403
    db.update_server(server["id"], admin_id, auto_mod_ops=True)
    assert player_client.get("/users", **on("testdomain")).status_code == 200


def test_moderator_bans_and_unbans(player_client, db, server):
    db.set_moderator(server["id"], "_Tobias4444", True)
    target = db.ensure_player_on_server(server["id"], OTHER_UUID)
    reason_id = db._fetchvalue("SELECT id FROM ban_reasons WHERE reason = 'spamming'")
    response = player_client.post("/api/mod/ban", json={"name": "notch", "reason_id": reason_id, "days": "",
                                                        "comment": "Chat"}, **on("testdomain"))
    assert response.status_code == 200
    [ban] = player_client.get("/api/mod/bans", **on("testdomain")).json["bans"]
    assert (ban["name"], ban["reason"], ban["banned_by"], ban["source"]) == ("Notch", "spamming", "_Tobias4444", "web")
    assert "Notch" in player_client.get("/spieler?player=Notch", **on("testdomain")).data.decode()
    assert player_client.post("/api/mod/unban", json={"player_id": target}, **on("testdomain")).status_code == 200
    assert player_client.get("/api/mod/bans", **on("testdomain")).json["bans"] == []


@pytest.mark.parametrize("body, status", [
    ({"name": "nobody"}, 404), ({"name": "Notch", "days": "abc"}, 400), ({"name": "Notch", "days": 5000}, 400),
])
def test_ban_validation(player_client, db, server, body, status):
    db.set_moderator(server["id"], "_Tobias4444", True)
    db.ensure_player_on_server(server["id"], OTHER_UUID)
    assert player_client.post("/api/mod/ban", json=body, **on("testdomain")).status_code == status


def test_permanent_ban_shows_on_player_page(client, db, server):
    player_id = db.ensure_player_on_server(server["id"], OTHER_UUID)
    db.ban_player(server["id"], "Notch", "Admin", days=0)
    body = client.get("/spieler?player=Notch", **on("testdomain")).data.decode()
    assert "dauerhaft" in body


def test_admin_manages_moderators_and_bans(admin_client, db, server):
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    url = f"/api/servers/{server['id']}"
    assert admin_client.post(f"{url}/moderators", json={"name": "_tobias4444", "moderator": True},
                             **on(None)).json["name"] == "_Tobias4444"
    assert admin_client.post(f"{url}/moderators", json={"name": "nobody", "moderator": True}, **on(None)).status_code == 404
    assert [m["name"] for m in admin_client.get(f"{url}/moderation", **on(None)).json["moderators"]] == ["_Tobias4444"]
    assert admin_client.post(f"{url}/update", json={"auto_mod_ops": True}, **on(None)).status_code == 200
    assert db.get_server_information_dict("testdomain")["auto_mod_ops"] is True

    assert admin_client.post(f"{url}/ban", json={"name": "_Tobias4444", "days": 1}, **on(None)).status_code == 200
    [ban] = admin_client.get(f"{url}/moderation", **on(None)).json["bans"]
    assert ban["banned_by"] == "Admin tobi"
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    assert admin_client.post(f"{url}/unban", json={"player_id": player_id}, **on(None)).status_code == 200
    page = admin_client.get("/manage", **on(None)).data.decode()
    assert "Moderation" in page and "OPs vom Minecraft-Server" in page


def test_admin_cannot_moderate_foreign_server(client, db, server):
    db.add_server_admin("eve", "secret123", "eve@example.com", email_verified=True)
    client.post("/api/login", json={"username": "eve", "password": "secret123"}, **on(None))
    url = f"/api/servers/{server['id']}"
    assert client.get(f"{url}/moderation", **on(None)).status_code == 404
    assert client.post(f"{url}/ban", json={"name": "x"}, **on(None)).status_code == 404
    assert client.post(f"{url}/moderators", json={"name": "x", "moderator": True}, **on(None)).status_code == 404


def test_ban_dropdown_groups_online_and_offline(player_client, db, server):
    db.set_moderator(server["id"], "_Tobias4444", True)  # online (online_player fixture)
    db.ensure_player_on_server(server["id"], OTHER_UUID)  # Notch, offline
    db.add_player("11111111-2222-3333-4444-555555555555", "alex")
    db.ensure_player_on_server(server["id"], "11111111-2222-3333-4444-555555555555")  # alex, offline
    body = player_client.get("/users", **on("testdomain")).data.decode()
    online, offline = body.index('label="Online (1)"'), body.index('label="Offline (2)"')
    assert online < body.index('value="_Tobias4444"') < offline < body.index('value="alex"') < body.index('value="Notch"')


def test_gold_prefix_only_for_moderators(player_client, db, server, online_player):
    body = {"text": "Gold", "color": "gold"}
    response = player_client.post("/api/prefix/save", json=body, **on("testdomain"))
    assert response.status_code == 403 and "Moderatoren" in response.json["error"]
    page = player_client.get("/add_pref", **on("testdomain")).data.decode()
    assert 'value="gold"' not in page and 'value="aqua" checked' in page

    db.set_moderator(server["id"], "_Tobias4444", True)
    assert 'value="gold"' in player_client.get("/add_pref", **on("testdomain")).data.decode()
    assert player_client.post("/api/prefix/save", json=body, **on("testdomain")).status_code == 200
    assert db.get_player_prefix(online_player)["color"] == "gold"
