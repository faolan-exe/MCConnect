"""Profile, privacy ("hide my stats"), favourites, stat card and activity feed."""
import json
from datetime import timedelta

import pytest

from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_achievements import custom
from tests.test_metrics import STATS_A, set_snapshot_day, two_players  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)


@pytest.fixture
def me(client, db, server, two_players):
    """Client logged in as _Tobias4444 (two players on the server: _Tobias4444 and Notch)."""
    a, b = two_players
    db.set_plugin_connected(server["id"], True)
    assert login_player(client, db, a).json["status"] == "success"
    return client


@pytest.fixture(autouse=True)
def no_avatar_download(monkeypatch):
    monkeypatch.setattr("web.card._avatar", lambda uuid: None)


# ------------------------------------------------------------------ profile

def test_profile_page_requires_login(client, server):
    assert client.get("/profil", **on("testdomain")).headers["Location"].startswith("/login")


def test_save_profile(me, db, two_players):
    a, _ = two_players
    response = me.post("/api/profile", json={"bio": "  Baue  gerade\n eine Brücke ", "hide_stats": False},
                       **on("testdomain"))
    assert response.status_code == 200
    assert db.get_profile(a) == {"bio": "Baue gerade eine Brücke", "hide_stats": False}
    assert "Baue gerade" in me.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert me.post("/api/profile", json={"bio": "x" * 161}, **on("testdomain")).status_code == 400
    assert "Meine Statistiken verbergen" in me.get("/profil", **on("testdomain")).get_data(as_text=True)


# ------------------------------------------------------------------ privacy

@pytest.fixture
def notch_hidden(db, two_players):
    _, b = two_players
    db.save_profile(b, "Psst", True)
    return b


def test_hidden_player_page(client, notch_hidden):
    html = client.get("/spieler?player=Notch", **on("testdomain")).get_data(as_text=True)
    assert "hat die eigenen Statistiken verborgen" in html and "Psst" in html
    assert "og:image" not in html
    assert client.get("/api/player_info/Notch", **on("testdomain")).status_code == 404
    assert client.get("/spieler/Notch/karte.png", **on("testdomain")).status_code == 404


def test_own_hidden_page_is_visible(me, db, two_players):
    a, _ = two_players
    db.save_profile(a, None, True)
    html = me.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Deine Statistiken sind verborgen" in html
    assert me.get("/api/player_info/_Tobias4444", buffered=False, **on("testdomain")).status_code == 200


def test_hidden_player_left_out_of_public_views(client, db, server, notch_hidden):
    for path in ("/rangliste", "/vergleich", "/teams"):
        assert "Notch" not in client.get(path, **on("testdomain")).get_data(as_text=True), path
    html = client.get("/vergleich?spieler=_Tobias4444,Notch", **on("testdomain")).get_data(as_text=True)
    assert "Noch niemand zum Vergleichen" in html
    player_list = client.get("/spieler", **on("testdomain")).get_data(as_text=True)
    assert "Notch" in player_list and "Statistiken verborgen" in player_list
    # server totals still count Notch (30 of the 137 blocks)
    assert "137" in client.get("/server-statistik", **on("testdomain")).get_data(as_text=True)


def test_hidden_player_left_out_of_competitions(db, server, notch_hidden):
    today = db.get_today()
    set_snapshot_day(db, notch_hidden, 3)
    db.update_player_stats(notch_hidden, {"stats": {"minecraft:mined": {"minecraft:stone": 90}}})
    cid = db.create_competition(server["id"], "Test", "blocks_mined", today - timedelta(days=1), today)
    assert db.get_competition_standings(db.get_competition(cid)) == []


def test_hidden_player_achievements_are_not_announced(db, server, plugin):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.save_profile(player_id, None, True)
    client = plugin().auth(server["key"])
    assert client.request(f"!STATS~{PLAYER_UUID}|{json.dumps(custom(deaths=10))}") == "success|102"
    assert client.request("!UNKNOWN~x") == "error|004"  # no broadcast in between


# ------------------------------------------------------------------ favourites

def test_favorites(me, db, two_players):
    a, _ = two_players
    assert me.post("/api/favorites/toggle", json={"name": "notch"}, **on("testdomain")).json == {"favorite": True}
    assert db.get_favorites(a)[0]["name"] == "Notch"
    html = me.get("/spieler", **on("testdomain")).get_data(as_text=True)
    assert 'data-filter="favorites"' in html and 'data-fav="1"' in html
    assert "Mich mit meinen Favoriten vergleichen" in me.get("/vergleich", **on("testdomain")).get_data(as_text=True)
    assert "★ Favorit" in me.get("/spieler?player=Notch", **on("testdomain")).get_data(as_text=True)
    assert me.post("/api/favorites/toggle", json={"name": "Notch"}, **on("testdomain")).json == {"favorite": False}
    assert me.post("/api/favorites/toggle", json={"name": "_Tobias4444"}, **on("testdomain")).status_code == 404
    assert me.post("/api/favorites/toggle", json={"name": "nobody"}, **on("testdomain")).status_code == 404


def test_favorites_require_login(client, two_players):
    assert client.post("/api/favorites/toggle", json={"name": "Notch"}, **on("testdomain")).status_code == 401
    assert 'data-filter="favorites"' not in client.get("/spieler", **on("testdomain")).get_data(as_text=True)


# ------------------------------------------------------------------ stat card

def test_stat_card(client, two_players):
    response = client.get("/spieler/_Tobias4444/karte.png", **on("testdomain"))
    assert response.status_code == 200 and response.mimetype == "image/png"
    assert response.data[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(response.data)).size == (1200, 630)
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert 'property="og:image" content="http://testdomain.mc.test/spieler/_Tobias4444/karte.png"' in html


def test_stat_card_unknown_player(client, server):
    assert client.get("/spieler/nobody/karte.png", **on("testdomain")).status_code == 404


# ------------------------------------------------------------------ activity feed

def test_feed(client, db, server, two_players):
    a, b = two_players  # both joined just now: "new"
    db.update_player_stats(a, custom(deaths=12))
    db.award_achievements(a)  # 1 new tier -> shown
    db.update_player_stats(b, custom(deaths=600, jump=200_000, fish_caught=2_000))
    db.award_achievements(b)  # bulk -> hidden
    events = client.get("/api/feed", **on("testdomain")).json["events"]
    kinds = [(e["kind"], e["name"]) for e in events]
    assert ("new", "_Tobias4444") in kinds and ("new", "Notch") in kinds
    assert ("achievement", "_Tobias4444") in kinds
    assert ("achievement", "Notch") not in kinds
    achievement = next(e for e in events if e["kind"] == "achievement")
    assert achievement["text"] == "hat den Erfolg »Pechvogel« (Bronze) erreicht" and achievement["tier"] == "bronze"


def test_feed_hides_hidden_players(client, db, notch_hidden):
    events = client.get("/api/feed", **on("testdomain")).json["events"]
    assert all(e["name"] != "Notch" for e in events)


def test_feed_shows_competitions(client, db, server):
    today = db.get_today()
    db.create_competition(server["id"], "Angel-Cup", "fish_caught", today, today + timedelta(days=3))
    events = client.get("/api/feed", **on("testdomain")).json["events"]
    assert events[0]["kind"] == "competition_start" and "Angel-Cup" in events[0]["text"]
