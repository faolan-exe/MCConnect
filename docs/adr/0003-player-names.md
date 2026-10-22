# ADR 0003: Mojang decides player names

Date: 2026-10-22 · Status: accepted · schema 31

## Context

Player names live in one table for all servers (`player`, keyed by the Minecraft uuid). Until now every name a plugin
reported with `!JOIN` overwrote the stored one. Anyone can sign up and connect a server, so a modified plugin could
rename any player on every MCConnect server (rankings, guestbooks, moderation log), e.g. give a known player the name
of another one. The name lookup for unknown uuids went through a third-party service (playerdb.co).

## Decision

1. **Mojang is the single source of truth** for its accounts: the session server
   (`sessionserver.mojang.com/session/minecraft/profile/<uuid>`) replaces playerdb.co.
2. A reported name that differs from the stored one is **checked** (`DatabaseManager.trusted_name`), depending on the
   kind of uuid:
   - **Mojang account** (uuid version 4): the name Mojang returns is stored, whatever the plugin sent. Real name
     changes arrive with the next join on any server.
   - **Offline-mode server** (version 3): the uuid is made from the name (`OfflinePlayer:<name>`), so it proves the
     name; another name is ignored.
   - **Everything else** (Bedrock players via Floodgate, or Mojang not reachable / not knowing the uuid): only the
     first name is taken, later changes are ignored.
3. Mojang is only asked when a name changes or a uuid is new, not on every join.

## Consequences

- A server cannot rename players any more (except unverifiable ones before anyone else reported them).
- Bedrock players keep their first name when they change their gamertag. If that matters, the Geyser API
  (xuid → gamertag) could become the source for them.
- While Mojang is unreachable, a join with a changed name waits for the lookup timeout (5 s) and keeps the old name.
