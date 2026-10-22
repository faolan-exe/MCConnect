# ADR 0004: One player login for the servers of one owner

Date: 2026-10-22 · Status: accepted

## Context

Players log in per server with a PIN that the server's plugin shows them in the game. The PIN goes to the plugin, and
the plugin runs on the owner's machine: a modified plugin can claim that any player is online and read the PIN, so
its owner can log in as anyone on that server. On the owner's own server that changes nothing (the owner has all
rights there anyway). A login that counted on all servers would let every owner log in as any player (moderators
included) everywhere. No check of the plugin can prevent this: the owner controls the machine and every key on it.

## Decision

- A PIN login also sets `mcc_player` for the whole domain: the player's Mojang uuid and the server's owner, signed
  with the secret key (expires with the session, HttpOnly, SameSite=Lax).
- On another server of **the same owner** where that uuid has played, the player is logged in automatically, as that
  server's player (own rights and bans per server). Servers of other owners ignore the cookie.
- Sessions started this way depend on the cookie: logging out on one of the servers (or logging in as someone else)
  ends them on all.

## Consequences

- Owners with several servers get one login for their players; nobody can use a server to log in elsewhere.
- A login for all servers needs a proof that does not pass through a plugin: Microsoft login (needs an approved
  Azure app) or an authentication server run by MCConnect that players join once with their game (Mojang's
  `hasJoined` check in the login handshake).
