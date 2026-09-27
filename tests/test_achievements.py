"""Achievements, competitions (with in-game announcements) and teams."""
import json
from datetime import timedelta

import pytest

from database import achievements
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_metrics import STATS_A, set_snapshot_day, two_players  # noqa: F401 (fixture)
from tests.test_socket import FakePlugin, plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

A = achievements.ACHIEVEMENTS_BY_KEY


def custom(**values):
    return {"stats": {"minecraft:custom": {f"minecraft:{k}": v for k, v in values.items()}}}


# ------------------------------------------------------------------ achievements

def test_tiers_and_progress():
    assert achievements.tier_of(A["unlucky"], 9) == -1
    assert achievements.tier_of(A["unlucky"], 10) == 0
    assert achievements.tier_of(A["unlucky"], 10_000) == 3
    p = achievements.progress(A["regular"], 30 * achievements.HOUR)  # 30 hours
    assert p["tier_label"] == "Bronze" and p["value"] == "30"
    assert p["next"]["label"] == "Silber" and p["next"]["threshold"] == "50"
    assert p["next"]["share"] == pytest.approx(0.5)
    assert "next" not in achievements.progress(A["unlucky"], 500)


def test_award_only_new_tiers(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player_id, custom(deaths=12))
    assert db.award_achievements(player_id) == [(A["unlucky"], 0)]
    assert db.award_achievements(player_id) == []
    db.update_player_stats(player_id, custom(deaths=60))
    assert db.award_achievements(player_id) == [(A["unlucky"], 1)]
    assert set(db.get_player_achievements(player_id)) == {("unlucky", 0), ("unlucky", 1)}


def test_new_achievement_is_announced_in_game(db, server, plugin):
    client = plugin().auth(server["key"])
    assert client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    assert client.request(f"!STATS~{PLAYER_UUID}|{json.dumps(custom(deaths=10))}") == "success|102"
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    name = db.get_player_name_from_player_id(player_id)
    assert client.recv() == f"!broadcast~gold|★ {name} hat den Erfolg »Pechvogel« (Bronze) erreicht!"


def test_stats_of_offline_players_award_silently(db, server, plugin, client):
    """The plugin sends the stats of all players when it connects: old tiers are no news."""
    connection = plugin().auth(server["key"])
    assert connection.request(f"!STATS~{PLAYER_UUID}|{json.dumps(custom(deaths=10))}") == "success|102"
    assert connection.request("!UNKNOWN~x") == "error|004"  # no broadcast in between
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    assert ("unlucky", 0) in db.get_player_achievements(player_id)  # stored nevertheless
    events = client.get("/api/feed", **on("testdomain")).json["events"]
    assert all(e["kind"] != "achievement" for e in events)


def test_achievement_right_after_quit_counts(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.register_player_quit(server["id"], PLAYER_UUID)
    db.update_player_stats(player_id, custom(deaths=10))  # stats arrive shortly after the quit
    assert db.award_achievements(player_id) == [(A["unlucky"], 0)]


def test_migration_marks_old_catch_up_achievements_silent(db, server):
    from database.databaseManagerV2 import MIGRATIONS
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player_id, custom(deaths=60))
    db.award_achievements(player_id)
    with db._cursor() as cur:  # the silver tier was stored during a sync long after the session
        cur.execute("""UPDATE player_achievements SET silent = false,
                       earned_at = now() + interval '1 hour' WHERE tier = 1""")
        cur.execute(MIGRATIONS[11][1])
        cur.execute("SELECT tier, silent FROM player_achievements ORDER BY tier")
        assert cur.fetchall() == [(0, False), (1, True)]


def test_many_achievements_at_once_are_not_announced(db, server, plugin):
    client = plugin().auth(server["key"])
    stats = custom(deaths=600, jump=200_000, fish_caught=2_000)
    assert client.request(f"!STATS~{PLAYER_UUID}|{json.dumps(stats)}") == "success|102"
    assert client.request("!BEAT_UNKNOWN~x") == "error|004"  # the next message is the answer, no broadcast


def test_achievements_on_player_page(client, db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player_id, custom(deaths=12))
    db.award_achievements(player_id)
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Erfolge · 1 von 52 Stufen" in html
    assert "Pechvogel" in html and "Silber ab 50" in html


# ------------------------------------------------------------------ competitions

@pytest.fixture
def competition(db, server, two_players):
    a, _ = two_players
    today = db.get_today()
    set_snapshot_day(db, a, 3)  # baseline before the start: 107 blocks
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": 150}})})
    cid = db.create_competition(server["id"], "Mining-Woche", "blocks_mined", today - timedelta(days=2),
                                today + timedelta(days=4), created_by="_Tobias4444")
    return db.get_competition(cid)


def test_competition_standings(db, competition):
    rows = db.get_competition_standings(competition)
    assert [(r["name"], r["value"]) for r in rows] == [("_Tobias4444", 50)]


def test_competition_pages(client, competition):
    html = client.get("/wettbewerbe", **on("testdomain")).get_data(as_text=True)
    assert "Läuft gerade" in html and "Mining-Woche" in html and "Noch 5 Tage" in html
    home = client.get("/", **on("testdomain")).get_data(as_text=True)
    assert "Zur Live-Rangliste" in home and "_Tobias4444" in home


