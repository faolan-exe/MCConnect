"""A server's own icons (database/glyphs.py IconSet, /users/icons, prefixes with :name:, per-server pack)."""
import io
import json
import zipfile

import pytest

from database import glyphs, rewards
from tests.conftest import PLAYER_UUID
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_rewards import me  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

LOGO = ["#ff0000" if i % 2 else "" for i in range(81)]
LOGO_CHAR = chr(glyphs.CUSTOM_FIRST)


@pytest.fixture
def mod(me, db, server):
    db.set_moderator(server["id"], "_Tobias4444", True)
    return me


def add_logo(db, server, name="logo", fallback="★"):
    return db.add_server_icon(server["id"], name, fallback, LOGO, glyphs.CUSTOM_FIRST)


# ------------------------------------------------------------------ checks and the icon set

def test_icon_checks():
    assert glyphs.check_pixels(LOGO) == (LOGO, None)
    assert glyphs.check_pixels(["#FF0000"] * 81)[0][0] == "#ff0000"
    assert glyphs.check_pixels([""] * 81)[1] and glyphs.check_pixels(["red"] * 81)[1] and glyphs.check_pixels([])[1]
    assert glyphs.check_icon(" Logo ", "") == ("logo", glyphs.DEFAULT_FALLBACK, None)
    assert glyphs.check_icon("crown", "x")[2]  # a built-in name
    assert glyphs.check_icon("a", "x")[2] and glyphs.check_icon("mit leer", "x")[2]
    for bad in ("ab", "|", ":", "\ue000"):
        assert glyphs.check_icon("logo", bad)[2], bad
    assert glyphs.check_icon("logo", " ")[1] == glyphs.DEFAULT_FALLBACK  # empty: the default


def test_icon_set_tokens_fallbacks_and_pack():
    icons = glyphs.IconSet([{"id": 1, "codepoint": glyphs.CUSTOM_FIRST, "name": "logo", "fallback": "★", "pixels": LOGO}])
    assert icons.from_tokens(":logo: Bau :nope:") == f"{LOGO_CHAR} Bau :nope:"
    assert icons.to_tokens(f"{LOGO_CHAR} Bau") == ":logo: Bau"
    assert icons.fallback_text(f"{LOGO_CHAR}{glyphs.char('crown')}") == "★♔"
    assert icons.glyph_map().endswith(f",{LOGO_CHAR}★")
    data, sha1 = icons.pack()
    assert sha1 != glyphs.BUILTIN.pack()[1]
    archive = zipfile.ZipFile(io.BytesIO(data))
    font = json.loads(archive.read("assets/minecraft/font/default.json"))
    assert font["providers"][-1]["chars"] == [LOGO_CHAR]
    assert archive.read(f"assets/mcconnect/textures/font/own_{glyphs.CUSTOM_FIRST:x}.png").startswith(b"\x89PNG")
    assert glyphs.rasterize(glyphs.pixel_svg(LOGO)) == glyphs.pixel_rows(LOGO)  # the website shows the same pixels


# ------------------------------------------------------------------ database

def test_codepoints_are_never_given_out_twice(db, server, two_players):
    first = add_logo(db, server)
    db.delete_server_icon(server["id"], first)
    add_logo(db, server, "logo2")
    assert [i["codepoint"] for i in db.get_server_icons(server["id"])] == [glyphs.CUSTOM_FIRST + 1]


def test_deleting_an_icon_gives_prefixes_its_fallback(db, server, two_players):
    a, _ = two_players
    icon = add_logo(db, server)
    db.save_own_prefix(a, f"{LOGO_CHAR}Bau", "aqua")
    assert db.delete_server_icon(server["id"], icon) == [PLAYER_UUID]
    assert db.get_player_prefix(a)["text"] == "★Bau"
    assert db.delete_server_icon(server["id"], icon) is None


# ------------------------------------------------------------------ website

def test_moderators_draw_icons(mod, db, server):
    assert "Eigene Icons" in mod.get("/users/icons", **on("testdomain")).get_data(as_text=True)
    response = mod.post("/api/mod/icons", json={"name": "logo", "fallback": "★", "pixels": LOGO}, **on("testdomain"))
    assert response.status_code == 200 and response.json["icons"][0]["url"] == f"/icons/{glyphs.CUSTOM_FIRST}.svg"
    icon_id = response.json["icons"][0]["id"]
    assert mod.post("/api/mod/icons", json={"name": "logo", "pixels": LOGO}, **on("testdomain")).status_code == 409
    assert mod.post("/api/mod/icons", json={"name": "x", "pixels": LOGO}, **on("testdomain")).status_code == 400
    changed = mod.post("/api/mod/icons", json={"id": icon_id, "name": "wappen", "fallback": "", "pixels": LOGO},
                       **on("testdomain"))
    assert changed.json["icons"][0]["name"] == "wappen" and changed.json["icons"][0]["fallback"] == glyphs.DEFAULT_FALLBACK
    svg = mod.get(f"/icons/{glyphs.CUSTOM_FIRST}.svg", **on("testdomain"))
    assert svg.mimetype == "image/svg+xml" and 'fill="#ff0000"' in svg.get_data(as_text=True)
    assert mod.post("/api/mod/icons", json={"delete": icon_id}, **on("testdomain")).json["icons"] == []
    assert mod.get(f"/icons/{glyphs.CUSTOM_FIRST}.svg", **on("testdomain")).status_code == 404
    assert [e["action"] for e in db.get_mod_log(server["id"])][:3] == ["icons"] * 3


