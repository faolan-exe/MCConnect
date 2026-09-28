# MCConnect

MCConnect is a further development of my [other mc-stats site.](https://github.com/Tobias-Auer/Full-Stack/tree/main/minecraft_webserver)


This will be version 2.0, with an improved and optimized backend with Postgres.
Unlike before, this program no longer needs to run on the same file system as the Minecraft server.
In addition, the website will support multiple Minecraft servers. 

Anyone will be able to add their own Minecraft server to this server with a simple, lightweight minecraft plugin.

Preview:
- Custom Server page for everyone
![Screenshot](%23readmeImages/Startseite.png)

- Player list and status information for every player ever joined the server
![Screenshot](%23readmeImages/SpielerUebersicht.png)

- Detailed player statistics for every player
![Screenshot](%23readmeImages/SpielerDetails.png)

- Privacy friendly and easy login for every player to view and change personal data:
![Screenshot](%23readmeImages/Login.png)

- Rankings, player comparison, teams (prefixes), competitions, achievements, server statistics,
  weekly recap and an activity feed
- And a lot more...

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
docker compose -f docker/db/docker-compose.yml -p mcconnect up -d   # local postgres
.venv/bin/python -m pytest -q -p no:logging                         # ~410 tests, uses database mcconnect_test
.venv/bin/python -m database.manage seed                            # dev admin 'tobi' + server 'testdomain'
.venv/bin/python mc_socket/main.py                                  # plugin socket on :9991 (TLS, dev certificate)
.venv/bin/python web/main.py                                        # http://mc.t-auer.local:5000
```

- `database/` – DB layer (`databaseManagerV2.py`), schema + migrations, stats classification, config (`MCC_*` env vars)
- `mc_socket/` – socket server the plugin connects to (protocol documented in `main.py`)
- `web/` – Flask app (main domain: admin area; `<subdomain>.`: server pages)
- `java plugin/MCDataLink/` – Spigot/Paper plugin (`mvn package`)
- `deploy/` – production docker compose (behind Nginx Proxy Manager, optional traefik); see [deploy/README.md](deploy/README.md)

## Status and next steps

State of 2026-09-28 (schema version 19, plugin 3.11). Written as a handoff for the next development
session; `docs/HANDOFF_spielervergleich.md` is the older handoff for the rankings feature (data model,
stat units, screenshot workflow) and still useful as background.

### Done

| Area | Where | Notes |
|---|---|---|
| Rankings | `/rangliste` | 19 metrics in 4 groups (`database/metrics.py`), all time / 30 / 7 days |
| Comparison | `/vergleich` | 2–4 players, best values, history chart, detail tables; `+` buttons collect players (`web/static/compare.js`) |
| Player list | `/spieler` | search, sort, "last online" filters, badges, top places, favourites |
| Player page | `/spieler?player=` | first/last seen with time, activity chart, ranking places, highlights, achievements (live), bio, stat card, moderator notes |
| Server statistics | `/server-statistik` | online history, peak-time heatmap, totals, new players; based on `player_sessions` |
| Start page | `/` | records, weekly recap, running competition, activity feed (`/api/feed`) |
| Achievements | `database/achievements.py` | 13 × 4 tiers, stored in `player_achievements`; tiers reached while not playing are `silent` (no feed, no chat) |
| Teams | `/teams` | prefixes as teams |
| Competitions | `/wettbewerbe`, created on `/users` | start/end announced in chat by the socket server |
| Profile & privacy | `/profil` | bio, "hide my stats" (`hide_stats`: left out of every public view except server totals), favourites |
| Stat card | `/spieler/<name>/karte.png` | Pillow, fonts in `web/card_fonts/`, used as `og:image` |
| Moderation | `/users` (moderators only) | server health (TPS, RAM, uptime, 24 h charts, availability), log of all moderation actions, inactive players, competitions, bans |
| Motivation (phase 5) | `database/motivation.py` | streaks + badges 7/30/100, anniversaries (`player_milestones`), record history (`record_history`, cooldown against flip-flops), community goals (`community_goals`, `/wettbewerbe#ziele`, created on `/users`), trophies (`trophies`: competition places, player of the week), fun facts on `/server-statistik`, trophy cabinet on the player page, hall of fame `/ruhmeshalle`; news in feed and chat |
| In game (phase 6) | `mc_socket/commands.py` | `/stats`, `/top`, `/wettbewerb`, `/duell`, `/report`, `/seitenleiste` answered by the socket server (`!CMD` → `!tell`); scoreboard sidebar (`!sidebar`, setting also on `/profil`); duels (`duels`, `/duelle`), reports (`reports`, `/melden`, list on `/users`, online moderators get a chat message) |
| Community (phase 7) | `/events`, `/umfragen`, `/galerie`, player page | event calendar with sign up and chat reminder (`events`, `event_signups`, `/events` in game), polls (`polls`, `poll_votes`, `/vote`, result in chat), build gallery with approval and likes (`builds`, `build_likes`, uploads like the server images), guestbook on the player page (`guestbook`, report/delete); created and moderated on `/users` (jump links at the top) |
| Moderation & access (phase 8) | `/mitmachen`, `/regeln`, `/users`, `/manage` | whitelist access per server by application and/or invite code (`access_requests`, `invite_codes`; the plugin runs `whitelist add`, also after a reconnect; the kick message links to `/mitmachen`) – codes can only be entered on the website, a player who is not on the whitelist cannot run commands; warnings with automatic ban after X (`warnings`), mutes (chat and `/msg`), also `/verwarnen`, `/stumm`, `/entstummen` in the game; X-ray hints; e-mail alerts to the owner (offline > 5 min, TPS < 15, sent by the socket server); rules & FAQ page, link on the first join; server health on the admin page |
| Reach & design (phase 9) | `/rueckblick/<name>`, main page, `web/static/css/dark.css` | year in review to click through and share (year gains from the first/last snapshot of the year – both are now kept forever, sessions, achievements, trophies, records); public server directory on the main domain (`servers.listed`, opt out on `/manage`); dark mode for all server pages incl. the old ones: `_theme.html` sets `<html data-dark>` from the system setting or the switch in the header (Auto/Dunkel/Hell, localStorage), `dark.css` redefines the `--srv-*` tokens and overrides the hard-coded colors |
| Plugin | `java plugin/MCDataLink` | 3.1 `!broadcast` (chat), 3.2 `!HEALTH` every minute, 3.3 live stats of online players every minute (`live-stats` in config.yml), 3.4 in-game commands and sidebar (`InGameCommands`, `Sidebar`), 3.5 `/vote` and `/events`, 3.6 mutes, whitelist sync, join link, moderator commands (`Moderation`), 3.7 `!WEBBANS`: `/pardon` in the game lifts a website ban, website bans made while the plugin was offline are sent on the next connect (`banned_players.delivered_at`), 3.8 optional TLS (`tls: true`, port 9992 when the socket server has `MCC_SOCKET_TLS_CERT/KEY`), 3.9 mute/ban times in the local time zone (sent by MCConnect), sidebar without score numbers on Paper 1.20.3+, 3.10 clickable chat: buttons `⟦label⇒/command⟧` (see `commands.button`; `clean()` strips the markers from player text) and links, announced with `!FEATURES~click`, older plugins get the command as text, 3.11 buttons only run the plugin's own commands (`ChatMarkup.isOwnCommand`), any other button is shown as text, 3.12 always TLS on 9991 (the `tls` option is gone; `tls-fingerprint` pins a self-signed certificate, `TlsPinning`) |

Charts are plain SVG without libraries (`web/static/*-chart.js`), styles for all of the above in
`web/static/css/stats.css`.

### Current work: round 3 (agreed 2026-09-28, built in this order, one commit per feature)

Progress is ticked off here, so a new session knows where to continue.

**A. Technik**
- [x] A1 Polling instead of SSE (`/api/player_count`, `/api/status`, `/api/player_info`): `fetch` every 5–10 s,
      paused in hidden tabs, result cached 2 s per server – no worker thread is held any more
- [x] A2 Partial CSP now (`frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'`);
      full CSP with nonces and without `onclick=` during the menu rework (B)
- [x] A3 TLS only, on port 9991 (no 9992): plain connections are refused. Local development and tests use a
      self-signed certificate (generated by a script); the plugin can pin it with a fingerprint in config.yml.
      Plugin default `tls: true`.
- [ ] A4 Live stats every 5 s: block break/place, mob kills/deaths, item use/craft events only mark
      (player, stat) as dirty; every 5 s (config.yml) the plugin sends the exact vanilla values of the dirty
      stats plus the custom stats (distance, play time) of online players. Server side: achievements, records,
      streaks at most every 15 s per player; sidebar, player page, competitions and duels follow faster.

**B. Menus** (settings are saved without a page reload – nothing collapses, no scrolling back)
- [ ] B1 Moderation `/users` → overview (open tasks: reports, builds, guestbook, applications; health) plus
      sub pages: players (bans, warnings/mutes, X-ray, activity), content (events, polls, competitions, goals,
      gallery, guestbook), access & rules, rewards, log
- [ ] B2 Admin `/manage` → server tiles, `/manage/<server>` with tabs: overview, plugin & connection,
      appearance (texts, images, directory), moderators & bans, notifications, danger zone
- [ ] B3 Header of the server pages regrouped (fewer dropdown entries): players, rankings & statistics,
      community, my area
- [ ] B4 Full CSP (nonces, no inline handlers)

**C. Join/leave messages & rewards**
- Only join/leave messages; prefixes keep deciding chat, tab list and name tag.
- Players pick from templates only (no free text); types: texts, style/effects (colors, bold, symbols),
  sound, also for leaving. No titles.
- Rewards stay unlocked when a streak breaks (best streak counts). Levels (~6, default: first no sound, later
  a quiet sound for everyone, the higher the more striking colors, sounds and symbols; the top level takes
  really long, e.g. a 365 day streak or 500 Ancient Debris). Conditions: streak badges, achievement tiers,
  play time/anniversary, trophies, any metric.
- Moderators/OPs choose their own color, gold, red and dark red are theirs only; ~10 player colors.
- Per server: level editor on the moderation page (conditions and what each level unlocks, reset to default),
  feature on/off. Players can mute other players' join sounds/messages in `/profil` (off by default, not
  prominent).
