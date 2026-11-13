# 0007: Discoveries – places with own advancements

Date: 2026-11-13 · Status: accepted · Addon Discoveries 1.0, schema 32

## Context

Owners want to define places (a village, a cave, a build). The first time a player enters one, it should be announced
like an advancement in the game and shown on the website.

## Decision

- **An addon, not part of MCDataLink.** Places are rectangles and circles in the addon's settings (website and
  `/discovery pos1|pos2|create|circle|delete|list`), each with a name (lower case = id), title, text and icon item.
  A new name is a new place. Switched off places count nowhere and cannot be found; their discoveries stay.
- **Real advancements.** Each place is an advancement (`discoveries:<id>`, hidden until found, toast and chat line by the
  game) under the root `discoveries:root`, the tab "Entdeckungen" (granted silently with the first place). They are
  added at runtime with `UnsafeValues.loadAdvancement` (the only way on Spigot and Paper; the server stores them in the
  `bukkit` data pack). The JSON depends on the version: icon `item` before 1.20.5, `id` since; the tab background with
  `textures/…png` before 1.21.5, relative since. Changed places are removed and loaded again, deleted ones removed;
  pausing the addon keeps them, since a removed advancement drops the players' progress.
- **The game remembers who found what** (advancement progress). Only after the decision to give the advancement the
  addon reports `found|uuid|place|ms` to MCConnect and keeps it in `plugins/Discoveries/state.json` until MCConnect
  answers `ok|uuid|place` (sent again every minute and on activation). Nothing is lost while MCConnect is away
  (lesson from [ADR 0006](0006-whitelist-confirmation.md)).
- **First discoverer = the earliest visit** (time of the game, never in the future; ties: player id), the same on the
  player page, the overview and the feed, also when a visit is reported late. MCConnect's extra chat line ("… als Erster
  entdeckt!", can be switched off) goes out when the first report of a place arrives.
- **Only from the creation on.** Players who stand in a place when it is created find it on their next check
  (every half second), earlier visits are unknown to the game.
- **Website:** player page "Entdeckungen · x von y" (found places with date, the others as "???"), page
  `/entdeckungen` (all places, how many found them, the first discoverer; places nobody found yet stay secret) and the
  live feed. Players who hide their stats count but are not named; spectators find nothing.

## Consequences

- `UnsafeValues` is not a stable API. If a server version refuses the JSON, the addon reports the problem on the
  website and that place stays inactive.
- Renaming a place (new name) lets everybody discover it again; changing its title, text or icon does not.