def test_only_moderators_draw_icons(me):
    assert me.get("/users/icons", **on("testdomain")).status_code == 403
    assert me.post("/api/mod/icons", json={"name": "logo", "pixels": LOGO}, **on("testdomain")).status_code == 403


def test_icon_limit(mod, db, server):
    for n in range(glyphs.MAX_CUSTOM):
        add_logo(db, server, f"icon{n}")
    assert mod.post("/api/mod/icons", json={"name": "zuviel", "pixels": LOGO}, **on("testdomain")).status_code == 400


def test_prefix_with_an_icon(me, db, server, two_players):
    a, _ = two_players
    add_logo(db, server)
    html = me.get("/add_pref", **on("testdomain")).get_data(as_text=True)
    assert 'data-token=":logo:"' in html
    assert me.post("/api/prefix/save", json={"text": ":logo: Bauteam", "color": "aqua"}, **on("testdomain")).status_code == 200
    assert db.get_player_prefix(a)["text"] == f"{LOGO_CHAR} Bauteam"
    assert 'value=":logo: Bauteam"' in me.get("/add_pref", **on("testdomain")).get_data(as_text=True)
    assert f'src="/icons/{glyphs.CUSTOM_FIRST}.svg"' in me.get("/teams", **on("testdomain")).get_data(as_text=True)
    too_long = {"text": ":logo:" + "x" * 16, "color": "aqua"}
    assert me.post("/api/prefix/save", json=too_long, **on("testdomain")).status_code == 400
    unknown = {"text": ":nope: Bau", "color": "aqua"}
    assert me.post("/api/prefix/save", json=unknown, **on("testdomain")).status_code == 400


def test_own_icons_as_join_symbols(mod, db, server, two_players):
    a, _ = two_players
    add_logo(db, server)
    levels = rewards.levels_of(None)
    levels[0]["unlocks"]["symbols"].append(LOGO_CHAR)
    assert mod.post("/api/mod/rewards", json={"levels": levels}, **on("testdomain")).status_code == 200
    response = mod.post("/api/joinstyle", json={"field": "symbol", "value": LOGO_CHAR}, **on("testdomain"))
    assert response.status_code == 200 and f"/icons/{glyphs.CUSTOM_FIRST}.svg" in response.json["join"]
    assert rewards.join_style_message(db, a).split("|")[1].startswith(f"&f{LOGO_CHAR} ")


def test_server_pack_download(client, db, server):
    add_logo(db, server)
    data, sha1 = glyphs.icon_set(db, server["id"]).pack()
    response = client.get(f"/resourcepack/{server['id']}/{sha1}.zip", base_url="http://mc.test")
    assert response.status_code == 200 and response.data == data
    assert client.get(f"/resourcepack/{server['id']}/{glyphs.BUILTIN.pack()[1]}.zip",
                      base_url="http://mc.test").status_code == 404


# ------------------------------------------------------------------ plugin protocol

def test_icons_reach_the_plugin(db, server, plugin, two_players, socket_server):
    a, _ = two_players
    add_logo(db, server)
    db.save_own_prefix(a, f"{LOGO_CHAR}Bau", "aqua")
    connection = plugin().auth(server["key"])
    assert connection.recv_prefix() == f"!prefix~{PLAYER_UUID}|aqua|★Bau"  # features not known yet: fallbacks
    connection.send("!FEATURES~click,glyphs")
    assert connection.recv() == "success|106"
    assert connection.recv_prefix() == f"!prefix~{PLAYER_UUID}|aqua|{LOGO_CHAR}Bau"  # again, with the icon
    assert connection.pack.endswith(f"|{glyphs.icon_set(db, server['id']).glyph_map()}")
    assert f"/resourcepack/{server['id']}/" in connection.pack

    icon = db.get_server_icons(server["id"])[0]["id"]
    uuids = db.delete_server_icon(server["id"], icon)
    socket_server.handle_server_event({"server_id": server["id"], "type": "icons", "uuids": uuids})
    assert connection.recv_prefix() == f"!prefix~{PLAYER_UUID}|aqua|★Bau"
    assert connection.pack.endswith(f"/resourcepack/{server['id']}/{glyphs.BUILTIN.pack()[1]}.zip|"
                                    f"{glyphs.BUILTIN.pack()[1]}|{glyphs.glyph_map()}")
