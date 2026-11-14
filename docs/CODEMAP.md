# Code map

Where things are, so a change starts in the right file. The route tables at the end are generated from the Flask
URL map: run `python tools/codemap.py` after adding or moving a route (`--check` tells whether the file is current).
How things work and why is in [README.md](../README.md) ("How things work") and in the ADRs in [docs/adr](adr/).

## Layout

| Folder | What |
|---|---|
| `web/main.py` | The whole Flask app: both blueprints (`main_bp` = main domain, `server_bp` = `<subdomain>.`), access decorators, view helpers, request hooks (`create_app` at the end) |
| `web/templates/` | Jinja templates; `server_base.html` (server pages), `base.html` (main domain), `mod/_layout.html` (moderation tabs) |
| `web/static/` | Own JS (no build step), CSS, fonts, glyph SVGs, `vendor/` (jQuery, three.js, MineRender for the player page only) |
| `web/skins.py`, `web/item_icons.py`, `web/card.py`, `web/uploads.py` | Heads/skins cache, block/item icons, stat card PNG, image uploads |
| `database/databaseManagerV2.py` | Every SQL query (`DatabaseManager`), schema and migrations (`SCHEMA_VERSION`) |
| `database/*.py` | Domain rules without Flask: metrics, achievements, motivation, rewards, badges, glyphs, registry, addons, discoveries, chat tooltips, stats classification, config |
| `mc_socket/main.py` | Socket server for the plugin (TLS on 9991): protocol, periodic checks, announcements |
| `mc_socket/commands.py` | In-game commands (`/stats`, `/top`, `/duell`, ...) and the sidebar |
| `java plugin/MCDataLink/` | The Spigot/Paper plugin; `java plugin/addons/` the official addons |
| `tests/` | pytest against PostgreSQL (`mcconnect_test`); browser tests use the installed Chrome via Playwright |
| `tools/e2e/` | End-to-end run with a real Paper server and bots |
| `deploy/` | Production compose file and the German deploy guide |

## Server pages: menu → page → files

The menu is `web/templates/header.html` (one markup for desktop and phone, styles in `web/static/css/nav.css`).

| Menu | Page | Template | Page script / styles |
|---|---|---|---|
| Start | `/` | `index-subpage.html` | inline; `server.css`, `stats.css` |
| Spieler | `/spieler` (list), `/spieler?player=` (player page), `/teams`, `/vergleich` | `spieler.html`, `spieler-info.html` (own old design, keep it), `teams.html`, `vergleich.html` | `compare.js`, `history-chart.js`; player page: `player-info.css`, `responsive-player-info.css`, `minecraft.css` |
| Statistiken | `/rangliste`, `/server-statistik`, `/ruhmeshalle`, `/entdeckungen` | same names | `bar-chart.js`, `line-chart.js`, `online-chart.js`; `stats.css` |
| Wettkämpfe | `/wettbewerbe`, `/duelle` | same names, `_goals.html` | `poll.js` (standings refresh) |
| Community | `/events` (events and polls; `/umfragen` redirects), `/galerie`, `/regeln`, `/mitmachen`, `/melden` | same names | inline |
| Konto | `/profil`, `/profil/spiel`, `/profil/prefix` (`/add_pref`, `/join_pref` redirect) | `profil.html` (one template, `tab` decides the part) | inline, `poll.js` on the prefix tab |
| Verwaltung | `/users`, `/users/spieler`, `/users/inhalte`, `/users/zugang`, tab "Im Spiel": `/users/belohnungen`, `/users/icons`, `/users/erweiterungen`; `/users/protokoll` | `mod/*.html` | `mod.js`, `addons.js`; tabs in `MOD_TABS` (`web/main.py`) |
| – | `/rueckblick/<name>`, `/login`, `/impressum`, `/datenschutz` | `rueckblick.html`, `login.html` (keep its design), `legal_*.html` | |

Main domain: `/` (`index-main.html`, server directory), `/login`, `/create`, `/manage` and `/manage/<id>` (tabs
overview, plugin, addons, server page, moderation, danger zone in `serverAdminServer.html` + `addons.js`).

## Conventions that are easy to miss

- **CSP**: no inline handlers. `data-on-click="fn"` (`actions.js`) or `addEventListener`; inline `<script>` needs
  `nonce="{{ csp_nonce() }}"`. Playwright's string predicates are blocked too, wait for selectors in browser tests.
- **No reloads after saving**: `postJson()` (in both base templates), then `refreshLiveParts()` (`poll.js`) swaps the
  elements marked `data-live="..."`; moderation forms use `data-mod-post` / `data-mod-form` / `data-setting` (`mod.js`).
