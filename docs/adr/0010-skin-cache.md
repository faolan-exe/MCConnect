# 0010: Skins and heads come from our own server, cached as files

Date: 2026-11-14 · Status: accepted

## Context

The pages loaded player heads, skins and the body picture straight from mc-heads.net, and the old player page loaded
jQuery, three.js and MineRender from three CDNs. mc-heads.net is down now and then (on 2026-11-14 it did not answer
for some players at all). Then:

- every head on the page was a broken image,
- the old player page filled its stats table on `window.onload`, which waits for every image: the table stayed on
  "Loading..." until the browser gave up on the skin,
- the CDN scripts were loaded synchronously in `<head>`: a slow CDN held up the whole page.

Each visitor's browser also sent its IP address to these sites.

## Decision

- The website serves `/mc/avatar/<uuid>/<size>`, `/mc/body/<uuid>/<size>` and `/mc/skin/<uuid>` itself (main
  domain and every server subdomain), from `web/skins.py`.
- The skin comes from Mojang (session server, then `textures.minecraft.net`, the official source); mc-heads.net is
  only the second source. Heads are cut out of the skin (face plus hat layer, any size). The body picture is
  mc-heads.net's render, cached; without it a flat front view made of the skin.
- **Files, not Redis** (`MCC_CACHE_DIR`, docker volume `cache`): the data are small binary files that should survive
  restarts and deployments, are read rarely (browsers cache them for 6 hours) and are the same for every worker.
  Redis would add a service for no measurable gain (see the query measurements of 2026-10-08).
- A file is fresh for 6 hours. A stale file is fetched again; if that fails the old file stays in use and the next
  try waits 10 minutes, so a site that is down slows down one request instead of every page. Without any file
  there is a neutral placeholder head (cached by the browser for 5 minutes only).
- Only PNGs of skin size (64x64, 64x32) are stored, re-encoded; texture URLs are followed only to
  `textures.minecraft.net`; anything that is not a uuid never leads to a request.
- jQuery 3.3.1, three.js r94 and MineRender 1.4.6 are in `web/static/vendor/` (same files as the CDN versions,
  checked against their SRI hashes). The second, older jQuery and the unused Font Awesome on the login page are
  gone. The CSP allows scripts, styles, fonts and connections only from this server.
- The stats table on the player page starts right away instead of on `window.onload`.

## Consequences

- A skin change shows up within 6 hours (plus the browser's 6 hours).
- The cache grows by a few KB per player and is never cleaned up; deleting the folder is always safe.
- Offline mode servers: Mojang does not know their uuids, so their skins come from mc-heads.net only.
- The privacy page no longer lists third parties that get the visitor's IP address.
