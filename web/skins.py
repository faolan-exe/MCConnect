"""Player skins, heads and body pictures, kept as files on this server (ADR 0010).

The pages used to load them from mc-heads.net, which is down now and then; then the heads were missing and the old
player page waited for them before it filled the stats table. Now the website serves them itself (/mc/...):

- The skin comes from Mojang (session server, the official source), mc-heads.net is only the second source.
- Heads are cut out of the skin here (face plus hat layer), in any size; nothing else is needed for them.
- The body picture (year in review) is mc-heads.net's render, cached; without it a flat front view of the skin.

A file is fresh for FRESH seconds. After that it is fetched again; if that fails the old file stays in use, and
the next try waits RETRY_AFTER seconds, so a site that is down slows down one request, not every page. Without any
file and without an answer there is a neutral placeholder head, never a broken image.
Files instead of Redis: the images are binary, must survive restarts and are read rarely (the browser caches them).
"""
import base64
import io
import json
import os
import re
import tempfile
import threading
import time

import requests
from PIL import Image, ImageDraw, UnidentifiedImageError

from database import config

FRESH = 6 * 3600          # seconds until a file is fetched again (skin changes show up within this time)
RETRY_AFTER = 10 * 60     # seconds between tries while the sources fail
TIMEOUT = 3               # seconds per request to another site
HOST_PAUSE = 60           # seconds a site that did not answer is skipped
MIN_SIZE, MAX_SIZE = 8, 512
BODY_SIZES = (60, 90, 120, 180, 240)
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
MOJANG_PROFILE = "https://sessionserver.mojang.com/session/minecraft/profile/{}"
TEXTURE_HOST = "textures.minecraft.net"
MC_HEADS = "https://mc-heads.net"
HEADERS = {"User-Agent": "MCConnect"}

_locks = {}
_locks_guard = threading.Lock()


def clean_uuid(value):
    """The uuid without dashes in lower case, or None if it is none."""
    value = str(value or "").replace("-", "").lower()
    return value if UUID_RE.match(value) else None


def clamp_size(size):
    return max(MIN_SIZE, min(MAX_SIZE, int(size)))


# ------------------------------------------------------------------ file cache

def _path(kind, name):
    return os.path.join(config.CACHE_DIR, kind, name)


def _age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _read(path):
    try:
        with open(path, "rb") as file:
            return file.read()
    except OSError:
        return None


