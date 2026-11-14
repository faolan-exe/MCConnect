# 0013: Connections and clients of the players, for moderators

Date: 2026-11-14 · Status: accepted · Schema 37 · Plugin 3.28

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
- No IP addresses, no hardware data of Java players.
- Stored: the latest state per player (`player_clients`) and one row per player and day with samples, sum, min and
  max of the ping (`player_ping_daily`, deleted after 90 days); the server's average ping lives in `server_health`
  (7 days like the other health values).
- Only moderators and the owner see it: `/users/spieler#clients` (table, shares of clients, versions and languages
  of the players seen in the last 30 days) and the average ping with its 24 h chart on `/users`. Ping below 80 ms
  counts as good, from 150 ms as high. The privacy page lists the data.

## Consequences

- One small message per minute and server; at most one row per player and day.
- Client brands are what the client says about itself; modified clients can send anything.
