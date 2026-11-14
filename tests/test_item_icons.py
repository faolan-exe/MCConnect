"""Icons of newer blocks and items from the texture atlas of misode/mcmeta (web/item_icons.py)."""
import io
import json
import os
import time

import pytest
from PIL import Image

from tests.test_web import app, client, on, online_player  # noqa: F401 (fixtures)
from web import item_icons, skins

AXE = (180, 100, 60, 255)
BRICKS = (90, 90, 80, 255)
URL = item_icons.ATLAS_URL


def atlas(entries):
    """(index json bytes, atlas png bytes) with one 16x16 square per entry {key: color}; "animated" keys get 2 frames."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    index = {}
    for n, (key, color) in enumerate(entries.items()):
        x = (n % 4) * 16
        height = 32 if "animated" in key else 16
        for px in range(x, x + 16):
            for py in range(0, height):
                image.putpixel((px, py), color if py < 16 else (1, 1, 1, 255))
        index[key] = [x, 0, 16, height]
    out = io.BytesIO()
    image.save(out, "PNG")
    return json.dumps(index).encode(), out.getvalue()


ITEMS = atlas({"item/copper_axe": AXE})
BLOCKS = atlas({"block/tuff_bricks": BRICKS, "block/oak_planks": BRICKS, "block/cut_copper": BRICKS,
                "block/magma_animated": BRICKS})


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(item_icons, "_memory", {})
    monkeypatch.setattr(item_icons, "_icons", {})
    calls = []

    def install(answers):
        def get(url):
            calls.append(url)
            return answers.get(url)
        monkeypatch.setattr(skins, "_get", get)
        return calls
    return install


def internet(items=ITEMS, blocks=BLOCKS):
    return {URL.format(kind="items", file="data.json"): items[0], URL.format(kind="items", file="atlas.png"): items[1],
            URL.format(kind="blocks", file="data.json"): blocks[0], URL.format(kind="blocks", file="atlas.png"): blocks[1]}


def color(png, xy=(0, 0)):
    return Image.open(io.BytesIO(png)).convert("RGBA").getpixel(xy)


def test_item_icon_from_the_items_atlas(web):
    web(internet())
    assert item_icons.resolve("copper_axe") == ("items", "item/copper_axe")
    png = item_icons.icon_png("copper_axe")
    assert Image.open(io.BytesIO(png)).size == (16, 16) and color(png) == AXE


@pytest.mark.parametrize("item_id, key", [
    ("tuff_brick_stairs", "block/tuff_bricks"),      # brick -> bricks
    ("oak_stairs", "block/oak_planks"),              # wood -> planks
    ("waxed_cut_copper_slab", "block/cut_copper"),   # waxed looks like plain
    ("waxed_cut_copper", "block/cut_copper"),
])
def test_block_shapes_use_the_texture_of_their_material(web, item_id, key):
    web(internet())
    assert item_icons.resolve(item_id) == ("blocks", key)


def test_unknown_ids_have_no_icon(web):
    web(internet())
    assert item_icons.resolve("copper_golem_statue") is None and item_icons.icon_png("copper_golem_statue") is None


def test_animated_textures_give_their_first_frame(web):
    web(internet())
    png = item_icons.icon_png("magma_animated")
    assert Image.open(io.BytesIO(png)).size == (16, 16) and color(png, (0, 15)) == BRICKS


@pytest.mark.parametrize("bad_index", [b"<html>", b"[]", b'{"item/x": [0, 0, 16]}', b'{"item/x": [-1, 0, 16, 16]}', b""])
def test_broken_index_is_not_stored(web, bad_index):
    web(internet(items=(bad_index, ITEMS[1])))
    assert item_icons.resolve("copper_axe") is None
    assert not os.path.exists(skins._path("atlas", "items.json"))


def test_atlas_that_is_no_png_is_not_stored(web):
    web(internet(items=(ITEMS[0], b"GIF89a....")))
    assert item_icons.icon_png("copper_axe") is None
    assert not os.path.exists(skins._path("atlas", "items.png"))


def test_files_are_fetched_once_and_refreshed_in_the_background(web):
    calls = web(internet())
    item_icons.icon_png("copper_axe")
    item_icons.has_icon("oak_stairs")
    fetched = len(calls)
    item_icons.has_icon("copper_axe")
    assert len(calls) == fetched  # fresh: no new request
    path = skins._path("atlas", "items.json")
    old = time.time() - item_icons.FRESH - 1
    os.utime(path, (old, old))
    calls = web({})  # GitHub down: the old file stays in use, the page does not wait
    assert item_icons.has_icon("copper_axe")


# ------------------------------------------------------------------ routes and pages

def test_icon_route(client, server, web):
    web(internet())
    response = client.get("/mc/item/copper_axe.png", **on("testdomain"))
    assert response.status_code == 200 and response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "public, max-age=86400"
    assert client.get("/mc/item/copper_axe.png", **on(None)).status_code == 200
    assert client.get("/mc/item/no_such_thing.png", **on("testdomain")).status_code == 404
    assert client.get("/mc/item/Copper-Axe.png", **on("testdomain")).status_code == 404


def test_only_ids_without_sprite_get_atlas_icons(web):
    from web.main import atlas_icons
    web(internet(blocks=atlas({"block/stone": BRICKS})))
    assert atlas_icons({"copper_axe", "stone", "no_such_thing"}) == ["copper_axe"]  # stone is in the sprite


def test_player_page_knows_which_stats_have_atlas_icons(client, db, online_player, web):
    web(internet())
    db.update_player_stats(online_player, {"stats": {"minecraft:used": {"minecraft:copper_axe": 3}}})
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert 'var atlas_icons = new Set(["copper_axe"]);' in html  # stone (mined) is in the sprite
    assert "/mc/item/${escapeHtml(itemId)}.png" in html
