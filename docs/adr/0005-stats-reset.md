# 0005: Resetting stats

Date: 2026-11-08 · Status: accepted · Plugin 3.25

## Context

Owners want to set the stats of a player or of the whole server back to 0. Emptying a `stats.json` has no effect:
`update_player_stats` only writes the values it receives, and the game keeps the stats of online players in memory
and writes them back to the file.

## Decision

- Only the owner of the server can do it (`/manage/<id>`, Gefahrenzone), with a confirmation. Moderators cannot.
- The plugin must be connected and report the feature `statsreset`. A reset while it is away would be undone by the
  next sync, so it is refused.
- Order: the website sends `!statsreset~<all|uuid>`, the plugin clears online players in memory and deletes the stats
  files, then answers `!STATSRESET`. Only `ok` clears the database. The answer is sent through the same queue as the
  stats, so older stats cannot arrive after it.
- Deleted: current values, daily snapshots, achievements, trophies, records, streak badges, finished sessions and the
  reward level. Kept: profile, rights, bans, prefixes, the first join and with it the anniversaries (they depend only on
  the date and would be reached again at once), open sessions.
- The reset cannot be undone. The result is written to the moderation log.

## Consequences

- Plugins before 3.25 cannot reset; the website says so.
- Competitions, goals and duels stay. Gains of the affected players count again from the reset on.
- Statistics the Bukkit API does not know (some mods) stay in the memory of players online during the reset.
