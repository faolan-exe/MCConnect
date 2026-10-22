"""Block and item registry: new Minecraft versions, mods and data packs (database/registry.py)."""
import json

import pytest
import requests

from database import registry, stats
from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)

NEW_BLOCK = "test_registry_block"  # stands for a block of a Minecraft version newer than the lookup tables
NEW_ITEM = "test_registry_item"


@pytest.fixture
def clean_lookups(db):
    """The global lists of the shared test database without the names of these tests (also afterwards)."""
    def clean():
        with db._cursor() as cur:
            cur.execute("DELETE FROM block_lookup WHERE name LIKE 'test_registry_%'")
            cur.execute("DELETE FROM item_lookup WHERE name LIKE 'test_registry_%'")
        db._load_lookups()
    clean()
    yield
    clean()


def used(name, value):
    return {"stats": {"minecraft:used": {name: value}}}


def snapshots(db, player_id, metric="blocks_placed"):
    rows = db._fetchall("SELECT day - current_date, value FROM stat_snapshots WHERE player_id = %s AND metric = %s "
                        "ORDER BY day", (player_id, metric))
    return [tuple(row) for row in rows]


def category_of(db, player_id, name):
    return db._fetchvalue("SELECT category FROM actions WHERE player_id = %s AND object = %s", (player_id, name))


# ------------------------------------------------------------------ names

@pytest.mark.parametrize("name, lookup", [
    ("minecraft:stone", "stone"),
    ("stone", "stone"),
    ("create:brass_block", "create:brass_block"),  # a mod's block is not the vanilla one of the same name
    ("minecraft:stone_axe", "stone_axe"),
])
def test_lookup_name(name, lookup):
    assert stats.lookup_name(name) == lookup
    assert registry.object_name(lookup) == name if ":" in name else "minecraft:" + name


@pytest.mark.parametrize("name, group", [
    ("create:brass_block", "block"),
    ("brass_block", None),               # the vanilla name of the same path is not the mod's block
    ("minecraft:brass_block", None),
    ("create:wrench", "item"),
    ("create:brass_pickaxe", "tool"),    # tools and armor of mods by their name, as in vanilla
    ("create:copper_helmet", "armor"),
])
def test_item_group_of_mod_names(name, group):
    assert stats.item_group(name, {"stone", "create:brass_block"}, {"create:wrench"}) == group


def test_normalize_drops_invalid_names_and_keeps_blocks_out_of_items():
    blocks, items = registry.normalize(
        ["minecraft:stone", "create:brass_block", "Bad Name", 7, "x" * 200],
        ["minecraft:stone", "minecraft:stick", "create:wrench", None])
    assert blocks == {"stone", "create:brass_block"}
    assert items == {"stick", "create:wrench"}  # stone is a block


@pytest.mark.parametrize("blocks, items", [(None, []), ([], "stone"), ({"stone": 1}, [])])
def test_normalize_needs_lists(blocks, items):
    with pytest.raises(ValueError):
        registry.normalize(blocks, items)


def test_normalize_limit():
    registry.normalize(["a"] * registry.MAX_NAMES, [])
    with pytest.raises(ValueError):
        registry.normalize(["a"] * registry.MAX_NAMES, ["b"])


@pytest.mark.parametrize("value", ["not json", "[]", '{"blocks": "stone", "items": []}', '{"items": []}'])
def test_parse_report_rejects(value):
    with pytest.raises(ValueError):
        registry.parse_report(value)


def test_parse_report():
    blocks, items, version = registry.parse_report(json.dumps(
        {"version": "26.3-R0.1", "blocks": ["minecraft:acacia_shelf", "create:brass_block"], "items": ["minecraft:stick"]}))
    assert (blocks, items, version) == ({"acacia_shelf", "create:brass_block"}, {"stick"}, "26.3-R0.1")
    assert registry.parse_report('{"blocks": [], "items": []}')[2] is None


