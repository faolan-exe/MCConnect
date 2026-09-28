"""Phase 6: in-game commands, sidebar, duels and reports (socket protocol and website)."""
import json
from datetime import timedelta

import pytest

from mc_socket import commands
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_achievements import custom
from tests.test_metrics import STATS_A, STATS_B, two_players  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)


def cmd(connection, uuid, command, args="", where="world|10|64|-20"):
    connection.send(f"!CMD~{uuid}|{where}|{command}|{args}")


def tells(connection, count):
    lines = [connection.recv() for _ in range(count)]
    assert all(line.startswith("!tell~") for line in lines), lines
    return [line.split("|", 1)[1] for line in lines]


def until(connection, prefix):
    """Messages up to (including) the first one starting with prefix."""
    seen = []
    while True:
        msg = connection.recv()
        seen.append(msg)
        if msg.startswith(prefix):
            return seen


@pytest.fixture
def game(db, server, plugin, two_players):
    """A connected plugin with both players online."""
    connection = plugin().auth(server["key"])
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    assert connection.request(f"!JOIN~{OTHER_UUID}|Notch") == "success|101"
    return connection


def test_metric_names_are_sent_after_auth(db, server, plugin):
    connection = plugin().auth(server["key"])
    connection.request("!BEAT_X~x")
    assert connection.metrics.startswith("!metrics~spielzeit|abgebaut|")


def test_stats_command(game):
    cmd(game, PLAYER_UUID, "stats")
    lines = until(game, "!tell~" + PLAYER_UUID + "|&7Mehr:")
    text = "\n".join(lines)
    assert "&6--- Statistik von _Tobias4444 ---" in text
    assert "&7Abgebaut: &f107" in text
    assert "#1 Blöcke abgebaut" in text
    assert "/spieler?player=_Tobias4444" in lines[-1]


def test_stats_of_hidden_player(db, game, two_players):
    db.save_profile(two_players[1], None, True)
    cmd(game, PLAYER_UUID, "stats", "notch")
    assert tells(game, 1) == ["&7Notch zeigt die Statistiken nicht öffentlich."]


def test_top_command(game):
    cmd(game, OTHER_UUID, "top", "abgebaut")
    assert tells(game, 3) == ["&6--- Top 5 · Blöcke abgebaut ---", "&e1. _Tobias4444 &7– 107", "&f2. Notch &7– 30"]
    cmd(game, OTHER_UUID, "top", "quatsch")
    assert tells(game, 2)[0] == "&cUnbekannte Kennzahl: quatsch"


def test_competition_command(db, server, game):
    today = db.get_today()
    db.create_competition(server["id"], "Mining-Woche", "blocks_mined", today, today + timedelta(days=2))
    db.create_goal(server["id"], "Sprünge", "jumps", 1000, today)
    cmd(game, PLAYER_UUID, "wettbewerb")
    text = "\n".join(until(game, f"!tell~{PLAYER_UUID}|&7Alles auf"))
    assert "&6--- Mining-Woche ---" in text and "noch 3 Tage" in text
    assert "&aZiel »Sprünge«: &f0 %" in text


def test_duel_flow(db, server, game, socket_server):
    cmd(game, PLAYER_UUID, "duell", "Notch abgebaut 2")
    first, second = game.recv(), game.recv()
    assert first == f"!tell~{OTHER_UUID}|&6_Tobias4444 fordert dich zum Duell heraus: &fBlöcke abgebaut, 2 Tage. " \
                    "&a&l/duell annehmen _Tobias4444 &c/duell ablehnen _Tobias4444"
    assert second.startswith(f"!tell~{PLAYER_UUID}|&aHerausforderung an Notch geschickt")
    assert game.recv() == f"!tell~{PLAYER_UUID}|&7Sie gilt 24 Stunden. &c/duell zurückziehen Notch"
    cmd(game, PLAYER_UUID, "duell", "Notch abgebaut 2")
    assert until(game, "!tell~")[-1] == f"!tell~{PLAYER_UUID}|&cMit Notch hast du schon ein offenes Duell."

    cmd(game, OTHER_UUID, "duell", "annehmen")
    assert game.recv().startswith("!broadcast~gold|⚔ Duell: _Tobias4444 gegen Notch")
    assert game.recv().startswith(f"!tell~{OTHER_UUID}|&aDuell angenommen!")

    notch = db.get_player_id_from_mojang_uuid_and_server_id(OTHER_UUID, server["id"])
    db.update_player_stats(notch, {"stats": dict(STATS_B["stats"], **{"minecraft:mined": {"minecraft:stone": 80}})})
    duel = db.get_player_duels(notch)[0]
    assert db.duel_gains(duel) == (0, 50)
    cmd(game, OTHER_UUID, "duell")
    assert "_Tobias4444 0 : 50 Notch" in "\n".join(tells(game, 2))

    with db._cursor() as cur:
        cur.execute("UPDATE duels SET ends_at = now() - interval '1 minute'")
    socket_server.periodic_checks()
    assert until(game, "!broadcast~")[-1] == "!broadcast~gold|⚔ Notch gewinnt das Duell gegen _Tobias4444: 0 : 50 (Blöcke abgebaut)!"
    assert db.get_player_duels(notch, ("finished",))[0]["opponent_gain"] == 50


