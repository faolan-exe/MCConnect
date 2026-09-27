# Übergabe: Spielervergleich / Ranglisten

Stand: 27.09.2026, Commit `8df8b7b` (main, gepusht auf `origin` = github.com/faolan-exe/MCConnect).
Diese Datei fasst zusammen, was eine neue Session für das Feature „Spieler eines Servers vergleichen
und gegenüberstellen“ wissen muss, damit sie nicht alles neu erkunden muss.

## Die Aufgabe

Der Nutzer (Tobias) möchte, dass man auf der Seite eines Minecraft-Servers **alle Spieler vergleichen und
gegenüberstellen** kann, z. B. nach Spielzeit, abgebauten Ressourcen, Toden usw. Er hat **keine feste
Vorstellung**, wie das aussehen soll. Erwartet wird, dass du ein Konzept vorschlägst (siehe Ideen unten),
kurz abstimmst und es dann umsetzt.

## Vorlieben des Nutzers (wichtig)

- **UI-Texte auf Deutsch, per „du“.** Code, Kommentare und Commits auf Englisch.
- **Keine „AI-Optik“:** keine typischen AI-Schriften (Inter, Poppins, Space Grotesk, system-ui …), keine
  verspielten AI-Animationen, keine Pixel-Schrift. Er will es „ein bisschen fancy“, aber seriös.
- Schriften (selbst gehostet, `web/static/css/fonts.css`): `--font-display` = **Chakra Petch**
  (Überschriften, Zahlen), `--font-body` = **Atkinson Hyperlegible**, `--font-mono` = **JetBrains Mono**.
  **Keine** externen CDNs für neue Dinge (DSGVO, Google-Fonts-Urteil). Neue Drittanbieter müssten in
  `web/templates/legal_datenschutz.html` ergänzt werden, also besser vermeiden (Charts selbst als SVG/CSS).
- Akzentfarbe Grün `#2fbf62` / `#1f8f47`, dunkel `#0f1512`, Hintergrund `#f1f2f0` (Variablen in `server.css`).
- **Commits ohne jede Claude-/AI-Attribution** (kein `Co-Authored-By`). Committen ist ok, pushen fragt man
  oder der Nutzer macht es selbst.
- Er testet visuell und gibt kurzes Feedback („mach X anders“). Screenshots vor dem Abschluss selbst prüfen
  (Anleitung unten).
- Beim Bauen von Diagrammen: den Skill `dataviz` laden, bevor Chart-Code geschrieben wird.

## Projektüberblick

| Teil | Pfad | Kurz |
|---|---|---|
| DB-Layer | `database/databaseManagerV2.py` | Klasse `DatabaseManager`, Connection-Pool, jede Methode holt eigenen Cursor (`self._cursor()`, `_fetchone/_fetchvalue/_fetchall/_execute`) |
| Stats-Logik | `database/stats.py` | Kategorie-Konstanten, `split_stats`, `format_time` |
| Config | `database/config.py` | alles über `MCC_*`-Env-Variablen |
| DB-CLI | `database/manage.py` | `status/init/reset --yes/seed/create-admin/add-server` |
| Socket-Server | `mc_socket/main.py` | Plugin-Protokoll (im Modul-Docstring dokumentiert) |
| Web | `web/main.py` | Flask App-Factory `create_app(db_manager=None, config_overrides=None)` |
| Plugin | `java plugin/MCDataLink/` | Spigot/Paper 1.13+, Java-8-Bytecode, gegen spigot-api 1.13.2 |
| Deploy | `deploy/` | Docker Compose hinter Nginx Proxy Manager, Anleitung `deploy/README.md` |

### Web-Architektur

- Zwei Blueprints: `main_bp` (Hauptdomain `mc.tobisit.de`: Landingpage, Admin-Bereich) und
  `server_bp` (`subdomain="<subdomain>"`, Serverseiten `<sub>.mc.tobisit.de`).
- `server_bp.url_value_preprocessor` setzt `g.subdomain` und `g.server` (dict aller Server-Spalten ohne Key),
  unbekannte Subdomain → 404. Views bekommen **kein** `subdomain`-Argument.
- `inject_server_context` liefert u. a. `server_name`, `whitelist`, `perm`, `name`, `prefix_colors` an alle
  Server-Templates.
