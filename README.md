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
.venv/bin/python -m pytest -q -p no:logging                         # ~470 tests, uses database mcconnect_test
.venv/bin/python -m database.manage seed                            # dev admin 'tobi' + server 'testdomain'
MCC_BASE_DOMAIN=mc.t-auer.local:5050 .venv/bin/python mc_socket/main.py   # plugin socket on :9991 (TLS, own certificate)
FLASK_SERVER_NAME=mc.t-auer.local:5050 .venv/bin/python -c "from web.main import create_app; create_app().run(port=5050)"
```

Port 5000 is taken by the macOS AirPlay receiver, hence 5050. `python -m mc_socket.tlscert` prints the
fingerprint of the local socket certificate (`certs/`) for a test plugin's `tls-fingerprint`.

- `database/` – DB layer (`databaseManagerV2.py`), schema + migrations, stats classification, metrics,
  achievements, motivation, rewards, config (`MCC_*` env vars)
- `mc_socket/` – socket server the plugin connects to (protocol documented in `main.py`), in-game commands
  (`commands.py`), TLS certificate (`tlscert.py`)
- `web/` – Flask app (main domain: admin area; `<subdomain>.`: server pages)
- `java plugin/MCDataLink/` – Spigot/Paper plugin (`mvn package`)
- `tools/e2e/` – end-to-end test with a real Paper server and bots
- `deploy/` – production docker compose (behind Nginx Proxy Manager, optional traefik); see [deploy/README.md](deploy/README.md)

## Status

State of 2026-09-29: schema version 24, plugin 3.16. Written as a handoff for the next development session.

### Features

| Area | Where | Notes |
|---|---|---|
| Rankings | `/rangliste` | 19 metrics in 4 groups (`database/metrics.py`), all time / 30 / 7 days; `+` buttons collect players for the comparison (`web/static/compare.js`) |
| Comparison | `/vergleich` | 2–4 players, best values, history chart, detail tables |
| Player list | `/spieler` | search, sort, "last online" filters, badges, top places, favourites |
| Player page | `/spieler?player=` | activity chart, ranking places, highlights, achievements (live), trophy cabinet, bio, stat card (`/spieler/<name>/karte.png`, `og:image`), guestbook, moderator notes and warnings |
| Server statistics | `/server-statistik` | online history, peak-time heatmap, totals, new players, fun facts; based on `player_sessions` |
| Start page | `/` | records, weekly recap, running competitions and goals, events, polls, activity feed (`/api/feed`) |
| Achievements | `database/achievements.py` | 13 × 4 tiers (`player_achievements`); tiers reached while not playing are `silent` (no feed, no chat) |
| Teams | `/teams` | prefixes as teams |
| Motivation | `database/motivation.py`, `/wettbewerbe`, `/ruhmeshalle` | competitions, community goals, streaks + badges 7/30/100, anniversaries (`player_milestones`), record history (`record_history`), trophies (competition places, player of the week), hall of fame; news in feed and chat |
| In game | `mc_socket/commands.py` | `/stats`, `/top`, `/wettbewerb`, `/duell`, `/report`, `/seitenleiste`, `/vote`, `/events`, `/joinmessage`, moderators `/verwarnen`, `/stumm`, `/entstummen` (`!CMD` → `!tell`, clickable buttons); scoreboard sidebar |
| Community | `/events`, `/umfragen`, `/galerie`, `/duelle`, player page | events with sign up and chat reminder, polls, build gallery with approval and likes, duels, guestbook |
| Access | `/mitmachen`, `/regeln` | whitelist by application and/or invite code (the plugin runs `whitelist add`; the kick message links to `/mitmachen`), optional rules & FAQ page |
| Join messages & rewards | `database/rewards.py`, `/profil`, `/users/belohnungen`, `/joinmessage` | reward levels (default 6, editable per server; any condition reaches a level, unlocks add up, a reached level is stored and never lowered); players choose text, leave text, color, symbol, style and sound from what they unlocked (templates only, moderators add own texts; moderators/OPs also gold/red/dark red); while on, everyone gets an MCConnect message (without a choice the first level's default, `!joindefault`); players can hide others' messages and all sounds |
| Moderation | `/users` (moderators) | overview (open tasks, server health, newest log entries) and sub pages `spieler` (reports, bans, warnings, X-ray hints, activity), `inhalte` (gallery, guestbook, events, polls, competitions, goals), `zugang` (whitelist, codes, rules/FAQ), `belohnungen`, `protokoll`; templates in `web/templates/mod/` |
| Admin | `/manage` (server owners) | server tiles; `/manage/<id>` with tabs: overview (health, e-mail alerts), plugin (config with `tls-fingerprint`), server page (texts, images, directory), moderation (moderators, bans), danger zone |
| Year in review | `/rueckblick/<name>` | year gains from the first/last snapshot of the year (both kept forever), sessions, achievements, trophies |
| Reach & design | main page, `web/static/css/dark.css` | public server directory (`servers.listed`), dark mode for all server pages (`_theme.html`, `--srv-*` tokens) |
| Profile & privacy | `/profil` | bio, "hide my stats" (`hide_stats`: left out of every public view except server totals), favourites, sidebar, join message |

Charts are plain SVG without libraries (`web/static/*-chart.js`), styles in `web/static/css/stats.css`.

### Plugin versions

3.1 `!broadcast` · 3.2 `!HEALTH` · 3.3 live stats · 3.4 in-game commands, sidebar · 3.5 `/vote`, `/events` ·
3.6 mutes, whitelist sync, join link, moderator commands · 3.7 `!WEBBANS` (`/pardon` lifts a website ban) ·
3.8 optional TLS · 3.9 times in the local time zone · 3.10 clickable chat (`⟦label⇒/command⟧`, `!FEATURES~click`) ·
3.11 buttons only run the plugin's own commands · 3.12 always TLS on 9991 (`tls-fingerprint`, `TlsPinning`) ·
3.13 live stats every 5 s (`live-stats-interval`; events mark what changed) · 3.14 join/leave messages
(`JoinMessages`, `!joinstyle`, `!joinmutes`, `!joinreset`), `/joinmessage` · 3.15 `!JOIN` sends the game's first
join, the first live update after a join is complete · 3.16 default join lines for everyone (`!joindefault`).

Plugins before 3.12 are refused (TLS only). Older plugins keep working but miss the newer features.

### How things work (read before changing them)

- **Gains and baselines.** Gains (today, 7/30 days, competitions, goals, duels, sidebar, weekly recap) are the
  difference between daily snapshots (`stat_snapshots`, written with every stats update). A player's first
  sync is also stored as the day before (baseline). **New or old player** decides that baseline: a player
  whose first join in the game (`player_server_info.first_played`, from plugin 3.15) lies after
  `servers.tracking_since` (first plugin connection) is new – baseline 0, everything counts; otherwise the
  first stats are the baseline (an old player's lifetime values are no gain). When the first join arrives
  later, `_correct_first_baseline` fixes the guess once, in both directions. Without it (plugins before
  3.15) the time since MCConnect's first sight is the fallback. This went wrong several times (schema 19,
  21, 23, 24: players who joined while the plugin was disconnected, partial first live updates) – keep the
  tests in `tests/test_metrics.py` green.
- **"Dabei seit"**, veterans, anniversaries, new players per week/month and the reward condition "days" use
  the game's first join if known (`COALESCE(first_played, first_seen)`).
- **Live values.** The plugin sends custom stats and changed block/item/mob stats every 5 s, all blocks once
  a minute. The socket server checks achievements, records and streaks at most every 15 s per player and
  updates sidebars 5 s after new stats. Pages poll (`web/static/poll.js`, `LiveCache`, 2 s per
  server/player); `/wettbewerbe` and `/duelle` refresh their standings every 10 s.
- **No page reloads.** Forms and buttons are saved with `fetch`; afterwards `refreshLiveParts()` (`poll.js`)
  replaces only the parts marked `data-live="…"`. Moderation pages use `web/static/mod.js`
  (`data-mod-post`, `data-mod-form`, `data-settings-form`, `data-setting`).
- **Plugin connection.** TLS only on 9991. The socket server creates its own certificate on the first start
  (`mc_socket/tlscert.py`, docker volume `socket_tls`); the admin page shows its fingerprint in the plugin
  config, the plugin pins it. An own certificate (`MCC_SOCKET_TLS_CERT/KEY`) is optional.
- **Streaks** count days with a session and days on which the play time grew (online while the plugin was
  disconnected, no session).
- The once-a-minute checks of the socket server run step by step, each guarded ("Periodic check failed:
  <step>" in the log).

### Security

- Free text in chat messages always goes through `commands.clean()` (no `&` codes, no button markers); the plugin
  only lets buttons run its own commands.
- A failing `!CMD` answers the player with an error, a failing request gets `error|005`; neither ends the plugin
  connection. Before `!AUTH` messages are limited to 1 KB.
- Rate limits (`RateLimiter` in `web/main.py`, in memory per worker): admin login per address and account, login
  pins per player and address, invite codes and applications per address. Behind a proxy `FLASK_PROXY_FIX` must
  be on and port 8000 must not be reachable directly (the production setup is fine: private network).
- Stats from the plugin are only stored for valid resource locations (`stats.OBJECT_NAME_RE`) and bigint values.
- Every response has `X-Frame-Options`, `nosniff` and a referrer policy; HTML pages a CSP
  (`content_security_policy()`): scripts from the own origins, cdnjs, googleapis and jsdelivr (jQuery, three.js,
  MineRender on the player page) and inline `<script nonce="{{ csp_nonce() }}">` only. **New inline scripts need
  the nonce; `onclick=` does not work** – use `data-on-click="function"` (`web/static/actions.js`) or
  `addEventListener`. MineRender's page view counter (minerender.org) is blocked on purpose.
- The dialog library (`messageBoxes.js`) is vendored in `web/static/vendor/messageBoxLib/` (fixed commit).

### Open

- The owner's visual feedback on the latest rounds is still missing.
- Invite codes can only be redeemed on the website (a player who is not on the whitelist cannot join the game).
- The year in review only knows gains since the snapshots started (schema 7).
- Players who never joined while the plugin was connected and have no first join from the game (plugins
  before 3.15) are guessed as old players.

### Conventions

- UI texts in German with "du", code/comments/commits in English, **no AI attribution in commits**.
  Minecraft names the way players say them (Ancient Debris, not "Antiker Schutt"). No "AI look":
  fonts Chakra Petch / Atkinson Hyperlegible / JetBrains Mono (self-hosted), no external CDNs for new things.
- Take screenshots before finishing (headless Chrome with `--host-resolver-rules`; pages that need a login are
  rendered with `app.test_client()` and `session_transaction`, see `docs/HANDOFF_spielervergleich.md`). Load the
  `dataviz` skill before writing chart code.
- New migration: add `N: [...]` to `MIGRATIONS` in `database/databaseManagerV2.py` **and** drop the new
  objects in `test_migration_from_version_1` (`tests/test_database.py`).
- Every new public view must respect `hide_stats` (`get_server_metrics()` leaves hidden players out
  unless `include_hidden=True`).
- New CSS uses the `--srv-*` / `--st-*` tokens (or gets a rule in `dark.css`), otherwise it stays light in dark mode.
- Plugin changes: bump the version in `pom.xml`, document new messages in the docstring of `mc_socket/main.py`,
  check that it compiles (`docker run --rm -v <copy>:/p -w /p maven:3.9-eclipse-temurin-17 mvn -q package`) and
  test it with `tools/e2e/`. Production builds the plugin in the Dockerfile; server owners download it on the
  admin page.
- Dev data: the dev database has fake players, sessions, snapshots and health samples on `testdomain`; more can
  be created with `ensure_player_on_server`, `update_player_stats` and by moving `stat_snapshots.day` /
  `player_sessions` into the past.

Legal information: 

1. The source code of this software is available for viewing, but no part of this software may be copied, modified, distributed, or used for any commercial purposes without explicit permission from the author.

2. The software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement. In no event shall the authors be liable for any claim, damages, or other liability, whether in an action of contract, tort, or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software.

3. Any use of the software not expressly permitted by this license is strictly prohibited.