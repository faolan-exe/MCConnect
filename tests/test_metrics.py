"""Rankings and player comparison: metrics, snapshots, history and the web pages."""
from datetime import datetime, timedelta

import pytest

from database import metrics, stats
from tests.conftest import OTHER_UUID, PLAYER_UUID, wait_for
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, on  # noqa: F401 (fixtures)

STATS_A = {"stats": {
    "minecraft:mined": {"minecraft:stone": 100, "minecraft:diamond_ore": 3, "minecraft:deepslate_diamond_ore": 4},
    "minecraft:killed": {"minecraft:zombie": 7},
    "minecraft:custom": {"minecraft:play_time": 72000, "minecraft:deaths": 5, "minecraft:mob_kills": 7,
                         "minecraft:walk_one_cm": 150000, "minecraft:sprint_one_cm": 50000},
}}
STATS_B = {"stats": {
    "minecraft:mined": {"minecraft:stone": 30},
    "minecraft:custom": {"minecraft:play_one_minute": 36000, "minecraft:deaths": 1},
}}


def mined(stone):
    return {"stats": dict(STATS_A["stats"], **{"minecraft:mined": {"minecraft:stone": stone}})}


def set_snapshot_day(db, player_id, days_ago):
    """Move today's snapshot into the past. The automatic baseline of the first sync (yesterday, while it is the
    earliest snapshot) is removed first, so the tests control the history completely."""
    with db._cursor() as cur:
        cur.execute("""DELETE FROM stat_snapshots s WHERE s.player_id = %s AND s.day = current_date - 1
                         AND NOT EXISTS (SELECT 1 FROM stat_snapshots e WHERE e.player_id = s.player_id
                                         AND e.day < current_date - 1)""", (player_id,))
        cur.execute("UPDATE stat_snapshots SET day = current_date - %s WHERE player_id = %s AND day = current_date",
                    (days_ago, player_id))


@pytest.fixture
def two_players(db, server):
    a = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    b = db.register_player_join(server["id"], OTHER_UUID, "Notch")
    db.update_player_stats(a, STATS_A)
    db.update_player_stats(b, STATS_B)
    return a, b


def test_format():
    m = metrics.METRICS_BY_KEY
    assert metrics.format_value(m["play_time"], 72000) == "1 Std."
    assert metrics.format_value(m["distance"], 50000) == "500 m"
    assert metrics.format_value(m["distance"], 250000) == "2,5 km"
    assert metrics.format_value(m["distance"], 1234567) == "12,3 km"
    assert metrics.format_value(m["blocks_mined"], 1234) == "1.234"
    assert metrics.format_value(m["damage_dealt"], 200) == "10 ♥"
    assert metrics.object_label("minecraft:deepslate_diamond_ore") == "Deepslate Diamond Ore"


def test_server_metrics_totals(db, server, two_players):
    players = {p["name"]: p["values"] for p in db.get_server_metrics(server["id"])}
    assert players["_Tobias4444"]["blocks_mined"] == 107
    assert players["_Tobias4444"]["diamonds"] == 7
    assert players["_Tobias4444"]["distance"] == 200000
    assert players["Notch"]["play_time"] == 36000  # old stat name
    assert players["Notch"]["mob_kills"] == 0


def test_server_metrics_are_per_server(db, server, other_server, two_players):
    assert db.get_server_metrics(other_server["id"]) == []


def test_snapshot_written_and_updated(db, two_players):
    a, _ = two_players
    today = "SELECT metric, value FROM stat_snapshots WHERE player_id = %s AND day = current_date"
    rows = dict(db._fetchall(today, (a,)))
    assert rows["blocks_mined"] == 107
    assert len(rows) == len(metrics.METRICS)
    db.update_player_stats(a, mined(200))
    assert dict(db._fetchall(today, (a,)))["blocks_mined"] == 207
    # the first sync is also the baseline of the day before, so today's gains count
    baseline = dict(db._fetchall("SELECT metric, value FROM stat_snapshots WHERE player_id = %s AND day = current_date - 1",
                                 (a,)))
    assert baseline["blocks_mined"] == 107
    assert db._fetchvalue("SELECT count(*) FROM stat_snapshots WHERE player_id = %s", (a,)) == 2 * len(metrics.METRICS)


def test_first_sync_counts_from_then_on(db, server, two_players):
    """A player first seen today: gains of today, of a competition starting today and in the sidebar."""
    a, _ = two_players
    db.update_player_stats(a, mined(130))
    today = db.get_today()
    assert db.get_metrics_between(server["id"], today, today)[str(a)]["blocks_mined"] == 30
    assert db.get_player_gain(a, "blocks_mined", today) == 30
    competition = db.get_competition(db.create_competition(server["id"], "Heute", "blocks_mined", today, today))
    assert [(r["name"], r["value"]) for r in db.get_competition_standings(competition)] == [("_Tobias4444", 30)]


