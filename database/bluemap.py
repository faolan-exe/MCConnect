"""
The world map of a server (BlueMap, ADR 0015): the address the owner enters and the links into the map.

BlueMap runs its own web server next to the Minecraft server. MCConnect only shows it in an iframe on /karte, so the
address must be https (a page on https may not frame http) and is checked here before it is stored.
"""
import re
from urllib.parse import urlsplit

MAX_LENGTH = 300
# BlueMap's map ids are the world names by default (world, world_nether, world_the_end).
MAP_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MAX_XZ = 30_000_000  # the world border of Minecraft
MIN_Y, MAX_Y = -2048, 4096
# distance of the camera when a link jumps to a position (BlueMap's own default view is much farther away)
LINK_DISTANCE = 150


def normalize_url(value):
    """
    Return (url, error). An empty value is (None, None): no map. Otherwise only https addresses without user name,
    password or #fragment (the links add their own fragment).
    """
    value = str(value or "").strip()
    if not value:
        return None, None
    error = "Die Karten-Adresse muss mit https:// beginnen, z. B. https://karte.example.de/."
    if len(value) > MAX_LENGTH or any(c.isspace() or ord(c) < 0x20 for c in value):
        return None, error
    try:
        parts = urlsplit(value)
        port = parts.port  # raises for an invalid port
    except ValueError:
        return None, error
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password or "@" in parts.netloc:
        return None, error
    if parts.fragment or value.endswith("#"):
        return None, "Die Karten-Adresse darf keinen Teil nach # enthalten."
    netloc = parts.hostname.lower() + (f":{port}" if port else "")
    path = parts.path or "/"
    return f"https://{netloc}{path}" + (f"?{parts.query}" if parts.query else ""), None


def origin(url):
    """https://host[:port] of a stored address (for the Content-Security-Policy frame-src), None without map."""
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def position(world, x, y, z):
    """(world, x, y, z) as checked values, or None if anything is missing or out of the world."""
    try:
        x, y, z = int(x), int(y), int(z)
    except (TypeError, ValueError):
        return None
    world = str(world or "")
    if not MAP_ID_RE.match(world) or abs(x) > MAX_XZ or abs(z) > MAX_XZ or not MIN_Y <= y <= MAX_Y:
        return None
    return world, x, y, z


def view_url(url, world=None, x=None, y=None, z=None):
    """The address for the iframe: the map itself, or with a valid position BlueMap's hash that jumps there."""
    if not url:
        return None
    place = position(world, x, y, z)
    if place is None:
        return url
    world, x, y, z = place
    # map:x:y:z:distance:rotation:angle:tilt:ortho:mode
    return f"{url}#{world}:{x}:{y}:{z}:{LINK_DISTANCE}:0:0:0:0:perspective"
