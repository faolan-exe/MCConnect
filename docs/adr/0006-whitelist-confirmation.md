# 0006: The plugin confirms whitelist entries

Date: 2026-11-13 · Status: accepted · Plugin 3.26

## Context

Accepted names (application or invite code) reach the server as `!whitelist~add|<name>`; the plugin runs
`whitelist add <name>`. The game looks the name up at Mojang unless it knows the player already. On 2026-11-13 Mojang
answered "Service unavailable": the game logged "That player does not exist" and added nobody, while MCConnect had
marked the names as synced when it sent them. The players were "freigeschaltet" on the website but kicked by the server.

## Decision

- The plugin checks after `whitelist add` whether the name is on the whitelist and answers `!WHITELISTED~<name>|<1|0>`
  (`!FEATURES~…,whitelistack`).
- With `whitelistack` a name counts as synced only after `1`. Unsynced names are sent again every 5 minutes while the
  plugin is connected and after every reconnect. On `0` the moderation page shows the name as waiting for the plugin.
- Older plugins: unchanged (synced when sent).
- Not done: looking up the uuid at Mojang on the website and whitelisting by uuid. MCConnect supports offline-mode
  servers, where players need no Mojang account and the uuid is made from the name; rejecting unknown names or sending
  a Mojang uuid would break them.

## Consequences

- A temporary Mojang outage only delays the entry; it no longer loses it.
- A name that does not exist (typo) is retried every 5 minutes and stays "wartet aufs Plugin" until a moderator sees it.
- A player who is already accepted can redeem a valid code again to be sent once more (without using it up).
