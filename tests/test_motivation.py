"""Phase 5: streaks, anniversaries, records, community goals, trophies, fun facts and the hall of fame."""
import json
from datetime import timedelta

import pytest

from database import metrics, motivation
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_achievements import custom
from tests.test_metrics import STATS_A, STATS_B, set_snapshot_day, two_players  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

M = metrics.METRICS_BY_KEY


def add_session(db, player_id, days_ago, hours=1):
    with db._cursor() as cur:
        cur.execute("""INSERT INTO player_sessions (player_id, started_at, ended_at)
                       VALUES (%s, current_date - %s + interval '12 hours',
                               current_date - %s + interval '12 hours' + %s * interval '1 hour')""",
                    (player_id, days_ago, days_ago, hours))


def played_days(db, player_id, days):
    for days_ago in days:
        add_session(db, player_id, days_ago)


def make_old_session(db, player_id, hours_ago=5):
    """Move the player's sessions into the past (the player is not playing right now)."""
    with db._cursor() as cur:
        cur.execute("""UPDATE player_sessions SET started_at = now() - %s * interval '1 hour' - interval '1 hour',
                       ended_at = now() - %s * interval '1 hour' WHERE player_id = %s""", (hours_ago, hours_ago, player_id))
        cur.execute("UPDATE player_server_info SET online = false WHERE player_id = %s", (player_id,))


# ------------------------------------------------------------------ streaks and milestones

def test_streaks(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID, "_Tobias4444")
    played_days(db, player_id, [20, 19, 18, 10, 2, 1])
    streak = db.get_player_streak(player_id)
    assert (streak["best"], streak["current"]) == (3, 2)
    assert streak["best_last"] == db.get_today() - timedelta(days=18)
    assert db.get_streaks(server["id"])[str(player_id)]["current"] == 2
    played_days(db, player_id, [4])  # a gap on day 3: the current streak stays at 2
    assert db.get_player_streak(player_id)["current"] == 2


def test_session_over_midnight_counts_for_both_days(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID, "_Tobias4444")
    with db._cursor() as cur:
        cur.execute("""INSERT INTO player_sessions (player_id, started_at, ended_at)
                       VALUES (%s, current_date - 1 - interval '1 hour', current_date - 1 + interval '1 hour')""",
                    (player_id,))
    assert db.get_player_streak(player_id)["best"] == 2


def test_streak_badge_is_announced_while_playing(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")  # online today
    played_days(db, player_id, range(1, 7))
    assert db.check_milestones(player_id) == [("streak", 7)]
    assert db.check_milestones(player_id) == []  # only once
    assert [m["value"] for m in db.get_player_milestones(player_id)] == [7]


def test_old_streak_badge_is_silent(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID, "_Tobias4444")
    played_days(db, player_id, range(40, 48))  # 8 days in a row, long ago
    assert db.check_milestones(player_id) == []
    assert db._fetchvalue("SELECT silent FROM player_milestones WHERE player_id = %s", (player_id,)) is True


def test_anniversary(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET first_seen = now() - interval '2 years 3 days' WHERE player_id = %s",
                    (player_id,))
    assert db.check_milestones(player_id) == [("anniversary", 2)]
    assert db.get_server_milestones(server["id"])[str(player_id)] == {"anniversary": 2}


def test_milestones_announced_on_join(db, server, plugin):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID, "_Tobias4444")
    played_days(db, player_id, range(1, 7))
    client = plugin().auth(server["key"])
    assert client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    assert client.recv() == "!broadcast~gold|★ _Tobias4444 war 7 Tage in Folge online – Serie!"


def test_milestone_texts():
    assert motivation.milestone_label("streak", 30) == "30-Tage-Serie"
    assert motivation.milestone_label("anniversary", 1) == "1 Jahr dabei"
    assert motivation.milestone_announcement("A", "anniversary", 1)[1] == \
        "★ A ist seit 1 Jahr auf dem Server – danke fürs Mitspielen!"


# ------------------------------------------------------------------ records

