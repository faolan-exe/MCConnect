# 0015: World map – the server's BlueMap on the server page

Date: 2026-11-18 · Status: accepted · schema 41

## Context

Many servers run BlueMap, a 3D web map of the world. BlueMap renders the world itself and brings its own web server
(port 8100 by default, http only), next to the Minecraft server. Players should find the map on the server page, and
moderators should get from a ticket straight to its place.

## Decision

- **The owner enters the address** of the BlueMap under `/manage/<id>` → "Serverseite" (column `servers.map_url`,
  optional, like the Discord link). Without an address there is no page and no menu entry.
- **iframe, no proxy, no own viewer.** `/karte` (own menu entry "Karte") shows the map in an iframe.
  MCConnect does not proxy the tiles (thousands of requests per visit through the web app, plus a request forgery
  risk) and does not render the map itself. The button "Im Vollbild öffnen" opens the map directly.
- **Only https.** A page on https may not frame http (mixed content). The address must be `https://` without user
  name, password or `#fragment` (`database/bluemap.normalize_url`). Owners put BlueMap behind a reverse proxy with a
  certificate (Caddy, nginx). The proxy must not forbid framing (`X-Frame-Options`, `frame-ancestors`).
- **CSP:** only `/karte` gets `frame-src <origin of the map>`; every other page keeps `frame-src 'self'`.
- **Public page.** The map is visible without login, like the rest of the server page. What BlueMap shows (e.g. live
  player markers) is set in BlueMap by the owner, not in MCConnect.
- **Links to a position:** `/karte?welt=&x=&y=&z=` sets BlueMap's hash
  (`#map:x:y:z:distance:rotation:angle:tilt:ortho:mode`). The map id is the world name, which is BlueMap's default.
  Invalid values (out of the world border, odd world names) fall back to the plain map. Tickets with a position link
  there. Discovery places do not: they are meant to be found.

## Consequences

- Servers without https for BlueMap cannot use the page (the settings refuse http with a hint).
- A BlueMap with renamed map ids only gets the plain map from ticket links.
- Markers from MCConnect (e.g. SpawnElytra areas, tickets) would need an addon using BlueMap's API; not part of this.
