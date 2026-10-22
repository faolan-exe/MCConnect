"""Which stat objects are blocks and which are items (block_lookup, item_lookup, server_registry).

The stats files only name objects ("minecraft:shelf"); whether one is a block decides its category
(database/stats.py: a placed block counts as BLOCK_USED, a used item as ITEM_USED). Three sources keep the
lists current (docs/adr/0002-block-registry.md):

- registry.json: the vanilla registry of a release, shipped with the code (offline baseline).
- the vanilla registry online (misode/mcmeta, updated for every release and snapshot), fetched by the socket
  server every REGISTRY_SYNC_HOURS; stored in the global lookup tables.
- the registry of the running server, reported by the plugin (!REGISTRY, plugin 3.24): also blocks of mods,
  data packs and versions newer than both. Stored per server (server_registry), so one server cannot change
  the categories of another.

Names are stored like the lookup tables always did: vanilla ones without namespace ("stone"), all others with
it ("create:brass_block"). Names are only ever added: a removed mod still has values in the stats files.
"""
import json
import os
import re

import requests

from . import config
from .stats import lookup_name

BUNDLED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry.json")
# The plugin reports Bukkit's NamespacedKeys, mcmeta plain vanilla paths.
NAME_RE = re.compile(r"^(?:[a-z0-9_.-]{1,64}:)?[a-z0-9_./-]{1,128}$")
# More than every registry of a big mod pack; a larger list is refused as a whole.
MAX_NAMES = 100_000
FETCH_TIMEOUT = 20


def object_name(name):
    """The lookup form back to the stored stat object: "stone" -> "minecraft:stone"."""
    return name if ":" in name else f"minecraft:{name}"


def normalize(blocks, items):
    """Valid names in lookup form: (set of blocks, set of items that are not blocks).

    Raises ValueError for anything but two lists of strings or more than MAX_NAMES names.
    """
    if not isinstance(blocks, list) or not isinstance(items, list):
        raise ValueError("blocks and items must be lists")
    if len(blocks) + len(items) > MAX_NAMES:
        raise ValueError("too many registry names")
    clean = lambda names: {lookup_name(n) for n in names if isinstance(n, str) and NAME_RE.match(n)}
    block_set = clean(blocks)
    return block_set, clean(items) - block_set


def parse_report(value):
    """The !REGISTRY json of the plugin: {"version", "blocks": [...], "items": [...]} -> (blocks, items, version)."""
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("registry must be an object")
    blocks, items = normalize(data.get("blocks"), data.get("items"))
    version = data.get("version")
    return blocks, items, str(version)[:64] if version else None


def bundled():
    """The registry shipped with the code: (blocks, items, version)."""
    with open(BUNDLED_FILE, encoding="utf-8") as file:
        data = json.load(file)
    return (*normalize(data["blocks"], data["items"]), data["version"])


def fetch(url=None, etag=None, session=requests):
    """The vanilla registry online (mcmeta's registries/data.min.json).

    Returns (blocks, items, etag), or None if it is unchanged since etag. Raises requests.RequestException
    or ValueError if it cannot be loaded.
    """
    url = url or config.REGISTRY_URL
    response = session.get(url, timeout=FETCH_TIMEOUT, headers={"If-None-Match": etag} if etag else {})
    if response.status_code == 304:
        return None
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("registry data must be an object")
    blocks, items = normalize(data.get("block"), data.get("item"))
    if not blocks:
        raise ValueError("registry data without blocks")
    return blocks, items, response.headers.get("ETag")
