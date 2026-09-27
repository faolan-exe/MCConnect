"""Metrics for rankings and player comparison.

A metric is the sum of some stat objects of one category (or of the whole
category), e.g. "blocks mined" = SUM(value) of category BLOCK_MINED. The same
definitions are used for the live values and for the daily snapshots
(both SQL over actions, see sql_columns).
"""
from collections import namedtuple

from . import stats

# unit: "time" (ticks), "distance" (cm), "damage" (tenths of a health point), "count"
# objects: tuple of stat objects, or None for the whole category
Metric = namedtuple("Metric", "key label group category objects unit lower_is_better")


def _metric(key, label, group, category, objects=None, unit="count", lower_is_better=False):
    if objects is not None:
        objects = tuple(o if ":" in o else f"minecraft:{o}" for o in objects)
    return Metric(key, label, group, category, objects, unit, lower_is_better)


DISTANCE_OBJECTS = (
    "walk_one_cm", "sprint_one_cm", "crouch_one_cm", "swim_one_cm", "walk_on_water_one_cm",
    "walk_under_water_one_cm", "climb_one_cm", "fly_one_cm", "aviate_one_cm", "boat_one_cm",
    "horse_one_cm", "minecart_one_cm", "pig_one_cm", "strider_one_cm", "happy_ghast_one_cm",
)

# German names of the ways to move (for the "favourite way to travel" highlight).
MOVEMENT_LABELS = {
    "minecraft:walk_one_cm": "Zu Fuß", "minecraft:sprint_one_cm": "Sprinten",
    "minecraft:crouch_one_cm": "Schleichen", "minecraft:swim_one_cm": "Schwimmen",
    "minecraft:walk_on_water_one_cm": "Über Wasser", "minecraft:walk_under_water_one_cm": "Unter Wasser",
    "minecraft:climb_one_cm": "Klettern", "minecraft:fly_one_cm": "Fliegen", "minecraft:aviate_one_cm": "Elytra",
    "minecraft:boat_one_cm": "Boot", "minecraft:horse_one_cm": "Pferd", "minecraft:minecart_one_cm": "Lore",
    "minecraft:pig_one_cm": "Schwein", "minecraft:strider_one_cm": "Schreiter",
    "minecraft:happy_ghast_one_cm": "Glücklicher Ghast",
}

GROUPS = (
    ("general", "Allgemein"),
    ("movement", "Bewegung"),
    ("ores", "Erze"),
    ("misc", "Sonstiges"),
)

METRICS = (
    # play_one_minute is the name before 1.17 (it counts ticks as well)
    _metric("play_time", "Spielzeit", "general", stats.CUSTOM, ("play_time", "play_one_minute"), "time"),
    _metric("blocks_mined", "Blöcke abgebaut", "general", stats.BLOCK_MINED),
    _metric("mob_kills", "Mobs getötet", "general", stats.CUSTOM, ("mob_kills",)),
    _metric("deaths", "Tode", "general", stats.CUSTOM, ("deaths",), lower_is_better=True),

    _metric("distance", "Strecke gesamt", "movement", stats.CUSTOM, DISTANCE_OBJECTS, "distance"),
    _metric("distance_elytra", "Mit Elytra geflogen", "movement", stats.CUSTOM, ("aviate_one_cm",), "distance"),
    _metric("jumps", "Sprünge", "movement", stats.CUSTOM, ("jump",)),

    _metric("diamonds", "Diamanterz", "ores", stats.BLOCK_MINED, ("diamond_ore", "deepslate_diamond_ore")),
    _metric("ancient_debris", "Antiker Schutt", "ores", stats.BLOCK_MINED, ("ancient_debris",)),
    _metric("emeralds", "Smaragderz", "ores", stats.BLOCK_MINED, ("emerald_ore", "deepslate_emerald_ore")),
    _metric("gold", "Golderz", "ores", stats.BLOCK_MINED, ("gold_ore", "deepslate_gold_ore")),
    _metric("iron", "Eisenerz", "ores", stats.BLOCK_MINED, ("iron_ore", "deepslate_iron_ore")),

    _metric("blocks_placed", "Blöcke platziert", "misc", stats.BLOCK_USED),
    _metric("damage_dealt", "Schaden ausgeteilt", "misc", stats.CUSTOM, ("damage_dealt",), "damage"),
    _metric("damage_taken", "Schaden erlitten", "misc", stats.CUSTOM, ("damage_taken",), "damage"),
    _metric("player_kills", "Spieler getötet", "misc", stats.CUSTOM, ("player_kills",)),
    _metric("fish_caught", "Fische gefangen", "misc", stats.CUSTOM, ("fish_caught",)),
    _metric("villager_trades", "Handel mit Dorfbewohnern", "misc", stats.CUSTOM, ("traded_with_villager",)),
    _metric("animals_bred", "Tiere gezüchtet", "misc", stats.CUSTOM, ("animals_bred",)),
)
METRICS_BY_KEY = {m.key: m for m in METRICS}
METRIC_CATEGORIES = sorted({m.category for m in METRICS})

# Shown as record tiles on the server start page.
RECORD_METRICS = ("play_time", "blocks_mined", "mob_kills", "distance")


def sql_columns():
    """SQL select expressions (one per metric, in METRICS order) over actions a, and their parameters."""
    columns, params = [], []
    for m in METRICS:
        if m.objects is None:
            columns.append("COALESCE(SUM(a.value) FILTER (WHERE a.category = %s), 0)")
            params.append(m.category)
        else:
            columns.append("COALESCE(SUM(a.value) FILTER (WHERE a.category = %s AND a.object = ANY(%s)), 0)")
            params.extend([m.category, list(m.objects)])
    return columns, params


def scaled(metric, value):
    """The value in its display unit: hours, blocks (meters), hearts or a plain count."""
    if metric.unit == "time":
        return value / 20 / 3600
    if metric.unit == "distance":
        return value / 100
    if metric.unit == "damage":
        return value / 20  # tenths of a health point, one heart = 2 health points
    return value


def _number(value, decimals=0):
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def format_value(metric, value):
    """German display text, e.g. "3 Tage 4 Std.", "12,4 km", "1.203"."""
    if metric.unit == "time":
        return stats.format_time(value / 20)
    if metric.unit == "distance":
        meters = value / 100
        return f"{_number(meters / 1000, 1)} km" if meters >= 1000 else f"{_number(meters)} m"
    if metric.unit == "damage":
        return f"{_number(value / 20)} ♥"
    return _number(value)


def format_count(value):
    return _number(value)


def format_distance(cm):
    return format_value(METRICS_BY_KEY["distance"], cm)


def object_label(name):
    """"minecraft:deepslate_diamond_ore" -> "Deepslate Diamond Ore"."""
    return stats.strip_namespace(name).replace("_", " ").title()
