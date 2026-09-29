"""Own icons in chat and name tags: pixel SVGs -> PNGs -> a resource pack with characters from the private use area.

The icons are drawn as 9x9 pixel SVGs (web/static/glyphs/<key>.svg, only <rect> elements with whole-number
coordinates and "#rrggbb" fills). The website shows the SVGs directly; the resource pack gets them as PNGs
(rendered here without extra libraries) in a bitmap font provider added to minecraft:default, so the
characters U+E000... work in every chat line, sign and name tag of a player who loaded the pack.

The plugin offers the pack (!pack, plugin 3.17) and replaces every icon with its FALLBACK Unicode character
for players who declined it (and MCConnect does so for older plugins, see fallback_text()).

Servers add own icons (moderators draw them on /users/icons): stored as 81 pixel colors, codepoints from
CUSTOM_FIRST on, never given out twice per server. IconSet is the icons of one server; its pack (built-in + own icons)
is served at /resourcepack/<server id>/<sha1>.zip. In prefixes they are written as ":name:" on the website.

Codepoints are stored in players' join styles, reward levels and prefixes: never change or reuse one, only append.
"""
import functools
import hashlib
import io
import json
import os
import re
import struct
import zipfile
import zlib

GLYPH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "static", "glyphs")
SIZE = 9
# 9 px high, 8 above the baseline: one pixel above capital letters and one below the baseline, centered on them
ASCENT = 8

# key: (character, fallback without the pack (in the game's Unicode font), German name)
ICONS = {
    "trophy": ("\ue000", "♕", "Trophäe"),
    "flame": ("\ue001", "✹", "Flamme"),
    "medal": ("\ue002", "◈", "Medaille"),
    "level": ("\ue003", "▲", "Stufe"),
    "clock": ("\ue004", "⌚", "Uhr"),
    "calendar": ("\ue005", "▦", "Kalender"),
    "pickaxe": ("\ue006", "⛏", "Spitzhacke"),
    "diamond": ("\ue007", "◆", "Diamant"),
    "emerald": ("\ue008", "◊", "Smaragd"),
    "creeper": ("\ue009", "☻", "Creeper"),
    "crown": ("\ue00a", "♔", "Krone"),
    "ender_eye": ("\ue00b", "◉", "Enderauge"),
    "totem": ("\ue00c", "☥", "Totem"),
}
BY_CHAR = {char: key for key, (char, _, _) in ICONS.items()}
FALLBACK = {char: fallback for char, fallback, _ in ICONS.values()}
ICON_RE = re.compile("[\ue000-\uf8ff]")  # the private use area: built-in and own icons
CUSTOM_FIRST = 0xE100
MAX_CUSTOM = 32
DEFAULT_FALLBACK = "■"
NAME_RE = re.compile(r"^[a-z0-9_]{2,16}$")
TOKEN_RE = re.compile(r":([a-z0-9_]{2,16}):")
_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")

PACK_DESCRIPTION = "MCConnect: Icons für Join-Nachrichten und Abzeichen"
# 34 = 1.21.1; older and newer clients load it as well (the font format has not changed since 1.13)
PACK_FORMAT, MIN_FORMAT, MAX_FORMAT = 34, 4, 999

_RECT_RE = re.compile(r"<rect\b([^>]*)/?>")
_ATTR_RE = re.compile(r'([a-z-]+)="([^"]*)"')


def is_icon(text):
    """A single icon character (built-in or a server's own)."""
    return bool(text) and len(text) == 1 and bool(ICON_RE.match(text))


def char(key):
    return ICONS[key][0]


def label(char_or_key):
    key = BY_CHAR.get(char_or_key, char_or_key)
    return ICONS[key][2] if key in ICONS else char_or_key


def fallback_text(text):
    """The text with every built-in icon replaced by its Unicode fallback (for players without the pack)."""
    return BUILTIN.fallback_text(text)


def svg_path(key):
    return os.path.join(GLYPH_DIR, key + ".svg")