def test_bundled_registry_is_newer_than_the_base_schema():
    blocks, items, version = registry.bundled()
    assert version
    assert {"stone", "acacia_shelf", "copper_chest"} <= blocks  # copper_chest and the shelves came with 1.21.9
    assert "stick" in items and "stone" not in items


# ------------------------------------------------------------------ download

class FakeResponse:
    def __init__(self, status, data=None, etag=None):
        self.status_code, self._data, self.headers = status, data, {"ETag": etag} if etag else {}

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    def __init__(self, response):
        self.response, self.requests = response, []

    def get(self, url, timeout, headers):
        self.requests.append((url, headers))
        return self.response


def test_fetch_registry():
    session = FakeSession(FakeResponse(200, {"block": ["stone", "icicle"], "item": ["stone", "stick"], "biome": []},
                                       etag='"abc"'))
    assert registry.fetch("https://example.com/r.json", session=session) == ({"stone", "icicle"}, {"stick"}, '"abc"')
    assert session.requests == [("https://example.com/r.json", {})]


def test_fetch_registry_unchanged():
    session = FakeSession(FakeResponse(304))
    assert registry.fetch("https://example.com/r.json", etag='"abc"', session=session) is None
    assert session.requests[0][1] == {"If-None-Match": '"abc"'}


@pytest.mark.parametrize("response, error", [
    (FakeResponse(500), requests.RequestException),
    (FakeResponse(200, ValueError("no json")), ValueError),
    (FakeResponse(200, ["stone"]), ValueError),
    (FakeResponse(200, {"block": [], "item": ["stick"]}), ValueError),  # an empty registry is a broken download
])
def test_fetch_registry_errors(response, error):
    with pytest.raises(error):
        registry.fetch("https://example.com/r.json", session=FakeSession(response))


# ------------------------------------------------------------------ global lists

def test_new_block_is_categorized_as_block(db, server, clean_lookups):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.update_player_stats(player_id, used(f"minecraft:{NEW_BLOCK}", 4))
    assert category_of(db, player_id, f"minecraft:{NEW_BLOCK}") == stats.ITEM_USED  # unknown: an item
    db.add_lookup_names({NEW_BLOCK}, set())
    db.update_player_stats(player_id, used(f"minecraft:{NEW_BLOCK}", 5))
    rows = db._fetchall("SELECT category, value FROM actions WHERE player_id = %s", (player_id,))
    assert [tuple(r) for r in rows] == [(stats.BLOCK_USED, 5)]  # moved, no stale item row


def test_learned_block_moves_stored_values_without_a_gain(db, server, clean_lookups):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.update_player_stats(player_id, {"stats": {"minecraft:used": {"minecraft:stone": 10, f"minecraft:{NEW_BLOCK}": 5},
                                                 "minecraft:picked_up": {f"minecraft:{NEW_BLOCK}": 2}}})
    assert db.get_player_metrics(player_id)["blocks_placed"] == 10
    assert snapshots(db, player_id) == [(-1, 10), (0, 10)]  # stats older than MCConnect: no gain today

    assert db.add_lookup_names({NEW_BLOCK}, {NEW_ITEM}) == 1
    assert category_of(db, player_id, f"minecraft:{NEW_BLOCK}") is not None
    assert db.get_all_blocks_stats(player_id)[1] == [{"object": "minecraft:stone", "value": 10},
                                                     {"object": f"minecraft:{NEW_BLOCK}", "value": 5}]
    assert db.get_all_blocks_stats(player_id)[3] == [{"object": f"minecraft:{NEW_BLOCK}", "value": 2}]
    assert db.get_player_metrics(player_id)["blocks_placed"] == 15
    # every snapshot moved with it: still no gain today (not 5)
    assert snapshots(db, player_id) == [(-1, 15), (0, 15)]
    assert NEW_BLOCK in db.blocks and NEW_ITEM in db.items
    assert db.add_lookup_names({NEW_BLOCK}, {NEW_ITEM}) == 0  # known: nothing to do


