# 0009: Protected addon fields and item data in recipes

Date: 2026-11-13 · Status: accepted · EasyNewCraftingRecipes 2.1, schema 34

## Context

Owners want recipe results with extras (invisible item frames, a knockback stick, …), also any item data that `/give`
accepts. Free item data can create dangerous items (e.g. a spawn egg with a command block minecart). Moderators may
edit addon settings when the owner allows it (`servers.addon_mods`).

## Decision

- **Item data** (`result_data`): optional text like `/give` (`item[components]` since 1.20.5, `item{nbt}` before; a
  pasted `/give @p … 5` is cut to the item). Parsed by the server itself (`ItemFactory.createItemStack` by reflection,
  else `UnsafeValues.modifyItemStack`); a mistake is shown as a problem on the website. The help text warns that the
  syntax depends on the Minecraft version.
- **Presets** for common items, built for the running version: invisible (glow) item frame, invisible / small / armed
  armor stand (entity data), knockback stick, unbreakable elytra, rocket with power 3 (Bukkit API), and throwable
  fireballs (ghast-like: explodes with power 1.5, no fire; blaze-like: sets fire). Fireballs are handled by the addon:
  right click with an item equal to the result (`isSimilar`: type, name, lore) throws one, one second cooldown, one item
  used outside of creative.
- **Protected fields:** a manifest field may say `"owner_only": true`. Moderators cannot change such a value (website:
  greyed out and refused with 403; in-game `!ADDONCONFIG` refused) unless the owner switches on "Moderatoren dürfen auch
  geschützte Felder ändern" (`servers.addon_mods_protected`). In lists a row is matched to the old row with the same
  title; moderators may still add (without data), move and remove rows.

## Consequences

- The risk is small for this server (moderators are OPs), but the default protects servers where they are not.
- An invisible item frame placed from item data becomes a normal one when it is broken (vanilla behaviour).
- Renaming a recipe that has item data counts as changing the data for moderators.
