# Plugin end-to-end test

A real Paper server in Docker with the plugin, the local socket server against the dev database, and
[mineflayer](https://github.com/PrismarineJS/mineflayer) bots as players. Used for plugin 3.4-3.9.

```bash
# 1. plugin jar (see the main README: maven in docker on a copy) and the key of the dev server
KEY=$(.venv/bin/python -c "import sys; sys.path.insert(0, '.'); from database.databaseManagerV2 import DatabaseManager; \
  print(DatabaseManager()._fetchvalue(\"SELECT server_key FROM servers WHERE subdomain = 'testdomain'\"))" | tail -1)
mkdir -p /tmp/mc/plugins/MCDataLink && cp "java plugin/MCDataLink/target/MCDataLink-*.jar" /tmp/mc/plugins/
FP=$(.venv/bin/python -m mc_socket.tlscert | tail -1 | cut -d'"' -f2)  # the local socket server's own certificate
printf 'key: %s\nhost: host.docker.internal\nport: 9991\ntls-fingerprint: "%s"\n' "$KEY" "$FP" > /tmp/mc/plugins/MCDataLink/config.yml

# 2. socket server and Paper (offline mode, so bots can join)
MCC_BASE_DOMAIN=mc.t-auer.local:5050 .venv/bin/python mc_socket/main.py &
docker run -d --name mcc-paper -e EULA=TRUE -e TYPE=PAPER -e VERSION=1.21.4 -e ONLINE_MODE=FALSE \
  -e ENABLE_RCON=true -e RCON_PASSWORD=test -e LEVEL_TYPE=flat -p 25565:25565 -v /tmp/mc:/data itzg/minecraft-server

# 3. bots: node bot.js <name> <chat line | wait:<ms> | dig:<1-4> | sidebar> ...
docker run --rm -v "$PWD/tools/e2e":/bot -w /bot node:20-alpine sh -c "npm i --silent mineflayer@4 && node bot.js TestBot /stats '/top abgebaut'"
docker exec mcc-paper rcon-cli whitelist on   # console commands
```

Notes: `jump:<ms>` gets the bot kicked on Paper 1.21.4 (invalid movement), use play time for competitions
instead. Paper throttles joins from one IP (4 s), so start a second bot a few seconds later. mineflayer 4 cannot
parse some player chat packets of 1.21.4 (the bot logs a parser error and goes on) and does not keep the
sidebar lines; `RAW=1` prints the scoreboard packets instead. Make a bot a moderator with
`db.set_moderator(server_id, "TestBot", True)`.

## Clicking through the website (ui-test.js)

A real browser (Puppeteer) clicks through the pages that save without reload (moderation, level editor, own
texts, profile, player page notes) and checks that nothing reloads or collapses and that the console (incl.
CSP violations) stays clean. Port 5070 (Chrome refuses 5060, macOS AirPlay has 5000):

```bash
FLASK_SERVER_NAME=mc.t-auer.local:5070 MCC_BASE_DOMAIN=mc.t-auer.local:5070 .venv/bin/python -c \
  "from web.main import create_app; create_app().run(host='0.0.0.0', port=5070)" &
SESSION=$(FLASK_SERVER_NAME=mc.t-auer.local:5070 .venv/bin/python tools/e2e/session.py | tail -1)
docker run --rm --add-host=testdomain.mc.t-auer.local:host-gateway --add-host=mc.t-auer.local:host-gateway \
  -e SESSION="$SESSION" -v "$PWD/tools/e2e":/home/pptruser/ui -w /home/pptruser ghcr.io/puppeteer/puppeteer node ui/ui-test.js
```
