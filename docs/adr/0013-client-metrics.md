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
- Plugin 3.30 adds click patterns and the rhythm of the client (`InputRhythm`): a handler in the player's Netty
  pipeline (found by type through reflection, added before `packet_handler`, only counts, never changes a packet)
  notes the arrival of every packet to the millisecond; Bukkit events only know the 50 ms tick. A click is an arm swing
  that is not part of mining (swings every tick between START and STOP, or until a pause of 150 ms, since an instantly
  broken block sends no STOP) or of a right click (swing within 25 ms after a use packet). Intervals below 500 ms count
  as clicking in a row: their count, sum, sum of squares, minimum, double clicks (< 25 ms) and an 8-bucket histogram
  per minute, plus the most clicks within one second. Rhythm: the longest silence between two packets and the stalls
  (silences of 250 ms or more for clients since 1.21.2 that send a packet at the end of every tick, 1.5 s for older
  ones), and the client's ticks. Bedrock players are skipped (Geyser makes up their packets). Config `input-analysis`.
  The web pages show only the click pattern: stalls, longest pause and the client's ticks proved not useful for
  moderators (2026-11-15) and are only stored.
- Hints from the click pattern (`database/clients.py`, never automatic punishment): "gleichmäßig" for at least 150
  intervals at 8+ clicks per second with a spread (coefficient of variation) below 8 % (people: 15–40 %; the measured
  times include connection jitter, so an auto clicker looks a bit less even than it is), "sehr schnell" from 20 clicks
  in one second, "Doppelklicks" from 10 % intervals below 25 ms with at least 100 intervals. Checked live with a bot:
  90 ms ± 2 ms gave 4 % spread, 115 ms ± 45 ms gave 73 %.
- Not done, on purpose: frames per second (the client sends per tick, not per frame, so FPS cannot be seen), probing
  installed mods and resource packs with translation keys (reads information from the player's device without consent,
  § 25 TDDDG, and newer clients block it), TCP/OS fingerprinting (TTL, window size and options are handled by the kernel
  and never reach Netty; behind Docker and a proxy one would see the proxy), ghost block tests (that is an anti-cheat
  plugin's job, e.g. Grim; changing what legit players see is risky).
- Outliers: typical values are medians, not means. Per player and day a histogram of the minute pings
  (`ping_hist`, 20 buckets) gives the median and the 90th percentile; the server's figure is the median of the players
  and the health sample carries `ping_median` of the online players (3.30; charts take the median of those minutes,
  the mean for older plugins). Stalls per hour on the overview are the median of the players. Means are still shown,
  labelled "mit Ausreißern".
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
