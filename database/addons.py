"""Addons: plugins that extend MCConnect through MCDataLink (plugin 3.22, see docs/adr/0001-addon-system.md).

An addon is a normal Bukkit plugin with a mcconnect-addon.json (id, name, summary, description, features, settings)
that registers with MCDataLink. MCDataLink reports the addons of a server (!ADDONS); the owner approves them on the
admin page (official addons from the store are approved by their checksum) and edits their settings there. The
settings description ("settings") comes from the addon, so a new addon gets its settings page without changes here.

The official addons (the store) are jars in MCC_ADDON_DIR or the local maven builds (java plugin/addons/*/target).

Settings description: a list of sections {"title", "description"?, "fields": [field, ...]}. Every field has "key",
"type", "label", optional "help", "default" and "show_if" ({"key": sibling, "is": bool} or {"key", "in": [values]},
only for the page). Types and their options:
    toggle                          default bool
    number      min, max, step, unit
    text        max, colors (& codes, preview), required, placeholders [{"name", "label", "example"}], suggest
    lines       like text, max_items (a list of texts)
    select      options [{"value", "label"}]
    multiselect options (a list of values)
    material    an item id like "glass_pane" (empty allowed unless required)
    recipe      9 item ids (3x3 grid, "" for an empty slot)
    position    {"x", "y", "z"}
    list        fields (no list inside), max_items, item_label, title_key: rows of fields
normalize() checks a settings object against the description: unknown keys are dropped, missing ones get the
default, wrong values the default (or are clamped/cut) and are reported as errors.
"""
import glob
import hashlib
import json
import os
import re
import zipfile

from database import config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The addon BetterJoinMessages shows MCConnect's join and leave messages (part of MCDataLink up to 3.21).
JOIN_ADDON = "betterjoinmessages"
# Store order and looks: id -> (Minecraft item for the icon, category)
STORE = {
    "betterjoinmessages": ("oak_sign", "Chat"),
    "spawnprotect": ("shield", "Schutz"),
    "easyheaddrop": ("player_head", "Trophäen"),
    "spawnelytra": ("elytra", "Bewegung"),
    "easynewcraftingrecipes": ("crafting_table", "Crafting"),
}

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MATERIAL_RE = re.compile(r"^[a-z0-9_]{1,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.jar$")
_VERSION_RE = re.compile(r"^version:\s*'?([0-9][0-9.]*)'?\s*$", re.MULTILINE)
_NAME_RE = re.compile(r"^name:\s*'?([A-Za-z0-9_.-]+)'?\s*$", re.MULTILINE)
# Characters that are never kept in setting texts: control characters, the legacy color sign and MCConnect's chat
# markup (buttons and tooltips are MCConnect's own; an addon's texts are shown as they are).
_TEXT_STRIP_RE = re.compile("[\x00-\x1f\x7f§⟦⟧⇒⇝]")

MAX_MANIFEST_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_SECTIONS = 12
MAX_FIELDS = 40
MAX_OPTIONS = 60
MAX_TEXT = 500
MAX_LIST_ITEMS = 200
COORDINATE_LIMIT = 30_000_000
TYPES = ("toggle", "number", "text", "lines", "select", "multiselect", "material", "recipe", "position", "list")
SUGGESTIONS = ("players", "worlds")

_cache = {}  # path -> (mtime, size, info)


class ManifestError(ValueError):
    pass


# ------------------------------------------------------------------ the store

def jar_paths():
    """Jars of the official addons: MCC_ADDON_DIR/*.jar, or the newest local maven build of each addon."""
    if config.ADDON_DIR:
        return sorted(glob.glob(os.path.join(config.ADDON_DIR, "*.jar")))
    return sorted(glob.glob(os.path.join(PROJECT_ROOT, "java plugin", "addons", "*", "target", "*.jar")))


