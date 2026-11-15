"""Connections and clients of the players (plugin 3.28, !CLIENTS and ping_avg in !HEALTH): stored per player and day,
shown to moderators only (/users/spieler#clients, ping on the overview)."""
import json

import pytest

from database import clients
from mc_socket.main import parse_clients, parse_health
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_moderation_log import HEALTH, mod  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

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
    assert clients.ping_quality(ms) == quality


@pytest.mark.parametrize("brand,bedrock,label", [("vanilla", False, "Vanilla"), ("fabric", False, "Fabric"),
                                                 ("NeoForge", False, "NeoForge"), ("forge", False, "Forge"),
                                                 ("lunarclient:v2.1", False, "Lunar Client"), ("MeinClient", False, "MeinClient"),
                                                 (None, False, "unbekannt"), ("vanilla", True, "Bedrock")])
def test_client_labels(brand, bedrock, label):
    assert clients.client_label(brand, bedrock) == label


@pytest.mark.parametrize("code,label", [("de_de", "Deutsch (Deutschland)"), ("de_at", "Deutsch (Österreich)"),
                                        ("en_us", "Englisch (USA)"), ("en_pt", "Englisch (Portugal)"), ("fi_fi", "fi_fi"),
                                        ("nl_xx", "Niederländisch"), (None, "–")])
def test_locale_labels(code, label):
    assert clients.locale_label(code) == label


# ------------------------------------------------------------------ pages (moderators only)

def test_moderators_see_connections_and_clients(mod, db, server):
    db.add_client_samples(server["id"], parse_clients(json.dumps([
        sample(ping=160, brand="vanilla"), sample(OTHER_UUID, ping=30, brand="fabric", locale="en_us", version="1.21.4")])))
    html = mod.get("/analyse", **S).get_data(as_text=True)
    section = html.split('id="spieler"')[1].split("</section>")[0]
    assert "_Tobias4444" in section and "Notch" in section and "cl-ping-bad" in section and "160 ms" in section
    assert "Fabric" in html and "Vanilla" in html and "Englisch (USA)" in html and "1.21.4" in html
    assert section.index("_Tobias4444") < section.index("Notch")  # slowest connection first
    assert 'href="/analyse"' in mod.get("/users/spieler", **S).get_data(as_text=True)  # pointer from the moderation page


def test_average_ping_on_the_overview(mod, db, server):
    db.add_health_sample(server["id"], dict(HEALTH, ping_avg=95))
    html = mod.get("/users", **S).get_data(as_text=True)
    assert "Ping typisch" in html and "95 ms" in html and "hl-ping-ok" in html and 'data-line-chart="ping-data"' in html


def test_clients_are_not_public(client, db, server):
    db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(brand="geheimclient")])))
    for path in ("/", "/spieler", "/spieler?player=_Tobias4444", "/server-statistik", "/api/player_info/_Tobias4444"):
        html = client.get(path, **S).get_data(as_text=True)
        assert "geheimclient" not in html and 'href="/analyse"' not in html, path
    assert client.get("/analyse", **S).status_code in (302, 403)
    assert client.get("/analyse/spieler/_Tobias4444", **S).status_code in (302, 403)


# ------------------------------------------------------------------ plugin 3.29: mods, pack, options, address

FULL = dict(channels=["fabric:registry/sync", "voicechat:secret", "minecraft:brand", "XAEROMINIMAP:bad", "x" * 70 + ":y",
                      "journeymap:version", "unbekannt:kanal"],
            pack="SUCCESSFULLY_LOADED", chat="HIDDEN", hand="LEFT", chat_colors=True, text_filter=False, listing="ja",
            particles="DECREASED", cape=False, skin_layers=6, host="Play.Survival-Deluxe.de", input="TOUCH", ui="POCKET",
            bedrock_version="1.21.50")


