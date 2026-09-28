"""Phase 9: year in review, server directory and dark mode."""
import pytest

from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_metrics import STATS_A, set_snapshot_day, two_players  # noqa: F401 (fixture)
from tests.test_web import admin_client, app, client, login_player, on  # noqa: F401 (fixtures)


def snapshot_on(db, player_id, day):
    """Move today's snapshots of a player to a given day."""
    with db._cursor() as cur:
        cur.execute("UPDATE stat_snapshots SET day = %s WHERE player_id = %s AND day = current_date", (day, player_id))


@pytest.fixture
def year_data(db, server, two_players):
    """_Tobias4444 mined 50 blocks and played 2 hours this year (first snapshot on Jan 1)."""
    a, b = two_players
    year = db.get_today().year
    snapshot_on(db, a, f"{year}-01-01")
    snapshot_on(db, b, f"{year}-01-01")
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{
        "minecraft:mined": {"minecraft:stone": 150},
        "minecraft:custom": dict(STATS_A["stats"]["minecraft:custom"], **{"minecraft:play_time": 72000 * 3})})})
    return year


def test_year_gains(db, server, two_players, year_data):
    a, _ = two_players
    gains, first = db.get_year_gains(server["id"], year_data)
    assert gains[str(a)]["blocks_mined"] == 50  # 107 at the start of the year, 157 now
    assert gains[str(a)]["play_time"] == 72000 * 2
    assert str(first) == f"{year_data}-01-01"


def test_year_review_page(client, db, server, two_players, year_data):
    html = client.get("/rueckblick/_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert f"_Tobias4444s Jahr {year_data}" in html
    assert "2 Std." in html and "hast du gespielt" in html and "Blöcke abgebaut" in html
    assert "Platz 1 von 1 auf dem Server" in html
    assert "Rückblick teilen" in html and "Aufgezeichnet seit" not in html


def test_year_review_respects_hidden_stats(client, db, server, two_players):
    db.save_profile(two_players[0], None, True)
    assert client.get("/rueckblick/_Tobias4444", **on("testdomain")).status_code == 404
    assert client.get("/rueckblick/Niemand", **on("testdomain")).status_code == 404
    assert client.get("/rueckblick/Notch?jahr=1999", **on("testdomain")).status_code == 404


def test_year_review_genitive(client, db, server):
    db.register_player_join(server["id"], OTHER_UUID, "Max")
    with db._cursor() as cur:
        cur.execute("UPDATE player SET name = 'Hans' WHERE uuid = %s", (OTHER_UUID,))
    assert "Hans’ Jahr" in client.get("/rueckblick/Hans", **on("testdomain")).get_data(as_text=True)


def test_player_page_links_the_review(client, db, server, two_players):
    assert "/rueckblick/_Tobias4444" in client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)


# ------------------------------------------------------------------ directory

def test_directory_on_the_main_page(client, db, server, other_server, two_players):
    assert "Server entdecken" not in client.get("/", **on(None)).get_data(as_text=True)  # no plugin seen yet
    db.set_plugin_connected(server["id"], True)
    db.set_plugin_connected(other_server["id"], False)
    html = client.get("/", **on(None)).get_data(as_text=True)
    assert "Server entdecken" in html and "Test Server" in html and "2 online" in html
    assert "Other Server" in html and "offline" in html
    assert html.index("Test Server") < html.index("Other Server")  # online first


def test_directory_opt_out(admin_client, db, server):
    db.set_plugin_connected(server["id"], True)
    assert admin_client.post(f"/api/servers/{server['id']}/update", json={"listed": False}, **on(None)).status_code == 200
    assert "Test Server" not in admin_client.get("/", **on(None)).get_data(as_text=True).split("Server entdecken")[-1]
    assert db.get_directory() == []
    assert "Im öffentlichen Serververzeichnis" in admin_client.get(f"/manage/{server['id']}", **on(None)).get_data(as_text=True)


# ------------------------------------------------------------------ dark mode

def test_dark_mode_is_on_every_server_page(client, db, server, two_players):
    for path in ("/", "/spieler", "/spieler?player=_Tobias4444", "/ruhmeshalle", "/events"):
        html = client.get(path, **on("testdomain")).get_data(as_text=True)
        assert "css/dark.css" in html and 'id="theme-switch"' in html, path
    assert "css/dark.css" in client.get("/login?next=/", **on("testdomain")).get_data(as_text=True)  # no header there


def test_heatmap_cells_have_levels(client, db, server, two_players):
    with db._cursor() as cur:
        cur.execute("UPDATE player_sessions SET started_at = now() - interval '3 hours', ended_at = now() - interval '1 hour'")
    html = client.get("/server-statistik", **on("testdomain")).get_data(as_text=True)
    assert 'data-level="6"' in html
