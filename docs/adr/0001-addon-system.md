# ADR 0001: Addons – an open plug-and-play system on top of MCDataLink

Date: 2026-10-22 · Status: accepted · Plugin 3.22, schema 28/29

## Context

MCDataLink (the connector plugin) grew every feature into one jar, including join/leave messages. Server owners
should be able to add functions (join messages, head drops, own recipes, spawn protection, ...) without each of
them needing its own API key, and other developers should be able to extend MCConnect the same way.

## Decision

1. **Addons are normal Bukkit plugins** with `depend: [MCDataLink]` and a `mcconnect-addon.json` (id, name, summary,
   description, features, **settings description**). In `onEnable` they call `MCConnect.register(this, addon)`
   (package `org.tobias.mcdatalink.api`). Everything goes through MCDataLink's existing pinned TLS connection; an
   addon has no key and no connection of its own.
2. **MCConnect holds the settings.** The settings page on `/manage/<id>#addons` is rendered from the addon's
   settings description (types: toggle, number, text, lines, select, multiselect, material, recipe, position,
   list). New addons need no change in MCConnect. MCConnect checks every value (`database/addons.normalize`), also
   settings an addon changes itself from an in-game command (`!ADDONCONFIG`). MCDataLink caches the last approved
   settings (`plugins/MCDataLink/addons/<id>.json`), so e.g. a spawn protection works right after a restart while
   MCConnect is not reachable.
3. **Approval by the owner.** An addon does nothing and gets no messages until the owner approved it. The approval is
   bound to the SHA-256 of the jar: a changed jar is locked again (`changed`) unless it is an official release.
   Official releases (every jar the store ever offered, `addon_releases`) are approved automatically, so do store
   installs. MCDataLink only accepts a registration whose addon class comes from the registering plugin's own class
   loader, and one plugin per id.
4. **Store with automatic install.** The store lists the jars in `MCC_ADDON_DIR` (Docker: built from
   `java plugin/addons/`). "Installieren" sends `!addoninstall` with URL and SHA-256 over the pinned connection;
   MCDataLink downloads, checks the checksum and that the jar is that addon, puts it into `plugins/` and loads and
   enables it at runtime (`PluginManager.loadPlugin`). Updates of a running addon go through the update folder
   (active after a restart). `addon-install: false` in MCDataLink's config switches remote installs off. Removing
   disables the plugin and deletes only a jar whose `mcconnect-addon.json` has that id.
5. **Join messages moved out.** MCDataLink 3.22 has no join messages any more; the addon BetterJoinMessages shows
   them. MCConnect sends the join protocol (`!joinstyle`, `!joinmutes`, `!joindefault`, `!joinreset`) only to it
   (`!addon~betterjoinmessages|...`) and only while it is active. Without it the reward levels, `/joinmessage`, the
   join part of `/profil` and the "level" line under the names are off. Plugins before 3.22 get `!joinreset~` only.

## Consequences

- Official addons: BetterJoinMessages, EasyHeadDrop, EasyNewCraftingRecipes, SpawnElytra, SpawnProtect (rewrites of
  the old standalone plugins with their bugs fixed). The old PermissionSystem was not ported: none of the addons
  needs it, their permissions default to OP and work with LuckPerms.
- **Security limits (honest):** all plugins share one JVM. Approval and the class loader check stop a plugin from
  using MCConnect by accident or by simply registering under another name, and make unknown addons visible; they do
  not stop a malicious plugin that uses reflection. The real protection stays: only install plugins you trust.
- Hot loading works for plain Bukkit plugins on Spigot and Paper; plugins that need `onLoad` (e.g. world generators)
  would need a restart – none of the official addons uses it.
- A server whose owner does not install BetterJoinMessages loses custom join messages with the update (wanted).

## Follow-up decisions (2026-10-22, schema 29)

- The owner can let **moderators edit addon settings** (`servers.addon_mods`, page `/users/erweiterungen`). Installing,
  approving, pausing and removing stay with the owner.
- The owner can switch on **automatic updates** (`servers.addon_auto_update`): when the plugin reports its addons (or
  the switch is turned on), newer store versions of *official* jars are installed through the update folder (active
  after the next restart). Own builds with the same id are never replaced.