- Login: `logged_in_player_id()`; Decorators `player_required`, `moderator_required` (Server) und
  `admin_required` (Hauptdomain). **POST-APIs nur mit JSON** (CSRF-Schutz), sonst 415.
- Live-Daten: `sse_response(generate_data, interval=2, lifetime=300)` für Server-Sent Events.
- Templates für Serverseiten:
  - `server_base.html`: Basislayout für neue Unterseiten (Header, Fonts, `server.css`, `components.css`,
    Footer, JS-Helfer `postJson`/`showMessage`). **Neue Seiten davon ableiten.** Inhalt steht in
    `<div class="srv-wrap srv-page">` (Abstand für den fixen Header ist dort schon drin).
  - `index-subpage.html`: Server-Startseite (Hero mit Banner, Zahlenleiste, Galerie). Eigenständig, nicht
    von `server_base.html` abgeleitet.
  - `spieler.html` (Spielerliste) und `spieler-info.html` (Spielerseite): **alte Templates des Nutzers**
    (Bootstrap + eigenes CSS). Die Stats-Tabellen der Spielerseite baut JS aus Arrays pro Kategorie, die
    **positionsabhängig** sind (siehe `_get_grouped_stats`). Nur vorsichtig anfassen.
  - `header.html`: gemeinsamer Header mit Menü (Startseite, Spieler, ▼ Prefixes, Verwaltung). Neuer
    Menüpunkt für den Vergleich gehört hierher. Menüpunkte hängen an `config.FEATURE_*`-Flags.
  - `_components.html`: Makros `prefix_chip(text, color, colors)` und `bans_panel(...)`.
- CSS: `server.css` (Klassen `srv-*`, z. B. `srv-card`, `srv-btn`, `srv-number`), `components.css`
  (`mcc-*`: Buttons, Inputs, Tabellen `mcc-table`, Badges, Prefix-Chip), `admin.css` (Hauptdomain).
- Statische Dateien kommen von der Hauptdomain (`url_for('static')` erzeugt auf Subdomains absolute
  URLs); Schriften haben deshalb einen CORS-Header.

## Datenmodell (Schema v6)

Relevant für den Vergleich:

```
player(uuid PK, name)
servers(id, subdomain, server_name, whitelist, ...)
player_server_info(player_id uuid PK, mojang_uuid → player, server_id → servers, online, first_seen,
                   last_seen, prefix_id, web_access_permissions, is_op, ...)   UNIQUE(server_id, mojang_uuid)
actions(player_id → player_server_info ON DELETE CASCADE, category smallint, object text, value bigint)
        PK (player_id, category, object)
prefixes(prefix_id, prefix_owner_id, server_id, prefix_text, color, password_hash)
```

- **Ein Spieler hat pro Server eine eigene `player_id`.** Vergleiche immer über
  `player_server_info.server_id` eingrenzen.
- **`actions` enthält nur den aktuellen Stand** (kumulierte Lebenszeit-Werte aus der Stats-Datei, wird per
  Upsert überschrieben). **Es gibt keine Historie.** Vergleiche „diese Woche / Verlauf“ bräuchten eine neue
  Snapshot-Tabelle (z. B. täglicher Snapshot der wichtigsten Werte pro Spieler).
- Aktualisiert werden die Werte, wenn das Plugin Stats schickt: beim Verbinden (alle Spieler), beim
  Welt-Autosave (Online-Spieler, Paper-Default alle 5 min) und kurz nach dem Verlassen des Servers.
- **Index:** Es gibt nur den PK `(player_id, category, object)`. Für Ranglisten über viele Spieler eines
  Servers (z. B. `WHERE category = 21 AND object = 'minecraft:play_time'`) lohnt ein Index auf
  `(category, object)` oder `(category, object, value DESC)`, als neue Migration.

### Kategorien (`database/stats.py`)

| Gruppe | Kategorien (id) |
|---|---|
| Blöcke | mined 17, used/placed 13, dropped 7, picked_up 3, crafted 2 |
| Items | used 18, dropped 14, picked_up 8, crafted 4 |
| Tools | used 20, broken 15, dropped 10, picked_up 6, crafted 0 |
| Rüstung | used 19, broken 16, dropped 9, picked_up 5, crafted 1 |
| Mobs | killed 12, killed_by 11 |
| Custom | 21 |

