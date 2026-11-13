# 0008: The plugin confirms bans, unbans and removals from the whitelist

Date: 2026-11-13 · Status: accepted · Plugin 3.27, schema 33

## Context

After [ADR 0006](0006-whitelist-confirmation.md) the other orders to the plugin were checked for the same gap
(counted as done when sent):

- **Website bans** are name bans. On newer servers `addBan` silently does nothing when the server cannot look the
  name up. The ban was marked as delivered anyway, and the next ban list sync (`!WEBBANS`) took the missing ban for a
  `/pardon` in the game and **deleted the website ban**.
- **Unbans** while the plugin was offline were lost: the player stayed banned in the game.
- **Removals from the whitelist** had no feedback.
- **Alert mails** were marked as sent before the mail went out; an SMTP error meant no alert for 6 hours.

## Decision

- Plugin 3.27 (`!FEATURES~…,confirm`) answers `!BANNED~uuid|1/0`, `!UNBANNED~uuid|1/0|name` and
  `!UNWHITELISTED~name|1/0`, checked against the ban list / whitelist after the command.
- A website ban counts as delivered only after `1`. An undelivered ban is never taken for a pardon, and is sent
  again every 5 minutes and after every reconnect. The player is kicked either way.
- Unbans and removals from the whitelist are stored in `plugin_tasks` and kept until the plugin confirms them
  (older plugins: until sent). They are sent after `!FEATURES`, so it is known whether an answer will come; again every
  5 minutes and after every reconnect. A new ban cancels a pending unban; accepting a name again (code, application)
  cancels its pending removal.
- Alert mails: the mailer calls back when a mail cannot be sent; the alert is then due again on the next check.

## Consequences

- Older plugins keep the old behaviour (done when sent).
- A ban that keeps failing (e.g. a name the server can never look up) is retried every 5 minutes; the player is kicked
  on each try while online.
