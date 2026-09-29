"""Join/leave messages and the reward levels that unlock their colors, symbols, styles, texts and sounds.

A level is reached when ANY of its conditions is met (e.g. a 365 day streak or 500 Ancient Debris). What a
level unlocks adds up with the levels below it. A reached level is stored and never lost again (a broken
streak keeps its colors). Moderators and OPs can also use MOD_COLORS. Every server can edit its levels
(servers.reward_levels, None = DEFAULT_LEVELS).

A player's choice (player_server_info.join_style) is a dict with the keys of STYLE_FIELDS; render() turns it
into the chat lines with "&" color codes that the plugin shows.
"""
import copy
import re

from database import metrics, motivation

# Minecraft chat colors: code and web color (for the preview). Players unlock COLORS, MOD_COLORS are for
# moderators and OPs only.
COLOR_CODES = {
    "white": ("f", "#FFFFFF"), "gray": ("7", "#AAAAAA"), "green": ("a", "#55FF55"), "aqua": ("b", "#55FFFF"),
    "blue": ("9", "#5555FF"), "yellow": ("e", "#FFFF55"), "dark_aqua": ("3", "#00AAAA"),
    "light_purple": ("d", "#FF55FF"), "dark_green": ("2", "#00AA00"), "dark_purple": ("5", "#AA00AA"),
    "gold": ("6", "#FFAA00"), "red": ("c", "#FF5555"), "dark_red": ("4", "#AA0000"),
}
COLOR_LABELS = {
    "white": "Weiß", "gray": "Grau", "green": "Grün", "aqua": "Türkis", "blue": "Blau", "yellow": "Gelb",
    "dark_aqua": "Petrol", "light_purple": "Pink", "dark_green": "Dunkelgrün", "dark_purple": "Lila",
    "gold": "Gold", "red": "Rot", "dark_red": "Dunkelrot",
}
MOD_COLORS = ("gold", "red", "dark_red")
COLORS = tuple(c for c in COLOR_CODES if c not in MOD_COLORS)
SYMBOLS = ("", "•", "✦", "★", "❖", "⚔", "☀", "❤", "✪", "♛")
STYLES = {"normal": "Normal", "bold": "Fett", "frame": "Mit Rahmen (Symbol links und rechts)",
          "rainbow": "Regenbogen (alle freigeschalteten Farben)"}
JOIN_TEXTS = {
    "joined": "{name} ist da.", "back": "{name} ist zurück!", "hello": "{name} sagt Hallo!",
    "landed": "{name} ist gelandet.", "adventure": "{name} sucht das nächste Abenteuer.",
    "tools": "{name} hat die Werkzeuge dabei.", "make_way": "Macht Platz – {name} ist da!",
    "legend": "Die Legende {name} ist zurück!",
}
LEAVE_TEXTS = {
    "left": "{name} ist weg.", "bye": "{name} sagt Tschüss.", "later": "{name} kommt bald wieder.",
    "rest": "{name} macht Feierabend.", "legend_bye": "Die Legende {name} verabschiedet sich.",
}
# key: (label, Minecraft sound, volume); from quiet to striking
SOUNDS = {
    "chime": ("Glöckchen (leise)", "minecraft:block.note_block.chime", 0.35),
    "bell": ("Glocke", "minecraft:block.note_block.bell", 0.45),
    "orb": ("Erfahrung", "minecraft:entity.experience_orb.pickup", 0.5),
    "levelup": ("Levelaufstieg", "minecraft:entity.player.levelup", 0.5),
    "fanfare": ("Fanfare", "minecraft:ui.toast.challenge_complete", 0.6),
}
UNLOCK_KINDS = {"colors": COLORS, "symbols": SYMBOLS, "styles": tuple(STYLES), "join_texts": tuple(JOIN_TEXTS),
                "leave_texts": tuple(LEAVE_TEXTS), "sounds": tuple(SOUNDS)}
