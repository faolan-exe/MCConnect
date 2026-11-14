# 0012: X-ray hints after the cave update, and checked hints

Date: 2026-11-14 · Status: accepted · Schema 36

## Context

The moderation page lists players with an unusually high ore rate as x-ray hints. The rule came from the time before
the cave update (1.18): 6 diamond ore per 1000 mined stone/deepslate/tuff, at least 15 ore and 2000 stone. Since 1.18
players find most diamonds in open caves and break little stone for them, so honest players passed that line early.
A hint could not be removed: it stayed on the page and in the open tasks for good.

## Decision

- The blocks around the ore count more completely: stone, deepslate, tuff, andesite, diorite and granite for
  diamonds (`XRAY_STONE_BLOCKS`), netherrack, basalt and blackstone for ancient debris (`XRAY_NETHER_BLOCKS`).
- Diamonds: at least 40 ore, 5000 stone blocks and 15 ore per 1000 of them. Debris: at least 20, 3000 blocks and
  12 per 1000 (`XRAY_*` in `web/main.py`). Cave miners reach about 5-10 diamonds per 1000; x-ray users dig straight to
  the ore and lie far above.
- A moderator marks a hint as checked ("Erledigt", `/api/mod/xray/dismiss`, moderation log `xray_dismiss`). The ore
  counts of that moment are stored (`xray_dismissed`); the hint comes back only after 30 more diamond ore or 15 more
  debris, and only if the rate is still high.

## Consequences

- Fewer and later hints; a real x-ray user with a short history may show up later than before.
- The thresholds are a guess from typical rates, not measured on the servers. If they are still too strict or too
  lax, only the constants change; `tests/test_rules_edges.py` checks their edges.
