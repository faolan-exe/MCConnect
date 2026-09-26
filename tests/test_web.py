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
    assert b"Statistiken" in client.get("/", **on("testdomain")).data
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
    assert b"Statistiken" not in response.data


def test_player_logout(client, db, online_player):
    login_player(client, db, online_player)
    response = client.post("/login", data={"text_input": "logout"}, **on("testdomain"))
    assert response.status_code == 302
    assert b"Statistiken" not in client.get("/", **on("testdomain")).data


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


def test_unfinished_menu_items_are_hidden(client, server):
    body = client.get("/", **on("testdomain")).data.decode()
    assert "/add_pref" not in body and "/users" not in body


def test_menu_items_can_be_enabled(db, server):
    app = create_app(db, {"TESTING": True, "SECRET_KEY": "t", "SERVER_NAME": BASE, "FEATURE_PREFIXES": True})
    assert "/add_pref" in app.test_client().get("/", **on("testdomain")).data.decode()


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