def test_competition_announcements(db, server, competition, plugin, socket_server):
    client = plugin().auth(server["key"])
    socket_server.announce_competitions()
    start = client.recv()
    assert start.startswith("!broadcast~gold|★ Wettbewerb »Mining-Woche« hat begonnen: Blöcke abgebaut bis ")
    assert "/wettbewerbe" in start
    socket_server.announce_competitions()  # announced only once
    set_snapshot_day(db, competition_player(db, server), 1)  # the gain happened yesterday
    with db._cursor() as cur:
        cur.execute("UPDATE competitions SET ends_on = current_date - 1")
    socket_server.announce_competitions()
    assert client.recv() == "!broadcast~gold|★ Wettbewerb »Mining-Woche« ist vorbei! 1. _Tobias4444 (50)"


def competition_player(db, server):
    return db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])


def test_competition_announcement_waits_for_the_plugin(db, server, competition, socket_server):
    socket_server.announce_competitions()  # not connected
    assert db.get_competition(competition["id"])["start_announced"] is False


@pytest.fixture
def mod_client(client, db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.set_plugin_connected(server["id"], True)
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, player_id).json["status"] == "success"
    return client


def test_create_and_delete_competition(mod_client, db, server):
    today = db.get_today()
    body = {"title": "Angel-Cup", "metric": "fish_caught", "starts_on": today.isoformat(),
            "ends_on": (today + timedelta(days=6)).isoformat()}
    response = mod_client.post("/api/mod/competitions", json=body, **on("testdomain"))
    assert response.status_code == 200
    competition = db.get_competition(response.json["id"])
    assert competition["title"] == "Angel-Cup" and competition["created_by"] == "_Tobias4444"
    assert "Angel-Cup" in mod_client.get("/users", **on("testdomain")).get_data(as_text=True)
    assert mod_client.post("/api/mod/competitions/delete", json={"id": competition["id"]},
                           **on("testdomain")).status_code == 200
    assert db.list_competitions(server["id"]) == []


@pytest.mark.parametrize("change", [
    {"title": "x"}, {"title": "§cRot"}, {"metric": "nope"}, {"starts_on": "2000-01-01"},
    {"ends_on": "kaputt"}, {"ends_on": "SWAP"}, {"ends_on": "LONG"},
])
def test_create_competition_validation(mod_client, db, change):
    today = db.get_today()
    body = {"title": "Angel-Cup", "metric": "fish_caught", "starts_on": today.isoformat(),
            "ends_on": (today + timedelta(days=6)).isoformat()}
    body.update(change)
    if body["ends_on"] == "SWAP":
        body["ends_on"] = (today - timedelta(days=1)).isoformat()
    if body["ends_on"] == "LONG":
        body["ends_on"] = (today + timedelta(days=200)).isoformat()
    assert mod_client.post("/api/mod/competitions", json=body, **on("testdomain")).status_code == 400


def test_create_competition_needs_moderator(client, server, db):
    assert client.post("/api/mod/competitions", json={}, **on("testdomain")).status_code == 401


# ------------------------------------------------------------------ teams

def test_teams_page(client, db, server, two_players):
    a, b = two_players
    db.save_own_prefix(a, "Bauteam", "gold")
    prefix_id = db.get_owned_prefix(a)["prefix_id"]
    assert db.join_prefix(b, prefix_id) == "ok"
    html = client.get("/teams?kennzahl=blocks_mined", **on("testdomain")).get_data(as_text=True)
    assert "[Bauteam]" in html and "2 Mitglieder" in html
    assert "137" in html  # 107 + 30 blocks
    assert client.get("/teams?kennzahl=unknown&zeitraum=7", **on("testdomain")).status_code == 200


def test_teams_page_without_prefixes(client, server):
    assert "Noch keine Teams" in client.get("/teams", **on("testdomain")).get_data(as_text=True)


# ------------------------------------------------------------------ live updates

def test_partial_live_stats_keep_the_other_values(db, server):
    """The plugin's live stats only contain some values; the others must stay."""
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player_id, custom(deaths=12, fish_caught=5))
    db.update_player_stats(player_id, {"stats": {"minecraft:custom": {"minecraft:deaths": 13}}})
    values = db.get_player_metrics(player_id)
    assert values["deaths"] == 13 and values["fish_caught"] == 5


def test_achievements_api(client, db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player_id, custom(deaths=12))
    db.award_achievements(player_id)
    data = client.get("/api/spieler/_Tobias4444/erfolge", **on("testdomain")).json
    assert data["count"] == 1 and "Pechvogel" in data["html"]
    db.save_profile(player_id, None, True)
    assert client.get("/api/spieler/_Tobias4444/erfolge", **on("testdomain")).status_code == 404


def test_my_new_achievements(client, db, server):
    assert client.get("/api/meine-erfolge", **on("testdomain")).json["new"] == []  # logged out
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.set_plugin_connected(server["id"], True)
    assert login_player(client, db, player_id).json["status"] == "success"
    first = client.get("/api/meine-erfolge", **on("testdomain")).json
    assert first["new"] == []
    assert "mcc-toasts" in client.get("/", **on("testdomain")).get_data(as_text=True)
    db.update_player_stats(player_id, custom(deaths=12))
    db.award_achievements(player_id)
    later = client.get("/api/meine-erfolge?seit=" + first["now"].replace("+", "%2B"), **on("testdomain")).json
    assert later["new"] == [{"name": "Pechvogel", "tier": "Bronze", "tier_key": "bronze", "description": "Tode"}]
    again = client.get("/api/meine-erfolge?seit=" + later["now"].replace("+", "%2B"), **on("testdomain")).json
    assert again["new"] == []