Konstanten wie `stats.BLOCK_MINED`, `stats.CUSTOM`, `stats.MOB_KILLED` nutzen, keine Zahlen hartkodieren.

### Custom-Stats (Kategorie 21) aus echten Daten (`sampleData/…json`, DataVersion 3465)

Einheiten beachten:
- **Zeit in Ticks** (÷ 20 = Sekunden): `minecraft:play_time` (ältere Versionen: `minecraft:play_one_minute`),
  `time_since_death`, `time_since_rest`, `sneak_time`, `total_world_time`.
- **Strecken in cm** (÷ 100 = Blöcke/Meter): `walk_one_cm`, `sprint_one_cm`, `crouch_one_cm`, `swim_one_cm`,
  `fly_one_cm`, `aviate_one_cm` (Elytra), `boat_one_cm`, `horse_one_cm`, `minecart_one_cm`, `climb_one_cm`,
  `fall_one_cm`, `walk_on_water_one_cm`, `walk_under_water_one_cm`.
- **Schaden in Zehntel-Herzen** (÷ 10): `damage_dealt`, `damage_taken`, `damage_absorbed`,
  `damage_blocked_by_shield`.
- Zähler: `deaths`, `mob_kills`, `player_kills`, `jump`, `fish_caught`, `animals_bred`, `enchant_item`,
  `traded_with_villager`, `talked_to_villager`, `sleep_in_bed`, `raid_win`, `raid_trigger`, `leave_game`,
  `open_chest`, `open_enderchest`, `open_shulker_box`, `bell_ring`, `play_record`, `interact_with_*`, …

Summen über ganze Kategorien (z. B. „alle abgebauten Blöcke“ = `SUM(value) WHERE category = 17`) sind
sinnvolle Vergleichswerte. `format_time(seconds)` in `stats.py` formatiert Zeiten deutsch („3 Std. 12 Min.“).

### Vorhandene DB-Methoden, die helfen

- `get_players_overview_from_subdomain(sub)` → `[{name, uuid, online}]`, nach Name sortiert
- `get_player_id_from_player_name_and_server_id(name, server_id)`
- `get_value_from_unique_object_from_action_table_with_player_id(object, player_id, category=CUSTOM)`
- `_get_grouped_stats(player_id, categories)` → Liste pro Kategorie (positionsabhängig, für `spieler-info`)
- `get_all_worn_prefixes(server_id)` → `{uuid: (text, color)}` (Prefix neben Namen anzeigen)
- `get_player_info_by_player_id(player_id)` → psi-Spalten + `name`

## Ideen für das Feature (Vorschlag, mit dem Nutzer abstimmen)

1. **Ranglisten-Seite** (`/rangliste` o. ä.): Karten „Top 10“ für die interessantesten Werte (Spielzeit,
   abgebaute Blöcke gesamt, Mob-Kills, Tode, gelaufene Strecke, Diamanten abgebaut, …), jede Zeile mit
   Kopf-Avatar (`https://mc-heads.net/avatar/<uuid>/64`), Prefix-Chip, Wert und Balken relativ zum
   Spitzenwert. Umschalter für die Kennzahl.
2. **Direktvergleich** (`/vergleich?spieler=a,b,c`): 2–4 Spieler auswählen, gegenüberstellen: Kennzahlen als
   gespiegelte Balken bzw. Zeilen mit Gewinner-Markierung, plus Detailtabellen pro Kategorie (Top-Blöcke,
   Top-Mobs), in denen die Spieler nebeneinander stehen.
3. **Server-Rekorde** auf der Startseite (`index-subpage.html`): 3–4 „Rekordhalter“-Kacheln als Teaser mit
   Link zur Rangliste.
4. Optional später: **Anteile** (wer hat wie viel % aller Diamanten abgebaut) und **Verlauf** über
   Snapshots (braucht neue Tabelle + täglichen Job, z. B. im Socket-Server).

Technische Empfehlung: Ranglisten per SQL mit `SUM`/`ORDER BY value DESC LIMIT n` über `actions` JOIN
`player_server_info` (Filter `server_id`) JOIN `player`. Balken als reines CSS/SVG (keine Chart-Bibliothek
vom CDN). Datenschutz: Die Werte sind schon öffentlich (Spielerseiten), neue Seiten ändern daran nichts.

## Migrationen

