"""Connections and clients of the players (plugin 3.28/3.29, ADR 0013): what the values mean and how they are named.

Mods are recognized by the plugin channels a client registers. A mod that registers none (most performance mods, many
cheat clients) cannot be seen; a channel list is what the client says about itself.
"""

# Ping in ms: below PING_GOOD nobody notices, above PING_BAD hits and blocks lag behind; a sample of PING_SPIKE or more
# counts as a spike.
PING_GOOD, PING_BAD, PING_SPIKE = 80, 150, 250
PING_BUCKETS = ((0, 50, "unter 50 ms"), (50, 80, "50–79 ms"), (80, 150, "80–149 ms"), (150, 250, "150–249 ms"),
                (250, None, "ab 250 ms"))

# Histogram of the minute pings per player and day: upper bounds in ms, the last bucket is everything above. The median
# and the 90th percentile come from it, so a few minutes with a hanging connection do not move the typical value.
PING_HIST_EDGES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 120, 140, 160, 200, 250, 300, 400, 600, 1000)

CLIENT_BRANDS = (("lunarclient", "Lunar Client"), ("badlion", "Badlion"), ("feather", "Feather"), ("labymod", "LabyMod"),
                 ("neoforge", "NeoForge"), ("forge", "Forge"), ("fabric", "Fabric"), ("quilt", "Quilt"),
                 ("optifine", "OptiFine"), ("vanilla", "Vanilla"), ("geyser", "Bedrock (Geyser)"))
VANILLA_LABELS = ("Vanilla", "Bedrock", "unbekannt")

# channel namespace -> (mod name, kind); kinds: "loader", "client" (launcher/client), "map", "voice", "build",
# "recipes", "other"
MODS = {
    "fabric": ("Fabric API", "loader"), "fabric-screen-handler-api-v1": ("Fabric API", "loader"),
    "c": ("Fabric/NeoForge (Common)", "loader"), "forge": ("Forge", "loader"), "fml": ("Forge", "loader"),
    "neoforge": ("NeoForge", "loader"), "quilt": ("Quilt", "loader"), "architectury": ("Architectury", "loader"),
    "lunar": ("Lunar Client", "client"), "lunarclient": ("Lunar Client", "client"), "apollo": ("Lunar Client", "client"),
    "badlion": ("Badlion", "client"), "blc": ("Badlion", "client"), "feather": ("Feather", "client"),
    "labymod": ("LabyMod", "client"), "labymod3": ("LabyMod", "client"), "essential": ("Essential", "client"),
    "voicechat": ("Simple Voice Chat", "voice"), "plasmo": ("Plasmo Voice", "voice"), "plasmovoice": ("Plasmo Voice", "voice"),
    "xaerominimap": ("Xaero's Minimap", "map"), "xaeroworldmap": ("Xaero's World Map", "map"),
    "xaero": ("Xaero's Minimap", "map"), "journeymap": ("JourneyMap", "map"), "voxelmap": ("VoxelMap", "map"),
    "bluemap": ("BlueMap", "map"), "worldedit": ("WorldEdit CUI", "build"), "axiom": ("Axiom", "build"),
    "litematica": ("Litematica", "build"), "servux": ("Servux/Litematica", "build"), "malilib": ("MaLiLib", "build"),
    "replaymod": ("Replay Mod", "other"), "jei": ("JEI", "recipes"), "roughlyenoughitems": ("REI", "recipes"),
    "rei": ("REI", "recipes"), "emi": ("EMI", "recipes"), "appleskin": ("AppleSkin", "other"),
    "emotecraft": ("Emotecraft", "other"), "figura": ("Figura", "other"), "vivecraft": ("Vivecraft (VR)", "other"),
    "noxesium": ("Noxesium", "other"), "carpet": ("Carpet", "other"), "itemswapper": ("ItemSwapper", "other"),
    "inventoryprofilesnext": ("Inventory Profiles Next", "other"), "jade": ("Jade", "other"), "wthit": ("WTHIT", "other"),
    "waila": ("WAILA", "other"), "freecam": ("Freecam", "other"), "ears": ("Ears", "other"), "skinlayers3d": ("3D Skin Layers", "other"),
    "betterf3": ("BetterF3", "other"), "chunky": ("Chunky", "other"), "flashback": ("Flashback", "other"),
}
MOD_KINDS = {"loader": "Mod-Loader", "client": "Client/Launcher", "map": "Karten", "voice": "Sprachchat",
             "build": "Bauen", "recipes": "Rezepte", "other": "Sonstige"}

