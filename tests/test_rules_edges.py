"""The limits of the rules, each checked at its edges: the last allowed value passes, the first one beyond is refused
(durations, lengths, numbers). Also the small helpers behind texts and rankings."""
from datetime import date, timedelta

import pytest

from mc_socket import commands
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)
from web import main as web

S = on("testdomain")


@pytest.fixture
def mod_client(client, db, server, two_players):
    db.set_plugin_connected(server["id"], True)
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET online = true")
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    return client


def check(client, path, cases):
    """cases: [(body, expected status)]; returns the cases that got another status."""
    return [(body, expected, status) for body, expected in cases
            if (status := client.post(path, json=body, **S).status_code) != expected]


def day(db, offset):
    return (db.get_today() + timedelta(days=offset)).isoformat()


def test_competition_limits(mod_client, db):
    base = lambda **kw: dict({"title": "Bauwoche", "metric": "blocks_mined", "starts_on": day(db, 0),
                              "ends_on": day(db, 6)}, **kw)
    assert not check(mod_client, "/api/mod/competitions", [
        (base(ends_on=day(db, 89)), 200),          # 90 days including the first
        (base(ends_on=day(db, 90)), 400),
        (base(ends_on=day(db, 0)), 200),           # one day
        (base(starts_on=day(db, 3), ends_on=day(db, 2)), 400),
        (base(starts_on=day(db, -1)), 400),        # not in the past
        (base(title="abc"), 200), (base(title="ab"), 400),
        (base(title="a" * 60), 200), (base(title="a" * 61), 400),
        (base(title="<b>Fett</b>"), 400),
        (base(metric="nope"), 400), (base(starts_on="2026-02-30"), 400),
    ])


def test_goal_limits(mod_client, db):
    base = lambda **kw: dict({"title": "Großes Ziel", "metric": "blocks_mined", "amount": "1000",
                              "starts_on": day(db, 0)}, **kw)
    assert not check(mod_client, "/api/mod/goals", [
        (base(ends_on=day(db, 364)), 200), (base(ends_on=day(db, 365)), 400),
        (base(), 200),                             # open end
        (base(metric="play_time", amount="0,5"), 200),  # German decimal comma: half an hour
        (base(amount="0,4"), 400),                 # rounds to 0 blocks
        (base(amount="0"), 400), (base(amount="-1"), 400), (base(amount="abc"), 400),
        (base(starts_on=day(db, -1)), 400), (base(starts_on=day(db, 5), ends_on=day(db, 4)), 400),
    ])


def test_duel_limits(mod_client, db, server, two_players):
    base = lambda **kw: dict({"name": "Notch", "metric": "blocks_mined", "days": 3}, **kw)
    assert not check(mod_client, "/api/duels", [
        (base(days=0), 400), (base(days=8), 400), (base(days="drei"), 400),
        (base(metric="deaths"), 400),              # less is better: no duel
        (base(name="_Tobias4444"), 409),           # not against yourself
        (base(name="Unbekannt"), 404),
        (base(days=7), 200), (base(days=1), 409),  # one open duel per pair
    ])


def test_poll_limits(mod_client):
    base = lambda **kw: dict({"question": "Was bauen wir?", "options": ["Burg", "Hafen"], "days": 3}, **kw)
    assert not check(mod_client, "/api/mod/polls", [
        (base(days=60), 200), (base(days=61), 400), (base(days=0), 400),
        (base(options=["a"] * 1), 400), (base(options=[str(i) for i in range(8)]), 200),
        (base(options=[str(i) for i in range(9)]), 400),
        (base(options=["Burg", "burg"]), 400),     # the same answer twice
        (base(options=["Burg", " ", "Hafen"]), 200),  # empty answers are dropped
        (base(options=["x" * 60, "y"]), 200), (base(options=["x" * 61, "y"]), 400),
        (base(question="Was?!"), 200), (base(question="Was?"), 400), (base(question="x" * 121), 400),
    ])


