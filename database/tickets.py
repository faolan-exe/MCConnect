"""The addon Tickets: players open tickets with /ticket, moderators answer them on the website (page /tickets).

The plugin side is in java plugin/addons/Tickets; the tickets live in the tables tickets and ticket_events.
"""
import re

ADDON_ID = "tickets"
# the same rule as the plugin (Tickets.CATEGORY)
CATEGORY_RE = re.compile(r"^[a-z0-9_-]{1,20}$")
REQUEST_RE = re.compile(r"^[0-9a-f]{32}$")
MIN_TEXT = 5
MAX_TEXT = 500
MAX_REPLY = 500
DEFAULT_MAX_OPEN = 3
# what moderators can do with a ticket; each one is told to the player in the chat
EVENT_KINDS = ("reply", "closed", "reopened")


def categories(addon):
    """The valid categories of the addon's settings in their order, lower case and without repeats ([] if inactive)."""
    if not addon or not addon.get("active"):
        return []
    result = []
    for line in (addon.get("config") or {}).get("categories") or []:
        category = str(line or "").strip().lower()
        if CATEGORY_RE.match(category) and category not in result:
            result.append(category)
    return result


def max_open(addon):
    """Open tickets a player may have at the same time."""
    try:
        value = int((addon.get("config") or {}).get("max_open", DEFAULT_MAX_OPEN))
    except (TypeError, ValueError):
        return DEFAULT_MAX_OPEN
    return max(1, value)


def notify_mods(addon):
    return (addon.get("config") or {}).get("notify_mods", True) is not False


def clean_text(text, limit):
    """One line of free text: whitespace collapsed, control characters removed, cut to the limit."""
    text = "".join(c for c in str(text or "") if c >= " " or c == "\t")
    return " ".join(text.split())[:limit]
