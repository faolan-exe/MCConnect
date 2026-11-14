# 0013: Connections and clients of the players, for moderators

Date: 2026-11-14 · Status: accepted · Schema 37/38 · Plugin 3.28/3.29

## Context

Moderators want to see why a player lags and what the community plays with: the average ping, and which client,
version and language players use. The server health only knew TPS and memory.

## Decision

- The plugin sends `!CLIENTS` once a minute with one entry per online player (`ClientReporter`): ping
  (`Player.getPing()` since 1.17, the NMS field before), client brand (Paper's `getClientBrandName()`, otherwise the
  `minecraft:brand` channel), client version (only with ViaVersion; without it everyone plays the server's version),
  language, view distance (`getClientViewDistance()` where it exists) and, with Floodgate, Bedrock and the device.
  Everything newer than the 1.13 API is read by reflection, so old servers send fewer fields. The health sample gets
  `ping_avg` of the online players.
- Plugin 3.29 adds everything else a server can learn about a client: the plugin channels it registered (they show
  mods such as voice chat, minimaps, WorldEdit CUI; `database/clients.py` names them), the status of the resource pack,
  Paper's client options (chat visibility, chat colors, text filter, server listings, particles, cape and skin layers),
  the main hand, the address the player joined with (host name the client typed, not its IP) and, with Floodgate, input
  mode, UI profile and Bedrock version. A field an older plugin does not send keeps its last value.
- No IP addresses, no hardware data of Java players.
- Stored: the latest state per player (`player_clients`) and one row per player and day with samples, sum, min and
  max of the ping (`player_ping_daily`, deleted after 90 days); the server's average ping lives in `server_health`
  (7 days like the other health values).
- Only moderators and the owner see it: the menu entry "Analyse" (`/analyse`: key figures, ping charts by hour, day
  and player, shares of clients, mod loaders, mods, versions, languages, client options, resource pack, join addresses
  and Bedrock details of the players seen in the last 30 days, and a page per player `/analyse/spieler/<name>` with 90
  days of ping and the client changes) and the average ping with its 24 h chart on `/users`. A sample of 250 ms or more
  counts as a ping spike; per day the client, version and whether mods were seen are kept for trends. Ping below 80 ms
  counts as good, from 150 ms as high. The privacy page lists the data.

## Consequences

- One small message per minute and server; at most one row per player and day.
- Client brands are what the client says about itself; modified clients can send anything.
