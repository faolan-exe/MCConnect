"""Icons (database/glyphs.py, resource pack) and the line under the names (database/badges.py, /profil, !badge)."""
import io
import json
import zipfile

import pytest

from database import badges, glyphs, rewards
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_ingame import until
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_rewards import game, me  # noqa: F401 (fixtures)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

CROWN = glyphs.char("crown")


# ------------------------------------------------------------------ icons and pack

@pytest.mark.parametrize("key", list(glyphs.ICONS))
def test_every_icon_is_a_drawn_9x9_pixel_image(key):
    with open(glyphs.svg_path(key), encoding="utf-8") as file:
        pixels = glyphs.rasterize(file.read())
    assert len(pixels) == glyphs.SIZE and all(len(row) == glyphs.SIZE for row in pixels)
    assert sum(1 for row in pixels for pixel in row if pixel[3]) > 15
    assert glyphs.icon_png(key).startswith(b"\x89PNG\r\n\x1a\n")


def test_icons_use_the_private_use_area_and_other_fallbacks():
    chars = [c for c, _, _ in glyphs.ICONS.values()]
    assert len(set(chars)) == len(chars) and all(0xE000 <= ord(c) <= 0xF8FF for c in chars)
    assert all(not glyphs.ICON_RE.search(fallback) and fallback not in ",|~" for _, fallback, _ in glyphs.ICONS.values())
    assert glyphs.fallback_text(f"&f{CROWN} Krone") == "&f♔ Krone"


def test_resource_pack_is_stable_and_complete():
    data, sha1 = glyphs.build_pack()
    glyphs.build_pack.cache_clear()
    assert glyphs.build_pack() == (data, sha1)  # same icons, same bytes: clients keep their cached copy
    archive = zipfile.ZipFile(io.BytesIO(data))
    meta = json.loads(archive.read("pack.mcmeta"))["pack"]
    assert meta["min_format"] <= meta["pack_format"] <= meta["max_format"]
    font = json.loads(archive.read("assets/minecraft/font/default.json"))
    assert [p["chars"][0] for p in font["providers"]] == [c for c, _, _ in glyphs.ICONS.values()]
    for provider in font["providers"]:
        assert provider["ascent"] <= provider["height"]
        archive.read("assets/mcconnect/textures/font/" + provider["file"].split("/")[-1])


def test_resource_pack_download(client):
    data, sha1 = glyphs.build_pack()
    response = client.get(f"/resourcepack/{sha1}.zip", base_url="http://mc.test")
    assert response.status_code == 200 and response.data == data and response.mimetype == "application/zip"
    assert client.get("/resourcepack/" + "0" * 40 + ".zip", base_url="http://mc.test").status_code == 404


def test_join_icons_are_white_and_unlocked_by_levels():
    style = dict(rewards.DEFAULT_STYLE, symbol=CROWN, color="aqua", style="frame")
    line = rewards.render("Steve", style, rewards.unlocked(rewards.DEFAULT_LEVELS, 5))
    assert line.startswith(f"&f{CROWN} ") and line.endswith(f" &f{CROWN}")  # the game tints glyphs with the text color
    assert CROWN in rewards.unlocked(rewards.DEFAULT_LEVELS, 5)["symbols"]
    assert CROWN not in rewards.unlocked(rewards.DEFAULT_LEVELS, 4)["symbols"]
    assert rewards.SYMBOLS[:10] == ("", "•", "✦", "★", "❖", "⚔", "☀", "❤", "✪", "♛")  # indices used by /joinmessage


def test_preview_shows_icons_as_images():
    from web.main import create_app, mc_html
    with create_app().test_request_context():
        html = str(mc_html(f"&f{CROWN} &b<x>"))
    assert '<img class="mc-glyph" src="/static/glyphs/crown.svg" alt="Krone"' in html and "&lt;x&gt;" in html


# ------------------------------------------------------------------ under the name

def test_badge_choice():
    assert badges.chosen(None) == badges.DEFAULT
    assert badges.chosen(["streak", "gone", "streak"]) == ["streak"]
    assert badges.check(None) == (None, None)
    assert badges.check(["hours", "hours", "days"]) == (["hours", "days"], None)
    assert badges.check(["nope"])[1] and badges.check(list(badges.BADGES))[1]
    assert badges.render(["trophies", "hours", "level"], {"trophies": 3, "hours": 1250}) == \
        f"&f{glyphs.char('trophy')}&f3 &f{glyphs.char('clock')}&f1.250h"