- **Messages**: `showMessage(element, text, "success"|"error")` for inline results, `messagebox_info/_error` for
  dialogs (`messagebox.js`); no `alert()`.
- **Access**: `player_required`, `moderator_required`, `admin_required` in `web/main.py`; POST APIs must be JSON
  (CSRF). `tests/test_security_matrix.py` checks every rule of the URL map, so new endpoints are covered.
- **Hidden players** (`hide_stats`) are left out of every public view; see `tests/test_security_matrix.py`.
- **One number, one source**: metrics from `database/metrics.py`, never single `actions` rows.
- **Mobile**: phone rules in media queries only (`mobile.css`, the 600px block of `responsive-player-info.css`);
  tables with class `mcc-stack` need `data-label` on every cell.
- **Dark mode**: `_theme.html` sets `<html data-dark>`, colors are the `--srv-*` tokens, fixes in `dark.css`.

## Tests by area

| Area | Tests |
|---|---|
| Access rules, CSRF, server separation, HTTPS redirect | `test_security_matrix.py`, `test_access.py` |
| Odd input on every API and the socket | `test_robustness.py`, `test_rules_edges.py` |
| Pages, prefixes, logins, admin area | `test_web.py`, `test_profile.py` |
| Browser: menu, tabs, saving without reload, dark mode | `test_browser_flows.py`, `test_mobile.py` (phone width), `test_messagebox.py`, `test_addon_ui.py` |
| Rankings, comparison, numbers | `test_metrics.py`, `test_stats.py`, `test_server_stats.py`, `test_registry.py` |
| Motivation, achievements, rewards, badges, icons | `test_motivation.py`, `test_achievements.py`, `test_rewards.py`, `test_badges.py`, `test_icons.py` |
| Community, in game, moderation | `test_community.py`, `test_ingame.py`, `test_moderation_log.py`, `test_confirmations.py` |
| Plugin connection, addons, updates | `test_socket.py`, `test_socket_tls.py`, `test_addons.py`, `test_addon_protected.py`, `test_plugin_update.py`, `test_discoveries.py` |
| Skins, item icons, mail, names | `test_skins.py`, `test_item_icons.py`, `test_mailer.py`, `test_mojang_names.py` |
| Templates and dark mode details | `test_template_markup.py`, `test_dark_mode.py`, `test_reach.py` |

## Routes

<!-- ROUTES:START (tools/codemap.py) -->

### Server pages (`<subdomain>.`)

| Path | Method | View (web/main.py) | Template |
|---|---|---|---|
| `/` | GET | `subdomain_index_route` | index-subpage.html |
| `/add_pref` | GET | `prefix_edit_page` |  |
| `/datenschutz` | GET | `datenschutz` |  |
| `/duelle` | GET | `duels_page` | duelle.html |
| `/entdeckungen` | GET | `discoveries_page` | entdeckungen.html |
| `/events` | GET | `events_page` | events.html |
| `/galerie` | GET | `gallery_page` | galerie.html |
| `/impressum` | GET | `impressum` |  |
| `/join_pref` | GET | `prefix_join_page` |  |
| `/login` | GET, POST | `player_login` | logout_confirmation.html, login.html |
| `/melden` | GET | `report_page` | melden.html |
| `/mitmachen` | GET | `access_page` | mitmachen.html |
| `/profil` | GET | `profile_page` | profil.html |
| `/profil/<tab>` | GET | `profile_page` | profil.html |
| `/rangliste` | GET | `rankings_page` | rangliste.html |
| `/regeln` | GET | `rules_page` | regeln.html |
| `/rueckblick/<path:player_name>` | GET | `year_review_page` | rueckblick.html |
| `/ruhmeshalle` | GET | `hall_of_fame_page` | ruhmeshalle.html |
| `/server-statistik` | GET | `server_stats_page` | server_statistik.html |
| `/spieler` | GET | `player_overview_route` | spieler_verborgen.html, spieler-info.html |
| `/spieler/<path:player_name>/karte.png` | GET | `player_card` |  |
| `/teams` | GET | `teams_page` | teams.html |
| `/umfragen` | GET | `polls_page` |  |
| `/users` | GET | `moderation_page` | mod/overview.html |
| `/users/belohnungen` | GET | `moderation_rewards_page` | mod/rewards.html |
| `/users/erweiterungen` | GET | `moderation_addons_page` | mod/addons.html |
| `/users/icons` | GET | `moderation_icons_page` | mod/icons.html |
| `/users/inhalte` | GET | `moderation_content_page` | mod/content.html |
| `/users/protokoll` | GET | `moderation_log_page` | mod/log.html |
| `/users/spieler` | GET | `moderation_players_page` | mod/players.html |
| `/users/zugang` | GET | `moderation_access_page` | mod/access.html |
| `/vergleich` | GET | `compare_page` | vergleich.html |
| `/wettbewerbe` | GET | `competitions_page` | wettbewerbe.html |

