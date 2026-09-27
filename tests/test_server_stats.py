"""Sessions, online history, peak times, weekly recap and the server statistics pages."""
from datetime import timedelta

import pytest

from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_metrics import STATS_A, STATS_B, set_snapshot_day, two_players  # noqa: F401 (fixture)
from tests.test_web import app, client, on  # noqa: F401 (fixtures)


def sessions(db, player_id):
    return db._fetchall("SELECT started_at, ended_at FROM player_sessions WHERE player_id = %s ORDER BY id",
                        (player_id,))


def shift_sessions(db, player_id, **interval):
    """Move all sessions of a player into the past."""
    delta = timedelta(**interval)
    with db._cursor() as cur:
        cur.execute("""UPDATE player_sessions SET started_at = started_at - %s,
                       ended_at = ended_at - %s WHERE player_id = %s""", (delta, delta, player_id))


def test_join_and_quit_create_a_session(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.register_player_join(server["id"], PLAYER_UUID)  # a second JOIN does not open another session
    assert len(sessions(db, player_id)) == 1 and sessions(db, player_id)[0][1] is None
    db.register_player_quit(server["id"], PLAYER_UUID)
    assert sessions(db, player_id)[0][1] is not None


def test_quick_rejoin_continues_the_session(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.set_all_players_offline(server["id"])  # plugin reconnect
    db.register_player_join(server["id"], PLAYER_UUID)
    assert len(sessions(db, player_id)) == 1
    assert sessions(db, player_id)[0][1] is None


def test_later_rejoin_starts_a_new_session(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.register_player_quit(server["id"], PLAYER_UUID)
    shift_sessions(db, player_id, minutes=10)
    db.register_player_join(server["id"], PLAYER_UUID)
    assert len(sessions(db, player_id)) == 2


def test_online_peak_and_history(db, server):
    a = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    b = db.register_player_join(server["id"], OTHER_UUID, "Notch")
    db.register_player_quit(server["id"], OTHER_UUID)
    with db._cursor() as cur:  # Notch played for the last 3 hours, overlapping with _Tobias4444
        cur.execute("UPDATE player_sessions SET started_at = started_at - interval '3 hours' WHERE player_id = %s", (b,))
    peak, at = db.get_online_peak(server["id"])
    assert peak == 2 and at is not None
    history = db.get_online_history(server["id"], 24, 60)
    assert max(count for _, count in history) == 2
    assert history[-3][1] == 1  # two hours ago only Notch was online
    assert history[-6][1] == 0  # five hours ago nobody


def test_peak_times_matrix(db, server):
    a = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.register_player_quit(server["id"], PLAYER_UUID)
    shift_sessions(db, a, hours=2)
    matrix = db.get_peak_times(server["id"])
    assert len(matrix) == 7 and all(len(row) == 24 for row in matrix)
    assert max(max(row) for row in matrix) > 0


def test_peak_times_without_sessions(db, server):
    assert max(max(row) for row in db.get_peak_times(server["id"])) == 0
    assert db.get_online_peak(server["id"]) == (0, None)


def test_metrics_between_and_daily_gain(db, server, two_players):
    a, _ = two_players
    set_snapshot_day(db, a, 10)  # 107 blocks
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": 150}})})
    set_snapshot_day(db, a, 3)  # 150
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": 170}})})
    today = db.get_today()
    gains = db.get_metrics_between(server["id"], today - timedelta(days=5), today - timedelta(days=2))
    assert gains[str(a)]["blocks_mined"] == 150 - 100  # (stone + 7 diamond ores) - 107
    daily = dict(db.get_server_daily_gain(server["id"], "blocks_mined", 30))
    assert daily[today - timedelta(days=3)] == 150 - 100
    assert daily[today] == 170 - 150
    assert daily[today - timedelta(days=5)] == 0


def test_new_players_per_month(db, server, two_players):
    months = db.get_new_players_per_month(server["id"])
    assert len(months) == 12 and months[-1][1] == 2


def test_server_stats_page(client, two_players):
    response = client.get("/server-statistik", **on("testdomain"))
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Stoßzeiten" in html and "Rekord gleichzeitig" in html
    assert "Alle zusammen · Allgemein" in html
    assert 'id="online-data"' in html


def test_weekly_recap_on_start_page(client, db, server, two_players):
    a, _ = two_players
    set_snapshot_day(db, a, 30)
    html = client.get("/", **on("testdomain")).get_data(as_text=True)
    assert "Rückblick KW" not in html  # no gain last week
    today = db.get_today()
    last_sunday = today - timedelta(days=today.weekday() + 1)
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": 400}})})
    set_snapshot_day(db, a, (today - last_sunday).days)
    html = client.get("/", **on("testdomain")).get_data(as_text=True)
    assert "Rückblick KW" in html and "Baumeister" in html and "300 Blöcke abgebaut" in html


def test_recap_needs_data_from_before_the_week(client, db, two_players):
    assert "Rückblick KW" not in client.get("/", **on("testdomain")).get_data(as_text=True)


def test_moderation_page_lists_player_activity(db, server):
    from tests.test_web import login_player
    from web.main import create_app
    app = create_app(db, {"TESTING": True, "SECRET_KEY": "test", "SERVER_NAME": "mc.test"})
    client = app.test_client()
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.ensure_player_on_server(server["id"], OTHER_UUID, "Notch")
    db.set_plugin_connected(server["id"], True)
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, player_id).json["status"] == "success"
    html = client.get("/users", **on("testdomain")).get_data(as_text=True)
    assert "Aktivität der Spieler" in html
    # never seen players come first, online players last
    assert html.index('data-name="Notch"') < html.index('data-name="_Tobias4444"')