- Set on `/profil` (Minecraft style preview, locked options with their condition) and in game with
  `/joinmessage` (clickable options).

### Security (review of 2026-09-28)

- Free text in chat messages always goes through `commands.clean()` (no `&` codes, no button markers); the plugin
  additionally only lets buttons run its own commands.
- A failing `!CMD` answers the player with an error and a failing request gets `error|005`; neither ends the
  plugin connection. Before `!AUTH` messages are limited to 1 KB.
- Rate limits (`RateLimiter` in `web/main.py`, in memory per worker): admin login per address and per account,
  login pins per player and per address, invite codes and applications per address. Behind a proxy
  `FLASK_PROXY_FIX` must be on and port 8000 must not be reachable directly, otherwise the client address can
  be forged with `X-Forwarded-For` (the production setup is fine: private network, proxy in another container).
- Stats from the plugin are only stored for valid resource locations (`stats.OBJECT_NAME_RE`) and bigint values.
- Every response has `X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options: nosniff`, a referrer policy and a
  partial CSP (`CSP` in `web/main.py`: framing, plugins, `<base>`, form targets); scripts are not restricted yet
  (inline scripts, see B4).
- Live values are polled (`web/static/poll.js`, `LiveCache` in `web/main.py`, 2 s per server/player); no
  request keeps a worker thread busy any more.

