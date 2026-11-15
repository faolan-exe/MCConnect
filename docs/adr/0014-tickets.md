# 0014: Tickets – player requests to the moderators

Date: 2026-11-15 · Status: accepted · Addon Tickets 1.0, schema 40

## Context

Players want to reach the moderators with a request (a bug, a grief, a question) without hoping that one of them is
online. `/report` (MCDataLink) is for reporting players: one line, no answer. Tickets need an answer and a state.

## Decision

- **An addon, not part of MCDataLink.** Only servers that install it get `/ticket` and the page `/tickets`.
- **Command:** `/ticket add <kategorie> <text>`, `/ticket status [nummer]`, `/ticket close <nummer>`. Every player may
  use it (`tickets.use`, default true). The categories are a list in the addon's settings (default bug, grief,
  hilfe, sonstiges; lower case, letters, digits, `_`, `-`, at most 20 characters), with tab completion.
- **Ticket numbers per server** (1, 2, 3, …): that is what players type. A player can only see and close their own
  tickets.
- **Spam limit:** at most `max_open` open tickets per player (setting, default 3). The text is 5 to 500 characters.
- **Nothing is lost while MCConnect is away** (as with Discoveries, [ADR 0007](0007-discoveries.md)): the addon keeps a
  new ticket in `plugins/Tickets/state.json` until MCConnect answers `done|<request id>`, and sends it again every
  minute. The request id is chosen by the addon and unique per server, so a repeat is stored once. A refused ticket
  (limit reached, category removed meanwhile) is confirmed too and the player is told why. Status and close need the
  connection.
- **The position** (world, x, y, z) is stored with the ticket, for griefs and bugs.
- **Moderators** see the tickets on `/tickets` (menu entry "Tickets" with the number of open ones, only for moderators
  and only while the addon is active). They answer, close and reopen there; each action is in the moderation log.
  Online moderators get a chat line for each new ticket (setting `notify_mods`, default on).
- **The player is told in the chat** about every answer, closing and reopening: at once when online, otherwise with
  the next join (`ticket_events.delivered`). Closing their own ticket is not told back to them.

## Consequences

- Answers are plain text in the chat; colors and chat buttons typed by players or moderators are removed.
- Closed tickets stay (with their history); the page filters by state and category. Tickets of a removed category
  stay visible under their old category.