def test_gain_over_period(db, server, two_players):
    a, b = two_players
    set_snapshot_day(db, a, 10)  # 10 days ago: 107 blocks
    db.update_player_stats(a, mined(143))  # today: 150
    week = {p["name"]: p["values"] for p in db.get_server_metrics(server["id"], days=7)}
    assert week["_Tobias4444"]["blocks_mined"] == 143 - 100
    # Notch only has a snapshot from today: gain since the first snapshot
    assert week["Notch"]["blocks_mined"] == 0


def test_gain_uses_newest_snapshot_before_range(db, server, two_players):
    a, _ = two_players
    set_snapshot_day(db, a, 20)  # 107
    db.update_player_stats(a, mined(120))
    set_snapshot_day(db, a, 9)  # 120
    db.update_player_stats(a, mined(125))  # today
    values = {p["name"]: p["values"] for p in db.get_server_metrics(server["id"], days=7)}
    assert values["_Tobias4444"]["blocks_mined"] == 5
    values = {p["name"]: p["values"] for p in db.get_server_metrics(server["id"], days=30)}
    assert values["_Tobias4444"]["blocks_mined"] == 132 - 107  # since the first snapshot


def test_old_snapshots_are_pruned_but_newest_kept(db, two_players):
    """Old snapshots go, except the newest old one (baseline) and the first and last of every year."""
    a, _ = two_players
    year = db.get_today().year - 2
    with db._cursor() as cur:
        for day in (f"{year}-01-05", f"{year}-02-10", f"{year}-03-15", f"{year + 1}-04-01", f"{year + 1}-05-01",
                    f"{year + 1}-06-01"):
            cur.execute("INSERT INTO stat_snapshots (player_id, metric, day, value) VALUES (%s, 'deaths', %s, 1)", (a, day))
    db.update_player_stats(a, STATS_A)
    days = [str(row[0]) for row in db._fetchall(
        "SELECT day FROM stat_snapshots WHERE player_id = %s AND metric = 'deaths' AND day < current_date ORDER BY day", (a,))]
    yesterday = str(db.get_today() - timedelta(days=1))  # baseline of the first sync
    assert days == [f"{year}-01-05", f"{year}-03-15", f"{year + 1}-04-01", f"{year + 1}-06-01", yesterday]


def test_metric_history(db, two_players):
    a, b = two_players
    set_snapshot_day(db, a, 5)
    db.update_player_stats(a, mined(193))
    dates, history = db.get_metric_history([str(a), str(b)], 7)
    assert len(dates) == 8
    assert history[str(a)]["blocks_mined"] == [None, None, 0, 0, 0, 0, 0, 193 - 100]
    assert history[str(b)]["blocks_mined"] == [None] * 6 + [0, 0]  # baseline yesterday


def test_top_objects(db, two_players):
    a, b = two_players
    top = db.get_top_objects([str(a), str(b)], stats.BLOCK_MINED, limit=2)
    assert top[0] == ("minecraft:stone", {str(a): 100, str(b): 30})
    assert top[1][0] == "minecraft:deepslate_diamond_ore"


# ------------------------------------------------------------------ pages

def test_rankings_page(client, two_players):
    response = client.get("/rangliste", **on("testdomain"))
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Blöcke abgebaut" in html
    assert html.index("_Tobias4444") < html.index("Notch")
    assert 'data-compare="Notch"' in html


def test_rankings_period(client, two_players):
    response = client.get("/rangliste?zeitraum=7", **on("testdomain"))
    assert response.status_code == 200
    assert "aufgezeichnet" in response.get_data(as_text=True)
    assert client.get("/rangliste?zeitraum=kaputt", **on("testdomain")).status_code == 200


def test_compare_page(client, two_players):
    response = client.get("/vergleich?spieler=_tobias4444,Notch,unknown", **on("testdomain"))
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Bestwert" in html
    assert 'id="history-data"' in html
    assert "Meist abgebaute Blöcke" in html


def test_compare_page_without_selection(client, two_players):
    response = client.get("/vergleich", **on("testdomain"))
    assert response.status_code == 200
    assert "Noch niemand zum Vergleichen" in response.get_data(as_text=True)


def test_compare_best_marks(client, two_players):
    # deaths: fewer is better -> Notch
    from web.main import best_indexes
    assert best_indexes([5, 1], lower_is_better=True) == {1}
    assert best_indexes([3, 3], lower_is_better=False) == set()
    assert best_indexes([4], lower_is_better=False) == set()


