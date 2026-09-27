"""Achievements: a metric (database/metrics.py) with four thresholds (tiers).

Thresholds are in the raw unit of the metric (ticks, cm, ...). Earned tiers are stored
in player_achievements when the plugin sends new stats.
"""
from collections import namedtuple

from . import metrics

Achievement = namedtuple("Achievement", "key name description metric thresholds")
# (key, label, minecraft chat color of the announcement, web color)
TIERS = (
    ("bronze", "Bronze", "gold", "#b0703c"),
    ("silver", "Silber", "gray", "#8d969b"),
    ("gold", "Gold", "yellow", "#c9a227"),
    ("diamond", "Diamant", "aqua", "#2bb3b1"),
)
HOUR = 20 * 3600  # ticks
KM = 100_000  # cm

ACHIEVEMENTS = (
    Achievement("regular", "Stammgast", "Stunden gespielt", "play_time", tuple(h * HOUR for h in (10, 50, 200, 500))),
    Achievement("miner", "Bergarbeiter", "Blöcke abgebaut", "blocks_mined", (10_000, 100_000, 500_000, 1_000_000)),
    Achievement("builder", "Baumeister", "Blöcke platziert", "blocks_placed", (5_000, 50_000, 250_000, 1_000_000)),
    Achievement("hunter", "Monsterjäger", "Mobs getötet", "mob_kills", (100, 1_000, 5_000, 20_000)),
    Achievement("diamonds", "Diamantenfieber", "Diamanterz abgebaut", "diamonds", (10, 100, 500, 1_000)),
    Achievement("netherite", "Schatzsucher", "Ancient Debris abgebaut", "ancient_debris", (4, 32, 128, 512)),
    Achievement("traveller", "Weltenbummler", "km zurückgelegt", "distance", tuple(k * KM for k in (10, 100, 1_000, 5_000))),
    Achievement("pilot", "Himmelsstürmer", "km mit Elytra geflogen", "distance_elytra", tuple(k * KM for k in (10, 100, 1_000, 3_000))),
    Achievement("jumper", "Flummi", "Sprünge", "jumps", (1_000, 10_000, 50_000, 100_000)),
    Achievement("angler", "Angelprofi", "Fische gefangen", "fish_caught", (10, 100, 500, 1_000)),
    Achievement("trader", "Händler", "Handel mit Dorfbewohnern", "villager_trades", (10, 100, 500, 2_000)),
    Achievement("breeder", "Züchter", "Tiere gezüchtet", "animals_bred", (10, 100, 500, 2_000)),
    Achievement("unlucky", "Pechvogel", "Tode", "deaths", (10, 50, 100, 500)),
)
ACHIEVEMENTS_BY_KEY = {a.key: a for a in ACHIEVEMENTS}


def tier_of(achievement, value):
    """Index of the highest reached tier (0 = bronze), or -1."""
    return sum(1 for threshold in achievement.thresholds if value >= threshold) - 1


def _amount(achievement, value):
    """The value in the unit of the description (hours, km or a count)."""
    unit = metrics.METRICS_BY_KEY[achievement.metric].unit
    return value / HOUR if unit == "time" else value / KM if unit == "distance" else value


def progress(achievement, value):
    """Display data: current tier, progress to the next tier and all tiers with thresholds."""
    tier = tier_of(achievement, value)
    fmt = lambda v: metrics.format_count(int(_amount(achievement, v)))
    result = {
        "key": achievement.key, "name": achievement.name, "description": achievement.description,
        "tier": tier, "tier_label": TIERS[tier][1] if tier >= 0 else None,
        "tier_key": TIERS[tier][0] if tier >= 0 else None,
        "tiers": [{"label": label, "key": key, "color": color, "reached": i <= tier, "threshold": fmt(t)}
                  for i, ((key, label, _, color), t) in enumerate(zip(TIERS, achievement.thresholds))],
        "value": fmt(value),
    }
    if tier + 1 < len(achievement.thresholds):
        low = achievement.thresholds[tier] if tier >= 0 else 0
        high = achievement.thresholds[tier + 1]
        result["next"] = {"label": TIERS[tier + 1][1], "threshold": fmt(high),
                          "share": max(0.0, min(1.0, (value - low) / (high - low)))}
    return result


def announcement(name, achievement, tier):
    """(chat color, text) for the in-game announcement of a new tier."""
    _, label, color, _ = TIERS[tier]
    return color, f"★ {name} hat den Erfolg »{achievement.name}« ({label}) erreicht!"