def test_parse_clients_reads_the_new_fields():
    row = parse_clients(json.dumps([sample(**FULL)]))[0]
    assert row["channels"] == ["fabric:registry/sync", "journeymap:version", "minecraft:brand", "unbekannt:kanal",
                               "voicechat:secret"]  # sorted, invalid ones dropped
    assert (row["pack"], row["chat"], row["main_hand"], row["particles"]) == ("successfully_loaded", "hidden", "left", "decreased")
    assert row["chat_colors"] is True and row["text_filter"] is False and row["listing"] is None and row["cape"] is False
    assert row["skin_layers"] == 6 and row["host"] == "play.survival-deluxe.de" and row["input"] == "touch"


@pytest.mark.parametrize("field,value,stored", [("skin_layers", 7, 7), ("skin_layers", 8, None), ("skin_layers", -1, None),
                                                ("host", "a b.de", None), ("host", "10.0.0.5", "10.0.0.5"),
                                                ("pack", "ok!", None), ("channels", "fabric:x", None)])
def test_new_field_limits(field, value, stored):
    key = {"hand": "main_hand"}.get(field, field)
    assert parse_clients(json.dumps([sample(**{field: value})]))[0][key] == stored


def test_mods_are_recognized_by_their_channels():
    found, unknown = clients.mods(["fabric:registry/sync", "fabric-networking-api-v1:early", "voicechat:secret",
                                   "minecraft:register", "worldedit:cui", "unbekannt:kanal", "BungeeCord"])
    assert found == [("Fabric API", "loader"), ("Simple Voice Chat", "voice"), ("WorldEdit CUI", "build")]
    assert unknown == ["unbekannt"]
    assert clients.mods(None) == ([], [])


def test_old_reports_keep_the_new_values(db, server):
    db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(**FULL)])))
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(ping=12)])))  # a 3.28 report
    row = db.get_player_clients(server["id"])[0]
    assert row["chat"] == "hidden" and row["host"] == "play.survival-deluxe.de" and "voicechat:secret" in row["channels"]


@pytest.mark.parametrize("ping,spike", [(249, 0), (250, 1), (900, 1)])
def test_ping_spikes_per_day(db, server, ping, spike):
    db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(ping=ping)])))
    assert db.get_player_clients(server["id"])[0]["spikes"] == spike


def test_the_day_remembers_client_and_mods(db, server):
    db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(**FULL)])))
    day = db.get_ping_days(server["id"], "_Tobias4444")[0]
    assert day[5] == "Fabric" and day[4] == 0
    assert db.get_client_days(server["id"], 3)[-1][1:3] == (1, 1)  # one player today, with mods


def test_analysis_page_and_player_detail(mod, db, server):
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(**FULL), sample(OTHER_UUID, ping=300, bedrock=True,
                                                                                              device="Android", input="TOUCH")])))
    html = mod.get("/analyse", **S).get_data(as_text=True)
    for text in ("Simple Voice Chat", "JourneyMap", "Fabric API", "aus", "links", "geladen", "play.survival-deluxe.de",
                 'id="bedrock"', "Android", "Touch", 'data-line-chart="ping-week"', 'data-bar-chart="ping-buckets"'):
        assert text in html, text
    detail = mod.get("/analyse/spieler/_Tobias4444", **S).get_data(as_text=True)
    assert "Simple Voice Chat" in detail and "unbekannt" in detail and "6 von 7" in detail and "Haupthand" in detail
    assert mod.get("/analyse/spieler/Niemand", **S).status_code == 404
    assert '<a class="nav-top" href="/analyse">Analyse</a>' in html or 'href="/analyse">Analyse</a>' in html


def test_analysis_without_data(mod):
    html = mod.get("/analyse", **S).get_data(as_text=True)
    assert "Noch keine Daten" in html and "3.28" in html


# ------------------------------------------------------------------ plugin 3.30: clicks and rhythm