def test_duel_decline_and_errors(db, server, game):
    cmd(game, PLAYER_UUID, "duell", "Notch tode")
    assert tells(game, 2)[0] == "&cUnbekannte Kennzahl: tode"  # less is better: no duels
    cmd(game, PLAYER_UUID, "duell", "Notch abgebaut 9")
    assert tells(game, 1) == ["&cEin Duell dauert 1 bis 7 Tage."]
    cmd(game, PLAYER_UUID, "duell", "_tobias4444 abgebaut")
    assert tells(game, 1) == ["&cDu kannst dich nicht selbst herausfordern."]
    cmd(game, PLAYER_UUID, "duell", "Notch fische 1")
    tells(game, 3)
    cmd(game, OTHER_UUID, "duell", "ablehnen")
    assert tells(game, 2) == ["&7Notch hat dein Duell abgelehnt.", "&7Duell abgelehnt."]


def test_pending_duels_expire(db, server, two_players):
    a, b = two_players
    duel_id, _ = db.create_duel(server["id"], a, b, "jumps", 1)
    with db._cursor() as cur:
        cur.execute("UPDATE duels SET created_at = now() - interval '25 hours'")
    assert db.respond_duel(duel_id, b, True) is None
    assert db.get_duel(duel_id)["status"] == "expired"


def test_report_command(db, server, game):
    db.set_moderator(server["id"], "Notch", True)
    cmd(game, PLAYER_UUID, "report", "notch baut mein Haus ab")
    moderator, answer = game.recv(), game.recv()
    assert moderator.startswith(f"!tell~{OTHER_UUID}|&c[Meldung] &f_Tobias4444 meldet Notch bei world 10 64 -20")
    assert answer == f"!tell~{PLAYER_UUID}|&aDanke! Deine Meldung ist bei den Moderatoren angekommen."
    report = db.list_reports(server["id"])[0]
    assert (report["target_name"], report["reason"], report["world"], report["x"], report["z"]) == \
        ("Notch", "baut mein Haus ab", "world", 10, -20)


def test_reports_are_limited(db, server, two_players):
    for _ in range(5):
        assert db.create_report(server["id"], two_players[0], None, "Spam") is not None
    assert db.create_report(server["id"], two_players[0], None, "Spam") is None


def test_sidebar(db, server, game, socket_server):
    cmd(game, PLAYER_UUID, "seitenleiste", "spielzeit")
    sidebar = game.recv()
    assert sidebar.startswith(f"!sidebar~{PLAYER_UUID}|Deine Spielzeit|&7Heute: &f")
    assert tells(game, 1)[0].startswith("&aSeitenleiste: spielzeit.")
    socket_server.periodic_checks()  # pushed again every minute
    assert until(game, "!sidebar~")[-1] == sidebar
    cmd(game, PLAYER_UUID, "seitenleiste", "aus")
    assert game.recv() == f"!sidebar~{PLAYER_UUID}|"


def test_competition_sidebar(db, server, two_players):
    today = db.get_today()
    db.create_competition(server["id"], "Mining-Woche", "blocks_mined", today, today + timedelta(days=2))
    title, lines = commands.sidebar(db, two_players[0], "competition")
    assert title == "Mining-Woche" and lines[-1] == "&7Du: noch keine Punkte"
    assert commands.sidebar(db, two_players[0], "off") is None


def test_unknown_player_and_command(db, server, plugin):
    connection = plugin().auth(server["key"])
    cmd(connection, PLAYER_UUID, "stats")
    assert tells(connection, 1)[0].startswith("&cMCConnect kennt dich noch nicht")


# ------------------------------------------------------------------ website

@pytest.fixture
def player_client(client, db, server, two_players):
    db.set_plugin_connected(server["id"], True)
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET online = true")
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    return client


def test_duels_on_the_website(player_client, client, db, server, two_players):
    a, b = two_players
    response = player_client.post("/api/duels", json={"name": "Notch", "metric": "jumps", "days": 2}, **on("testdomain"))
    assert response.status_code == 200
    assert player_client.post("/api/duels", json={"name": "Notch", "metric": "jumps", "days": 2},
                              **on("testdomain")).status_code == 409
    html = player_client.get("/duelle", **on("testdomain")).get_data(as_text=True)
    assert "wartet auf Antwort" in html and "Herausfordern" in html
    assert player_client.post("/api/duels/respond", json={"id": response.json["id"], "accept": True},
                              **on("testdomain")).status_code == 404  # only the opponent can accept
    assert db.respond_duel(response.json["id"], b, True)["status"] == "running"
    assert "läuft" in client.get("/duelle", **on("testdomain")).get_data(as_text=True)