def test_event_limits(mod_client, db):
    base = lambda **kw: dict({"title": "Bootsrennen", "starts_at": day(db, 2) + "T20:00"}, **kw)
    assert not check(mod_client, "/api/mod/events", [
        (base(starts_at=day(db, 364) + "T12:00"), 200), (base(starts_at=day(db, 367) + "T12:00"), 400),
        (base(starts_at=day(db, -1) + "T12:00"), 400),
        (base(title="abc"), 200), (base(title="ab"), 400), (base(title="x" * 61), 400),
        (base(place="x" * 80), 200), (base(place="x" * 81), 400),
        (base(description="x" * 500), 200), (base(description="x" * 501), 400),
        (base(starts_at="morgen"), 400),
    ])


def test_invite_code_limits(mod_client):
    assert not check(mod_client, "/api/mod/codes", [
        ({"code": "ABCD", "max_uses": 1, "days": 1}, 200), ({"code": "ABC"}, 400),
        ({"code": "A" * 24, "max_uses": 1000, "days": 365}, 200), ({"code": "B" * 25}, 400),
        ({"code": "ABCE", "max_uses": 0}, 400), ({"code": "ABCF", "max_uses": 1001}, 400),
        ({"code": "ABCG", "days": 0}, 400), ({"code": "ABCH", "days": 366}, 400),
        ({"code": "abcd"}, 409),                   # codes are case-insensitive: ABCD exists
        ({"code": "MIT LEER"}, 400), ({}, 200),    # without a code one is generated
    ])


def test_mute_limits(mod_client, db, two_players):
    assert not check(mod_client, "/api/mod/mute", [
        ({"name": "Notch", "minutes": commands.MUTE_MAX_MINUTES}, 200),
        ({"name": "Notch", "minutes": commands.MUTE_MAX_MINUTES + 1}, 400),
        ({"name": "Notch", "minutes": -1}, 400), ({"name": "Notch", "minutes": "lang"}, 400),
        ({"name": "Notch", "minutes": 0}, 200),    # 0 lifts the mute
    ])
    assert db.get_player_info_by_player_id(two_players[1])["muted_until"] is None


def test_ban_limits(mod_client, db, server):
    assert not check(mod_client, "/api/mod/ban", [
        ({"name": "Notch", "days": 3651}, 400), ({"name": "Notch", "days": -1}, 400),
        ({"name": "Notch", "days": "x"}, 400), ({"name": "Unbekannt"}, 404),
        ({"name": "Notch", "days": 3650}, 200),
    ])
    end = db.list_active_bans(server["id"])[0]["end"]
    assert 3649 <= (end.date() - date.today()).days <= 3650


def test_moderation_setting_limits(mod_client):
    assert not check(mod_client, "/api/mod/settings", [
        ({"warn_threshold": 20}, 200), ({"warn_threshold": 21}, 400), ({"warn_threshold": -1}, 400),
        ({"warn_ban_days": 3650}, 200), ({"warn_ban_days": 3651}, 400),
        ({"rules": "x" * 10_000}, 200), ({"rules": "x" * 10_001}, 400), ({"faq": "x" * 10_001}, 400),
        ({"access_mode": "everyone"}, 400), ({"access_mode": "both"}, 200),
    ])


def test_player_text_limits(mod_client):
    assert not check(mod_client, "/api/profile", [({"bio": "x" * 160}, 200), ({"bio": "x" * 161}, 400)])
    assert not check(mod_client, "/api/guestbook", [
        ({"name": "Notch", "text": "x"}, 400), ({"name": "Notch", "text": "xy"}, 200),
        ({"name": "Notch", "text": "x" * 301}, 400), ({"name": "Notch", "text": "x  " * 150}, 200),  # spaces collapse
    ])
    assert not check(mod_client, "/api/reports", [
        ({"reason": "abcd"}, 400), ({"reason": "abcde"}, 200), ({"reason": "x" * 301}, 400),
        ({"name": "Unbekannt", "reason": "abcdef"}, 404),
    ])
    assert not check(mod_client, "/api/mod/notes", [
        ({"name": "Notch", "text": " "}, 400), ({"name": "Notch", "text": "x" * 1000}, 200),
        ({"name": "Notch", "text": "x" * 1001}, 400),
    ])
    assert not check(mod_client, "/api/mod/warn", [({"name": "Notch", "reason": "ab"}, 400)])