def test_first_records_are_seeded_silently(db, server, two_players):
    a, b = two_players
    assert db.update_records(b) == []
    records = db.get_current_records(server["id"])
    assert records["blocks_mined"]["name"] == "_Tobias4444" and records["blocks_mined"]["value"] == 107
    assert records["blocks_mined"]["first"] is True
    assert "deaths" not in records  # less is better: no record
    assert db.get_record_history(server["id"]) == []


def test_taking_a_record_while_playing(db, server, two_players):
    a, b = two_players
    db.update_records(a)
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 500}})})
    events = db.update_records(b)
    assert [(m.key, previous, value) for m, previous, value in events] == [("blocks_mined", "_Tobias4444", 500)]
    history = db.get_record_history(server["id"])
    assert [(h["metric"], h["name"], h["previous"]) for h in history] == [("blocks_mined", "Notch", "_Tobias4444")]
    assert db.get_current_records(server["id"])["blocks_mined"]["first"] is False
    # the holder improves the own record: no new entry
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 600}})})
    assert db.update_records(b) == []
    assert db.get_current_records(server["id"])["blocks_mined"]["value"] == 600


def test_record_taken_straight_back_is_undone(db, server, two_players):
    a, b = two_players
    db.update_records(a)
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 500}})})
    db.update_records(b)
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": 800}})})
    assert db.update_records(a) == []  # within the cooldown: not announced, the short change is removed
    assert db.get_record_history(server["id"]) == []
    assert db.get_current_records(server["id"])["blocks_mined"]["value"] == 807


def test_records_of_offline_players_are_silent(db, server, two_players):
    a, b = two_players
    db.update_records(a)
    make_old_session(db, b)
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 500}})})
    assert db.update_records(b) == []
    assert db.get_record_history(server["id"])[0]["name"] == "Notch"  # still in the chronicle


def test_hidden_players_take_no_records(db, server, two_players):
    a, b = two_players
    db.update_records(a)
    db.save_profile(b, None, True)
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 500}})})
    assert db.update_records(b) == []
    assert db.get_current_records(server["id"])["blocks_mined"]["name"] == "_Tobias4444"


def test_record_is_announced_in_game(db, server, plugin):
    client = plugin().auth(server["key"])
    assert client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    assert client.request(f"!JOIN~{OTHER_UUID}|Notch") == "success|101"
    assert client.request(f"!STATS~{PLAYER_UUID}|{json.dumps(custom(fish_caught=3))}") == "success|102"
    assert client.request(f"!STATS~{OTHER_UUID}|{json.dumps(custom(fish_caught=5))}") == "success|102"
    assert client.recv() == "!broadcast~gold|★ Neuer Rekord! Notch hat _Tobias4444 bei »Fische gefangen« überholt: 5"


# ------------------------------------------------------------------ community goals

@pytest.fixture
def goal(db, server, two_players):
    a, b = two_players
    set_snapshot_day(db, a, 3)
    set_snapshot_day(db, b, 3)
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": 150}})})
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 60}})})
    goal_id = db.create_goal(server["id"], "Hundert Blöcke", "blocks_mined", 100, db.get_today() - timedelta(days=1))
    return next(g for g in db.list_goals(server["id"]) if g["id"] == goal_id)


def test_goal_progress(db, goal):
    total, per_player = db.get_goal_progress(goal)
    assert total == 80  # 50 + 30
    assert sorted(per_player.values()) == [30, 50]
    assert db.take_reached_goals([goal["server_id"]]) == []


def test_goal_reached_is_announced_once(db, server, goal, two_players, plugin, socket_server):
    a, _ = two_players
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": 200}})})
    client = plugin().auth(server["key"])
    socket_server.periodic_checks()
    assert client.recv() == "!broadcast~gold|★ Gemeinschaftsziel »Hundert Blöcke« geschafft! Alle zusammen: 100 Blöcke abgebaut"
    assert db.take_reached_goals([server["id"]]) == []
    kinds = [e["kind"] for e in db.get_feed(server["id"])]
    assert "goal" in kinds