def test_reports_on_the_website(player_client, db, server):
    assert player_client.get("/melden", **on("testdomain")).status_code == 200
    assert player_client.post("/api/reports", json={"name": "Notch", "reason": "kurz"}, **on("testdomain")).status_code == 400
    assert player_client.post("/api/reports", json={"name": "Niemand", "reason": "Lava am Spawn"},
                              **on("testdomain")).status_code == 404
    assert player_client.post("/api/reports", json={"name": "notch", "reason": "Lava am Spawn"},
                              **on("testdomain")).status_code == 200
    report = db.list_reports(server["id"])[0]
    assert (report["target_name"], report["source"], report["world"]) == ("Notch", "web", None)
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert "<b>1</b> Meldungen" in player_client.get("/users", **on("testdomain")).get_data(as_text=True)
    assert "Lava am Spawn" in player_client.get("/users/spieler", **on("testdomain")).get_data(as_text=True)
    assert player_client.post("/api/mod/reports/resolve", json={"id": report["id"]}, **on("testdomain")).status_code == 200
    assert db.list_reports(server["id"]) == []
    assert db.get_mod_log(server["id"])[0]["action"] == "report_resolve"


def test_sidebar_setting_on_the_profile(player_client, db, two_players):
    assert "Seitenleiste im Spiel" in player_client.get("/profil", **on("testdomain")).get_data(as_text=True)
    assert player_client.post("/api/sidebar", json={"mode": "competition"}, **on("testdomain")).status_code == 200
    assert db.get_sidebar(two_players[0]) == "competition"
    assert player_client.post("/api/sidebar", json={"mode": "x"}, **on("testdomain")).status_code == 400


def test_pages_need_login(client, server):
    assert client.get("/melden", **on("testdomain")).status_code == 302
    assert client.post("/api/duels", json={}, **on("testdomain")).status_code == 401
    assert client.get("/duelle", **on("testdomain")).status_code == 200


def test_withdraw_a_challenge(db, server, game):
    cmd(game, PLAYER_UUID, "duell", "Notch jumps 1")
    tells(game, 3)
    cmd(game, PLAYER_UUID, "duell", "zurückziehen Notch")
    assert tells(game, 2) == ["&7_Tobias4444 hat die Herausforderung zum Duell zurückgezogen.",
                              "&7Herausforderung an Notch zurückgezogen."]
    cmd(game, OTHER_UUID, "duell", "annehmen")
    assert tells(game, 1) == ["&7Du hast keine offene Herausforderung."]


def test_buttons_for_plugins_with_clickable_chat(db, server, plugin, two_players):
    connection = plugin().auth(server["key"])
    assert connection.request("!FEATURES~click") == "success|106"
    assert connection.request(f"!JOIN~{OTHER_UUID}|Notch") == "success|101"
    cmd(connection, PLAYER_UUID, "duell", "Notch jumps 1")
    invite = connection.recv()
    assert "\u27e6&a&l[Annehmen]\u21d2/duell annehmen _Tobias4444\u27e7" in invite


def test_players_cannot_inject_buttons():
    assert commands.clean("\u27e6x\u21d2/op me\u27e7") == "x/op me"
    assert commands.without_buttons(commands.button("&a[Ja]", "/vote #3 1")) == "&a/vote #3 1"


def test_withdraw_on_the_website(player_client, db, server, two_players):
    duel_id = player_client.post("/api/duels", json={"name": "Notch", "metric": "jumps", "days": 1},
                                 **on("testdomain")).json["id"]
    assert "Zurückziehen" in player_client.get("/duelle", **on("testdomain")).get_data(as_text=True)
    assert player_client.post("/api/duels/cancel", json={"id": duel_id}, **on("testdomain")).status_code == 200
    assert db.get_duel(duel_id)["status"] == "cancelled"
    assert player_client.post("/api/duels/cancel", json={"id": duel_id}, **on("testdomain")).status_code == 404


def test_one_failing_periodic_step_does_not_stop_the_others(db, server, game, socket_server, monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")
    monkeypatch.setattr(socket_server, "announce_competitions", broken)
    cmd(game, PLAYER_UUID, "seitenleiste", "spielzeit")
    until(game, "!tell~")
    socket_server.periodic_checks()  # the sidebar update still comes
    assert until(game, "!sidebar~")[-1].startswith(f"!sidebar~{PLAYER_UUID}|Deine Spielzeit|")