def test_prefix_limits(mod_client):
    assert not check(mod_client, "/api/prefix/save", [
        ({"text": "x" * 16, "color": "red"}, 200), ({"text": "x" * 17, "color": "red"}, 400),
        ({"text": "", "color": "red"}, 400), ({"text": "&cRot", "color": "red"}, 400),
        ({"text": "Team", "color": "pink"}, 400),
        ({"text": "Team", "color": "red", "password": "p" * 3}, 400),
        ({"text": "Team", "color": "red", "password": "p" * 4}, 200),
    ])


def test_application_limits(client, db, server):
    db.update_server_settings(server["id"], access_mode="application")
    message = "x" * 10
    assert not check(client, "/api/access/apply", [
        ({"name": "ab", "message": message}, 400), ({"name": "abc", "message": message}, 200),
        ({"name": "a" * 17, "message": message}, 400), ({"name": "mit-strich", "message": message}, 400),
        ({"name": "Kurz", "message": "x" * 9}, 400), ({"name": "Lang", "message": "x" * 501}, 400),
        ({"name": "Genau", "message": "x" * 500}, 200),
    ])


# ------------------------------------------------------------------ warnings

def test_warnings_ban_again_at_every_multiple_of_the_threshold(db, server, two_players):
    notch = two_players[1]
    db.update_server_settings(server["id"], warn_threshold=2, warn_ban_days=1)
    results = [db.warn_player(server["id"], notch, f"Grund {i}", "mod")["ban"] is not None for i in range(1, 5)]
    assert results == [False, True, False, True]


def test_threshold_zero_never_bans(db, server, two_players):
    db.update_server_settings(server["id"], warn_threshold=0)
    assert all(db.warn_player(server["id"], two_players[1], "x", "mod")["ban"] is None for _ in range(5))


@pytest.mark.parametrize("days,text", [(0, "dauerhaft gebannt"), (1, "für 1 Tag gebannt"), (7, "für 7 Tage gebannt")])
def test_warning_ban_text_names_the_duration(db, server, two_players, days, text):
    db.update_server_settings(server["id"], warn_threshold=1, warn_ban_days=days)
    message = commands.apply_warning(db, server["id"], two_players[1], "Spam", "mod", lambda *a: None, lambda *a: None)
    assert message == f"Notch ist verwarnt (1/1) und {text}."
    end = db.list_active_bans(server["id"])[0]["end"]
    assert (end is None) == (days == 0)


# ------------------------------------------------------------------ helpers

@pytest.mark.parametrize("days,text", [(0, "seit heute"), (1, "seit 1 Tag"), (59, "seit 59 Tagen"), (60, "seit 2 Monaten"),
                                       (364, "seit 12 Monaten"), (365, "seit 1 Jahr"), (729, "seit 1 Jahr"),
                                       (730, "seit 2 Jahren")])
def test_since_text(days, text):
    today = date(2026, 10, 22)
    assert web.since_text(today - timedelta(days=days), today) == text


@pytest.mark.parametrize("seconds,text", [(0, "0 Min."), (59, "0 Min."), (60, "1 Min."), (3599, "59 Min."),
                                          (3600, "1 Std. 0 Min."), (86399, "23 Std. 59 Min."), (86400, "1 Tag 0 Std."),
                                          (2 * 86400 + 3 * 3600, "2 Tage 3 Std.")])
def test_format_duration(seconds, text):
    assert web.format_duration(seconds) == text


