"""Connections and clients of the players (plugin 3.28/3.29, ADR 0013): what the values mean and how they are named.

Mods are recognized by the plugin channels a client registers. A mod that registers none (most performance mods, many
cheat clients) cannot be seen; a channel list is what the client says about itself.
"""

# Ping in ms: below PING_GOOD nobody notices, above PING_BAD hits and blocks lag behind; a sample of PING_SPIKE or more
# counts as a spike.
PING_GOOD, PING_BAD, PING_SPIKE = 80, 150, 250
PING_BUCKETS = ((0, 50, "unter 50 ms"), (50, 80, "50–79 ms"), (80, 150, "80–149 ms"), (150, 250, "150–249 ms"),
                (250, None, "ab 250 ms"))

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
