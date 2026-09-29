"""What players show under their name in the game (below-name scoreboard line, plugin 3.17 on Paper 1.20.3+).

Every player picks up to MAX_BADGES values (icon + number, e.g. trophies and the current streak) or none; players
who chose nothing show DEFAULT. Players who hide their stats (hide_stats) show nothing. The line uses the icons
of database/glyphs.py; the plugin shows their Unicode fallbacks to players without the resource pack.
"""
from datetime import datetime, timezone

from database import glyphs, rewards

# key: (icon, label on the website, short description)
BADGES = {
    "trophies": ("trophy", "Trophäen", "Wettbewerbsplätze und Spieler der Woche"),
    "streak": ("flame", "Serie", "Tage in Folge online (aktuell)"),
    "level": ("level", "Stufe", "deine Belohnungsstufe der Join-Nachrichten"),
    "tiers": ("medal", "Erfolge", "erreichte Erfolgsstufen"),
    "hours": ("clock", "Spielzeit", "Stunden"),
    "days": ("calendar", "Dabei seit", "Tage seit deinem ersten Join"),
}
DEFAULT = ["trophies", "streak", "level"]
MAX_BADGES = 4


def chosen(stored):
    """The keys a player shows: their choice (known keys only) or the default."""
    if stored is None:
        return list(DEFAULT)
    return [key for key in dict.fromkeys(stored) if key in BADGES][:MAX_BADGES]


def check(keys):
    """(keys, None) for a valid choice (list, None = default), otherwise (None, error)."""
    if keys is None:
        return None, None
    if not isinstance(keys, list) or any(key not in BADGES for key in keys):
        return None, "Unbekannte Auswahl."
    keys = list(dict.fromkeys(keys))
    if len(keys) > MAX_BADGES:
        return None, f"Höchstens {MAX_BADGES} – mehr passt nicht lesbar unter einen Namen."
    return keys, None


def values(db, player_id, keys):
    """{key: number} of the chosen badges (the level only while join messages are on)."""
    result = {}
    metrics = None
    for key in keys:
        if key == "trophies":
            result[key] = len(db.get_player_trophies(player_id))
        elif key == "streak":
            result[key] = db.get_player_streak(player_id)["current"]
        elif key == "tiers":
            result[key] = len(db.get_player_achievements(player_id))
        elif key == "level":
            state = rewards.player_state(db, player_id)
            if state["enabled"]:
                result[key] = state["level"] + 1
        elif key == "hours":
            metrics = metrics if metrics is not None else db.get_player_metrics(player_id)
            result[key] = int(metrics.get("play_time", 0) / 20 / 3600)
        elif key == "days":
            first = db.get_first_join(player_id)
            result[key] = (datetime.now(timezone.utc) - first).days if first else 0
    return result


def render(keys, numbers):
    """The line with "&" color codes: white icons, each followed by its number."""
    parts = []
    for key in keys:
        if key in numbers:
            suffix = "h" if key == "hours" else ""
            parts.append(f"&f{glyphs.char(BADGES[key][0])}&f{numbers[key]:,}{suffix}".replace(",", "."))
    return " ".join(parts)


def line(db, player_id):
    """The line under the player's name ("" = nothing)."""
    profile = db.get_profile(player_id)
    if profile is None or profile["hide_stats"]:
        return ""
    keys = chosen(db.get_name_badges(player_id))
    return render(keys, values(db, player_id, keys)) if keys else ""


def message(uuid, text):
    """!badge for the plugin."""
    return f"!badge~{uuid}|{rewards.clean(text)}"
