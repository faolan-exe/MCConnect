"""Shareable stat card of a player (1200x630 PNG, the usual og:image size)."""
import io
import os
import threading
import time

import requests
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = os.path.join(os.path.dirname(__file__), "card_fonts")
WIDTH, HEIGHT = 1200, 630
NIGHT = (15, 21, 18)
GREEN = (47, 191, 98)
GREEN_TEXT = (110, 231, 154)
MUTED = (160, 172, 166)
WHITE = (255, 255, 255)
TILE = (29, 38, 33)
AVATAR_SIZE = 168
AVATAR_TTL = 6 * 3600

_avatars = {}  # uuid -> (fetched at, image or None)
_avatar_lock = threading.Lock()


def _font(name, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, f"{name}.ttf"), size)


def _avatar(uuid):
    """The player's head from mc-heads.net (cached), or None if it can't be loaded."""
    with _avatar_lock:
        cached = _avatars.get(uuid)
    if cached and time.monotonic() - cached[0] < AVATAR_TTL:
        return cached[1]
    image = None
    try:
        response = requests.get(f"https://mc-heads.net/avatar/{uuid}/{AVATAR_SIZE}", timeout=3)
        if response.ok:
            image = Image.open(io.BytesIO(response.content)).convert("RGBA")
            image = image.resize((AVATAR_SIZE, AVATAR_SIZE), Image.NEAREST)
    except (requests.RequestException, OSError):
        image = None
    with _avatar_lock:
        _avatars[uuid] = (time.monotonic(), image)
    return image


def _hex(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def _fit(draw, text, font_name, size, max_width, min_size=28):
    """The largest font (down to min_size) in which the text fits into max_width."""
    while size > min_size and draw.textlength(text, font=_font(font_name, size)) > max_width:
        size -= 2
    return _font(font_name, size)


def render_card(name, uuid, server_name, prefix, prefix_colors, tiles, ranks, achievements):
    """
    prefix: {"text", "color"} or None; tiles: [(label, value)] (4); ranks: ["#1 Spielzeit", ...].
    Returns PNG bytes.
    """
    image = Image.new("RGB", (WIDTH, HEIGHT), NIGHT)
    draw = ImageDraw.Draw(image)
    # faint block grid and the green accent on the left, like the server start page
    for x in range(0, WIDTH, 48):
        draw.line([(x, 0), (x, HEIGHT)], fill=(22, 30, 26))
    for y in range(0, HEIGHT, 48):
        draw.line([(0, y), (WIDTH, y)], fill=(22, 30, 26))
    draw.rectangle([0, 0, 10, HEIGHT], fill=GREEN)

    left, top = 72, 64
    avatar = _avatar(uuid)
    if avatar is not None:
        image.paste(avatar, (left, top), avatar)
    else:
        draw.rounded_rectangle([left, top, left + AVATAR_SIZE, top + AVATAR_SIZE], radius=12, fill=TILE)

    text_left = left + AVATAR_SIZE + 40
    draw.text((text_left, top + 4), server_name.upper(), font=_font("chakra-petch-700", 24), fill=MUTED)
    y = top + 44
    if prefix:
        chip_font = _font("chakra-petch-700", 26)
        chip = f"[{prefix['text']}]"
        chip_width = draw.textlength(chip, font=chip_font)
        draw.rounded_rectangle([text_left, y, text_left + chip_width + 20, y + 40], radius=6, fill=(27, 31, 29))
        draw.text((text_left + 10, y + 4), chip, font=chip_font, fill=_hex(prefix_colors.get(prefix["color"], "#AAAAAA")))
        y += 50
    name_font = _fit(draw, name, "chakra-petch-700", 76, WIDTH - text_left - 72)
    draw.text((text_left, y), name, font=name_font, fill=WHITE)

    # four value tiles
    tile_top, gap = 300, 20
    tile_width = (WIDTH - 2 * left - 3 * gap) // 4
    for i, (label, value) in enumerate(tiles[:4]):
        x = left + i * (tile_width + gap)
        draw.rounded_rectangle([x, tile_top, x + tile_width, tile_top + 150], radius=14, fill=TILE)
        draw.text((x + 22, tile_top + 24), label, font=_font("atkinson-hyperlegible-700", 22), fill=MUTED)
        value_font = _fit(draw, value, "chakra-petch-700", 40, tile_width - 44, min_size=22)
        draw.text((x + 22, tile_top + 72), value, font=value_font, fill=WHITE)

    # ranking places and achievements
    y = tile_top + 190
    x = left
    chip_font = _font("chakra-petch-700", 24)
    for rank in ranks:
        width = draw.textlength(rank, font=chip_font) + 28
        draw.rounded_rectangle([x, y, x + width, y + 44], radius=8, outline=(47, 90, 64), width=2)
        draw.text((x + 14, y + 7), rank, font=chip_font, fill=GREEN_TEXT)
        x += width + 12
    draw.text((WIDTH - left, y + 8), achievements, font=_font("atkinson-hyperlegible-700", 24), fill=MUTED, anchor="ra")

    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()