### Open

- Phases 5-9 were built in one go without feedback rounds; screenshots were checked, the owner's visual feedback is
  still missing. The plugin features were tested end to end on Paper 1.21.4 with bots (`tools/e2e/`): commands,
  duels, reports, sidebar, mutes, warnings, bans with kick message, `/pardon`, whitelist codes and the kick link.
  Prefix name tags stay visible with the sidebar on. Not tested in the game: the TLS connection (needs a trusted
  certificate; the server side has tests).
- Invite codes can only be redeemed on the website: a player who is not on the whitelist cannot join, so there is
  no way to type a code in the game.
- The year in review only knows gains since the snapshots started (schema 7); 2026 starts at the first snapshot.
- Gains (today, competitions, goals, duels, sidebar) are measured against the last snapshot before the start day.
  A player's first sync is therefore also stored as the day before (baseline, schema 19); without it, players
  first seen on the start day stayed at 0 all day.
- The once-a-minute checks of the socket server run step by step, each guarded; a failing step is logged as
  "Periodic check failed: <step>" and the others go on.
- New CSS should use the `--srv-*` / `--st-*` tokens (or get a rule in `dark.css`), otherwise it stays light in dark mode.
- `database/manage.py seed` is dev only. The plugin connection is TLS only (port 9991); locally the socket server
  creates a self-signed certificate in `certs/` and logs the fingerprint for the plugin's `tls-fingerprint`.

