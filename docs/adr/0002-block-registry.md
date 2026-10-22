# ADR 0002: Block registry that keeps up with new Minecraft versions and mods

Date: 2026-10-22 · Status: accepted · Plugin 3.24, schema 30

## Context

The stats files only name objects (`minecraft:acacia_shelf`). Whether an object is a block decides its category:
a placed block is `BLOCK_USED` and counts for "Blöcke platziert", a used item is `ITEM_USED` and does not
(`database/stats.py`). The block list came once from `database/blocks.json` (an old minecraft-data export) when the
database was created and never changed. By Minecraft 26.3 it lacked 235 blocks (shelves, concrete/wool slabs and
stairs, copper chests, ...), so placing them did not count as placed blocks. Blocks of mods (hybrid servers like
Mohist/Arclight) and of data packs were never known.

## Decision

1. **Three sources, all only adding names** (`database/registry.py`):
   - `database/registry.json`: the vanilla registry of a release, shipped with the code (offline baseline). The
     socket server applies it on every start.
   - **misode/mcmeta** (`summary` branch, `registries/data.min.json`): generated automatically for every release and
     snapshot. The socket server fetches it on start and every `MCC_REGISTRY_SYNC_HOURS` (24) with `If-None-Match`,
     retries after an hour on errors, and can be switched off with `MCC_REGISTRY_URL=off`. Snapshots are included on
     purpose: their names are harmless before a server runs them, and the blocks are known on release day.
   - **The plugin** (3.24) sends `!REGISTRY` right after the auth, before the stats: `Material` (blocks and items,
     without legacy entries; hybrid servers add the mods' materials there) plus Paper's `Registry.BLOCK`/`Registry.ITEM`
     by reflection (also data-driven content). That is exactly what the server can produce, whatever its version.
2. **Vanilla names globally, everything a plugin reports per server** (`server_registry`). A server could report
   anything; per server it can only change the categories of its own players. Stats of a server are sorted with the
   global lists plus that server's own.
3. **Names keep their namespace unless it is `minecraft`** (`stats.lookup_name`): `stone`, but `create:brass_block`.
   Before, the namespace was stripped, so a mod's block was taken for a vanilla one of the same name.
4. **Names are never removed.** A removed mod still has values in the stats files; dropping its names would store
   them as items again next to the block rows.
5. **Learned blocks move stored values** (`DatabaseManager._reclassify`): values of a name that is now a block move
   from the item categories (used, dropped, picked up, crafted) to the block ones; a leftover pair keeps the higher
   value. Every snapshot of the affected players and metrics moves by the same amount, so the correction is no gain
   (rankings of a period, competitions, goals, duels stay right) – the values count as if they had always been there.
6. **Locking.** Stats updates take a shared advisory lock, a change of the lists an exclusive one, and the in-memory
   lists change before that commit. So no stats update can store a name as an item after it was moved.

## Consequences

- New blocks count correctly from the day mcmeta has them (usually the day of the snapshot), on plugin 3.24 from
  the first connection even without internet access of MCConnect.
- On the first start after this change, the 235 missing blocks move once; players who placed them get a higher
  "Blöcke platziert" without a gain. `record_history` is not rewritten: a player may take a "blocks placed" record
  with the corrected value (announced like any record, at most `MAX_ANNOUNCED_RECORDS`).
- MCConnect depends on a community project (mcmeta) for the online list; if it disappears, the shipped list and the
  plugins' reports still work. Update `database/registry.json` with a Minecraft release:
  `curl -s https://raw.githubusercontent.com/misode/mcmeta/<version>-summary/registries/data.min.json` and keep
  `{"version", "blocks", "items" (items that are no blocks)}`.
- Unknown objects keep the old fallbacks: `mined` is always a block, everything else an item until a source knows it.
