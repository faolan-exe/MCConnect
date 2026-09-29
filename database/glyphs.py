"""Own icons in chat and name tags: pixel SVGs -> PNGs -> a resource pack with characters from the private use area.

The icons are drawn as 9x9 pixel SVGs (web/static/glyphs/<key>.svg, only <rect> elements with whole-number
coordinates and "#rrggbb" fills). The website shows the SVGs directly; the resource pack gets them as PNGs
(rendered here without extra libraries) in a bitmap font provider added to minecraft:default, so the
characters U+E000... work in every chat line, sign and name tag of a player who loaded the pack.

The plugin offers the pack (!pack, plugin 3.17) and replaces every icon with its FALLBACK Unicode character
for players who declined it (and MCConnect does so for older plugins, see fallback_text()).

Codepoints are stored in players' join styles and reward levels: never change or reuse one, only append.
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
ICON_RE = re.compile("[" + "".join(BY_CHAR) + "]")

PACK_DESCRIPTION = "MCConnect: Icons für Join-Nachrichten und Abzeichen"
# 34 = 1.21.1; older and newer clients load it as well (the font format has not changed since 1.13)
PACK_FORMAT, MIN_FORMAT, MAX_FORMAT = 34, 4, 999

_RECT_RE = re.compile(r"<rect\b([^>]*)/?>")
_ATTR_RE = re.compile(r'([a-z-]+)="([^"]*)"')


def is_icon(text):
    return bool(text) and text in BY_CHAR


def char(key):
    return ICONS[key][0]


def label(char_or_key):
    key = BY_CHAR.get(char_or_key, char_or_key)
    return ICONS[key][2] if key in ICONS else char_or_key


def fallback_text(text):
    """The text with every icon replaced by its Unicode fallback (for players without the pack)."""
    return ICON_RE.sub(lambda m: FALLBACK[m.group(0)], text) if text else text


def svg_path(key):
    return os.path.join(GLYPH_DIR, key + ".svg")


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


@functools.lru_cache(maxsize=1)
def build_pack():
    """(zip bytes, sha1 hex) of the resource pack. Same icons -> same bytes (fixed timestamps and order)."""
    files = {
        "pack.mcmeta": json.dumps({"pack": {
            "pack_format": PACK_FORMAT, "supported_formats": [MIN_FORMAT, MAX_FORMAT],
            "min_format": MIN_FORMAT, "max_format": MAX_FORMAT, "description": PACK_DESCRIPTION}},
            ensure_ascii=False, indent=2).encode(),
        "pack.png": _pack_icon(),
        "assets/minecraft/font/default.json": json.dumps({"providers": [
            {"type": "bitmap", "file": f"mcconnect:font/{key}.png", "ascent": ASCENT, "height": SIZE, "chars": [c]}
            for key, (c, _, _) in ICONS.items()]}, indent=2).encode(),
    }
    for key in ICONS:
        files[f"assets/mcconnect/textures/font/{key}.png"] = icon_png(key)
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
    """The icons and their fallbacks for the plugin: "<icon><fallback>,<icon><fallback>,..."."""
    return ",".join(c + fallback for c, fallback, _ in ICONS.values())
