"""Classification of vanilla minecraft statistics into MCConnect categories.

A player's stats file (world/stats/<uuid>.json) looks like
{"stats": {"minecraft:mined": {"minecraft:stone": 12, ...}, ...}}.
Every (stat type, object) pair is mapped to one numeric category that is
stored in actions.category. See sampleData/layout.txt.
"""
import json
import re

# --- category ids (stored in the database, do not renumber) ---
TOOL_CRAFTED, ARMOR_CRAFTED, BLOCK_CRAFTED, BLOCK_PICKED_UP = 0, 1, 2, 3
ITEM_CRAFTED, ARMOR_PICKED_UP, TOOL_PICKED_UP, BLOCK_DROPPED = 4, 5, 6, 7
ITEM_PICKED_UP, ARMOR_DROPPED, TOOL_DROPPED, MOB_KILLED_BY = 8, 9, 10, 11
MOB_KILLED, BLOCK_USED, ITEM_DROPPED, TOOL_BROKEN = 12, 13, 14, 15
ARMOR_BROKEN, BLOCK_MINED, ITEM_USED, ARMOR_USED = 16, 17, 18, 19
TOOL_USED, CUSTOM = 20, 21

ARMOR_CATEGORIES = (ARMOR_USED, ARMOR_BROKEN, ARMOR_DROPPED, ARMOR_PICKED_UP, ARMOR_CRAFTED)
TOOL_CATEGORIES = (TOOL_USED, TOOL_BROKEN, TOOL_DROPPED, TOOL_PICKED_UP, TOOL_CRAFTED)
ITEM_CATEGORIES = (ITEM_USED, ITEM_DROPPED, ITEM_PICKED_UP, ITEM_CRAFTED)
BLOCK_CATEGORIES = (BLOCK_MINED, BLOCK_USED, BLOCK_DROPPED, BLOCK_PICKED_UP, BLOCK_CRAFTED)
MOB_CATEGORIES = (MOB_KILLED, MOB_KILLED_BY)
CUSTOM_CATEGORIES = (CUSTOM,)

STAT_TYPES = (
    "minecraft:broken", "minecraft:mined", "minecraft:dropped", "minecraft:used",
    "minecraft:killed", "minecraft:crafted", "minecraft:killed_by",
    "minecraft:custom", "minecraft:picked_up",
)

# Minecraft resource locations ("minecraft:stone"); anything else is not stored (the names end up in the web pages).
OBJECT_NAME_RE = re.compile(r"^(?:[a-z0-9_.-]{1,64}:)?[a-z0-9_./-]{1,128}$")
MAX_VALUE = 2 ** 63 - 1  # bigint

TOOL_SUFFIXES = ("_axe", "_pickaxe", "_shovel", "_hoe", "_sword")
TOOL_NAMES = {
    "bow", "crossbow", "shield", "flint_and_steel", "brush", "trident", "shears",
    "fishing_rod", "mace", "carrot_on_a_stick", "warped_fungus_on_a_stick",
}
ARMOR_SUFFIXES = ("_boots", "_leggings", "_chestplate", "_helmet")
ARMOR_NAMES = {"elytra"}

_GROUP_CATEGORIES = {
    "tool": {
        "minecraft:broken": TOOL_BROKEN, "minecraft:dropped": TOOL_DROPPED,
        "minecraft:used": TOOL_USED, "minecraft:crafted": TOOL_CRAFTED,
        "minecraft:picked_up": TOOL_PICKED_UP,
    },
    "armor": {
        "minecraft:broken": ARMOR_BROKEN, "minecraft:dropped": ARMOR_DROPPED,
        "minecraft:used": ARMOR_USED, "minecraft:crafted": ARMOR_CRAFTED,
        "minecraft:picked_up": ARMOR_PICKED_UP,
    },
    "block": {
        "minecraft:mined": BLOCK_MINED, "minecraft:dropped": BLOCK_DROPPED,
        "minecraft:used": BLOCK_USED, "minecraft:crafted": BLOCK_CRAFTED,
        "minecraft:picked_up": BLOCK_PICKED_UP,
    },
    "item": {
        "minecraft:dropped": ITEM_DROPPED, "minecraft:used": ITEM_USED,
        "minecraft:crafted": ITEM_CRAFTED, "minecraft:picked_up": ITEM_PICKED_UP,
    },
}
_TYPE_CATEGORIES = {
    "minecraft:killed": MOB_KILLED,
    "minecraft:killed_by": MOB_KILLED_BY,
    "minecraft:custom": CUSTOM,
    # Only blocks can be mined, so unknown objects (e.g. from newer game
    # versions than blocks.json) still end up in the right place.
    "minecraft:mined": BLOCK_MINED,
}


def strip_namespace(name):
    return name.split(":", 1)[1] if ":" in name else name


def item_group(name, blocks, items):
    """Return "tool", "armor", "block", "item" or None for a minecraft id."""
    name = strip_namespace(name)
    if name in TOOL_NAMES or name.endswith(TOOL_SUFFIXES):
        return "tool"
    if name in ARMOR_NAMES or name.endswith(ARMOR_SUFFIXES):
        return "armor"
    if name in blocks:
        return "block"
    if name in items:
        return "item"
    return None


def categorize(name, stat_type, blocks, items):
    """Map an object and its stat type to a category id, or None if it has no place."""
    if stat_type in ("minecraft:killed", "minecraft:killed_by", "minecraft:custom"):
        return _TYPE_CATEGORIES[stat_type]
    group = item_group(name, blocks, items)
    if group is not None:
        category = _GROUP_CATEGORIES[group].get(stat_type)
        if category is not None:
            return category
    if stat_type in _TYPE_CATEGORIES:
        return _TYPE_CATEGORIES[stat_type]
    # Unknown object: treat it as a plain item.
    return _GROUP_CATEGORIES["item"].get(stat_type)


def split_stats(stats, blocks, items):
    """Parse a stats file (json string or dict) into [(object, category, value), ...].

    Zero values and objects without a category are skipped, so are invalid names and values.
    Raises ValueError if the file is not a stats object.
    """
    if isinstance(stats, str):
        stats = json.loads(stats)
    all_stats = stats.get("stats", {}) if isinstance(stats, dict) else None
    if not isinstance(all_stats, dict):
        raise ValueError("stats must be an object with a \"stats\" object")
    result = []
    for stat_type in STAT_TYPES:
        values = all_stats.get(stat_type, {})
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if not value or not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not OBJECT_NAME_RE.match(name) or not 0 < value <= MAX_VALUE:
                continue
            category = categorize(name, stat_type, blocks, items)
            if category is not None:
                result.append((name, category, int(value)))
    return result


def format_time(seconds):
    """Format seconds as e.g. "2 Tage 3 Std." / "5 Min. 3 Sec.".

    Minutes are only shown below one day, seconds only below one hour.
    """
    if not seconds:
        return "0 Sec."
    days = int(seconds // 86400)
    seconds %= 86400
    hours = int(seconds // 3600)
    seconds %= 3600
    minutes = int(seconds // 60)
    seconds = int(seconds % 60)

    parts = []
    if days > 0:
        parts.append(f"{days} Tag{'e' if days > 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} Std.")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes} Min.")
    if seconds > 0 and days == 0 and hours == 0:
        parts.append(f"{seconds} Sec.")
    return " ".join(parts) or "0 Sec."