def test_learned_block_keeps_the_higher_of_a_stale_pair(db, server, clean_lookups):
    """An item row left over next to the block row (both stored before this fix) becomes one row."""
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    obj = f"minecraft:{NEW_BLOCK}"
    with db._cursor() as cur:
        cur.execute("INSERT INTO actions (player_id, category, object, value) VALUES (%s, %s, %s, 3), (%s, %s, %s, 7)",
                    (player_id, stats.ITEM_USED, obj, player_id, stats.BLOCK_USED, obj))
    db.add_lookup_names({NEW_BLOCK}, set())
    rows = db._fetchall("SELECT category, value FROM actions WHERE player_id = %s", (player_id,))
    assert [tuple(r) for r in rows] == [(stats.BLOCK_USED, 7)]


def test_items_are_not_moved(db, server, clean_lookups):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.update_player_stats(player_id, used(f"minecraft:{NEW_ITEM}", 3))
    assert db.add_lookup_names(set(), {NEW_ITEM}) == 0
    assert category_of(db, player_id, f"minecraft:{NEW_ITEM}") == stats.ITEM_USED


def test_lookup_names_for_the_item_picker(db, clean_lookups):
    db.add_lookup_names({NEW_BLOCK}, {NEW_ITEM})
    assert {"stone", NEW_BLOCK, NEW_ITEM} <= db.get_lookup_names()


# ------------------------------------------------------------------ registry of a server

def test_server_registry_only_counts_on_that_server(db, server, other_server):
    here = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    there = db.ensure_player_on_server(other_server["id"], OTHER_UUID)
    for player_id in (here, there):
        db.update_player_stats(player_id, used("create:brass_block", 6))

    assert db.add_server_registry(server["id"], {"create:brass_block"}, {"create:wrench"}, "26.3") == 1
    assert category_of(db, here, "create:brass_block") == stats.BLOCK_USED
    assert category_of(db, there, "create:brass_block") == stats.ITEM_USED  # another server's registry
    db.update_player_stats(there, used("create:brass_block", 7))
    assert category_of(db, there, "create:brass_block") == stats.ITEM_USED
    assert db._fetchvalue("SELECT registry_version FROM servers WHERE id = %s", (server["id"],)) == "26.3"
    assert "create:brass_block" not in db.blocks  # the global lists stay vanilla


def test_server_registry_keeps_names_of_removed_mods(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.add_server_registry(server["id"], {"create:brass_block"}, set())
    assert db.add_server_registry(server["id"], {"stone"}, set()) == 0  # stone is known globally
    db._server_registry = {}  # like a restart of the socket server
    db.update_player_stats(player_id, used("create:brass_block", 2))  # still in the stats file
    assert category_of(db, player_id, "create:brass_block") == stats.BLOCK_USED


def test_server_registry_turns_an_item_into_a_block(db, server):
    db.add_server_registry(server["id"], set(), {"create:casing"})
    assert db.add_server_registry(server["id"], {"create:casing"}, set()) == 1
    assert db._fetchvalue("SELECT is_block FROM server_registry WHERE server_id = %s AND name = 'create:casing'",
                          (server["id"],)) is True


def test_plugin_reports_registry(db, server, plugin):
    client = plugin().auth(server["key"])
    report = {"version": "26.3-R0.1-SNAPSHOT", "blocks": ["minecraft:stone", "create:brass_block"],
              "items": ["create:wrench"]}
    assert client.request("!REGISTRY~" + json.dumps(report)) == "success|108"
    assert client.request(f"!STATS~{PLAYER_UUID}|{json.dumps(used('create:brass_block', 1))}") == "success|102"
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    assert category_of(db, player_id, "create:brass_block") == stats.BLOCK_USED


@pytest.mark.parametrize("value", ["not json", '{"blocks": 1, "items": []}'])
def test_plugin_registry_invalid(server, plugin, value):
    client = plugin().auth(server["key"])
    assert client.request("!REGISTRY~" + value) == "error|005"