def test_records_on_start_page(client, two_players):
    html = client.get("/", **on("testdomain")).get_data(as_text=True)
    assert "Rekorde" in html
    assert "_Tobias4444" in html


def test_rankings_can_be_disabled(db, server):
    from web.main import create_app
    app = create_app(db, {"TESTING": True, "SECRET_KEY": "test", "SERVER_NAME": "mc.test",
                          "FEATURE_RANKINGS": False})
    client = app.test_client()
    assert client.get("/rangliste", base_url="http://testdomain.mc.test").status_code == 404
    assert "Rekorde" not in client.get("/", base_url="http://testdomain.mc.test").get_data(as_text=True)


def test_player_list_page(client, db, server, two_players):
    a, b = two_players
    db.register_player_quit(server["id"], OTHER_UUID)
    db.set_moderator(server["id"], "_Tobias4444", True)
    db.set_player_op(server["id"], OTHER_UUID, True)
    db.ban_player(server["id"], "Notch", "tobi", days=1)
    html = client.get("/spieler", **on("testdomain")).get_data(as_text=True)
    assert html.index("Notch") < html.index("_Tobias4444")  # sorted by name
    assert "Moderator" in html and ">OP<" in html and "Gebannt" in html
    assert 'data-online="1"' in html and 'data-last="20' in html
    assert "Dabei seit" in html and "Spielzeit 1 Std." in html
    assert "<b>#1</b> Blöcke abgebaut" in html
    assert "</b> Tode" not in html  # no "achievement" for dying


def test_player_page_extras(client, db, two_players):
    a, _ = two_players
    set_snapshot_day(db, a, 1)
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{
        "minecraft:custom": dict(STATS_A["stats"]["minecraft:custom"], **{"minecraft:play_time": 72000 + 36000})})})
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Aktivität der letzten 30 Tage" in html
    assert "Ranglisten-Plätze" in html and "Platz 1" in html
    assert "Lieblingsblock" in html and "Stone" in html
    assert "Liebste Fortbewegung" in html and "Zu Fuß" in html
    assert '"text": "30 Min."' in html  # yesterday -> today


def test_player_page_without_snapshots_has_no_activity(client, db, two_players):
    with db._cursor() as cur:
        cur.execute("DELETE FROM stat_snapshots WHERE player_id = %s", (two_players[1],))
    html = client.get("/spieler?player=Notch", **on("testdomain")).get_data(as_text=True)
    assert "Aktivität der letzten 30 Tage" not in html
    assert "Highlights" in html


def new_player_stats(minutes, stone):
    return {"stats": {"minecraft:custom": {"minecraft:play_time": minutes * 60 * 20},
                      "minecraft:mined": {"minecraft:stone": stone}}}


def test_new_player_counts_from_the_first_join(db, server):
    """A new player's first stats often arrive late (world save, quit): everything since the first join counts."""
    player = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    with db._cursor() as cur:  # first join 30 minutes ago; the plugin was disconnected for a while: no session
        cur.execute("UPDATE player_server_info SET first_seen = now() - interval '30 minutes'")
        cur.execute("DELETE FROM player_sessions")
    db.update_player_stats(player, new_player_stats(29, 400))
    today = db.get_today()
    assert db.get_metrics_between(server["id"], today, today)[str(player)]["blocks_mined"] == 400
    goal_id = db.create_goal(server["id"], "Seit gestern", "blocks_mined", 1000, today - timedelta(days=1))
    goal = next(g for g in db.list_goals(server["id"]) if g["id"] == goal_id)
    assert db.get_goal_progress(goal)[1] == {str(player): 400}


def test_known_player_seen_for_the_first_time_counts_from_then_on(db, server):
    """More play time than time online since MCConnect: the stats are older, they do not count as gain."""
    player = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player, new_player_stats(600, 5000))
    today = db.get_today()
    assert db.get_metrics_between(server["id"], today, today)[str(player)]["blocks_mined"] == 0
    db.update_player_stats(player, new_player_stats(601, 5010))
    assert db.get_metrics_between(server["id"], today, today)[str(player)]["blocks_mined"] == 10