def minute(intervals, clicks=None, cps_max=6, rhythm="tick", stalls=0, gap_max=60, ticks=1200):
    """One minute of the plugin's click pattern for the given intervals in ms (all below 500)."""
    hist = [0] * 8
    for ms in intervals:
        hist[next(i for i, high in enumerate((30, 50, 70, 90, 120, 160, 250, 500)) if ms < high)] += 1
    return {"rhythm": rhythm, "gap_max": gap_max, "stalls": stalls, "ticks": ticks,
            "clicks": len(intervals) + 1 if clicks is None else clicks, "cps_max": cps_max, "click_n": len(intervals),
            "click_sum": sum(intervals), "click_sq": sum(ms * ms for ms in intervals),
            "click_min": min(intervals) if intervals else None, "doubles": sum(1 for ms in intervals if ms < 25),
            "click_hist": hist}


def test_parse_rhythm_keeps_a_consistent_minute():
    row = parse_clients(json.dumps([sample(**minute([100, 110, 20]))]))[0]
    assert row["click_n"] == 3 and row["click_sum"] == 230 and row["doubles"] == 1 and row["click_min"] == 20
    assert row["click_hist"] == [1, 0, 0, 0, 2, 0, 0, 0] and row["rhythm"] == "tick" and row["ticks"] == 1200


@pytest.mark.parametrize("broken", [
    {"click_hist": [1, 2, 3]},                   # wrong number of buckets
    {"click_hist": [9, 0, 0, 0, 0, 0, 0, 0]},    # buckets do not add up to click_n
    {"click_n": 5},                              # more intervals than clicks
    {"click_sum": 3 * 500 + 1},                  # an interval of 500 ms or more is not clicking in a row
    {"doubles": 4},                              # more double clicks than intervals
    {"click_min": None},                         # intervals without a shortest one
    {"cps_max": True},
])
def test_parse_rhythm_drops_a_click_pattern_that_does_not_fit(broken):
    row = parse_clients(json.dumps([sample(**dict(minute([100, 110, 120]), **broken))]))[0]
    assert row["click_n"] is None and row["click_hist"] is None and row["cps_max"] is None
    assert row["rhythm"] == "tick" and row["clicks"] == 4  # the rest of the minute stays


def test_parse_rhythm_without_data_and_with_a_wrong_rhythm():
    row = parse_clients(json.dumps([sample(rhythm="frames", gap_max=-1, stalls="2")]))[0]
    assert row["rhythm"] is None and row["gap_max"] is None and row["stalls"] is None and row["clicks"] is None


def even(n, ms=100, jitter=0):
    return [ms + (jitter if i % 2 else -jitter) for i in range(n)]


@pytest.mark.parametrize("intervals,flagged", [
    (even(150, 100, 4), True),     # 10 per second, 4 % spread: an auto clicker
    (even(149, 100, 4), False),    # too few intervals to judge
    (even(150, 125, 4), True),     # exactly 8 per second
    (even(150, 126, 4), False),    # slower than 8 per second: slow clicks are even anyway
    (even(150, 100, 8), False),    # 8 % spread is the limit
    (even(150, 100, 25), False),   # a person
])
def test_even_clicks_edges(intervals, flagged):
    summary = clients.click_summary(minute(intervals))
    assert ("gleichmäßig" in summary["flags"]) is flagged


@pytest.mark.parametrize("cps_max,flagged", [(19, False), (20, True)])
def test_fast_clicks_edge(cps_max, flagged):
    assert ("sehr schnell" in clients.click_summary(minute(even(10, 100, 30), cps_max=cps_max))["flags"]) is flagged


