# 0011: Names without a Mojang account are not sent to the whitelist again

Date: 2026-11-14 · Status: accepted · Schema 35

## Context

Since [ADR 0006](0006-whitelist-confirmation.md) an accepted name counts as whitelisted only after the plugin confirms
it, and is sent again every 5 minutes otherwise. A name without a Mojang account (a typo in an application, e.g.
"MrSchief4120") can never be whitelisted by an online mode server. It was sent forever, and every try put two lines
into the server console:

    WARN: Couldn't find profile with name: MrSchief4120
    INFO: System chat: That player does not exist

## Decision

- When the plugin answers `!WHITELISTED~<name>|0`, the socket server asks Mojang
  (`api.mojang.com/users/profiles/minecraft/<name>`, in its own thread).
- Only a clear "no such account" (204, 404, 400) marks the request `unknown_name`: it is not sent again. Mojang down,
  rate limited or not reachable says nothing: the name stays in the retry of ADR 0006.
- The moderation page shows such names ("diesen Minecraft-Namen gibt es nicht – Tippfehler?").
- Sending the name again clears the mark: a code redeemed with that name again, or a later `|1` of the plugin.

## Consequences

- Offline mode servers are not affected: they whitelist any name and answer `|1`, so Mojang is never asked.
- A player whose account is created after the application is sent again when a code is redeemed with the name.