def _write(path, data):
    """Atomic: other workers never read half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass


def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def cached(kind, name, fetch, fresh=FRESH):
    """
    The cached file kind/name: fresh (younger than fresh seconds) as it is, otherwise fetched again with fetch() (bytes
    or None). If fetching fails, the old file (however old) or None. A failure is noted in a ".failed" file, the next
    try comes after RETRY_AFTER.
    """
    path = _path(kind, name)
    age = _age(path)
    if age is not None and age < fresh:
        return _read(path)
    with _lock(path):  # one download per file at a time in this worker
        age = _age(path)
        if age is not None and age < fresh:  # fetched by another thread meanwhile
            return _read(path)
        failed = _age(path + ".failed")
        if failed is None or failed >= RETRY_AFTER:
            data = fetch()
            if data:
                _write(path, data)
                if failed is not None:
                    try:
                        os.unlink(path + ".failed")
                    except OSError:
                        pass
                return data
            _write(path + ".failed", b"")
    return _read(path)


# ------------------------------------------------------------------ sources

_down_until = {}  # host -> time.monotonic() until which it is not asked (it did not answer)


def _get(url):
    """
    The body of a successful answer, or None (error, timeout, not found). A host that does not answer at all is
    skipped for HOST_PAUSE seconds: a page with 20 heads must not wait 20 times for the same timeout.
    """
    host = url.split("/")[2]
    if _down_until.get(host, 0) > time.monotonic():
        return None
    try:
        response = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    except requests.RequestException:
        _down_until[host] = time.monotonic() + HOST_PAUSE
        return None
    return response.content if response.status_code == 200 and response.content else None


def _as_png(data, sizes):
    """The image re-encoded as PNG if it is a PNG of one of the sizes, else None (no foreign bytes are served)."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.size not in sizes:
                return None
            out = io.BytesIO()
            image.convert("RGBA").save(out, "PNG", optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def _mojang_skin(uuid):
    if uuid[12] != "4":
        return None  # Mojang accounts have random (version 4) uuids; offline mode servers make version 3 ones
    profile = _get(MOJANG_PROFILE.format(uuid))
    if not profile:
        return None  # also: no Mojang account (offline mode servers)
    try:
        for prop in json.loads(profile).get("properties", []):
            if prop.get("name") == "textures":
                url = json.loads(base64.b64decode(prop["value"]))["textures"]["SKIN"]["url"]
                break
        else:
            return None
    except (ValueError, KeyError, TypeError):
        return None  # no own skin: mc-heads.net knows which default skin the account has
    # only Mojang's texture server, whatever the answer says
    match = re.match(r"^https?://" + re.escape(TEXTURE_HOST) + r"/texture/([0-9a-f]+)$", url)
    return _get(f"https://{TEXTURE_HOST}/texture/{match.group(1)}") if match else None


def _download_skin(uuid):
    for source in (lambda: _mojang_skin(uuid), lambda: _get(f"{MC_HEADS}/skin/{uuid}")):
        png = _as_png(source() or b"", {(64, 64), (64, 32)})
        if png:
            return png
    return None


# Set by the web app: True for the uuids of players MCConnect knows. Only their skins are downloaded; otherwise every
# made-up uuid in a URL would make this server ask two other sites and leave a file behind (ADR 0010).
is_known = lambda uuid: True  # noqa: E731


def skin(uuid):
    """The skin (PNG bytes, 64x64 or old 64x32) of the player, or None if it was never loaded."""
    uuid = clean_uuid(uuid)
    if not uuid:
        return None
    if _age(_path("skins", f"{uuid}.png")) is None and _age(_path("skins", f"{uuid}.png.failed")) is None \
            and not is_known(uuid):
        return None
    return cached("skins", f"{uuid}.png", lambda: _download_skin(uuid))


# ------------------------------------------------------------------ pictures made of the skin

def _skin_image(uuid):
    data = skin(uuid)
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except (UnidentifiedImageError, OSError):
        return None


def _layer(image, box, legacy):
    """An overlay part (hat, jacket, ...). Old 64x32 skins often fill the hat with a solid color, which the game
    does not show either."""
    part = image.crop(box)
    if legacy and part.getextrema()[3][0] == 255:
        return None
    return part


def placeholder(size):
    """A neutral head (no skin available): gray face with two eyes, the same in light and dark mode."""
    face = Image.new("RGBA", (8, 8), (138, 146, 141, 255))
    draw = ImageDraw.Draw(face)
    draw.rectangle([0, 0, 7, 1], fill=(96, 104, 99, 255))  # hair
    draw.point([(2, 4), (5, 4)], fill=(40, 46, 43, 255))
    draw.line([(3, 6), (4, 6)], fill=(110, 118, 113, 255))
    return face.resize((size, size), Image.NEAREST)


def head_image(uuid, size):
    """The face with the hat layer as a size x size image (RGBA); the placeholder without a skin."""
    size = clamp_size(size)
    image = _skin_image(uuid)
    if image is None:
        return placeholder(size)
    legacy = image.size == (64, 32)
    face = image.crop((8, 8, 16, 16))
    hat = _layer(image, (40, 8, 48, 16), legacy)
    if hat is not None:
        face.alpha_composite(hat)
    return face.resize((size, size), Image.NEAREST)


def _flat_body(uuid, height):
    """Front view of the skin (head, body, arms, legs with their layers), height px high; None without a skin."""
    image = _skin_image(uuid)
    if image is None:
        return None
    legacy = image.size == (64, 32)
    slim = not legacy and image.getpixel((54, 20))[3] == 0  # the 3 px arms of slim skins leave this column empty
    arm = 3 if slim else 4
    body = Image.new("RGBA", (8 + 2 * arm, 32))

    def part(base, layer, at, mirror=False):
        piece = image.crop(base)
        over = None if layer is None else _layer(image, layer, legacy)
        if over is not None:
            piece.alpha_composite(over)
        if mirror:
            piece = piece.transpose(Image.FLIP_LEFT_RIGHT)
        body.alpha_composite(piece, at)

    part((8, 8, 16, 16), (40, 8, 48, 16), (arm, 0))                          # head
    part((20, 20, 28, 32), None if legacy else (20, 36, 28, 48), (arm, 8))   # body
    part((44, 20, 44 + arm, 32), None if legacy else (44, 36, 44 + arm, 48), (0, 8))  # right arm
    part((4, 20, 8, 32), None if legacy else (4, 36, 8, 48), (arm, 20))      # right leg
    if legacy:  # old skins have one arm and one leg, the game mirrors them
        part((44, 20, 48, 32), None, (arm + 8, 8), mirror=True)
        part((4, 20, 8, 32), None, (arm + 4, 20), mirror=True)
    else:
        part((36, 52, 36 + arm, 64), (52, 52, 52 + arm, 64), (arm + 8, 8))   # left arm
        part((20, 52, 24, 64), (4, 52, 8, 64), (arm + 4, 20))                # left leg
    scale = max(1, height // 32)
    return body.resize((body.width * scale, 32 * scale), Image.NEAREST)


def body_png(uuid, size):
    """The body picture of mc-heads.net (cached); without it the flat front view or the placeholder head."""
    uuid = clean_uuid(uuid)
    size = min(BODY_SIZES, key=lambda s: abs(s - int(size)))  # few sizes: few files
    if uuid:
        data = cached("bodies", f"{uuid}-{size}.png",
                      lambda: _as_png(_get(f"{MC_HEADS}/body/{uuid}/{size}") or b"", _body_sizes(size)))
        if data:
            return data
        flat = _flat_body(uuid, size * 2)
        if flat is not None:
            return png_bytes(flat)
    return png_bytes(placeholder(size))


def _body_sizes(size):
    """mc-heads.net answers /body/<uuid>/<size> with a picture size px wide and about twice as high."""
    return {(size, h) for h in range(size, size * 3)}


def png_bytes(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def has_skin(uuid):
    """True if a skin (fresh or old) is available, so the head is not the placeholder."""
    return skin(uuid) is not None
