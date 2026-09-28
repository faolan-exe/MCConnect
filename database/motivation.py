"""Streaks, anniversaries, records, community goals and fun facts: thresholds and texts.

The data lives in player_milestones, record_history, community_goals and trophies
(see DatabaseManager); this module only holds the rules and the German texts.
"""
from . import metrics

# Badges for days online in a row.
STREAK_MILESTONES = (7, 30, 100)
# Records are kept for every metric where more is better (not for deaths).
RECORD_METRICS = tuple(m for m in metrics.METRICS if not m.lower_is_better)
# Competition places that get a trophy, with their color (gold, silver, bronze).
PLACE_COLORS = {1: "#c9a227", 2: "#8d969b", 3: "#b0703c"}


def years_text(years):
    return "1 Jahr" if years == 1 else f"{years} Jahren"


def milestone_label(kind, value):
    """Badge text, e.g. "7-Tage-Serie" or "2 Jahre dabei"."""
    if kind == "streak":
        return f"{value}-Tage-Serie"
    return "1 Jahr dabei" if value == 1 else f"{value} Jahre dabei"


def milestone_announcement(name, kind, value):
    """(chat color, text) for a new streak badge or anniversary."""
    if kind == "streak":
        return "gold", f"★ {name} war {value} Tage in Folge online – Serie!"
    return "gold", f"★ {name} ist seit {years_text(value)} auf dem Server – danke fürs Mitspielen!"


def record_announcement(name, metric, previous, value):
    text = f"★ Neuer Rekord! {name} hat {previous} bei »{metric.label}« überholt: {metrics.format_value(metric, value)}"
    return "gold", text


def goal_announcement(goal):
    metric = metrics.METRICS_BY_KEY[goal["metric"]]
    return "gold", (f"★ Gemeinschaftsziel »{goal['title']}« geschafft! Alle zusammen: "
                    f"{format_goal_value(metric, goal['target'])} {metric.label}")


def player_of_week_announcement(trophy):
    return "gold", f"★ Spieler der Woche ({trophy['title']}): {trophy['name']} mit {trophy['detail']}!"


# ------------------------------------------------------------------ community goal units

# Goals are entered in these units (the database stores the raw unit of the metric).
_GOAL_UNITS = {"time": ("Stunden", 20 * 3600), "distance": ("km", 100_000), "damage": ("Herzen", 20), "count": ("", 1)}


def goal_unit(metric):
    """Unit label for the goal form, e.g. "Stunden" ("" for plain counts)."""
    return _GOAL_UNITS[metric.unit][0]


def format_goal_value(metric, value):
    """A goal value in the goal unit, e.g. "381 Std." or "12,5 km" (all values of a goal in one unit)."""
    if metric.unit == "time":  # below 10 hours with a decimal: 21 minutes are "0,4 Std.", not "0 Std."
        hours = value / (20 * 3600)
        return f"{metrics._number(hours, 1 if 0 < hours < 10 and hours != int(hours) else 0)} Std."
    if metric.unit == "distance":
        return f"{metrics._number(value / 100_000, 1)} km"
    return metrics.format_value(metric, value)


def goal_target(metric, amount):
    """Raw target value from an amount in the goal unit."""
    return int(round(amount * _GOAL_UNITS[metric.unit][1]))


# ------------------------------------------------------------------ fun facts

EARTH_KM = 40_075
MOON_KM = 384_400
MARATHON_KM = 42.195
CHUNK_BLOCKS = 16 * 16 * 384  # overworld from bedrock to the build limit
ARMOR_DIAMONDS = 24  # helmet 5, chestplate 8, leggings 7, boots 4
DEBRIS_PER_INGOT = 4  # netherite scrap per ingot
JUMP_METERS = 1.25
EVEREST_METERS = 8_849
MC_DAY_TICKS = 24_000  # 20 minutes


def _n(value, decimals=0):
    return metrics._number(value, decimals)


def _times(value):
    """"2,5-mal" / "12-mal" (one decimal below 10)."""
    return f"{_n(value, 1 if value < 10 else 0)}-mal"


