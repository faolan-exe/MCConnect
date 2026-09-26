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

- And a lot more...

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
docker compose -f docker/db/docker-compose.yml -p mcconnect up -d   # local postgres
.venv/bin/python -m pytest                                          # ~160 tests, uses database mcconnect_test
.venv/bin/python -m database.manage seed                            # dev admin 'tobi' + server 'testdomain'
.venv/bin/python mc_socket/main.py                                  # plugin socket on :9991
.venv/bin/python web/main.py                                        # http://mc.t-auer.local:5000
```

- `database/` – DB layer (`databaseManagerV2.py`), schema + migrations, stats classification, config (`MCC_*` env vars)
- `mc_socket/` – socket server the plugin connects to (protocol documented in `main.py`)
- `web/` – Flask app (main domain: admin area; `<subdomain>.`: server pages)
- `java plugin/MCDataLink/` – Spigot/Paper plugin (`mvn package`)
- `deploy/` – production docker compose with traefik; see [deploy/README.md](deploy/README.md)

Legal information: 

1. The source code of this software is available for viewing, but no part of this software may be copied, modified, distributed, or used for any commercial purposes without explicit permission from the author.

2. The software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement. In no event shall the authors be liable for any claim, damages, or other liability, whether in an action of contract, tort, or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software.

3. Any use of the software not expressly permitted by this license is strictly prohibited.