- `MIGRATIONS` dict in `databaseManagerV2.py`, `SCHEMA_VERSION = max(MIGRATIONS)`, aktuell **6**.
  Neue Migration = neuer Eintrag `7: [sql, ...]`. Läuft beim Start automatisch (Advisory-Lock, sicher bei
  mehreren Prozessen).
- **Beim Hinzufügen einer Migration auch `test_migration_from_version_1` in `tests/test_database.py`
  anpassen:** der Test baut die DB manuell auf v1 zurück und muss die neuen Objekte zuerst entfernen.

## Tests

```bash
docker compose -f docker/db/docker-compose.yml -p mcconnect up -d    # Container mcconnect_db
.venv/bin/python -m pytest -q -p no:logging                           # 234 Tests, ~35 s
```

- Echte Postgres-Test-DB `mcconnect_test` (wird automatisch angelegt/zurückgesetzt), `FakeMinecraft`
  statt playerdb.co. Fixtures in `tests/conftest.py`: `db`, `admin_id`, `server` (Subdomain `testdomain`),
  `other_server`.
- `tests/test_web.py`: `app`, `client`, `on("testdomain")` bzw. `on(None)` für `base_url`,
  `online_player` (Spieler `_Tobias4444` mit Stats), `player_client` (eingeloggt auf testdomain),
  `admin_client`, `first_event(response)` für SSE.
- UUIDs: `PLAYER_UUID` (`_Tobias4444`), `OTHER_UUID` (`Notch`) aus `tests/conftest.py`.
  `db.update_player_stats(player_id, {"stats": {...}})` legt Test-Stats an.

## Lokal ansehen / Screenshots

```bash
.venv/bin/python -m database.manage seed          # Dev-Admin tobi / testPassword, Server testdomain
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from web.main import create_app; create_app().run(host='127.0.0.1', port=5000, threaded=True)"
```

- `SERVER_NAME` lokal: `mc.t-auer.local:5000` (aus `web/config.py`). Aufruf per Chrome mit
  `--host-resolver-rules="MAP *.mc.t-auer.local 127.0.0.1, MAP mc.t-auer.local 127.0.0.1"`.
- Headless-Chrome-Screenshots: `--headless=new --user-data-dir=<scratchpad>/chrome-$RANDOM --timeout=6000
  --screenshot=… --window-size=1280,1400` im Hintergrund starten und nach der Datei pollen.
  **Nicht** `--virtual-time-budget` nutzen, offene SSE-Verbindungen blockieren das.
  Headless-Mindestbreite ist **500 px** (für „Mobil“ 500 statt 400 nehmen).
- Eingeloggte Seiten: mit `app.test_client()` und `session_transaction(base_url=…)` rendern
  (`player_id`, `server_id`, `uuid` setzen), HTML speichern, `"/static/` auf
  `http://mc.t-auer.local:5000/static/` umschreiben, Datei screenshotten.
- Dev-DB enthält auf `testdomain` die Spieler `_Tobias4444` (echte Stats aus `sampleData`), `Notch`, `jeb_`.
  Für einen Vergleich weitere Spieler mit Fake-Stats anlegen (`ensure_player_on_server` +
  `update_player_stats`).
- Maven ist **nicht** installiert (brew scheitert an der Xcode-Lizenz). Plugin-Build nur nötig, wenn das
  Plugin geändert wird: Maven-Tarball von archive.apache.org ins Scratchpad laden. Für dieses Feature ist
  vermutlich keine Plugin-Änderung nötig.

## Deploy beim Nutzer

Proxmox-LXC, `/opt/mcconnect`, hinter Nginx Proxy Manager. Update:
`cd /opt/mcconnect && git pull && cd deploy && docker compose up -d --build`.

## Offene Punkte aus der alten Session (nicht Teil dieser Aufgabe)

- Plugin-Features (Prefix im Spiel, Bans, Ingame-Banliste) sind noch nicht auf einem echten Server getestet.
- `/pardon` im Spiel hebt einen Website-Ban auf der Website nicht auf.
- Login-Link im Header hat noch eine Farbanimation (`mymove` in `header.css`); Nutzer noch nicht gefragt.
- `database/manage.py seed` ist nur für Entwicklung gedacht (Frage offen, ob in Produktion sperren).
- Socket-Verbindung Plugin ↔ Server ist unverschlüsselt (Key im Klartext).