# Own texts of a server (servers.reward_texts: {"join": {key: text}, "leave": {key: text}}), keys "c1", "c2", ...
CUSTOM_TEXT_MAX = 80
CUSTOM_TEXTS_PER_KIND = 30
CUSTOM_TEXT_RE = re.compile(r"^[^&§|~\u27e6\u21d2\u27e7\x00-\x1f]{1,80}$")


class Catalog:
    """The options of a server: the fixed palettes plus its own join and leave texts."""

    def __init__(self, custom=None):
        custom = custom or {}
        self.custom = {"join": dict(custom.get("join") or {}), "leave": dict(custom.get("leave") or {})}
        self.texts = {"join_texts": dict(JOIN_TEXTS, **self.custom["join"]),
                      "leave_texts": dict(LEAVE_TEXTS, **self.custom["leave"])}
        self.kinds = dict(UNLOCK_KINDS, join_texts=tuple(self.texts["join_texts"]),
                          leave_texts=tuple(self.texts["leave_texts"]))

    def text(self, kind, key):
        """The template of a join ("join_texts") or leave ("leave_texts") text, None if unknown."""
        return self.texts[kind].get(key)


DEFAULT_CATALOG = Catalog()


def check_custom_text(text):
    """(cleaned text, None) for an own join/leave text, or (None, error). It needs "{name}" exactly once."""
    text = " ".join(str(text or "").split())
    if text.count("{name}") != 1:
        return None, "Der Text braucht genau einmal {name} – dort steht dann der Spielername."
    if not 5 <= len(text) <= CUSTOM_TEXT_MAX or not CUSTOM_TEXT_RE.match(text):
        return None, f"5-{CUSTOM_TEXT_MAX} Zeichen, ohne & § | ~ (Farben kommen aus der Auswahl der Spieler)."
    return text, None

# condition types: label, unit
CONDITIONS = {
    "streak": ("Serie (Tage in Folge online, beste)", "Tage"),
    "tiers": ("Erfolgsstufen", "Stufen"),
    "play_hours": ("Spielzeit", "Stunden"),
    "days": ("Dabei seit", "Tagen"),
    "trophies": ("Trophäen (Wettbewerbsplätze, Spieler der Woche)", "Trophäen"),
    "metric": ("Kennzahl", ""),
}

DEFAULT_LEVELS = [
    {"name": "Neuling", "conditions": [],
     "unlocks": {"colors": ["white", "gray"], "symbols": [""], "styles": ["normal"],
                 "join_texts": ["joined", "hello"], "leave_texts": ["left", "bye"], "sounds": []}},
    {"name": "Stammgast", "conditions": [{"type": "streak", "value": 7}, {"type": "play_hours", "value": 10},
                                         {"type": "tiers", "value": 5}],
     "unlocks": {"colors": ["green", "aqua"], "symbols": ["•", "✦"], "styles": ["bold"],
                 "join_texts": ["back", "landed"], "leave_texts": ["later"], "sounds": []}},
    {"name": "Abenteurer", "conditions": [{"type": "streak", "value": 30}, {"type": "play_hours", "value": 50},
                                          {"type": "tiers", "value": 15}, {"type": "trophies", "value": 1}],
     "unlocks": {"colors": ["blue", "yellow"], "symbols": ["★"], "styles": [],
                 "join_texts": ["adventure", "tools"], "leave_texts": ["rest"], "sounds": ["chime"]}},
    {"name": "Veteran", "conditions": [{"type": "streak", "value": 60}, {"type": "play_hours", "value": 150},
                                       {"type": "tiers", "value": 25}, {"type": "days", "value": 180}],
     "unlocks": {"colors": ["dark_aqua", "light_purple"], "symbols": ["❖", "⚔"], "styles": ["frame"],
                 "join_texts": [], "leave_texts": [], "sounds": ["bell", "orb"]}},
    {"name": "Held", "conditions": [{"type": "streak", "value": 100}, {"type": "play_hours", "value": 400},
                                    {"type": "tiers", "value": 35}, {"type": "trophies", "value": 5}],
     "unlocks": {"colors": ["dark_green", "dark_purple"], "symbols": ["☀", "❤"], "styles": ["rainbow"],
                 "join_texts": ["make_way"], "leave_texts": [], "sounds": ["levelup"]}},
    {"name": "Legende", "conditions": [{"type": "streak", "value": 365}, {"type": "play_hours", "value": 1500},
                                       {"type": "metric", "metric": "ancient_debris", "value": 500}],
     "unlocks": {"colors": [], "symbols": ["✪", "♛"], "styles": [],
                 "join_texts": ["legend"], "leave_texts": ["legend_bye"], "sounds": ["fanfare"]}},
]
MAX_LEVELS = 10
LEVEL_NAME_RE = re.compile(r"^[A-Za-z0-9ÄÖÜäöüß _.!?+*#()'-]{2,24}$")