PACK_LABELS = {"successfully_loaded": "geladen", "accepted": "angenommen", "downloaded": "heruntergeladen",
               "declined": "abgelehnt", "failed_download": "Download fehlgeschlagen", "failed_reload": "fehlgeschlagen",
               "invalid_url": "fehlgeschlagen", "discarded": "verworfen"}
CHAT_LABELS = {"full": "alles", "system": "nur Befehle", "hidden": "aus", "unknown": "unbekannt"}
HAND_LABELS = {"right": "rechts", "left": "links"}
PARTICLE_LABELS = {"all": "alle", "decreased": "weniger", "minimal": "minimal"}
INPUT_LABELS = {"keyboard_mouse": "Tastatur & Maus", "touch": "Touch", "controller": "Controller", "vr": "VR",
                "unknown": "unbekannt"}

LOCALE_LABELS = {"de": "Deutsch", "en": "Englisch", "fr": "Französisch", "es": "Spanisch", "it": "Italienisch",
                 "nl": "Niederländisch", "pl": "Polnisch", "tr": "Türkisch", "ru": "Russisch", "pt": "Portugiesisch",
                 "cs": "Tschechisch", "sv": "Schwedisch", "da": "Dänisch", "uk": "Ukrainisch", "ja": "Japanisch",
                 "zh": "Chinesisch", "ko": "Koreanisch"}
LOCALE_REGIONS = {"de": "Deutschland", "at": "Österreich", "ch": "Schweiz", "us": "USA", "gb": "UK", "au": "Australien",
                  "ca": "Kanada", "br": "Brasilien", "pt": "Portugal", "es": "Spanien", "fr": "Frankreich",
                  "nl": "Niederlande", "be": "Belgien", "lu": "Luxemburg"}


def ping_quality(ms):
    if ms is None:
        return None
    return "good" if ms < PING_GOOD else "ok" if ms < PING_BAD else "bad"


def ping_bucket(ms):
    return next(label for low, high, label in PING_BUCKETS if ms >= low and (high is None or ms < high))


def ping_hist_row(ms):
    """A histogram with the one sample `ms` (to add to the day)."""
    row = [0] * (len(PING_HIST_EDGES) + 1)
    row[next((i for i, high in enumerate(PING_HIST_EDGES) if ms < high), len(PING_HIST_EDGES))] = 1
    return row


def percentile(hist, q):
    """The q-quantile (0..1) of a ping histogram in ms, linear within its bucket; None without samples. Values in the
    last bucket (from the highest edge on) count as that edge."""
    total = sum(hist or ())
    if not total:
        return None
    target, seen = q * total, 0
    for i, count in enumerate(hist):
        if count and seen + count >= target:
            if i == len(PING_HIST_EDGES):
                return PING_HIST_EDGES[-1]
            low = PING_HIST_EDGES[i - 1] if i else 0
            return round(low + (PING_HIST_EDGES[i] - low) * (target - seen) / count)
        seen += count
    return PING_HIST_EDGES[-1]