def test_rankings_share_places_on_ties_and_skip_zeros():
    metric = web.metrics_mod.METRICS_BY_KEY["blocks_mined"]
    players = [{"name": n, "uuid": n, "values": {"blocks_mined": v}} for n, v in
               (("b", 5), ("A", 5), ("c", 3), ("d", 0), ("e", 3), ("f", 1))]
    rows = web.ranked(players, metric)
    assert [(r["rank"], r["name"]) for r in rows] == [(1, "A"), (1, "b"), (3, "c"), (3, "e"), (5, "f")]
    assert rows[0]["share"] == 1 and rows[-1]["share"] == 0.2
    assert web.ranked([], metric) == []


def test_year_rank_shares_places():
    gains = {"a": {"play_time": 50}, "b": {"play_time": 50}, "c": {"play_time": 10}, "d": {"play_time": 0}, "h": {"play_time": 99}}
    visible = {"a", "b", "c", "d"}  # h hides the stats
    assert web.year_rank(gains, visible, "a", "play_time") == (1, 3)
    assert web.year_rank(gains, visible, "b", "play_time") == (1, 3)
    assert web.year_rank(gains, visible, "c", "play_time") == (3, 3)
    assert web.year_rank(gains, visible, "d", "play_time") == (None, 3)


def test_rate_limiter_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(web.time, "monotonic", lambda: now[0])
    limiter = web.RateLimiter(2, 60)
    assert limiter.allow("ip") and limiter.allow("ip") and not limiter.allow("ip")
    assert limiter.allow("other")                  # per key
    now[0] += 59.9
    assert not limiter.allow("ip")
    now[0] += 0.2                                  # the first hits are older than the window
    assert limiter.allow("ip")


def test_live_cache_computes_once_per_period(monkeypatch):
    now, calls = [0.0], []
    monkeypatch.setattr(web.time, "monotonic", lambda: now[0])
    cache = web.LiveCache(seconds=2)
    compute = lambda: calls.append(1) or len(calls)
    assert cache.get("k", compute) == 1 and cache.get("k", compute) == 1
    now[0] += 1.99
    assert cache.get("k", compute) == 1
    now[0] += 0.02
    assert cache.get("k", compute) == 2


def test_best_value_of_compared_players():
    assert web.best_indexes([3, 7, 7], lower_is_better=False) == {1, 2}
    assert web.best_indexes([3, 7, 7], lower_is_better=True) == {0}
    assert web.best_indexes([0, 0, 0], lower_is_better=False) == set()


def test_faq_blocks():
    assert web.faq_entries("Frage eins?\nAntwort\n  zweite Zeile\n\n \nFrage zwei?") == [
        ("Frage eins?", "Antwort\nzweite Zeile"), ("Frage zwei?", "")]
    assert web.faq_entries(None) == []


@pytest.mark.parametrize("diamonds,stone,flagged", [(15, 2500, True), (14, 2000, False), (15, 2501, False),
                                                    (15, 1999, False), (30, 5000, True)])
def test_xray_hint_edges(app, db, server, two_players, diamonds, stone, flagged):
    """Diamonds: at least 15 ore, 2000 stone and 6 ore per 1000 stone."""
    db.update_player_stats(two_players[1], {"stats": {"minecraft:mined": {
        "minecraft:diamond_ore": diamonds, "minecraft:stone": stone}}})
    with app.app_context():
        names = [h["name"] for h in web.xray_hints(server["id"]) if "diamonds" in h["flags"]]
    assert ("Notch" in names) == flagged


def test_safe_next_path_edges():
    for bad in ("//evil.example", "/\\evil.example", "/\t/evil.example", "https://evil.example", "", None,
                "/a\nb", "/\x7f"):
        assert web.safe_next_path(bad) == "/", bad
    assert web.safe_next_path("/rangliste?zeitraum=7") == "/rangliste?zeitraum=7"