# a player's choice
STYLE_FIELDS = ("join_text", "leave_text", "color", "symbol", "style", "sound")
DEFAULT_STYLE = {"join_text": "joined", "leave_text": "left", "color": "white", "symbol": "", "style": "normal",
                 "sound": ""}


def levels_of(stored):
    """The levels of a server: its own (servers.reward_levels) or the default ones."""
    return copy.deepcopy(stored) if stored else copy.deepcopy(DEFAULT_LEVELS)


def condition_met(condition, facts):
    if condition["type"] == "metric":
        return facts["metrics"].get(condition.get("metric"), 0) >= metrics_raw(condition)
    return facts.get(condition["type"], 0) >= condition["value"]


def metrics_raw(condition):
    """The raw value of a metric condition (entered in the unit of the goals, e.g. hours or km)."""
    metric = metrics.METRICS_BY_KEY.get(condition.get("metric"))
    return motivation.goal_target(metric, condition["value"]) if metric else float("inf")


def reached_level(levels, facts):
    """Index of the highest level with a met condition (level 0 has none and is always reached).
    Levels count on their own: a higher level can be reached without the one below it."""
    reached = 0
    for index, level in enumerate(levels):
        if index == 0 or any(condition_met(c, facts) for c in level["conditions"]):
            reached = index
    return reached


def unlocked(levels, level, moderator=False, catalog=DEFAULT_CATALOG):
    """{kind: [values]} of everything unlocked up to (including) the level (only options that still exist)."""
    result = {kind: [] for kind in catalog.kinds}
    for entry in levels[:level + 1]:
        for kind in catalog.kinds:
            for value in entry["unlocks"].get(kind, []):
                if value not in result[kind] and value in catalog.kinds[kind]:
                    result[kind].append(value)
    if moderator:
        result["colors"] += [c for c in MOD_COLORS if c not in result["colors"]]
    for kind, order in catalog.kinds.items():  # in the order of the palettes
        result[kind].sort(key=lambda value, order=order: order.index(value) if value in order else len(order))
    if moderator:
        result["colors"] = [c for c in result["colors"] if c not in MOD_COLORS] + list(MOD_COLORS)
    return result


def unlocked_at(levels, catalog=DEFAULT_CATALOG):
    """{(kind, value): level index} of the first level that unlocks each option (for "locked" hints)."""
    first = {}
    for index, level in enumerate(levels):
        for kind in catalog.kinds:
            for value in level["unlocks"].get(kind, []):
                first.setdefault((kind, value), index)
    return first


def normalized_style(style, available):
    """The player's choice, with every value that is not (or no longer) available replaced by a default."""
    style = dict(DEFAULT_STYLE, **(style or {}))
    for field, kind in (("join_text", "join_texts"), ("leave_text", "leave_texts"), ("color", "colors"),
                        ("symbol", "symbols"), ("style", "styles"), ("sound", "sounds")):
        options = available[kind]
        if style[field] not in options and not (field == "sound" and style[field] == ""):
            style[field] = options[0] if options else DEFAULT_STYLE[field]
    return style