def test_goal_units():
    assert motivation.goal_target(M["play_time"], 2) == 2 * 72000
    assert motivation.goal_target(M["distance"], 1.5) == 150_000
    assert motivation.goal_unit(M["blocks_mined"]) == ""


@pytest.fixture
def mod_client(client, db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.set_plugin_connected(server["id"], True)
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, player_id).json["status"] == "success"
    return client


def test_create_and_delete_goal(mod_client, db, server):
    today = db.get_today().isoformat()
    body = {"title": "Tausend Stunden", "metric": "play_time", "amount": "1000", "starts_on": today, "ends_on": ""}
    response = mod_client.post("/api/mod/goals", json=body, **on("testdomain"))
    assert response.status_code == 200
    goal = db.list_goals(server["id"])[0]
    assert goal["target"] == 1000 * 72000 and goal["ends_on"] is None
    assert db.get_mod_log(server["id"])[0]["action"] == "goal_create"
    html = mod_client.get("/", **on("testdomain")).get_data(as_text=True)
    assert "Gemeinschaftsziele" in html and "Tausend Stunden" in html
    assert "Tausend Stunden" in mod_client.get("/wettbewerbe", **on("testdomain")).get_data(as_text=True)
    assert "Gemeinschaftsziele" in mod_client.get("/users/inhalte", **on("testdomain")).get_data(as_text=True)
    assert mod_client.post("/api/mod/goals/delete", json={"id": response.json["id"]}, **on("testdomain")).status_code == 200
    assert db.list_goals(server["id"]) == []


@pytest.mark.parametrize("change", [{"amount": "0"}, {"amount": "abc"}, {"metric": "nope"}, {"title": "x"},
                                    {"starts_on": "2000-01-01"}])
def test_invalid_goals(mod_client, db, change):
    body = dict({"title": "Ziel", "metric": "jumps", "amount": "10", "starts_on": db.get_today().isoformat()}, **change)
    assert mod_client.post("/api/mod/goals", json=body, **on("testdomain")).status_code == 400


def test_goals_need_a_moderator(client, db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.set_plugin_connected(server["id"], True)
    login_player(client, db, player_id)
    assert client.post("/api/mod/goals", json={}, **on("testdomain")).status_code == 403


# ------------------------------------------------------------------ trophies

def test_competition_trophies(db, server, two_players):
    a, b = two_players
    set_snapshot_day(db, a, 5)
    set_snapshot_day(db, b, 5)
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": 150}})})
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 40}})})
    set_snapshot_day(db, a, 2)
    set_snapshot_day(db, b, 2)
    today = db.get_today()
    db.create_competition(server["id"], "Mining-Woche", "blocks_mined", today - timedelta(days=4), today - timedelta(days=1))
    assert db.award_finished_competitions([server["id"]]) == 2
    assert db.award_finished_competitions([server["id"]]) == 0  # once
    trophies = db.get_player_trophies(a)
    assert [(t["kind"], t["place"], t["title"], t["detail"]) for t in trophies] == [("competition", 1, "Mining-Woche", "50")]
    assert db.get_player_trophies(b)[0]["place"] == 2


def test_player_of_the_week(db, server, two_players):
    a, b = two_players
    today = db.get_today()
    last_sunday = today - timedelta(days=today.weekday() + 1)
    for player_id in (a, b):
        set_snapshot_day(db, player_id, (today - last_sunday).days + 8)  # before the week
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:custom": {"minecraft:play_time": 72000 * 5}})})
    for player_id in (a, b):
        set_snapshot_day(db, player_id, (today - last_sunday).days)  # on Sunday
    trophy = db.settle_player_of_week(server["id"])
    assert trophy["name"] == "_Tobias4444" and trophy["detail"] == "4 Std. Spielzeit"
    assert db.settle_player_of_week(server["id"]) is None  # once per week
    assert db.get_server_trophies(server["id"], "player_of_week")[0]["title"] == trophy["title"]
    assert any(e["kind"] == "player_of_week" for e in db.get_feed(server["id"]))