### Built on 2026-09-27 (phases 5-9, all done – feedback from the owner still open)

1. ~~**Phase 5 – Motivation**~~ (done, see above): streaks (days online in a row, ranking, badges at 7/30/100),
   community goals (server-wide goal with progress bar, created by moderators), record history
   ("new record!" in feed and chat, who held which record when), anniversaries ("1 year on the server"
   in feed/chat, badge), fun facts on the server statistics page, trophy cabinet on the player page
   (competition wins, player of the week, records held) and a hall of fame page.
2. ~~**Phase 6 – In game**~~ (done, see above): `/stats [player]`, `/top <metric>`, `/wettbewerb` in the chat;
   optional scoreboard sidebar (competition standings / own play time, switchable per player);
   duels (1 vs 1 challenge for a metric and a period, accept on the website or with `/duell`, winner in chat);
   report system (`/report` or website, with position and time, list for moderators).
3. ~~**Phase 7 – Community**~~ (done, see above): event calendar (start page, chat reminder, sign up), polls (moderators create,
   vote on the website or with `/vote`), build gallery (players upload screenshots with title/coordinates,
   moderators approve, likes), guestbook on the player page (report/delete).
4. ~~**Phase 8 – Moderation & access**~~ (done, see above): whitelist access **either by application** (form on the website,
   moderators accept, plugin whitelists) **or by invite code/password** (enter it on the website or in game
   to be whitelisted directly); warnings with reason (shown in game, automatic ban after X) and chat mute;
   X-ray suspicion hints for moderators (unusual ore/stone ratio or ores per hour, hint only); e-mail alert
   to the admin when the server goes offline or the TPS stay below 15 (SMTP exists); rules & FAQ page,
   **optional per server**, shown with a link on the first join.
5. ~~**Phase 9 – Reach & design**~~ (done, see above): personal year in review "Wrapped" (to click through and share; best built
   for December when a year of snapshots exists), public server directory on the main domain (opt out per
   server), dark mode for all server pages (incl. the old templates).

### Continuing in a new session

- UI texts in German with "du", code/comments/commits in English, **no AI attribution in commits**.
  Minecraft names the way players say them (Ancient Debris, not "Antiker Schutt"). No "AI look":
  fonts Chakra Petch / Atkinson Hyperlegible / JetBrains Mono (self-hosted), no external CDNs for new things.
- The owner tests visually and gives short feedback: take screenshots before finishing (headless Chrome,
  see `docs/HANDOFF_spielervergleich.md`; pages that need a login are rendered with `app.test_client()`
  and `session_transaction`). Load the `dataviz` skill before writing chart code.
- New migration: add `N: [...]` to `MIGRATIONS` in `database/databaseManagerV2.py` **and** drop the new
  objects in `test_migration_from_version_1` (`tests/test_database.py`).
- Every new public view must respect `hide_stats` (`get_server_metrics()` leaves hidden players out
  unless `include_hidden=True`).
- Plugin changes: bump the version in `pom.xml`, document new messages in the docstring of
  `mc_socket/main.py`, check that it compiles (Maven is not installed locally):
  `docker run --rm -v <copy of java plugin/MCDataLink>:/build -w /build maven:3.9-eclipse-temurin-17 mvn -q -B package`.
  Production builds the plugin in the Dockerfile; server owners download it on the admin page.
- Dev data: the dev database has fake players, sessions, snapshots and health samples on `testdomain`;
  more can be created with `ensure_player_on_server`, `update_player_stats` and by moving
  `stat_snapshots.day` / `player_sessions` into the past.

Legal information: 

1. The source code of this software is available for viewing, but no part of this software may be copied, modified, distributed, or used for any commercial purposes without explicit permission from the author.

2. The software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement. In no event shall the authors be liable for any claim, damages, or other liability, whether in an action of contract, tort, or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software.

3. Any use of the software not expressly permitted by this license is strictly prohibited.