def test_line_respects_hidden_stats(db, two_players):
    a, _ = two_players
    assert badges.line(db, a) == f"&f{glyphs.char('trophy')}&f0 &f{glyphs.char('flame')}&f1 &f{glyphs.char('level')}&f1"  # played today: streak 1
    db.set_name_badges(a, [])
    assert badges.line(db, a) == ""
    db.set_name_badges(a, ["hours"])
    assert badges.line(db, a).endswith("h")
    db.save_profile(a, "", True)
    assert badges.line(db, a) == ""


def test_choose_badges_on_the_profile(me, db, two_players):
    a, _ = two_players
    html = me.get("/profil", **on("testdomain")).get_data(as_text=True)
    assert "Unter deinem Namen" in html and 'data-badge="streak" aria-pressed="true"' in html
    response = me.post("/api/badges", json={"keys": ["hours", "trophies"]}, **on("testdomain"))
    assert response.status_code == 200 and response.json["keys"] == ["hours", "trophies"]
    assert "clock.svg" in response.json["preview"] and db.get_name_badges(a) == ["hours", "trophies"]
    assert me.post("/api/badges", json={"keys": list(badges.BADGES)}, **on("testdomain")).status_code == 400
    assert me.post("/api/badges", json={"keys": []}, **on("testdomain")).json["preview"] == ""
    me.post("/api/badges", json={"keys": None}, **on("testdomain"))
    assert db.get_name_badges(a) is None


# ------------------------------------------------------------------ plugin protocol

def badge_of(connection, uuid):
    """The newest !badge line of a player (waits for it)."""
    for _ in range(40):
        found = [m for m in connection.badges if m.startswith(f"!badge~{uuid}|")]
        if found:
            return found[-1]
        connection.recv(skip_heartbeats=False)
    raise AssertionError(f"no !badge for {uuid}")


def test_join_sends_the_line_under_the_name(game):
    # no "glyphs" feature announced: the Unicode fallbacks instead of the icons
    assert badge_of(game, PLAYER_UUID) == f"!badge~{PLAYER_UUID}|&f♕&f0 &f✹&f1 &f▲&f1"
    assert badge_of(game, OTHER_UUID).startswith(f"!badge~{OTHER_UUID}|&f♕")


def test_plugins_with_glyphs_get_the_pack_and_the_icons(db, server, plugin, two_players):
    a, _ = two_players
    db.raise_reward_level(a, 5)
    db.set_join_style(a, dict(rewards.DEFAULT_STYLE, symbol=CROWN))
    connection = plugin().auth(server["key"])
    assert "&f♔ " in until(connection, f"!joinstyle~{PLAYER_UUID}")[-1]  # before !FEATURES: fallbacks
    connection.send("!FEATURES~click,glyphs")
    assert f"&f{CROWN} " in until(connection, f"!joinstyle~{PLAYER_UUID}")[-1]  # sent again with the icon
    _, sha1 = glyphs.build_pack()
    assert connection.pack.startswith("!pack~http") and connection.pack.endswith(
        f"/resourcepack/{sha1}.zip|{sha1}|{glyphs.glyph_map()}")
    assert connection.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    assert badge_of(connection, PLAYER_UUID) == (f"!badge~{PLAYER_UUID}|&f{glyphs.char('trophy')}&f0 "
                                                 f"&f{glyphs.char('flame')}&f1 &f{glyphs.char('level')}&f6")


def test_changing_the_badges_updates_the_game(db, server, game, two_players, socket_server):
    a, _ = two_players
    badge_of(game, PLAYER_UUID)
    game.badges.clear()
    db.set_name_badges(a, [])
    socket_server.handle_server_event({"server_id": server["id"], "type": "badge", "player_id": str(a)})
    assert badge_of(game, PLAYER_UUID) == f"!badge~{PLAYER_UUID}|"
    game.badges.clear()
    socket_server.update_badge(server["id"], str(a))  # unchanged: nothing is sent
    assert game.request(f"!JOIN~{OTHER_UUID}|Notch") == "success|101"
    badge_of(game, OTHER_UUID)
    assert not [m for m in game.badges if m.startswith(f"!badge~{PLAYER_UUID}")]