def test_player_of_the_week_needs_data(db, server, two_players):
    assert db.settle_player_of_week(server["id"]) is None  # snapshots only from today


# ------------------------------------------------------------------ pages

def test_hall_of_fame_page(client, db, server, two_players):
    a, b = two_players
    db.update_records(a)
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 500}})})
    db.update_records(b)
    played_days(db, a, [1, 2, 3])
    html = client.get("/ruhmeshalle", **on("testdomain")).get_data(as_text=True)
    assert "Rekordhalter" in html and "Rekordchronik" in html
    assert "holt »Blöcke abgebaut« von _Tobias4444" in html
    assert "Längste Serien" in html and "4 Tage" in html  # today (join) + 3 days before
    assert "Dienstälteste" in html


def test_hall_of_fame_hides_hidden_players(client, db, server, two_players):
    a, b = two_players
    db.update_records(a)
    db.save_profile(a, None, True)
    html = client.get("/ruhmeshalle", **on("testdomain")).get_data(as_text=True)
    assert "_Tobias4444" not in html


def test_trophy_cabinet_and_badges(client, db, server, two_players):
    a, _ = two_players
    played_days(db, a, range(1, 7))
    db.check_milestones(a)
    db.update_records(a)
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Trophäenschrank" in html and "7-Tage-Serie" in html and "Blöcke abgebaut" in html
    assert "Nächstes Abzeichen bei 30 Tagen" in html
    assert "7-Tage-Serie" in client.get("/spieler", **on("testdomain")).get_data(as_text=True)


def test_feed_shows_records_and_milestones(client, db, server, two_players):
    a, b = two_players
    played_days(db, a, range(1, 7))
    db.check_milestones(a)
    db.update_records(a)
    db.update_player_stats(b, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 500}})})
    db.update_records(b)
    texts = [e["text"] for e in client.get("/api/feed", **on("testdomain")).json["events"]]
    assert "war 7 Tage in Folge online" in texts
    assert "hat den Rekord »Blöcke abgebaut« geholt" in texts


def test_fun_facts():
    facts = motivation.fun_facts({"distance": 50_000_000, "diamonds": 50, "jumps": 10_000, "deaths": 9, "play_time": 0},
                                 3, top_killer=("minecraft:creeper", 4))
    by_label = {f["label"]: f for f in facts}
    assert by_label["zurückgelegt"]["text"] == "Das sind 12 Marathons."
    assert by_label["Diamanterz abgebaut"]["text"] == "Genug für 2 komplette Diamantrüstungen."
    assert by_label["Sprünge"]["text"] == "Übereinander 12,5 km hoch – 1,4-mal der Mount Everest."
    assert "Creeper" in by_label["Tode"]["text"] and "3,0 pro Spieler" in by_label["Tode"]["text"]
    assert "gespielt" not in by_label


def test_fun_facts_on_server_stats(client, db, server, two_players):
    html = client.get("/server-statistik", **on("testdomain")).get_data(as_text=True)
    assert "Schon gewusst?" in html and "Am häufigsten: Stone" in html


def test_streak_counts_days_without_session_when_the_play_time_grew(db, server):
    """Online while the plugin was disconnected: no session, but the play time of that day grew."""
    player = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    with db._cursor() as cur:
        cur.execute("UPDATE player_sessions SET started_at = current_date - 2 + time '10:00', ended_at = current_date - 2 + time '11:00'")
        for days_ago, play in ((3, 1000), (2, 2000), (1, 3000), (0, 4000)):  # yesterday only in the stats
            cur.execute("INSERT INTO stat_snapshots (player_id, metric, day, value) VALUES (%s, 'play_time', current_date - %s, %s)",
                        (player, days_ago, play))
        cur.execute("INSERT INTO player_sessions (player_id, started_at) VALUES (%s, now())", (player,))
    streak = db.get_player_streak(player)
    assert (streak["best"], streak["current"]) == (3, 3)  # the day before yesterday, yesterday (stats), today
    assert db.get_streaks(server["id"])[str(player)]["best"] == 3
