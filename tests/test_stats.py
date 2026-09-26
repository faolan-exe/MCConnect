import json
import os

import pytest

from database import stats
from database.stats import categorize, format_time, item_group, split_stats

BLOCKS = {"stone", "dirt", "waxed_copper_block", "oak_log"}
ITEMS = {"bowl", "stick", "diamond"}

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sampleData", "4ebe5f6f-c231-4315-9d60-097c48cc6d30.json")


@pytest.mark.parametrize("name, group", [
    ("minecraft:diamond_pickaxe", "tool"),
    ("minecraft:stone_axe", "tool"),
    ("minecraft:bow", "tool"),
    ("minecraft:fishing_rod", "tool"),
    ("minecraft:iron_helmet", "armor"),
    ("minecraft:elytra", "armor"),
    ("minecraft:stone", "block"),
    ("minecraft:diamond", "item"),
    ("minecraft:something_new", None),
    # substring false positives of the old implementation
    ("minecraft:waxed_copper_block", "block"),  # contains "axe"
    ("minecraft:bowl", "item"),                  # contains "bow"
    ("minecraft:stick", "item"),
])
def test_item_group(name, group):
    assert item_group(name, BLOCKS, ITEMS) == group


@pytest.mark.parametrize("name, stat_type, category", [
    ("minecraft:iron_pickaxe", "minecraft:broken", stats.TOOL_BROKEN),
    ("minecraft:iron_pickaxe", "minecraft:crafted", stats.TOOL_CRAFTED),
    ("minecraft:iron_boots", "minecraft:used", stats.ARMOR_USED),
    ("minecraft:stone", "minecraft:mined", stats.BLOCK_MINED),
    ("minecraft:stone", "minecraft:used", stats.BLOCK_USED),
    ("minecraft:diamond", "minecraft:picked_up", stats.ITEM_PICKED_UP),
    ("minecraft:zombie", "minecraft:killed", stats.MOB_KILLED),
    ("minecraft:creeper", "minecraft:killed_by", stats.MOB_KILLED_BY),
    ("minecraft:play_time", "minecraft:custom", stats.CUSTOM),
    # unknown objects (newer game versions) still get a sensible category
    ("minecraft:new_block", "minecraft:mined", stats.BLOCK_MINED),
    ("minecraft:new_thing", "minecraft:used", stats.ITEM_USED),
    # an item cannot be "mined" -> treated as block
    ("minecraft:diamond", "minecraft:mined", stats.BLOCK_MINED),
])
def test_categorize(name, stat_type, category):
    assert categorize(name, stat_type, BLOCKS, ITEMS) == category


def test_split_stats_skips_zero_values_and_unknown_types():
    data = {"stats": {
        "minecraft:mined": {"minecraft:stone": 5, "minecraft:dirt": 0},
        "minecraft:custom": {"minecraft:deaths": 3},
        "minecraft:unknown_type": {"minecraft:stone": 1},
    }}
    assert sorted(split_stats(json.dumps(data), BLOCKS, ITEMS)) == [
        ("minecraft:deaths", stats.CUSTOM, 3),
        ("minecraft:stone", stats.BLOCK_MINED, 5),
    ]


def test_split_stats_sample_file_has_valid_categories():
    with open(SAMPLE) as f:
        entries = split_stats(f.read(), BLOCKS, ITEMS)
    assert len(entries) > 100
    assert {category for _, category, _ in entries} <= set(range(22))
    # every (object, category) pair is unique, as required by the primary key
    assert len({(name, category) for name, category, _ in entries}) == len(entries)


@pytest.mark.parametrize("seconds, text", [
    (0, "0 Sec."),
    (None, "0 Sec."),
    (5, "5 Sec."),
    (65, "1 Min. 5 Sec."),
    (3 * 3600 + 12 * 60 + 7, "3 Std. 12 Min."),
    (86400 + 3600 + 60, "1 Tag 1 Std."),
    (2 * 86400 + 30, "2 Tage"),
])
def test_format_time(seconds, text):
    assert format_time(seconds) == text