def check_pixels(pixels):
    """(81 colors "#rrggbb" or "" for transparent, None) or (None, error)."""
    if not isinstance(pixels, list) or len(pixels) != SIZE * SIZE:
        return None, "Das Icon braucht 9 × 9 Pixel."
    cleaned = [str(p or "").lower() for p in pixels]
    if any(p and not _COLOR_RE.match(p) for p in cleaned):
        return None, "Ungültige Farbe."
    if sum(1 for p in cleaned if p) < 3:
        return None, "Male mindestens 3 Pixel."
    return cleaned, None


def check_icon(name, fallback):
    """(name, fallback, None) or (None, None, error) for an own icon."""
    name = str(name or "").strip().lower()
    fallback = str(fallback or "").strip() or DEFAULT_FALLBACK
    if not NAME_RE.match(name):
        return None, None, "Der Name braucht 2-16 Zeichen: a-z, 0-9 und _ (im Präfix schreibst du ihn als :name:)."
    if name in ICONS:
        return None, None, "Diesen Namen hat schon ein eingebautes Icon."
    if len(fallback) != 1 or ICON_RE.match(fallback) or fallback in ",|~&§:" or not fallback.isprintable() or fallback.isspace():
        return None, None, "Das Ersatzzeichen muss genau ein normales Zeichen sein, z. B. ■ oder ★."
    return name, fallback, None


def pixel_rows(pixels):
    """RGBA rows of 81 stored colors."""
    return [[(int(p[1:3], 16), int(p[3:5], 16), int(p[5:7], 16), 255) if p else (0, 0, 0, 0)
             for p in pixels[row * SIZE:(row + 1) * SIZE]] for row in range(SIZE)]


def pixel_svg(pixels, title=""):
    """An SVG (one rect per horizontal run, like the built-in icons) of 81 stored colors."""
    rects = []
    for y in range(SIZE):
        x = 0
        while x < SIZE:
            color = pixels[y * SIZE + x]
            end = x + 1
            while end < SIZE and pixels[y * SIZE + end] == color:
                end += 1
            if color:
                rects.append(f'<rect x="{x}" y="{y}" width="{end - x}" height="1" fill="{color}"/>')
            x = end
    safe = title.replace("&", "&amp;").replace("<", "&lt;")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}" '
            f'shape-rendering="crispEdges"><title>{safe}</title>{"".join(rects)}</svg>')


def rasterize(svg):
    """RGBA pixel rows ([[(r, g, b, a), ...], ...], SIZE x SIZE) of an icon SVG made of rects."""
    pixels = [[(0, 0, 0, 0)] * SIZE for _ in range(SIZE)]
    for match in _RECT_RE.finditer(svg):
        attrs = dict(_ATTR_RE.findall(match.group(1)))
        x, y = int(attrs.get("x", 0)), int(attrs.get("y", 0))
        width, height = int(attrs["width"]), int(attrs["height"])
        fill = attrs["fill"]
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", fill) or x < 0 or y < 0 or x + width > SIZE or y + height > SIZE:
            raise ValueError(f"unsupported rect {match.group(0)}")
        color = (int(fill[1:3], 16), int(fill[3:5], 16), int(fill[5:7], 16), 255)
        for row in range(y, y + height):
            pixels[row][x:x + width] = [color] * width
    return pixels


def png(pixels):
    """PNG bytes (RGBA, 8 bit) of pixel rows."""
    height, width = len(pixels), len(pixels[0])
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in pixels)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def scaled(pixels, factor):
    """Pixel rows enlarged by a whole factor (nearest neighbour)."""
    return [[pixel for pixel in row for _ in range(factor)] for row in pixels for _ in range(factor)]


def icon_png(key):
    with open(svg_path(key), encoding="utf-8") as file:
        return png(rasterize(file.read()))