def check_choice(field, value, available, catalog=DEFAULT_CATALOG):
    """None if the value may be chosen, otherwise the error text."""
    kind = {"join_text": "join_texts", "leave_text": "leave_texts", "color": "colors", "symbol": "symbols",
            "style": "styles", "sound": "sounds"}.get(field)
    if kind is None:
        return "Unbekannte Einstellung."
    if field == "sound" and value == "":
        return None
    if value not in catalog.kinds[kind] and not (kind == "colors" and value in MOD_COLORS):
        return "Unbekannte Auswahl."
    if value not in available[kind]:
        return "Das ist noch nicht freigeschaltet."
    return None


def render_name(name, style, available):
    code = COLOR_CODES[style["color"]][0]
    if style["style"] == "rainbow":
        colors = [COLOR_CODES[c][0] for c in available["colors"] if c not in ("white", "gray")] or [code]
        return "".join(f"&{colors[i % len(colors)]}{letter}" for i, letter in enumerate(name))
    return f"&{code}{'&l' if style['style'] == 'bold' else ''}{name}"


def render(name, style, available, kind="join", catalog=DEFAULT_CATALOG):
    """The chat line of a join ("join") or leave ("leave") message with "&" color codes."""
    template = (catalog.text("join_texts", style["join_text"]) or JOIN_TEXTS["joined"] if kind == "join"
                else catalog.text("leave_texts", style["leave_text"]) or LEAVE_TEXTS["left"])
    rendered = render_name(name, style, available) + "&r&7"
    before, _, after = template.partition("{name}")
    symbol = style["symbol"]
    code = COLOR_CODES[style["color"]][0]
    line = f"&7{before}{rendered}{after}"
    if symbol:
        line = f"&{code}{symbol} {line}" + (f" &{code}{symbol}" if style["style"] == "frame" else "")
    return line


def sound_of(style):
    """(Minecraft sound, volume) of the choice, or None."""
    entry = SOUNDS.get(style.get("sound") or "")
    return (entry[1], entry[2]) if entry else None


def validate_levels(data, catalog=DEFAULT_CATALOG):
    """(levels, None) for a valid level list from the editor, otherwise (None, error text)."""
    if not isinstance(data, list) or not 1 <= len(data) <= MAX_LEVELS:
        return None, f"1 bis {MAX_LEVELS} Stufen."
    levels = []
    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            return None, "Ungültige Stufe."
        name = " ".join(str(raw.get("name") or "").split())
        if not LEVEL_NAME_RE.match(name):
            return None, f"Stufe {index + 1}: Der Name muss 2-24 Zeichen lang sein."
        conditions = []
        for condition in raw.get("conditions") or []:
            kind = condition.get("type") if isinstance(condition, dict) else None
            if kind not in CONDITIONS:
                return None, f"Stufe {index + 1}: unbekannte Bedingung."
            try:
                value = float(condition.get("value"))
            except (TypeError, ValueError):
                return None, f"Stufe {index + 1}: Bedingungen brauchen eine Zahl."
            if not 0 < value < 10 ** 9:
                return None, f"Stufe {index + 1}: Die Zahl muss größer als 0 sein."
            entry = {"type": kind, "value": int(value) if value == int(value) else value}
            if kind == "metric":
                if condition.get("metric") not in metrics.METRICS_BY_KEY:
                    return None, f"Stufe {index + 1}: unbekannte Kennzahl."
                entry["metric"] = condition["metric"]
            conditions.append(entry)
        if index > 0 and not conditions:
            return None, f"Stufe {index + 1} braucht mindestens eine Bedingung (die erste Stufe hat jeder)."
        if len(conditions) > 8:
            return None, f"Stufe {index + 1}: höchstens 8 Bedingungen."
        unlocks = {}
        for kind, options in catalog.kinds.items():
            values = (raw.get("unlocks") or {}).get(kind) or []
            if not isinstance(values, list) or any(v not in options for v in values):
                return None, f"Stufe {index + 1}: unbekannte Freischaltung."
            unlocks[kind] = list(dict.fromkeys(values))
        levels.append({"name": name, "conditions": [] if index == 0 else conditions, "unlocks": unlocks})
    first = unlocked(levels, 0, catalog=catalog)
    if not (first["colors"] and first["join_texts"] and first["leave_texts"] and first["styles"] and first["symbols"]):
        return None, "Die erste Stufe braucht mindestens eine Farbe, ein Symbol (oder keins), einen Stil und je einen Join- und Leave-Text."
    return levels, None


