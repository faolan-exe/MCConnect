"""Connections and clients of the players (plugin 3.28, !CLIENTS and ping_avg in !HEALTH): stored per player and day,
shown to moderators only (/users/spieler#clients, ping on the overview)."""
import json

import pytest

from mc_socket.main import parse_clients, parse_health
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_moderation_log import HEALTH, mod  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)
from web import main as web

S = on("testdomain")


def sample(uuid=PLAYER_UUID, **fields):
    return dict({"uuid": uuid, "ping": 42, "brand": "fabric", "locale": "de_de", "view": 12}, **fields)


# ------------------------------------------------------------------ what the plugin sends

def test_parse_clients_keeps_valid_fields_and_drops_broken_ones():
    rows = parse_clients(json.dumps([
        sample(ping=-1, view=0, locale="DE_de", brand="<b>fabric</b>\n", version="1.21.4"),
        {"uuid": "kein-uuid", "ping": 5},
        sample(OTHER_UUID, ping=True, view=65, locale="x" * 30, brand=7, bedrock=True, device="Android"),
        "kein objekt",
    ]))
    assert len(rows) == 2
    first, second = rows
    assert first["ping"] is None and first["view"] is None and first["locale"] == "de_de"
    assert first["brand"] == "<b>fabric</b>" and first["version"] == "1.21.4" and first["bedrock"] is False
    assert second["ping"] is None and second["view"] is None and second["locale"] is None and second["brand"] is None
    assert second["bedrock"] is True and second["device"] == "Android"


@pytest.mark.parametrize("ping,stored", [(0, 0), (60000, 60000), (60001, None), (-1, None), ("12", None)])
def test_ping_limits(ping, stored):
    assert parse_clients(json.dumps([sample(ping=ping)]))[0]["ping"] == stored


def test_parse_clients_needs_a_list():
    with pytest.raises(ValueError):
        parse_clients(json.dumps({"uuid": PLAYER_UUID}))


def test_health_with_average_ping():
    assert parse_health(json.dumps(dict(HEALTH, ping_avg=37)))["ping_avg"] == 37
    assert parse_health(json.dumps(dict(HEALTH, ping_avg=-1)))["ping_avg"] is None  # nobody online
    assert parse_health(json.dumps(HEALTH))["ping_avg"] is None  # plugins before 3.28


# ------------------------------------------------------------------ storing

def test_plugin_reports_clients_and_the_day_adds_up(db, server, plugin):
    db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    connection = plugin().auth(server["key"])
    for ping in (40, 100, 70):
        assert connection.request("!CLIENTS~" + json.dumps([sample(ping=ping)])) == "success|109"
    assert connection.request("!CLIENTS~" + json.dumps([sample(OTHER_UUID)])) == "success|109"  # not on this server
    assert connection.request("!CLIENTS~{}") == "error|005"
    rows = db.get_player_clients(server["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "_Tobias4444" and row["ping"] == 70 and row["ping_avg"] == pytest.approx(70)
    assert (row["ping_min"], row["ping_max"]) == (40, 100) and row["brand"] == "fabric" and row["view_distance"] == 12


def test_a_report_without_brand_keeps_the_known_one(db, server):
    db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(brand="lunarclient:1.2")])))
    db.add_client_samples(server["id"], parse_clients(json.dumps([{"uuid": PLAYER_UUID, "ping": 30}])))
    row = db.get_player_clients(server["id"])[0]
    assert row["brand"] == "lunarclient:1.2" and row["locale"] == "de_de" and row["ping"] == 30


def test_old_daily_pings_are_deleted_and_not_counted(db, server):
    player = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    with db._cursor() as cur:
        cur.execute("INSERT INTO player_clients (player_id, ping) VALUES (%s, 50)", (player,))
        cur.execute("""INSERT INTO player_ping_daily VALUES (%s, current_date - 91, 1, 900, 900, 900),
                                                            (%s, current_date - 8, 1, 500, 500, 500)""", (player, player))
    assert db.get_player_clients(server["id"])[0]["ping_avg"] is None  # 8 days ago: outside the 7 days
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(ping=20)])))
    assert db._fetchvalue("SELECT count(*) FROM player_ping_daily") == 2  # the 91 days old row is gone
    assert db.get_player_clients(server["id"])[0]["ping_avg"] == pytest.approx(20)


# ------------------------------------------------------------------ labels

@pytest.mark.parametrize("ms,quality", [(None, None), (0, "good"), (79, "good"), (80, "ok"), (149, "ok"), (150, "bad")])
def test_ping_quality_edges(ms, quality):
    assert web.ping_quality(ms) == quality


@pytest.mark.parametrize("brand,bedrock,label", [("vanilla", False, "Vanilla"), ("fabric", False, "Fabric"),
                                                 ("NeoForge", False, "NeoForge"), ("forge", False, "Forge"),
                                                 ("lunarclient:v2.1", False, "Lunar Client"), ("MeinClient", False, "MeinClient"),
                                                 (None, False, "unbekannt"), ("vanilla", True, "Bedrock")])
def test_client_labels(brand, bedrock, label):
    assert web.client_label(brand, bedrock) == label


@pytest.mark.parametrize("code,label", [("de_de", "Deutsch (Deutschland)"), ("de_at", "Deutsch (Österreich)"),
                                        ("en_us", "Englisch (USA)"), ("en_pt", "Englisch (Portugal)"), ("fi_fi", "fi_fi"),
                                        ("nl_xx", "Niederländisch"), (None, "–")])
def test_locale_labels(code, label):
    assert web.locale_label(code) == label


# ------------------------------------------------------------------ pages (moderators only)

def test_moderators_see_connections_and_clients(mod, db, server):
    db.add_client_samples(server["id"], parse_clients(json.dumps([
        sample(ping=160, brand="vanilla"), sample(OTHER_UUID, ping=30, brand="fabric", locale="en_us", version="1.21.4")])))
    html = mod.get("/users/spieler", **S).get_data(as_text=True)
    section = html.split('id="clients"')[1].split("</section>")[0]
    assert "_Tobias4444" in section and "Notch" in section and "cl-ping-bad" in section and "160 ms" in section
    assert "Fabric" in section and "Vanilla" in section and "Englisch (USA)" in section and "1.21.4" in section
    assert section.index("_Tobias4444") < section.index("Notch")  # slowest connection first
    assert 'href="#clients"' in html


def test_average_ping_on_the_overview(mod, db, server):
    db.add_health_sample(server["id"], dict(HEALTH, ping_avg=95))
    html = mod.get("/users", **S).get_data(as_text=True)
    assert "Ping Ø" in html and "95 ms" in html and "hl-ping-ok" in html and 'data-line-chart="ping-data"' in html


def test_clients_are_not_public(client, db, server):
    db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(brand="geheimclient")])))
    for path in ("/", "/spieler", "/spieler?player=_Tobias4444", "/server-statistik", "/api/player_info/_Tobias4444"):
        assert "geheimclient" not in client.get(path, **S).get_data(as_text=True), path
    assert client.get("/users/spieler", **S).status_code in (302, 403)