def median(values):
    """The median of the values without None, or None."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def client_label(brand, bedrock=False):
    """"Fabric", "Vanilla", ... for the brand a client sends; unknown brands as they are."""
    if bedrock:
        return "Bedrock"
    if not brand:
        return "unbekannt"
    lower = brand.lower()
    return next((label for key, label in CLIENT_BRANDS if key in lower), brand)


def locale_label(code):
    if not code:
        return "–"
    language, _, region = code.partition("_")
    name = LOCALE_LABELS.get(language, code)
    return f"{name} ({LOCALE_REGIONS[region]})" if region in LOCALE_REGIONS and language in LOCALE_LABELS else name


def mods(channels):
    """[(mod name, kind)] recognized in the plugin channels (each mod once, sorted), and the unknown namespaces."""
    found, unknown = {}, set()
    for channel in channels or ():
        namespace = channel.split(":", 1)[0] if ":" in channel else channel.split("|", 1)[0].lower()
        if namespace in ("minecraft", "bungeecord", "velocity", "mcdatalink"):
            continue
        if namespace in MODS:
            name, kind = MODS[namespace]
            found[name] = kind
        elif namespace.startswith("fabric-") or namespace.startswith("fabric_"):
            found["Fabric API"] = "loader"
        else:
            unknown.add(namespace)
    return sorted(found.items(), key=lambda item: (list(MOD_KINDS).index(item[1]), item[0].lower())), sorted(unknown)


def label(mapping, value):
    return mapping.get((value or "").lower(), value) if value else "–"


# Click patterns (plugin 3.30). Intervals below 500 ms count as clicking in a row (see InputRhythm.BURST_MS).
CLICK_HIST_LABELS = ("unter 30 ms", "30–49 ms", "50–69 ms", "70–89 ms", "90–119 ms", "120–159 ms", "160–249 ms",
                     "250–499 ms")
# Hints, never proof: an auto clicker clicks unnaturally evenly. Only judged with enough intervals and when the player
# clicks fast (at least 8 per second in a row), since slow clicks are even anyway. The measured times include the
# jitter of the connection, so a real auto clicker looks a bit less even than it is.
CLICK_MIN_INTERVALS = 150
CLICK_EVEN_MAX_MEAN = 125   # ms between two clicks, i.e. 8 clicks per second or more
CLICK_EVEN_CV = 0.08        # spread of the intervals relative to their mean; people click with 0.15–0.40
CLICK_FAST_CPS = 20         # clicks within one second; butterfly clicking reaches this, normal clicking does not
CLICK_DOUBLE_SHARE = 0.10   # share of intervals below 25 ms: a mouse that double clicks (or butterfly clicking)
CLICK_DOUBLE_MIN = 100
# Rhythm: stalls are silences of the client above 250 ms (1.21.2+) or 1.5 s (older clients).
STALL_TICK_MS, STALL_MOVE_MS = 250, 1500


def click_summary(r):
    """Figures of a summed click pattern (keys as in get_rhythm) or None without clicks in a row: clicks per second
    in a row, spread (cv), share of double clicks and the hints."""
    n = r.get("click_n") or 0
    if n == 0:
        return None
    mean = r["click_sum"] / n
    spread = max(r["click_sq"] / n - mean * mean, 0.0) ** 0.5
    cv = spread / mean if mean > 0 else 0.0
    doubles = (r.get("doubles") or 0) / n
    flags = []
    if n >= CLICK_MIN_INTERVALS and mean <= CLICK_EVEN_MAX_MEAN and cv < CLICK_EVEN_CV:
        flags.append("gleichmäßig")
    if (r.get("cps_max") or 0) >= CLICK_FAST_CPS:
        flags.append("sehr schnell")
    if n >= CLICK_DOUBLE_MIN and doubles >= CLICK_DOUBLE_SHARE:
        flags.append("Doppelklicks")
    return {"n": n, "mean": round(mean), "cps": round(1000 / mean, 1) if mean > 0 else None, "cv": round(cv, 3),
            "cv_percent": round(cv * 100), "doubles_percent": round(doubles * 100), "flags": flags}


def rhythm_summary(r):
    """Stalls per hour of play and the client's ticks per second (1.21.2+ clients) of a summed rhythm."""
    minutes = r.get("rhythm_minutes") or 0
    if minutes == 0:
        return None
    tick_minutes = r.get("tick_minutes") or 0
    return {"minutes": minutes, "stalls": r.get("stalls") or 0,
            "stalls_per_hour": round((r.get("stalls") or 0) * 60 / minutes, 1), "gap_max": r.get("gap_max"),
            "tps": round(r["ticks"] / (tick_minutes * 60), 1) if tick_minutes else None,
            "threshold": STALL_TICK_MS if tick_minutes * 2 >= minutes else STALL_MOVE_MS}
