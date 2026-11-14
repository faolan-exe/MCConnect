"""Icons of blocks and items that the sprite in css/minecraft.css does not have (newer versions, e.g. the copper tools).

The sprite (icons-minecraft-0.4.png) ends around 1.20. Newer ids get their icon from the texture atlases of
misode/mcmeta (branch "atlas", the same project as the block registry, database/registry.py): "items" has the
inventory icons of every item of the newest version, "blocks" the block textures (a flat face, the sprite has 3D
renders). Index and atlas are kept as files like the skins (web/skins.py, ADR 0010) and fetched again once a day;
an old file stays in use meanwhile, so pages never wait for GitHub once the files are there.
"""
import io
import json
import os
import threading

from PIL import Image, UnidentifiedImageError

from web import skins

ATLAS_URL = "https://raw.githubusercontent.com/misode/mcmeta/atlas/{kind}/{file}"
KINDS = ("items", "blocks")
FRESH = 24 * 3600
# block ids without an own texture: the texture of the block they are made of
SHAPES = ("_stairs", "_slab", "_wall", "_fence_gate", "_fence", "_pressure_plate", "_button", "_trapdoor", "_door")
FACES = ("", "_front", "_side", "_top")
MAX_ATLAS_PIXELS = 4096 * 4096

_memory = {}  # (kind, "index"/"atlas") -> (file mtime, parsed)
_memory_lock = threading.Lock()
_icons = {}   # id -> PNG bytes or None, cleared when an atlas changes


def _fetch_index(kind):
    data = skins._get(ATLAS_URL.format(kind=kind, file="data.json"))
    try:
        index = json.loads(data or b"")
    except ValueError:
        return None
    ok = isinstance(index, dict) and index and all(
        isinstance(v, list) and len(v) == 4 and all(isinstance(n, int) and n >= 0 for n in v) for v in index.values())
    return json.dumps(index).encode() if ok else None


def _fetch_atlas(kind):
    data = skins._get(ATLAS_URL.format(kind=kind, file="atlas.png"))
    try:
        with Image.open(io.BytesIO(data or b"")) as image:
            if image.format != "PNG" or image.width * image.height > MAX_ATLAS_PIXELS:
                return None
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return data


def _file(name, fetch):
    """The cached file (bytes or None). The first time it is fetched right away; later a stale file is used and
    fetched again in the background."""
    path = skins._path("atlas", name)
    age = skins._age(path)
    if age is None:
        return skins.cached("atlas", name, fetch, fresh=FRESH)
    if age >= FRESH:
        threading.Thread(target=skins.cached, args=("atlas", name, fetch), kwargs={"fresh": FRESH},
                         name="atlas-refresh", daemon=True).start()
    return skins._read(path)


def _load(kind, part):
    """The parsed index (dict) or atlas (RGBA image) of a kind, read again when the file changed; None without."""
    name, fetch = (f"{kind}.json", _fetch_index) if part == "index" else (f"{kind}.png", _fetch_atlas)
    data = _file(name, lambda: fetch(kind))
    try:
        mtime = os.path.getmtime(skins._path("atlas", name))
    except OSError:
        mtime = None
    with _memory_lock:
        known = _memory.get((kind, part))
        if known and known[0] == mtime:
            return known[1]
    if not data:
        return None
    try:
        parsed = json.loads(data) if part == "index" else Image.open(io.BytesIO(data)).convert("RGBA")
    except (ValueError, UnidentifiedImageError, OSError):
        return None
    with _memory_lock:
        if known is not None:
            _icons.clear()  # a new version of the atlas: cut the icons again
        _memory[(kind, part)] = (mtime, parsed)
    return parsed


def _candidates(item_id):
    yield "items", f"item/{item_id}"
    bases = [item_id]
    for shape in SHAPES:
        if item_id.endswith(shape):
            base = item_id[: -len(shape)]
            # tuff_brick_stairs -> tuff_bricks, oak_stairs -> oak_planks, waxed_cut_copper_slab -> cut_copper
            bases += [base, base + "s", base + "_planks", base.removeprefix("waxed_")]
            break
    bases.append(item_id.removeprefix("waxed_"))  # waxed blocks look like the plain ones
    for base in dict.fromkeys(bases):
        for face in FACES:
            yield "blocks", f"block/{base}{face}"


def resolve(item_id):
    """(kind, atlas key) of the icon of a block or item id (without "minecraft:"), or None."""
    for kind, key in _candidates(item_id):
        index = _load(kind, "index")
        if index and key in index:
            return kind, key
    return None


def has_icon(item_id):
    return resolve(item_id) is not None


def icon_png(item_id):
    """The 16x16 icon as PNG bytes (first frame of animated textures), or None if there is none."""
    if item_id in _icons:
        return _icons[item_id]
    found = resolve(item_id)
    png = None
    if found:
        kind, key = found
        atlas = _load(kind, "atlas")
        x, y, w, h = _load(kind, "index")[key]
        if atlas is not None and x + w <= atlas.width and y + min(w, h) <= atlas.height:
            icon = atlas.crop((x, y, x + w, y + min(w, h)))  # animated textures are frames stacked downwards
            if icon.size != (16, 16):
                icon = icon.resize((16, 16), Image.NEAREST)
            png = skins.png_bytes(icon)
    if len(_icons) > 5000:
        _icons.clear()
    _icons[item_id] = png
    return png


def warm_up():
    """Fetch the indexes and atlases if they are missing (in the background, e.g. at startup)."""
    def run():
        for kind in KINDS:
            _load(kind, "index")
            _load(kind, "atlas")
    threading.Thread(target=run, name="atlas-warm-up", daemon=True).start()