def jar_info(path):
    """{"id", "plugin", "version", "sha256", "size", "file", "manifest", "path"} of an addon jar (cached until the file
    changes), or None if it is no valid addon."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    cached = _cache.get(path)
    if cached and cached[:2] == (stat.st_mtime, stat.st_size):
        return cached[2]
    try:
        with open(path, "rb") as file:
            data = file.read()
        with zipfile.ZipFile(path) as archive:
            plugin_yml = archive.read("plugin.yml").decode("utf-8")
            manifest = check_manifest(json.loads(archive.read("mcconnect-addon.json").decode("utf-8")))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        info = None
    else:
        version, name = _VERSION_RE.search(plugin_yml), _NAME_RE.search(plugin_yml)
        info = {"id": manifest["id"], "plugin": name.group(1) if name else manifest["name"],
                "version": version.group(1) if version else None, "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data), "file": f"{name.group(1) if name else manifest['name']}.jar", "manifest": manifest,
                "commands": plugin_commands(plugin_yml), "path": path}
    _cache[path] = (stat.st_mtime, stat.st_size, info)
    return info


def plugin_commands(plugin_yml):
    """[{"name", "description", "usage", "aliases"}] from the "commands:" part of a plugin.yml (the simple layout of
    the official addons: two spaces per level)."""
    commands, current, inside = [], None, False
    for line in plugin_yml.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            inside = line.strip() == "commands:"
            continue
        if not inside:
            continue
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = {"name": match.group(1), "description": "", "usage": "", "aliases": []}
            commands.append(current)
            continue
        match = re.match(r"^    (description|usage|aliases):\s*(.*)$", line)
        if match and current is not None:
            value = match.group(2).strip().strip("'\"")
            if match.group(1) == "aliases":
                current["aliases"] = [a.strip() for a in value.strip("[]").split(",") if a.strip()]
            else:
                current[match.group(1)] = value.replace("<command>", current["name"])
    return clean_commands(commands)


def clean_commands(commands):
    """Commands as an addon reports them (untrusted): at most 20, short texts."""
    result = []
    for command in commands[:20] if isinstance(commands, list) else []:
        if not isinstance(command, dict) or not isinstance(command.get("name"), str) or not re.match(r"^[A-Za-z0-9_-]{1,32}$", command["name"]):
            continue
        aliases = command.get("aliases") if isinstance(command.get("aliases"), list) else []
        result.append({"name": command["name"], "description": _text(command.get("description"), 120),
                       "usage": _text(command.get("usage"), 200),
                       "aliases": [a for a in aliases[:5] if isinstance(a, str) and re.match(r"^[A-Za-z0-9_-]{1,32}$", a)]})
    return result


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def catalog():
    """The official addons {id: jar_info}, the newest version of each, in store order."""
    found = {}
    for path in jar_paths():
        info = jar_info(path)
        if info and (info["id"] not in found or version_key(info["version"]) > version_key(found[info["id"]]["version"])):
            found[info["id"]] = info
    order = list(STORE)
    return dict(sorted(found.items(), key=lambda item: (order.index(item[0]) if item[0] in order else len(order), item[0])))


def download_url(addon_id):
    return f"{config.PUBLIC_SCHEME}://{config.BASE_DOMAIN}/download/addons/{addon_id}.jar"


def install_message(info):
    """!addoninstall for the plugin: id|version|url|sha256|file name."""
    return f"!addoninstall~{info['id']}|{info['version']}|{download_url(info['id'])}|{info['sha256']}|{info['file']}"


def remove_message(addon_id, file_name):
    return f"!addonremove~{addon_id}|{file_name if file_name and FILE_RE.match(file_name) else ''}"


def state_message(addon_id, active, settings_config):
    """!addonstate for the plugin: id|active 0/1|settings json (one line)."""
    return f"!addonstate~{addon_id}|{1 if active else 0}|{json.dumps(settings_config if active else {}, ensure_ascii=False)}"


# ------------------------------------------------------------------ the addon's description

def _text(value, limit, default=""):
    return _TEXT_STRIP_RE.sub("", value)[:limit] if isinstance(value, str) else default


def _number(value, default):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def check_manifest(manifest):
    """The cleaned mcconnect-addon.json of an addon (it comes from a plugin: never trusted). Raises ManifestError."""
    if not isinstance(manifest, dict):
        raise ManifestError("mcconnect-addon.json must be an object")
    if len(json.dumps(manifest)) > MAX_MANIFEST_BYTES:
        raise ManifestError("mcconnect-addon.json is too large")
    addon_id = manifest.get("id")
    if not isinstance(addon_id, str) or not ID_RE.match(addon_id):
        raise ManifestError("invalid addon id")
    name = _text(manifest.get("name"), 40).strip()
    if not name:
        raise ManifestError("the addon has no name")
    sections = manifest.get("settings", [])
    if not isinstance(sections, list) or len(sections) > MAX_SECTIONS:
        raise ManifestError("settings must be a list of at most %d sections" % MAX_SECTIONS)
    features = manifest.get("features", [])
    return {
        "id": addon_id, "name": name, "summary": _text(manifest.get("summary"), 200),
        "description": _text(manifest.get("description"), 1500),
        "features": [_text(f, 140) for f in features[:12] if isinstance(f, str)] if isinstance(features, list) else [],
        "settings": [_check_section(section) for section in sections],
    }


def _check_section(section):
    if not isinstance(section, dict):
        raise ManifestError("a settings section must be an object")
    return {"title": _text(section.get("title"), 60) or "Einstellungen", "description": _text(section.get("description"), 400),
            "fields": _check_fields(section.get("fields"), nested=False)}


def _check_fields(fields, nested):
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise ManifestError("fields must be a list of at most %d fields" % MAX_FIELDS)
    result, keys = [], set()
    for field in fields:
        checked = _check_field(field, nested)
        if checked["key"] in keys:
            raise ManifestError(f"the key {checked['key']} is used twice")
        keys.add(checked["key"])
        result.append(checked)
    for field in result:  # show_if may only point to a sibling
        condition = field.get("show_if")
        if condition and condition["key"] not in keys:
            del field["show_if"]
    return result


def _check_options(options):
    if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
        raise ManifestError("options must be a list of 1 to %d entries" % MAX_OPTIONS)
    result, values = [], set()
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("value"), str) or len(option["value"]) > 64:
            raise ManifestError("an option needs a text value of at most 64 characters")
        if option["value"] in values:
            continue
        values.add(option["value"])
        result.append({"value": option["value"], "label": _text(option.get("label"), 120) or option["value"]})
    return result


def _check_field(field, nested):
    if not isinstance(field, dict):
        raise ManifestError("a field must be an object")
    key, kind = field.get("key"), field.get("type")
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ManifestError(f"invalid field key {key!r}")
    if kind not in TYPES or (nested and kind == "list"):
        raise ManifestError(f"invalid type of {key}")
    checked = {"key": key, "type": kind, "label": _text(field.get("label"), 80) or key, "help": _text(field.get("help"), 300)}
    condition = field.get("show_if")
    if isinstance(condition, dict) and isinstance(condition.get("key"), str):
        if isinstance(condition.get("is"), bool):
            checked["show_if"] = {"key": condition["key"], "is": condition["is"]}
        elif isinstance(condition.get("in"), list):
            checked["show_if"] = {"key": condition["key"],
                                  "in": [v for v in condition["in"][:20] if isinstance(v, (str, int, float, bool))]}
    if kind == "number":
        low, high = float(_number(field.get("min"), -1e9)), float(_number(field.get("max"), 1e9))
        step = float(_number(field.get("step"), 1)) or 1
        if low > high or step <= 0:
            raise ManifestError(f"invalid range of {key}")
        checked.update(min=low, max=high, step=step, unit=_text(field.get("unit"), 30))
    elif kind in ("text", "lines"):
        checked.update(max=max(1, min(MAX_TEXT, int(_number(field.get("max"), 100)))), colors=field.get("colors") is True,
                       required=field.get("required") is True)
        placeholders = field.get("placeholders") if isinstance(field.get("placeholders"), list) else []
        checked["placeholders"] = [{"name": _text(p.get("name"), 20), "label": _text(p.get("label"), 40),
                                    "example": _text(p.get("example"), 40)}
                                   for p in placeholders[:10] if isinstance(p, dict) and _text(p.get("name"), 20)]
        if field.get("suggest") in SUGGESTIONS:
            checked["suggest"] = field["suggest"]
        if kind == "lines":
            checked["max_items"] = max(1, min(50, int(_number(field.get("max_items"), 10))))
    elif kind in ("select", "multiselect"):
        checked["options"] = _check_options(field.get("options"))
    elif kind == "material":
        checked["required"] = field.get("required") is True
    elif kind == "list":
        checked.update(fields=_check_fields(field.get("fields"), nested=True),
                       max_items=max(1, min(MAX_LIST_ITEMS, int(_number(field.get("max_items"), 50)))),
                       item_label=_text(field.get("item_label"), 40) or "Eintrag")
        keys = [f["key"] for f in checked["fields"]]
        checked["title_key"] = field.get("title_key") if field.get("title_key") in keys else keys[0] if keys else None
    # the default goes through the same checks as a value from the website
    checked["default"] = _value(checked, field.get("default", _empty(checked)), [], key, use_default=False)
    return checked


def _empty(field):
    kind = field["type"]
    if kind == "toggle":
        return False
    if kind == "number":
        return field["min"] if field["min"] > 0 else min(field["max"], 0)
    if kind in ("lines", "multiselect", "list"):
        return []
    if kind == "select":
        return field["options"][0]["value"]
    if kind == "recipe":
        return [""] * 9
    if kind == "position":
        return {"x": 0, "y": 64, "z": 0}
    return ""


# ------------------------------------------------------------------ settings

def defaults(sections):
    return {field["key"]: field["default"] for section in sections for field in section["fields"]}


def normalize(sections, data):
    """(settings, errors): data checked against the description. errors: [{"path": "regions.0.name", "message"}]."""
    errors = []
    if not isinstance(data, dict):
        data = {}
        errors.append({"path": "", "message": "Die Einstellungen müssen ein Objekt sein."})
    result = {}
    for section in sections:
        result.update(_object(section["fields"], data, errors, ""))
    if len(json.dumps(result, ensure_ascii=False)) > MAX_CONFIG_BYTES:
        errors.append({"path": "", "message": "Die Einstellungen sind zu groß."})
    return result, errors


def _object(fields, data, errors, prefix):
    result = {}
    for field in fields:
        path = prefix + field["key"]
        result[field["key"]] = _value(field, data[field["key"]], errors, path) if field["key"] in data else field["default"]
    return result


def _value(field, value, errors, path, use_default=True):
    """The checked value; a wrong one becomes the default (use_default=False: the type's empty value) with an error."""
    kind = field["type"]

    def fail(message, replacement=None):
        errors.append({"path": path, "message": message})
        if replacement is not None:
            return replacement
        return field["default"] if use_default else _empty(field)

    if kind == "toggle":
        return value if isinstance(value, bool) else fail("Muss an oder aus sein.")
    if kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
            return fail("Muss eine Zahl sein.")
        if value < field["min"] or value > field["max"]:
            clamped = min(field["max"], max(field["min"], value))
            return fail(f"Muss zwischen {_plain(field['min'])} und {_plain(field['max'])} liegen.", _rounded(field, clamped))
        return _rounded(field, value)
    if kind == "text":
        if not isinstance(value, str):
            return fail("Muss ein Text sein.")
        text = _TEXT_STRIP_RE.sub("", value)
        if len(text) > field["max"]:
            return fail(f"Höchstens {field['max']} Zeichen.", text[:field["max"]])
        if field["required"] and not text.strip():
            return fail("Darf nicht leer sein.")
        return text
    if kind == "lines":
        if not isinstance(value, list):
            return fail("Muss eine Liste von Zeilen sein.")
        lines = [_TEXT_STRIP_RE.sub("", line)[:field["max"]] for line in value if isinstance(line, str)]
        if any(isinstance(line, str) and len(_TEXT_STRIP_RE.sub("", line)) > field["max"] for line in value):
            errors.append({"path": path, "message": f"Höchstens {field['max']} Zeichen pro Zeile."})
        if len(lines) > field["max_items"]:
            return fail(f"Höchstens {field['max_items']} Zeilen.", lines[:field["max_items"]])
        return lines
    if kind == "select":
        values = [option["value"] for option in field["options"]]
        return value if value in values else fail("Diese Auswahl gibt es nicht.")
    if kind == "multiselect":
        if not isinstance(value, list):
            return fail("Muss eine Liste sein.")
        values = [option["value"] for option in field["options"]]
        if any(v not in values for v in value):
            errors.append({"path": path, "message": "Unbekannte Auswahl entfernt."})
        return [v for v in values if v in value]
    if kind == "material":
        material = _material(value)
        if material is None:
            return fail("Ungültige Item-ID (z. B. glass_pane).")
        if field.get("required") and not material:
            return fail("Bitte ein Item wählen.")
        return material
    if kind == "recipe":
        if not isinstance(value, list) or len(value) != 9:
            return fail("Das Raster braucht 9 Felder.")
        slots = [_material(slot) for slot in value]
        if any(slot is None for slot in slots):
            return fail("Ungültige Item-ID im Raster.", [slot or "" for slot in slots])
        return slots
    if kind == "position":
        if not isinstance(value, dict):
            return fail("Muss x, y und z haben.")
        position = {}
        for axis, limit in (("x", COORDINATE_LIMIT), ("y", 4096), ("z", COORDINATE_LIMIT)):
            number = value.get(axis)
            if not isinstance(number, (int, float)) or isinstance(number, bool) or number != number or abs(number) > limit:
                return fail("Ungültige Koordinaten.")
            position[axis] = int(number)
        return position
    if kind == "list":
        if not isinstance(value, list):
            return fail("Muss eine Liste sein.")
        if len(value) > field["max_items"]:
            errors.append({"path": path, "message": f"Höchstens {field['max_items']} Einträge."})
        rows = []
        for index, row in enumerate(value[:field["max_items"]]):
            if not isinstance(row, dict):
                errors.append({"path": f"{path}.{index}", "message": "Ungültiger Eintrag."})
                continue
            rows.append(_object(field["fields"], row, errors, f"{path}.{index}."))
        return rows
    return fail("Unbekannter Typ.")


def _material(value):
    """"" or a lower case item id without "minecraft:", None if invalid."""
    if not isinstance(value, str):
        return None
    material = value.strip().lower()
    if material.startswith("minecraft:"):
        material = material[len("minecraft:"):]
    return material if material == "" or MATERIAL_RE.match(material) else None


def _rounded(field, value):
    whole = float(field["step"]).is_integer() and float(field["min"]).is_integer()
    return int(round(value)) if whole else round(float(value), 4)


def _plain(number):
    return str(int(number)) if float(number).is_integer() else str(number).replace(".", ",")
