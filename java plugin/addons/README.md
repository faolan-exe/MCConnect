# MCConnect addons

Plugins that extend MCConnect through MCDataLink (3.22+). Background and security model:
[docs/adr/0001-addon-system.md](../../docs/adr/0001-addon-system.md).

| Addon | What it does | In game |
|---|---|---|
| BetterJoinMessages | MCConnect's join/leave messages (reward levels, `/profil`), first-join message, greeting, fixed lines per player, silent players, no message on quick reconnects | `/joinmessage` |
| Discoveries | places to discover (rectangles, circles): the first visit gives a real advancement (toast, chat line, own tab), the website shows who found what and the first discoverers ([ADR 0007](../../docs/adr/0007-discoveries.md)) | `/discovery` |
| EasyHeadDrop | player heads (date, killer, weapon) with chance, looting bonus and anti-farming cooldown; optional mob heads and chat announcement | – |
| EasyNewCraftingRecipes | own shaped, shapeless and furnace recipes, applied without a restart, problems shown on the website; special results (invisible item frames, knockback stick, throwable fireballs, …) and own item data like `/give` (owner only, [ADR 0009](../../docs/adr/0009-protected-addon-fields.md)) | – |
| SpawnElytra | double jump in rectangles and circles, each with its own boost, starts a glide without an elytra, no fall damage | `/spawnelytra` |
| SpawnProtect | protected regions (box or circle around the spawn) with 13 protections, bypass, greeting | `/spawnprotect` (`/sprot`) |

Build: `mvn package` in `java plugin/` (builds MCDataLink and all addons). The Dockerfile copies the addon jars to
`MCC_ADDON_DIR`; that folder is the store.

## Writing an addon

1. `plugin.yml`: `depend: [MCDataLink]`, compile against `org.tobias:MCDataLink` (scope `provided`).
2. `src/main/resources/mcconnect-addon.json`:

   ```json
   {"id": "chatgames", "name": "ChatGames", "summary": "Kleine Ratespiele im Chat.",
    "description": "…", "features": ["…"],
    "settings": [{"title": "Spiel", "fields": [
      {"key": "interval", "type": "number", "min": 1, "max": 60, "default": 10, "unit": "Minuten", "label": "Abstand"},
      {"key": "prize", "type": "material", "default": "diamond", "label": "Preis"}]}]}
   ```

   Field types and their options are documented in `database/addons.py`. `"owner_only": true` protects a field:
   moderators (and in-game changes) cannot change it unless the owner allows it. `show_if` hides a field unless a sibling
   has a value (`{"key": "mode", "in": ["custom"]}` or `{"key": "enabled", "is": true}`).
3. Register in `onEnable` and react to the settings:

   ```java
   handle = MCConnect.register(this, new Addon() {
       public void onActivate(AddonConfig config) { interval = config.getInt("interval", 10); start(); }
       public void onDeactivate() { stop(); }
   });
   ```

   `onActivate` comes when the owner approved the addon and again after every change; `onDeactivate` when it is
   paused or not approved. Callbacks run on the main thread. `AddonHandle` also offers `chat(markup)` (MCConnect's
   colors, icons, tooltips), `saveConfig(json, player)` (change the own settings from a command), `reportProblems`
   (shown on the website), `command(player, name, args)` and `send(message)` (to a handler in
   `mc_socket/main.py: ADDON_HANDLERS`).
4. Drop the jar into `plugins/` and restart: it shows up under *Erweiterungen* and waits for the owner's approval.