@pytest.mark.parametrize("doubles,n,flagged", [(10, 100, True), (9, 100, False), (10, 99, False)])
def test_double_click_edges(doubles, n, flagged):
    intervals = [20] * doubles + [140, 180] * ((n - doubles) // 2) + [160] * ((n - doubles) % 2)
    assert ("Doppelklicks" in clients.click_summary(minute(intervals))["flags"]) is flagged


def test_click_summary_figures():
    assert clients.click_summary(minute([])) is None  # one click is no clicking in a row
    s = clients.click_summary(minute([100, 100, 100, 100]))
    assert s["cps"] == 10.0 and s["cv"] == 0 and s["doubles_percent"] == 0


def test_rhythm_summary():
    assert clients.rhythm_summary({"rhythm_minutes": 0}) is None
    r = clients.rhythm_summary({"rhythm_minutes": 30, "tick_minutes": 30, "ticks": 36000, "stalls": 6, "gap_max": 900})
    assert r["stalls_per_hour"] == 12.0 and r["tps"] == 20.0 and r["threshold"] == 250
    old = clients.rhythm_summary({"rhythm_minutes": 10, "tick_minutes": 0, "ticks": 0, "stalls": 1, "gap_max": 2000})
    assert old["tps"] is None and old["threshold"] == 1500


def test_minutes_add_up_to_the_day(db, server):
    db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(**minute([100, 20], cps_max=4, stalls=1, gap_max=300))])))
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(**minute([60], cps_max=7, rhythm="move", gap_max=1600))])))
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(ping=30)])))  # a 3.29 report adds nothing
    day = db.get_rhythm(server["id"])["_Tobias4444"]
    assert day["rhythm_minutes"] == 2 and day["tick_minutes"] == 1 and day["ticks"] == 1200 and day["stalls"] == 1
    assert day["gap_max"] == 1600 and day["clicks"] == 5 and day["cps_max"] == 7 and day["click_n"] == 3
    assert day["click_sum"] == 180 and day["click_min"] == 20 and day["doubles"] == 1
    assert day["click_hist"] == [1, 0, 1, 0, 1, 0, 0, 0]
    assert db.get_rhythm_days(server["id"], "_tobias4444")[0][1]["click_n"] == 3


def test_analysis_shows_clicks_and_rhythm(mod, db, server):
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample(**minute(even(160, 90, 2), cps_max=12, stalls=3))])))
    html = mod.get("/analyse", **S).get_data(as_text=True)
    for text in ('id="klicks"', 'data-bar-chart="click-hist"', "gleichmäßig", "Meiste Aussetzer", "_Tobias4444"):
        assert text in html, text
    detail = mod.get("/analyse/spieler/_Tobias4444", **S).get_data(as_text=True)
    for text in ("Klicks in Folge", "gleichmäßig", 'data-line-chart="click-cv"', "Ticks pro Sekunde des Clients", "20.0"):
        assert text in detail, text


def test_analysis_without_click_data(mod, db, server):
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample()])))
    assert "Noch keine Daten" in mod.get("/analyse", **S).get_data(as_text=True)
    assert "ab Plugin 3.30, nur Java" in mod.get("/analyse/spieler/_Tobias4444", **S).get_data(as_text=True)


@pytest.mark.parametrize("version,restart,old_329", [("3.28", True, True), ("3.29", True, False), ("3.30", False, False),
                                                     ("3.31", False, False)])
def test_analysis_tells_which_plugin_runs(mod, db, server, version, restart, old_329):
    db.add_health_sample(server["id"], dict(HEALTH, plugin_version=version))
    db.add_client_samples(server["id"], parse_clients(json.dumps([sample()])))
    html = mod.get("/analyse", **S).get_data(as_text=True)
    assert f"Server läuft mit MCDataLink {version}" in html and "letzte Meldung" in html
    assert ("Neustart nötig" in html) is restart
    assert ("diese Daten kommen ab 3.29" in html) is old_329
    assert ("diese Daten kommen ab 3.30" in html) is restart


def test_plugin_version_tuple():
    from web.main import plugin_version_tuple
    assert plugin_version_tuple("3.30") == (3, 30) and plugin_version_tuple("3.9.1") == (3, 9)
    assert plugin_version_tuple(None) is None and plugin_version_tuple("dev") is None