### Server APIs and files

| Path | Method | View (web/main.py) | Template |
|---|---|---|---|
| `/api/access/apply` | POST | `access_apply_api` |  |
| `/api/access/redeem` | POST | `access_redeem_api` |  |
| `/api/badges` | POST | `badges_save_api` |  |
| `/api/builds` | POST | `build_upload_api` |  |
| `/api/builds/delete` | POST | `build_delete_api` |  |
| `/api/builds/like` | POST | `build_like_api` |  |
| `/api/duels` | POST | `duel_create_api` |  |
| `/api/duels/cancel` | POST | `duel_cancel_api` |  |
| `/api/duels/respond` | POST | `duel_respond_api` |  |
| `/api/events/signup` | POST | `event_signup_api` |  |
| `/api/favorites/toggle` | POST | `favorite_toggle_api` |  |
| `/api/feed` | GET | `feed_api` |  |
| `/api/guestbook` | POST | `guestbook_add_api` |  |
| `/api/guestbook/delete` | POST | `guestbook_delete_api` |  |
| `/api/guestbook/report` | POST | `guestbook_report_api` |  |
| `/api/joinmutes` | POST | `join_mutes_api` |  |
| `/api/joinstyle` | POST | `join_style_api` |  |
| `/api/login` | POST | `minecraft_login_api` |  |
| `/api/meine-erfolge` | GET | `my_new_achievements_api` |  |
| `/api/minecraft/items` | GET | `minecraft_items_api` |  |
| `/api/mod/addons` | GET | `mod_addons_api` |  |
| `/api/mod/addons/<addon_id>/config` | POST | `mod_addon_config_api` |  |
| `/api/mod/applications` | POST | `mod_application_api` |  |
| `/api/mod/ban` | POST | `mod_ban_api` |  |
| `/api/mod/bans` | GET | `mod_bans_api` |  |
| `/api/mod/builds/approve` | POST | `mod_build_approve_api` |  |
| `/api/mod/builds/delete` | POST | `mod_build_delete_api` |  |
| `/api/mod/codes` | POST | `mod_code_create_api` |  |
| `/api/mod/codes/delete` | POST | `mod_code_delete_api` |  |
| `/api/mod/competitions` | POST | `mod_competition_create_api` |  |
| `/api/mod/competitions/delete` | POST | `mod_competition_delete_api` |  |
| `/api/mod/events` | POST | `mod_event_create_api` |  |
| `/api/mod/events/delete` | POST | `mod_event_delete_api` |  |
| `/api/mod/goals` | POST | `mod_goal_create_api` |  |
| `/api/mod/goals/delete` | POST | `mod_goal_delete_api` |  |
| `/api/mod/guestbook/keep` | POST | `mod_guestbook_keep_api` |  |
| `/api/mod/icons` | POST | `mod_icons_api` |  |
| `/api/mod/mute` | POST | `mod_mute_api` |  |
| `/api/mod/notes` | POST | `mod_note_add_api` |  |
| `/api/mod/notes/delete` | POST | `mod_note_delete_api` |  |
| `/api/mod/polls` | POST | `mod_poll_create_api` |  |
| `/api/mod/polls/delete` | POST | `mod_poll_delete_api` |  |
| `/api/mod/reports/resolve` | POST | `mod_report_resolve_api` |  |
| `/api/mod/rewards` | POST | `mod_rewards_api` |  |
| `/api/mod/settings` | POST | `mod_settings_api` |  |
| `/api/mod/unban` | POST | `mod_unban_api` |  |
| `/api/mod/warn` | POST | `mod_warn_api` |  |
| `/api/mod/warnings/delete` | POST | `mod_warning_delete_api` |  |
| `/api/mod/whitelist/remove` | POST | `mod_whitelist_remove_api` |  |
| `/api/player_count` | GET | `live_player_count` |  |
| `/api/player_info/<path:player_name>` | GET | `live_player_info` |  |
| `/api/polls/vote` | POST | `poll_vote_api` |  |
| `/api/prefix/delete` | POST | `prefix_delete_api` |  |
| `/api/prefix/join` | POST | `prefix_join_api` |  |
| `/api/prefix/leave` | POST | `prefix_leave_api` |  |
| `/api/prefix/save` | POST | `prefix_save_api` |  |
| `/api/profile` | POST | `profile_save_api` |  |
| `/api/reports` | POST | `report_create_api` |  |
| `/api/sidebar` | POST | `sidebar_save_api` |  |
| `/api/spieler/<path:player_name>/erfolge` | GET | `player_achievements_api` | _achievements.html |
| `/api/status` | GET | `live_status` |  |
| `/icons/<int:codepoint>.svg` | GET | `own_icon` |  |
| `/mc/avatar/<player>/<int:size>` | GET | `player_head` |  |
| `/mc/body/<player>/<int:size>` | GET | `player_body` |  |
| `/mc/item/<item_id>.png` | GET | `item_icon` |  |
| `/mc/skin/<player>` | GET | `player_skin` |  |