@pytest.mark.parametrize("migration", [21, 23])
def test_migration_gives_new_players_their_first_session_back(db, server, migration):
    """Migrations 21 (sessions) and 23 (first join): baselines of new players stored with the old rule become 0."""
    from database.databaseManagerV2 import MIGRATIONS
    new = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    old = db.register_player_join(server["id"], OTHER_UUID, "Notch")
    with db._cursor() as cur:
        cur.execute("UPDATE player_sessions SET started_at = now() - interval '30 minutes'")
        cur.execute("UPDATE player_server_info SET first_seen = now() - interval '30 minutes'")
    db.update_player_stats(new, new_player_stats(29, 400))
    db.update_player_stats(old, new_player_stats(600, 5000))
    with db._cursor() as cur:  # the old rule: the first stats were the baseline
        cur.execute("UPDATE stat_snapshots s SET value = t.value FROM stat_snapshots t WHERE s.player_id = t.player_id "
                    "AND s.metric = t.metric AND s.day = current_date - 1 AND t.day = current_date")
        if migration == 23:
            cur.execute("DELETE FROM player_sessions")  # the plugin was disconnected: no sessions
        for statement in MIGRATIONS[migration]:
            cur.execute(statement)
    today = db.get_today()
    gains = db.get_metrics_between(server["id"], today, today)
    assert gains[str(new)]["blocks_mined"] == 400 and gains[str(old)]["blocks_mined"] == 0


def epoch_ms(delta):
    from datetime import datetime, timezone
    return int((datetime.now(timezone.utc) + delta).timestamp() * 1000)


def test_first_join_in_the_game_decides_new_or_old(db, server, plugin):
    """Plugin 3.15 sends the game's first join: after MCConnect's start on the server = new (all stats count)."""
    from tests.conftest import OTHER_UUID
    connection = plugin().auth(server["key"])  # the recording starts now
    # joined the game an hour ago while the plugin was disconnected: 50 minutes of play, seen only now
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444|0|{epoch_ms(timedelta(hours=-1))}") == "success|101"
    with db._cursor() as cur:  # the recording started two hours ago
        cur.execute("UPDATE servers SET tracking_since = now() - interval '2 hours'")
    db.update_player_stats(db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"]), new_player_stats(50, 300))
    # an old player: first join long before MCConnect
    assert connection.request(f"!JOIN~{OTHER_UUID}|Notch|0|{epoch_ms(timedelta(days=-400))}") == "success|101"
    db.update_player_stats(db.get_player_id_from_mojang_uuid_and_server_id(OTHER_UUID, server["id"]), new_player_stats(3, 50))
    today = db.get_today()
    gains = db.get_metrics_between(server["id"], today, today)
    new, old = (str(db.get_player_id_from_mojang_uuid_and_server_id(u, server["id"])) for u in (PLAYER_UUID, OTHER_UUID))
    assert gains[new]["blocks_mined"] == 300 and gains[new]["play_time"] == 50 * 60 * 20
    assert gains[old]["blocks_mined"] == 0  # 3 minutes since the first join here, but an old player


def test_wrong_baseline_is_corrected_by_the_first_join_info(db, server, plugin):
    """A new player counted as old before plugin 3.15 (stats first, first join unknown) is corrected on the next join."""
    player = db.ensure_player_on_server(server["id"], PLAYER_UUID, "_Tobias4444")  # stats before any join
    db.update_player_stats(player, new_player_stats(90, 700))
    today = db.get_today()
    assert db.get_metrics_between(server["id"], today, today)[str(player)]["blocks_mined"] == 0
    with db._cursor() as cur:
        cur.execute("UPDATE servers SET tracking_since = now() - interval '1 day'")
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444|0|{epoch_ms(timedelta(hours=-3))}") == "success|101"
    assert db.get_metrics_between(server["id"], today, today)[str(player)]["blocks_mined"] == 700
    # only once: a later join does not touch the history again
    db.update_player_stats(player, new_player_stats(95, 720))
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444|0|{epoch_ms(timedelta(hours=-3))}") == "success|101"
    assert db.get_metrics_between(server["id"], today, today)[str(player)]["blocks_mined"] == 720


def test_old_player_is_not_corrected(db, server, plugin):
    player = db.ensure_player_on_server(server["id"], PLAYER_UUID, "_Tobias4444")
    db.update_player_stats(player, new_player_stats(900, 9000))
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444|0|{epoch_ms(timedelta(days=-30))}") == "success|101"
    today = db.get_today()
    assert db.get_metrics_between(server["id"], today, today)[str(player)]["blocks_mined"] == 0


def test_old_player_new_to_mcconnect_is_not_new(db, server, plugin):
    """An old player seen by MCConnect for the first time: anniversary from the game's first join, not "new"."""
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444|0|{epoch_ms(timedelta(days=-800))}") == "success|101"
    player = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    wait_for(lambda: [(m["kind"], m["value"]) for m in db.get_player_milestones(player)] == [("anniversary", 2)])
    assert "new" not in [e["kind"] for e in db.get_feed(server["id"])]  # not "ist neu auf dem Server"
    today = db.get_today()
    assert db.get_new_players_between(server["id"], today, today) == 0
    assert db.get_veterans(server["id"])[0]["first_seen"].year == (datetime.now() - timedelta(days=800)).year