def condition_text(condition):
    """"30 Tage Serie", "500 Ancient Debris" ..."""
    value = f"{condition['value']:,}".replace(",", ".")
    if condition["type"] == "metric":
        metric = metrics.METRICS_BY_KEY.get(condition.get("metric"))
        if metric is None:
            return "unbekannte Kennzahl"
        unit = motivation.goal_unit(metric)
        return f"{value}{' ' + unit if unit else ''} {metric.label}"
    return {"streak": f"{value} Tage Serie", "tiers": f"{value} Erfolgsstufen", "play_hours": f"{value} Std. Spielzeit",
            "days": f"{value} Tage dabei", "trophies": f"{value} Trophäen"}[condition["type"]]


def level_hint(level):
    """How a level is reached: "30 Tage Serie oder 50 Std. Spielzeit"."""
    return " oder ".join(condition_text(c) for c in level["conditions"]) or "von Anfang an"


# ------------------------------------------------------------------ with the database

def player_state(db, player_id):
    """Everything about a player's rewards: {"enabled", "levels", "level", "new_level" (just reached, or None),
    "available", "style" (normalized choice; the default one if the player chose nothing), "sounds_off", "moderator"}.
    A level reached for the first time is stored here."""
    server_id = db.get_server_id_from_player_id(player_id)
    enabled, stored, texts = db.get_reward_settings(server_id)
    catalog = Catalog(texts)
    levels = levels_of(stored)
    settings = db.get_join_settings(player_id)
    level = min(max(settings["level"], reached_level(levels, db.get_reward_facts(player_id))), len(levels) - 1)
    new_level = level if level > settings["level"] and db.raise_reward_level(player_id, level) else None
    moderator = db.is_moderator(player_id)
    available = unlocked(levels, level, moderator, catalog)
    return {"enabled": enabled, "levels": levels, "level": level, "new_level": new_level, "available": available,
            "catalog": catalog,
            "style": normalized_style(settings["style"], available),
            "sounds_off": settings["sounds_off"], "moderator": moderator}


def join_style_message(db, player_id, state=None):
    """!joinstyle for the plugin: the player's join and leave line and sound (empty lines: the plugin's default
    lines from !joindefault, or the game's own message while the feature is off)."""
    state = state or player_state(db, player_id)
    uuid = db.get_mojang_uuid_from_player_id(player_id)
    if not state["enabled"]:
        return f"!joinstyle~{uuid}||||"
    name = db.get_player_name_from_player_id(player_id)
    sound = sound_of(state["style"])
    return "!joinstyle~" + "|".join((str(uuid), clean(render(name, state["style"], state["available"], "join", state["catalog"])),
                                     clean(render(name, state["style"], state["available"], "leave", state["catalog"])),
                                     sound[0] if sound else "", str(sound[1]) if sound else ""))


def join_default_message(stored_levels, texts):
    """!joindefault for the plugin: the lines of a player who chose nothing (and of a brand-new one the plugin
    does not know yet), with "{name}" for the plugin to fill in. The default choice of the first level."""
    catalog = Catalog(texts)
    levels = levels_of(stored_levels)
    available = unlocked(levels, 0, catalog=catalog)
    style = normalized_style(None, available)
    return "!joindefault~" + "|".join((clean(render("{name}", style, available, "join", catalog)),
                                       clean(render("{name}", style, available, "leave", catalog))))


def join_mutes_message(uuid, sounds_off, muted_uuids):
    """!joinmutes for the plugin: whose messages a player does not see, and whether they hear the sounds."""
    return f"!joinmutes~{uuid}|{1 if sounds_off else 0}|{','.join(muted_uuids)}"


def clean(line):
    """Protocol separators out of a rendered line (the parts are fixed texts and Minecraft names anyway)."""
    return line.replace("|", "/").replace("~", "-").replace("\n", " ")