def fun_facts(totals, player_count, top_block=None, top_killer=None, top_mob=None):
    """
    Facts about all players together: [{"value", "label", "text"}]. totals are the summed metric
    values (raw units); top_* are (object, count) of the most mined block, the most frequent cause
    of death and the most killed mob, or None.
    """
    facts = []
    km = totals.get("distance", 0) / 100_000
    if km >= 1:
        if km >= MOON_KM:
            text = f"Weiter als bis zum Mond ({_n(MOON_KM)} km)!"
        elif km >= EARTH_KM:
            text = f"Das reicht {_times(km / EARTH_KM)} um die Erde."
        elif km >= MARATHON_KM:
            text = f"Das sind {_n(km / MARATHON_KM)} Marathons."
        else:
            text = f"Das sind {_n(km * 1000 / 400)} Runden auf einer 400-m-Bahn."
        facts.append({"value": metrics.format_distance(totals["distance"]), "label": "zurückgelegt", "text": text})

    ticks = totals.get("play_time", 0)
    if ticks >= MC_DAY_TICKS:
        facts.append({"value": metrics.format_value(metrics.METRICS_BY_KEY["play_time"], ticks), "label": "gespielt",
                      "text": f"Das sind {_n(ticks // MC_DAY_TICKS)} Minecraft-Tage (je 20 Minuten)."})

    mined = totals.get("blocks_mined", 0)
    if mined:
        text = (f"Genug, um {_times(mined / CHUNK_BLOCKS)} einen ganzen Chunk bis zum Bedrock auszuhöhlen."
                if mined >= CHUNK_BLOCKS / 10 else "Jeder Block zählt!")
        if top_block:
            text += f" Am häufigsten: {metrics.object_label(top_block[0])}."
        facts.append({"value": _n(mined), "label": "Blöcke abgebaut", "text": text})

    diamonds = totals.get("diamonds", 0)
    if diamonds:
        sets = diamonds // ARMOR_DIAMONDS
        text = (f"Genug für {_n(sets)} komplette Diamantrüstung{'en' if sets != 1 else ''}."
                if sets else f"Noch {ARMOR_DIAMONDS - diamonds} bis zur ersten kompletten Diamantrüstung.")
        facts.append({"value": _n(diamonds), "label": "Diamanterz abgebaut", "text": text})

    debris = totals.get("ancient_debris", 0)
    if debris:
        ingots = debris // DEBRIS_PER_INGOT
        text = (f"Das ergibt {_n(ingots)} Netherite-Barren." if ingots
                else f"Noch {DEBRIS_PER_INGOT - debris} bis zum ersten Netherite-Barren.")
        facts.append({"value": _n(debris), "label": "Ancient Debris", "text": text})

    jumps = totals.get("jumps", 0)
    if jumps:
        meters = jumps * JUMP_METERS
        text = (f"Übereinander {_n(meters / 1000, 1)} km hoch – {_times(meters / EVEREST_METERS)} der Mount Everest."
                if meters >= EVEREST_METERS else f"Übereinander {_n(meters)} m hoch.")
        facts.append({"value": _n(jumps), "label": "Sprünge", "text": text})

    kills = totals.get("mob_kills", 0)
    if kills:
        text = f"Am häufigsten erwischt hat es: {metrics.object_label(top_mob[0])}." if top_mob else "Die Nächte sind sicherer geworden."
        facts.append({"value": _n(kills), "label": "Mobs besiegt", "text": text})

    deaths = totals.get("deaths", 0)
    if deaths and player_count:
        text = f"Im Schnitt {_n(deaths / player_count, 1)} pro Spieler."
        if top_killer:
            text += f" Gefährlichster Gegner: {metrics.object_label(top_killer[0])}."
        facts.append({"value": _n(deaths), "label": "Tode", "text": text})

    fish = totals.get("fish_caught", 0)
    if fish:
        facts.append({"value": _n(fish), "label": "Fische geangelt",
                      "text": "Petri Heil!" if fish < 100 else f"Das sind {_n(fish / max(1, player_count), 1)} pro Spieler."})
    return facts