def _pack_icon():
    """pack.png: the trophy on a dark tile, 8x enlarged."""
    with open(svg_path("trophy"), encoding="utf-8") as file:
        icon = rasterize(file.read())
    tile = [[(38, 42, 51, 255)] * (SIZE + 2) for _ in range(SIZE + 2)]
    for y, row in enumerate(icon):
        for x, pixel in enumerate(row):
            if pixel[3]:
                tile[y + 1][x + 1] = pixel
    return png(scaled(tile, 8))


@functools.lru_cache(maxsize=64)
def build_pack(custom=()):
    """(zip bytes, sha1 hex) of the resource pack with the built-in icons and a server's own ones
    (((char, pixels tuple), ...)). Same icons -> same bytes (fixed timestamps and order)."""
    files = {
        "pack.mcmeta": json.dumps({"pack": {
            "pack_format": PACK_FORMAT, "supported_formats": [MIN_FORMAT, MAX_FORMAT],
            "min_format": MIN_FORMAT, "max_format": MAX_FORMAT, "description": PACK_DESCRIPTION}},
            ensure_ascii=False, indent=2).encode(),
        "pack.png": _pack_icon(),
        "assets/minecraft/font/default.json": json.dumps({"providers": [
            {"type": "bitmap", "file": f"mcconnect:font/{key}.png", "ascent": ASCENT, "height": SIZE, "chars": [c]}
            for key, (c, _, _) in ICONS.items()] + [
            {"type": "bitmap", "file": f"mcconnect:font/own_{ord(c):x}.png", "ascent": ASCENT, "height": SIZE, "chars": [c]}
            for c, _ in custom]}, indent=2).encode(),
    }
    for key in ICONS:
        files[f"assets/mcconnect/textures/font/{key}.png"] = icon_png(key)
    for c, pixels in custom:
        files[f"assets/mcconnect/textures/font/own_{ord(c):x}.png"] = png(pixel_rows(pixels))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    data = buffer.getvalue()
    return data, hashlib.sha1(data).hexdigest()


def glyph_map():
    """The built-in icons and their fallbacks for the plugin: "<icon><fallback>,<icon><fallback>,..."."""
    return BUILTIN.glyph_map()


class IconSet:
    """The icons of one server: the built-in ones plus its own (rows of DatabaseManager.get_server_icons)."""

    def __init__(self, custom=()):
        self.custom = [dict(row, char=chr(row["codepoint"])) for row in custom]
        self.by_char = {icon["char"]: icon for icon in self.custom}
        self.by_name = {icon["name"]: icon for icon in self.custom}

    def fallback(self, char):
        own = self.by_char.get(char)
        return own["fallback"] if own else FALLBACK.get(char, DEFAULT_FALLBACK)

    def fallback_text(self, text):
        return ICON_RE.sub(lambda m: self.fallback(m.group(0)), text) if text else text

    def label(self, char):
        own = self.by_char.get(char)
        return own["name"] if own else label(char)

    def glyph_map(self):
        pairs = [(c, fallback) for c, fallback, _ in ICONS.values()] + [(i["char"], i["fallback"]) for i in self.custom]
        return ",".join(c + fallback for c, fallback in pairs)

    def pack(self):
        """(zip bytes, sha1 hex) of this server's pack."""
        return build_pack(tuple((i["char"], tuple(i["pixels"])) for i in self.custom))

    def to_tokens(self, text):
        """Own icons as ":name:" (for editing a prefix on the website)."""
        return "".join(f":{self.by_char[c]['name']}:" if c in self.by_char else c for c in text or "")

    def from_tokens(self, text):
        """":name:" of an own icon as its character; unknown names stay text."""
        return TOKEN_RE.sub(lambda m: self.by_name[m.group(1)]["char"] if m.group(1) in self.by_name else m.group(0),
                            text or "")


BUILTIN = IconSet()


def icon_set(db, server_id):
    """The IconSet of a server."""
    return IconSet(db.get_server_icons(server_id)) if server_id is not None else BUILTIN