### Main domain pages

| Path | Method | View (web/main.py) | Template |
|---|---|---|---|
| `/` | GET | `main_index_route` | index-main.html |
| `/create` | GET | `create_new_server` | serverAdminCreate.html |
| `/datenschutz` | GET | `datenschutz` |  |
| `/forgot_password` | GET | `forgot_password` | forgot_password.html |
| `/healthz` | GET | `healthz` |  |
| `/impressum` | GET | `impressum` |  |
| `/login` | GET | `server_admin_login` | serverAdminLogin.html |
| `/manage` | GET | `manage_server` | serverAdminManage.html |
| `/manage/<int:server_id>` | GET | `manage_one_server` | serverAdminServer.html |
| `/reset_password/<token>` | GET | `reset_password_page` | message.html, reset_password.html |
| `/verify_email/<username>/<token>` | GET | `verify_email` | message.html |

### Main domain APIs and files

| Path | Method | View (web/main.py) | Template |
|---|---|---|---|
| `/api/login` | POST | `server_login_api` |  |
| `/api/logout` | POST | `logout` |  |
| `/api/minecraft/items` | GET | `minecraft_items_api` |  |
| `/api/password_reset/confirm` | POST | `password_reset_confirm` |  |
| `/api/password_reset/request` | POST | `password_reset_request` |  |
| `/api/player_count` | GET | `live_total_player_count` |  |
| `/api/resend_verification` | POST | `resend_verification` |  |
| `/api/servers` | POST | `create_server_api` |  |
| `/api/servers/<int:server_id>/addons` | GET | `admin_addons_api` |  |
| `/api/servers/<int:server_id>/addons/<addon_id>/approve` | POST | `admin_addon_approve_api` |  |
| `/api/servers/<int:server_id>/addons/<addon_id>/config` | POST | `admin_addon_config_api` |  |
| `/api/servers/<int:server_id>/addons/<addon_id>/enabled` | POST | `admin_addon_enabled_api` |  |
| `/api/servers/<int:server_id>/addons/<addon_id>/install` | POST | `admin_addon_install_api` |  |
| `/api/servers/<int:server_id>/addons/<addon_id>/remove` | POST | `admin_addon_remove_api` |  |
| `/api/servers/<int:server_id>/addons/rights` | POST | `admin_addon_rights_api` |  |
| `/api/servers/<int:server_id>/ban` | POST | `admin_ban_api` |  |
| `/api/servers/<int:server_id>/delete` | POST | `delete_server_api` |  |
| `/api/servers/<int:server_id>/images` | POST | `upload_server_image_api` |  |
| `/api/servers/<int:server_id>/images/<int:image_id>/delete` | POST | `delete_server_image_api` |  |
| `/api/servers/<int:server_id>/moderation` | GET | `admin_moderation_api` |  |
| `/api/servers/<int:server_id>/moderators` | POST | `admin_set_moderator_api` |  |
| `/api/servers/<int:server_id>/regenerate_key` | POST | `regenerate_key_api` |  |
| `/api/servers/<int:server_id>/stats-reset` | POST | `admin_stats_reset_api` |  |
| `/api/servers/<int:server_id>/unban` | POST | `admin_unban_api` |  |
| `/api/servers/<int:server_id>/update` | POST | `update_server_api` |  |
| `/api/servers/status` | GET | `servers_status_api` |  |
| `/api/signup` | POST | `signup` |  |
| `/download/MCDataLink.jar` | GET | `download_plugin` |  |
| `/download/addons/<addon_id>.jar` | GET | `download_addon` |  |
| `/mc/avatar/<player>/<int:size>` | GET | `player_head` |  |
| `/mc/body/<player>/<int:size>` | GET | `player_body` |  |
| `/mc/item/<item_id>.png` | GET | `item_icon` |  |
| `/mc/skin/<player>` | GET | `player_skin` |  |
| `/resourcepack/<int:server_id>/<sha1>.zip` | GET | `resource_pack` |  |
| `/resourcepack/<sha1>.zip` | GET | `resource_pack` |  |
| `/uploads/<filename>` | GET | `uploaded_image` |  |

<!-- ROUTES:END -->
