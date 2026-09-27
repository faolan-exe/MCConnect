"""Rankings and player comparison: metrics, snapshots, history and the web pages."""
import pytest

from database import metrics, stats
from tests.conftest import OTHER_UUID, PLAYER_UUID
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
    with db._cursor() as cur:
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
    rows = dict(db._fetchall("SELECT metric, value FROM stat_snapshots WHERE player_id = %s", (a,)))
    assert rows["blocks_mined"] == 107
    assert len(rows) == len(metrics.METRICS)
    db.update_player_stats(a, mined(200))
    assert db._fetchvalue("SELECT value FROM stat_snapshots WHERE player_id = %s AND metric = 'blocks_mined'",
                          (a,)) == 207
    assert db._fetchvalue("SELECT count(*) FROM stat_snapshots WHERE player_id = %s", (a,)) == len(metrics.METRICS)


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
    a, _ = two_players
    set_snapshot_day(db, a, 200)
    db.update_player_stats(a, STATS_A)
    set_snapshot_day(db, a, 150)
    db.update_player_stats(a, STATS_A)
    days = [row[0] for row in db._fetchall(
        "SELECT current_date - day FROM stat_snapshots WHERE player_id = %s AND metric = 'deaths' ORDER BY day", (a,))]
    assert days == [150, 0]


def test_metric_history(db, two_players):
    a, b = two_players
    set_snapshot_day(db, a, 5)
    db.update_player_stats(a, mined(193))
    dates, history = db.get_metric_history([str(a), str(b)], 7)
    assert len(dates) == 8
    assert history[str(a)]["blocks_mined"] == [None, None, 0, 0, 0, 0, 0, 193 - 100]
    assert history[str(b)]["blocks_mined"] == [None] * 7 + [0]


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
