"""The addon Discoveries: places a player discovers by entering them (see java plugin/addons/Discoveries).

The places live in the addon's settings (server_addons.config["areas"]); the discoveries in the table discoveries.
"""
import re

ADDON_ID = "discoveries"
# the "name" of a place in the settings; its lower case form is the id (advancement key, discoveries.place)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
PLACE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def places(addon):
    """
    The active places of a server in the order of the settings: [{"id", "title", "description", "icon"}]. Empty if the
    addon is missing or not active. Like the plugin: invalid names and repeated ids are left out, so are disabled places.
    """
    if not addon or not addon.get("active"):
        return []
    result, seen = [], set()
    for row in (addon.get("config") or {}).get("areas") or []:
        name = str(row.get("name") or "").strip()
        if not NAME_RE.match(name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        if row.get("enabled", True) is False:
            continue
        result.append({"id": name.lower(), "title": str(row.get("title") or name).strip() or name,
                       "description": str(row.get("description") or "").strip(),
                       "icon": str(row.get("icon") or "").strip().lower() or "filled_map"})
    return result
