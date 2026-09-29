"""Socket server the MCDataLink minecraft plugin connects to.

TLS only, on SOCKET_PORT (plugin 3.12; plain connections are answered with error|006 and closed).
The certificate is the server's own one (created on the first start in MCC_SOCKET_TLS_DIR, the plugins pin
its fingerprint, see mc_socket/tlscert.py) or an own one from MCC_SOCKET_TLS_CERT/KEY.

Protocol: every message is a 10 byte, space padded, ascii length header followed
by the utf-8 payload.

Plugin -> server:
    !AUTH~<server_key>           authenticate (must be the first message)
    !BEAT                        heartbeat
    !JOIN~<uuid>[|<name>[|<op 0/1>[|<first played, epoch ms>]]]  player joined (first played: plugin 3.15)
    !QUIT~<uuid>                 player left
    !STATS~<uuid>|<stats json>   content of world/stats/<uuid>.json
    !BANS~<json list>            the server's ban list without MCConnect bans:
                                 [{"name", "reason", "source", "created", "expires"}] (epoch ms, 0 = never)
    !WEBBANS~<json list>         names of the MCConnect bans still in the server's ban list (plugin 3.7);
                                 a delivered website ban that is missing was lifted with /pardon
    !HEALTH~<json>               once a minute: {"tps", "mem_used_mb", "mem_max_mb", "players", "chunks",
                                 "entities", "uptime_s", "mc_version", "plugin_version"}
    !CMD~<uuid>|<world>|<x>|<y>|<z>|<command>|<args>   in-game command (stats, top, wettbewerb, duell, report,
                                 seitenleiste, vote, events; see mc_socket/commands.py), answered with !tell
    !FEATURES~<name>,<name>      what the plugin can do (after auth): "click" = buttons in chat messages (3.10),
                                 "glyphs" = icons of the resource pack with fallbacks (3.17; older plugins get
                                 every icon replaced by its Unicode fallback, see database/glyphs.py)
    !DISCONNECT                  close the connection

Server -> plugin:
    !heartbeat                   heartbeat
    !sendAllPlayerStats          request stats of every known player
    !loginPin~<uuid>~<pin>       show the website login pin to the player
    !prefix~<uuid>|<color>|<text>  show a prefix (empty color and text: remove it)
    !ban~<uuid>|<name>|<end ms, 0 = permanent>|<reason>[|<until text>]   ban (and kick) a player; the until
                                 text ("bis 27.09.2026 19:14 Uhr", local time zone) is shown in the kick message (3.9)
    !unban~<uuid>|<name>         lift a ban
    !broadcast~<color>|<text>    chat message to everyone (achievements, competitions, records, streaks,
                                 anniversaries, community goals, player of the week);
                                 color is a ChatColor name, e.g. gold
    !tell~<uuid>|<text>          chat message to one player; "&" color codes (plugin 3.4); buttons as
                                 ⟦label⇒/command⟧ only for plugins that announced "click" (see commands.button)
    !sidebar~<uuid>|<title>|<line>|...   scoreboard sidebar of a player ("&" color codes); empty title hides it
    !metrics~<name>|<name>|...   metric names for the tab completion of /top and /duell (after auth)
    !whitelist~add|<name>        put a player on the server's whitelist (accepted application or invite code)
    !joininfo~<url>              where players who are not on the whitelist can apply (kick message; empty = none)
    !mute~<uuid>|<until ms, 0 = unmuted>|<reason>[|<until text>]   block the chat of a player (plugin 3.6);
                                 the until text is in the local time zone (the server's JVM may run in UTC)
    !joinstyle~<uuid>|<join line>|<leave line>|<sound>|<volume>   own join/leave message of a player
                                 ("&" colors; empty lines: the game's own message; plugin 3.14)
    !joinmutes~<uuid>|<sounds off 0/1>|<uuid>,<uuid>   whose join/leave messages a player does not see
    !joindefault~<join line>|<leave line>   the lines of players without an own style ("{name}" for the name;
                                 plugin 3.16), sent with every full sync while the feature is on
    !joinreset~                  forget all join styles, mutes and the default lines (sent before a full sync)
    !pack~<url>|<sha1>|<icon><fallback>,...   the icon resource pack to offer the players and the Unicode fallback
                                 of each icon for players without it (plugin 3.17, after "glyphs")
    !badge~<uuid>|<line>         the line under a player's name ("&" colors, icons; empty: nothing; plugin 3.17)
    success|<code> / error|<code>

Error codes:
000: disconnected
001: The license key is invalid
002: No license key is provided
003: Update player status failed
004: Invalid command
005: Invalid request
006: TLS required (the plugin connected without encryption)

Success codes:
100: Auth successful
101: updated player status successfully
102: stats saved
103: ban list synced
104: health sample stored
105: website bans synced
106: features noted
"""
import html
import json
import os
import re
import select
import socket
import ssl
import sys
import threading
import time
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from colorlogx import get_logger
from database import achievements, badges, config, glyphs, metrics, motivation, rewards
from mc_socket import commands, tlscert
from database.databaseManagerV2 import DatabaseManager

logger = get_logger("socket")

HEADER = 10
MAX_MESSAGE_SIZE = 16 * 1024 * 1024
# Before the authentication only small messages are accepted (!AUTH, !BEAT), so a stranger cannot make
# the server allocate MAX_MESSAGE_SIZE per connection.
MAX_UNAUTHENTICATED_MESSAGE_SIZE = 1024
HEARTBEAT_SEND_INTERVAL = 5
HEARTBEAT_TIMEOUT = 20
MAX_UNAUTHORIZED_MESSAGES = 5
# How often the "plugin alive" timestamp is written to the database.
PLUGIN_TOUCH_INTERVAL = 30
# How often started/ended competitions, reached community goals and the player of the week are checked.
COMPETITION_CHECK_INTERVAL = 60
# Plugin 3.13 sends the stats of online players every few seconds. Achievements, records and streaks of a
# player are checked at most this often (later updates wait in _pending_stats), sidebars follow every
# LIVE_INTERVAL seconds after new stats.
AFTER_STATS_INTERVAL = 15
LIVE_INTERVAL = 5
# More new achievement tiers at once are not announced (first sync of an old player).
MAX_ANNOUNCED_ACHIEVEMENTS = 2
# More records taken with one stats update are not announced.
MAX_ANNOUNCED_RECORDS = 3


class ProtocolError(Exception):
    pass


def encode_msg(msg):
    payload = msg.encode("utf-8")
    header = str(len(payload)).encode("utf-8")
    return header + b" " * (HEADER - len(header)) + payload


def _recv_exactly(conn, length):
    data = bytearray()
    while len(data) < length:
        packet = conn.recv(length - len(data))
        if not packet:
            raise ConnectionError("connection closed")
        data.extend(packet)
    return bytes(data)


def recv_msg(conn, max_size=MAX_MESSAGE_SIZE):
    """Read one framed message. Raises ConnectionError / ProtocolError."""
    header = _recv_exactly(conn, HEADER).decode("utf-8", errors="replace").strip()
    try:
        length = int(header)
    except ValueError:
        raise ProtocolError(f"invalid header {header!r}")
    if length < 0 or length > max_size:
        raise ProtocolError(f"invalid message length {length}")
    return _recv_exactly(conn, length).decode("utf-8")


def parse_health(value):
    """Validated health sample from the plugin's JSON (unknown or broken fields become None)."""
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("health sample must be an object")

    def number(key, kind, low, high):
        try:
            number = kind(data.get(key))
        except (TypeError, ValueError):
            return None
        return number if low <= number <= high else None

    text = lambda key: str(data.get(key) or "")[:100] or None
    return {"tps": number("tps", float, 0, 100), "mem_used_mb": number("mem_used_mb", int, 0, 10 ** 7),
            "mem_max_mb": number("mem_max_mb", int, 0, 10 ** 7), "players": number("players", int, 0, 10 ** 6),
            "chunks": number("chunks", int, 0, 10 ** 8), "entities": number("entities", int, 0, 10 ** 8),
            "uptime_s": number("uptime_s", int, 0, 10 ** 10), "mc_version": text("mc_version"),
            "plugin_version": text("plugin_version")}


def parse_uuid(value):
    return str(uuid_mod.UUID(value.strip()))


class ClientConnection:
    """One connected plugin. send() is thread-safe."""

    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr
        self.server_id = None
        self.features = set()  # announced by the plugin with !FEATURES, e.g. "click" (plugin 3.10)
        self._send_lock = threading.Lock()

    def send(self, msg):
        logger.debug(f"[{self.addr}] -> {msg[:200]}")
        with self._send_lock:
            self.conn.sendall(encode_msg(msg))

    def close(self):
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()


class SocketServer:
    def __init__(self, db_manager, host=config.SOCKET_HOST, port=config.SOCKET_PORT,
                 heartbeat_interval=HEARTBEAT_SEND_INTERVAL, heartbeat_timeout=HEARTBEAT_TIMEOUT,
                 poll_interval=1.0, mailer=None, tls_cert=config.SOCKET_TLS_CERT, tls_key=config.SOCKET_TLS_KEY,
                 tls_dir=config.SOCKET_TLS_DIR):
        self.db = db_manager
        self.tls_cert, self.tls_key, self.tls_dir = tls_cert, tls_key, tls_dir
        self._tls_context = None
        self._tls_loaded = None  # modification times of the loaded certificate files
        self.mailer = mailer  # SMTPMailer for the alerts to the server owners, or None
        self.host = host
        self.port = port
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.poll_interval = poll_interval  # how quickly threads notice stop()
        self.active_connections = {}  # server_id -> ClientConnection
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = None
        self._live_lock = threading.Lock()
        self._pending_stats = {}  # player_id -> server_id: new stats not checked yet (throttled)
        self._last_checked = {}  # player_id -> monotonic time of the last after_stats
        self._changed_servers = set()  # servers with new stats since the last sidebar update
        self._badge_lines = {}  # (server_id, uuid) -> the last !badge line sent (only changes are sent)
        self._threads = []

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        """Bind and start accepting connections and login pin notifications in background threads."""
        if not (self.tls_cert and self.tls_key):
            self.tls_cert, self.tls_key = tlscert.ensure_certificate(self.tls_dir)
            logger.info(f"Own TLS certificate {self.tls_cert}, plugins pin it with "
                        f"tls-fingerprint: \"{tlscert.fingerprint(self.tls_cert)}\" (shown on the admin page)")
        self.tls_context()  # fail early on a broken certificate
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen()
        self._sock.settimeout(self.poll_interval)
        self.port = self._sock.getsockname()[1]
        logger.info(f"Socket server listening with TLS on {self.host}:{self.port}")
        self._spawn(self._accept_loop, self._sock, name="socket-accept")
        self._spawn(self._login_pin_loop, name="login-pins")
        self._spawn(self._competition_loop, name="competitions")
        self._spawn(self._live_loop, name="live")

    def _spawn(self, target, *args, name=None):
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()

    def stop(self, timeout=5):
        """Stop accepting, close all clients and wait for the worker threads."""
        self._stop.set()
        if self._sock:
            self._sock.close()
        with self._lock:
            clients = list(self.active_connections.values())
        for client in clients:
            client.close()
        for thread in self._threads:
            thread.join(timeout)

    def serve_forever(self):
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down")
        finally:
            self.stop()

    def tls_context(self):
        """TLS context with the configured certificate, reloaded when the files change (renewals)."""
        loaded = (os.path.getmtime(self.tls_cert), os.path.getmtime(self.tls_key))
        if self._tls_context is None or loaded != self._tls_loaded:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.tls_cert, self.tls_key)
            self._tls_context, self._tls_loaded = context, loaded
            logger.info(f"Loaded TLS certificate {self.tls_cert}")
        return self._tls_context

    def _accept_loop(self, sock):
        while not self._stop.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._spawn(self._start_client, conn, addr, name=f"client-{addr[0]}:{addr[1]}")

    def _start_client(self, conn, addr):
        try:
            conn.settimeout(10)  # a silent client must not block the thread forever
            first = conn.recv(1, socket.MSG_PEEK)
            if first != b"\x16":  # not a TLS handshake: an old plugin (tls: false) or something else
                logger.info(f"{addr} connected without TLS, refused")
                if first:
                    conn.sendall(encode_msg("error|006"))
                conn.close()
                return
            conn = self.tls_context().wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError) as e:
            logger.info(f"{addr} TLS handshake failed: {e}")
            conn.close()
            return
        conn.settimeout(None)
        self.handle_client_connection(conn, addr)

    def _login_pin_loop(self):
        while not self._stop.is_set():
            try:
                self.db.listen_for_login_pins(self.deliver_login_pin, self._stop, self.poll_interval,
                                              event_callback=self.handle_server_event)
            except Exception:
                logger.exception("Login pin listener failed, restarting in 5 seconds")
                self._stop.wait(5)

    def _live_loop(self):
        while not self._stop.is_set():
            self._stop.wait(LIVE_INTERVAL)
            try:
                self.live_checks()
            except Exception:
                logger.exception("Live check failed")

    def stats_received(self, server_id, player_id):
        """New stats of a player: check achievements & co. now, or later if they were checked just before."""
        player_id = str(player_id)
        now = time.monotonic()
        with self._live_lock:
            self._changed_servers.add(server_id)
            due = now - self._last_checked.get(player_id, float("-inf")) >= AFTER_STATS_INTERVAL
            if due:
                self._last_checked[player_id] = now
                self._pending_stats.pop(player_id, None)
            else:
                self._pending_stats[player_id] = server_id
        if due:
            self.after_stats(server_id, player_id)

    def live_checks(self):
        """Every LIVE_INTERVAL seconds: the throttled after_stats that are due, sidebars of servers with new stats."""
        now = time.monotonic()
        with self._live_lock:
            due = [(player_id, server_id) for player_id, server_id in self._pending_stats.items()
                   if now - self._last_checked.get(player_id, float("-inf")) >= AFTER_STATS_INTERVAL]
            for player_id, _ in due:
                del self._pending_stats[player_id]
                self._last_checked[player_id] = now
            changed, self._changed_servers = self._changed_servers, set()
        for player_id, server_id in due:
            self._step(f"stats of {player_id}", self.after_stats, server_id, player_id)
        with self._lock:
            connected = [server_id for server_id in changed if server_id in self.active_connections]
        if connected:
            self._step("live sidebars", self.update_sidebars, connected)

    def _competition_loop(self):
        while not self._stop.is_set():
            try:
                self.periodic_checks()
            except Exception:
                logger.exception("Periodic check failed")
            self._stop.wait(COMPETITION_CHECK_INTERVAL)

    # ------------------------------------------------------------------ announcements
    def broadcast(self, server_id, color, text):
        return self._send_to_server(server_id, f"!broadcast~{color}|{self._clean(text)}", chat=True)

    def announce_achievements(self, server_id, player_id, awards):
        if not awards or len(awards) > MAX_ANNOUNCED_ACHIEVEMENTS or self.db.is_stats_hidden(player_id):
            return
        name = self.db.get_player_name_from_player_id(player_id) or "Jemand"
        for achievement, tier in awards:
            self.broadcast(server_id, *achievements.announcement(name, achievement, tier))

    def announce_milestones(self, server_id, player_id, milestones):
        if not milestones or self.db.is_stats_hidden(player_id):
            return
        name = self.db.get_player_name_from_player_id(player_id) or "Jemand"
        for kind, value in milestones:
            self.broadcast(server_id, *motivation.milestone_announcement(name, kind, value))

    def announce_records(self, server_id, player_id, records):
        if not records or len(records) > MAX_ANNOUNCED_RECORDS:
            return
        name = self.db.get_player_name_from_player_id(player_id) or "Jemand"
        for metric, previous, value in records:
            self.broadcast(server_id, *motivation.record_announcement(name, metric, previous, value))

    def after_stats(self, server_id, player_id):
        """Achievements, records, milestones and reward levels after new stats of a player."""
        self.announce_achievements(server_id, player_id, self.db.award_achievements(player_id))
        self.announce_records(server_id, player_id, self.db.update_records(player_id))
        self.announce_milestones(server_id, player_id, self.db.check_milestones(player_id))
        self.check_reward_level(server_id, player_id)
        self.update_badge(server_id, player_id)

    def update_badge(self, server_id, player_id, force=False):
        """Send the line under the player's name if it changed (force: always, e.g. after a join)."""
        uuid = str(self.db.get_mojang_uuid_from_player_id(player_id))
        line = badges.line(self.db, player_id)
        key = (server_id, uuid)
        if not force and self._badge_lines.get(key) == line:
            return
        if self._send_to_server(server_id, badges.message(uuid, line)):
            self._badge_lines[key] = line

    def pack_message(self):
        data, sha1 = glyphs.build_pack()
        return f"!pack~{config.PUBLIC_SCHEME}://{config.BASE_DOMAIN}/resourcepack/{sha1}.zip|{sha1}|{glyphs.glyph_map()}"

    def check_reward_level(self, server_id, player_id):
        """Tell a player who reached a new reward level what it unlocks (and update the message, e.g. rainbow)."""
        state = rewards.player_state(self.db, player_id)
        if state["new_level"] is None or not state["enabled"]:
            return
        level = state["levels"][state["new_level"]]
        uuid = str(self.db.get_mojang_uuid_from_player_id(player_id))
        self.tell(server_id, uuid, f"&6★ Neue Stufe »{commands.clean(level['name'])}«! &7Neue Farben, Symbole und mehr für "
                                   "deine Join-Nachricht: " + commands.button("&a[/joinmessage]", "/joinmessage"))
        self._send_to_server(server_id, rewards.join_style_message(self.db, player_id, state))

    def sync_join_messages(self, server_id):
        """All join styles and mutes of a server to its plugin (after the auth or when the feature is switched)."""
        self._send_to_server(server_id, "!joinreset~")
        enabled, stored, texts = self.db.get_reward_settings(server_id)
        if not enabled:
            return
        self._send_to_server(server_id, rewards.join_default_message(stored, texts))
        styled, mutes = self.db.get_join_sync(server_id)
        for player_id in styled:
            self._send_to_server(server_id, rewards.join_style_message(self.db, player_id))
        for uuid, (sounds_off, muted) in mutes.items():
            self._send_to_server(server_id, rewards.join_mutes_message(uuid, sounds_off, muted))

    def periodic_checks(self):
        """
        Everything that runs once a minute. Each step is guarded on its own: a failing step is logged and
        does not stop the others (event reminders, sidebar updates, ...).
        """
        self._step("alerts", self.send_alerts)
        with self._lock:
            connected = list(self.active_connections)
        if not connected:
            return
        self._step("competitions", self.announce_competitions, connected)
        self._step("competition trophies", self.db.award_finished_competitions, connected)
        self._step("events", self.announce_events, connected)
        self._step("community goals", self.announce_goals, connected)
        for server_id in connected:
            self._step(f"player of the week (server {server_id})", self.announce_player_of_week, server_id)
        self._step("polls", self.announce_polls, connected)
        self._step("duels", self.announce_duels, connected)
        self._step("sidebars", self.update_sidebars, connected)

    def _step(self, name, function, *args):
        try:
            function(*args)
        except Exception:
            logger.exception(f"Periodic check failed: {name}")

    def announce_events(self, connected):
        for kind, event in self.db.take_due_event_announcements(connected):
            self.broadcast(event["server_id"], *commands.event_announcement(kind, event))

    def announce_goals(self, connected):
        for goal in self.db.take_reached_goals(connected):
            self.broadcast(goal["server_id"], *motivation.goal_announcement(goal))

    def announce_player_of_week(self, server_id):
        trophy = self.db.settle_player_of_week(server_id)
        if trophy:
            self.broadcast(server_id, *motivation.player_of_week_announcement(trophy))

    def announce_polls(self, connected):
        for poll in self.db.take_finished_polls(connected):
            self.broadcast(poll["server_id"], *commands.poll_result(poll))

    def announce_duels(self, connected):
        for duel in self.db.take_finished_duels(connected):
            self.broadcast(duel["server_id"], *commands.duel_result(duel))

    def update_sidebars(self, connected):
        for server_id, player_id, uuid, mode in self.db.get_sidebar_players(connected):
            self._step(f"sidebar of {uuid}", lambda: self.send_sidebar(server_id, uuid, commands.sidebar(self.db, player_id, mode)))

    def send_alerts(self):
        """E-mail the owners of servers that are offline or lag (see DatabaseManager.get_alert_candidates)."""
        for alert in self.db.get_alert_candidates():
            url = f"{config.PUBLIC_SCHEME}://{alert['subdomain']}.{config.BASE_DOMAIN}/users#health"
            since = alert["since"].strftime("%d.%m.%Y, %H:%M Uhr")
            name = " ".join(alert["server_name"].split())  # one line for the subject
            if alert["kind"] == "offline":
                subject = f"MCConnect: {name} ist nicht erreichbar"
                text = (f"das Plugin von <b>{html.escape(name)}</b> ist seit {since} nicht mehr mit MCConnect verbunden. "
                        "Läuft der Server noch?")
            else:
                subject = f"MCConnect: {name} laggt"
                text = (f"<b>{html.escape(name)}</b> hat seit {since} weniger als {self.db_alert_tps()} TPS. "
                        "Die Spieler merken das als Ruckeln.")
            body = (f"<p>Hallo,</p><p>{text}</p><p>Serverzustand: <a href=\"{url}\">{url}</a></p>"
                    "<p>Diese E-Mails kannst du auf der Verwaltungsseite von MCConnect abschalten.</p>")
            if self.mailer:
                self.mailer.send_email(alert["email"], subject, body)
            else:
                logger.warning(f"No SMTP configured, alert not sent: {subject}")
            self.db.mark_alert_sent(alert["server_id"], alert["kind"])

    @staticmethod
    def db_alert_tps():
        from database.databaseManagerV2 import ALERT_TPS
        return int(ALERT_TPS)

    # ------------------------------------------------------------------ whitelist, mutes
    def sync_whitelist(self, server_id):
        names = self.db.get_unsynced_whitelist(server_id)
        sent = [name for name in names if self._send_to_server(server_id, f"!whitelist~add|{self._clean(name)}")]
        if sent:
            self.db.mark_whitelist_synced(server_id, sent)

    @staticmethod
    def until_text(until_ms, with_year=False):
        """"bis 27.09. 19:14 Uhr" in the configured time zone ("dauerhaft" for 0)."""
        if not until_ms:
            return "dauerhaft"
        local = datetime.fromtimestamp(until_ms / 1000, ZoneInfo(config.TIMEZONE))
        return local.strftime("bis %d.%m.%Y %H:%M Uhr" if with_year else "bis %d.%m. %H:%M Uhr")

    def mute_message(self, mojang_uuid, until, reason):
        until_ms = int(until.timestamp() * 1000) if until else 0
        return f"!mute~{mojang_uuid}|{until_ms}|{self._clean(reason)}|{self.until_text(until_ms) if until_ms else ''}"

    def send_mute(self, server_id, mojang_uuid, until, reason):
        return self._send_to_server(server_id, self.mute_message(mojang_uuid, until, reason))

    def send_ban(self, server_id, ban):
        end_ms = int(ban["end"].timestamp() * 1000) if ban["end"] else 0
        if self._send_to_server(server_id, f"!ban~{ban['uuid']}|{self._clean(ban['name'])}|{end_ms}|"
                                           f"{self._clean(ban['reason'])}|{self.until_text(end_ms, True)}"):
            self.db.mark_ban_delivered(server_id, ban["uuid"])

    # ------------------------------------------------------------------ in-game commands
    def tell(self, server_id, mojang_uuid, text):
        return self._send_to_server(server_id, f"!tell~{mojang_uuid}|{text.replace('|', '/').replace(chr(10), ' ')}",
                                    chat=True)

    def send_sidebar(self, server_id, mojang_uuid, content):
        if content is None:
            return self._send_to_server(server_id, f"!sidebar~{mojang_uuid}|")
        title, lines = content
        return self._send_to_server(server_id, "!sidebar~" + "|".join(
            [str(mojang_uuid), title] + [line.replace("|", "/") for line in lines]))

    def update_sidebar(self, server_id, player_id):
        uuid = self.db.get_mojang_uuid_from_player_id(player_id)
        self.send_sidebar(server_id, uuid, commands.sidebar(self.db, player_id, self.db.get_sidebar(player_id)))

    def run_command(self, server_id, value):
        """!CMD from the plugin: answer the player with !tell lines."""
        parts = value.split("|", 6)
        if len(parts) < 6:
            raise ValueError("command needs uuid, position and name")
        uuid = parse_uuid(parts[0])
        try:
            location = (parts[1][:64], int(float(parts[2])), int(float(parts[3])), int(float(parts[4])))
        except ValueError:
            location = None
        ctx = commands.CommandContext(
            self.db, server_id, uuid, location if parts[1] else None,
            tell=lambda to, text: self.tell(server_id, to, text),
            broadcast=lambda color, text: self.broadcast(server_id, color, text),
            update_sidebar=lambda player_id: self.update_sidebar(server_id, player_id),
            ban=lambda result: self.send_ban(server_id, result),
            mute=lambda uuid, until, reason: self.send_mute(server_id, uuid, until, reason),
            send=lambda message: self._send_to_server(server_id, message))
        try:
            lines = commands.handle(ctx, parts[5], parts[6] if len(parts) > 6 else "")
        except Exception:
            # a failing command must not end the connection of the whole server
            logger.exception(f"Command {parts[5]!r} of {uuid} on server {server_id} failed")
            lines = ["&cDabei ist etwas schiefgelaufen. Versuche es später noch einmal."]
        for line in lines:
            self.tell(server_id, uuid, line)

    def announce_competitions(self, connected=None):
        """Announce started and ended competitions on the given (default: connected) servers."""
        if connected is None:
            with self._lock:
                connected = list(self.active_connections)
            if not connected:
                return
        for kind, competition in self.db.take_due_competition_announcements(connected):
            metric = metrics.METRICS_BY_KEY.get(competition["metric"])
            if metric is None:
                continue
            title = competition["title"]
            if kind == "start":
                text = (f"★ Wettbewerb »{title}« hat begonnen: {metric.label} bis "
                        f"{competition['ends_on'].strftime('%d.%m.')}. Stand auf {self.competition_url(competition)}")
            else:
                top = self.db.get_competition_standings(competition)[:3]
                places = ", ".join(f"{i}. {row['name']} ({metrics.format_value(metric, row['value'])})"
                                   for i, row in enumerate(top, 1))
                text = f"★ Wettbewerb »{title}« ist vorbei! " + (places or "Diesmal hat niemand mitgemacht.")
            self.broadcast(competition["server_id"], "gold", text)

    def competition_url(self, competition):
        subdomain = self.db.get_subdomain_from_server_id(competition["server_id"])
        return f"{config.PUBLIC_SCHEME}://{subdomain}.{config.BASE_DOMAIN}/wettbewerbe"

    # ------------------------------------------------------------------ login pins
    def deliver_login_pin(self, server_id, mojang_uuid, pin):
        with self._lock:
            client = self.active_connections.get(server_id)
        if client is None:
            logger.warning(f"Login pin for server {server_id}, but the server is not connected")
            return False
        try:
            client.send(f"!loginPin~{mojang_uuid}~{pin}")
            return True
        except OSError as e:
            logger.error(f"Could not deliver login pin to server {server_id}: {e}")
            return False

    # ------------------------------------------------------------------ website events
    @staticmethod
    def _clean(text):
        """Remove the protocol separators from free text."""
        return str(text or "").replace("~", "-").replace("|", "/").replace("\n", " ")

    def _send_to_server(self, server_id, msg, chat=False):
        """Send a message to the plugin of a server. chat: may contain buttons (removed for older plugins)."""
        with self._lock:
            client = self.active_connections.get(server_id)
        if client is None:
            return False
        if "glyphs" not in client.features and glyphs.ICON_RE.search(msg):
            msg = glyphs.fallback_text(msg)  # the plugin cannot show the icons (before 3.17 or before !FEATURES)
        if chat and "click" not in client.features:
            msg = commands.without_buttons(msg)
            if msg.startswith("!broadcast~"):  # older plugins show broadcasts without "&" color codes
                msg = re.sub(r"&[0-9a-fk-or]", "", msg)
        try:
            client.send(msg)
            return True
        except OSError as e:
            logger.error(f"Could not send to server {server_id}: {e}")
            return False

    def prefix_message(self, server_id, mojang_uuid):
        prefix = self.db.get_prefix_for_uuid(server_id, mojang_uuid)
        text, color = prefix if prefix else ("", "")
        return f"!prefix~{mojang_uuid}|{color}|{self._clean(text)}"

    def handle_server_event(self, event):
        """Forward a change made on the website (see DatabaseManager.notify_server_event) to the plugin."""
        server_id, kind = event["server_id"], event["type"]
        if kind == "prefix":
            for mojang_uuid in event.get("uuids", []):
                self._send_to_server(server_id, self.prefix_message(server_id, mojang_uuid))
        elif kind == "ban":
            end_ms = int(event.get("end_ms") or 0)
            if self._send_to_server(server_id, f"!ban~{event['uuid']}|{self._clean(event['name'])}|{end_ms}|"
                                               f"{self._clean(event.get('reason'))}|{self.until_text(end_ms, True)}"):
                self.db.mark_ban_delivered(server_id, event["uuid"])
        elif kind == "unban":
            self._send_to_server(server_id, f"!unban~{event['uuid']}|{self._clean(event['name'])}")
        elif kind == "broadcast":
            self.broadcast(server_id, event.get("color") or "gold", event.get("text"))
        elif kind == "tell":
            for mojang_uuid in event.get("uuids", []):
                self.tell(server_id, mojang_uuid, str(event.get("text") or ""))
        elif kind == "sidebar":
            self.update_sidebar(server_id, event["player_id"])
        elif kind == "whitelist":
            self.sync_whitelist(server_id)
        elif kind == "mute":
            until = datetime.fromtimestamp(event["until_ms"] / 1000, timezone.utc) if event.get("until_ms") else None
            self.send_mute(server_id, event["uuid"], until, event.get("reason"))
        elif kind == "joininfo":
            self.send_joininfo(server_id)
        elif kind == "joinstyle":
            self._send_to_server(server_id, rewards.join_style_message(self.db, event["player_id"]))
        elif kind == "joinmutes":
            player_id = event["player_id"]
            settings = self.db.get_join_settings(player_id)
            self._send_to_server(server_id, rewards.join_mutes_message(
                self.db.get_mojang_uuid_from_player_id(player_id), settings["sounds_off"],
                [m["uuid"] for m in self.db.get_join_mutes(player_id)]))
        elif kind == "badge":
            self.update_badge(server_id, event["player_id"])
        elif kind == "joinsync":
            self.sync_join_messages(server_id)
        else:
            logger.warning(f"Unknown server event {kind}")

    # ------------------------------------------------------------------ clients
    def handle_client_connection(self, conn, addr):
        client = ClientConnection(conn, addr)
        logger.info(f"{addr} connected")
        last_received = last_sent = last_touch = time.monotonic()
        unauthorized_messages = 0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_sent >= self.heartbeat_interval:
                    client.send("!heartbeat")
                    last_sent = now
                if now - last_received > self.heartbeat_timeout:
                    logger.info(f"{addr} sent nothing for {self.heartbeat_timeout}s, disconnecting")
                    break

                # TLS may hold decrypted data that select() does not see
                pending = isinstance(conn, ssl.SSLSocket) and conn.pending()
                ready = pending or select.select([conn], [], [], self.poll_interval)[0]
                if not ready:
                    continue
                data = recv_msg(conn, MAX_MESSAGE_SIZE if client.server_id is not None else MAX_UNAUTHENTICATED_MESSAGE_SIZE)
                last_received = time.monotonic()
                if client.server_id is not None and last_received - last_touch >= PLUGIN_TOUCH_INTERVAL:
                    self.db.touch_plugin(client.server_id)
                    last_touch = last_received
                logger.debug(f"[{addr}] <- {data[:200]}")

                if data == "!BEAT":
                    continue
                if data == "!DISCONNECT":
                    break
                if client.server_id is None:
                    unauthorized_messages += 1
                    if not self._authenticate(client, data) and unauthorized_messages >= MAX_UNAUTHORIZED_MESSAGES:
                        logger.info(f"{addr} failed to authenticate, disconnecting")
                        break
                    continue
                self.execute_command(client, data)
        except (ConnectionError, OSError) as e:
            logger.info(f"{addr} connection lost: {e}")
        except ProtocolError as e:
            logger.warning(f"{addr} protocol error: {e}")
        except Exception:
            logger.exception(f"Unexpected error with client {addr}")
        finally:
            self._unregister(client)
            client.close()
            logger.info(f"{addr} disconnected")

    def _authenticate(self, client, data):
        if not data.startswith("!AUTH~"):
            client.send("error|002")
            return False
        server_id = self.db.get_server_id_by_auth_key(data.split("~", 1)[1].strip())
        if server_id is None:
            client.send("error|001")
            return False
        client.server_id = server_id
        with self._lock:
            old = self.active_connections.get(server_id)
            self.active_connections[server_id] = client
        if old is not None:
            logger.warning(f"Server {server_id} connected twice, closing the old connection")
            old.close()
        # The plugin re-sends JOIN for everyone online after auth.
        self.db.set_all_players_offline(server_id)
        self.db.set_plugin_connected(server_id, True)
        self.db.mark_tracking_started(server_id)
        logger.info(f"{client.addr} authenticated as server {server_id}")
        client.send("success|100")
        client.send("!sendAllPlayerStats")
        for mojang_uuid, (text, color) in self.db.get_all_worn_prefixes(server_id).items():
            client.send(f"!prefix~{mojang_uuid}|{color}|{self._clean(text)}")
        client.send("!metrics~" + "|".join(commands.METRIC_NAMES))
        for mojang_uuid, (until, reason) in self.db.get_mutes(server_id).items():
            client.send(self.mute_message(mojang_uuid, until, reason))
        self.send_joininfo(server_id, only_if_set=True)
        self.sync_join_messages(server_id)
        self.sync_whitelist(server_id)
        for ban in self.db.get_undelivered_web_bans(server_id):  # banned on the website while offline
            self.send_ban(server_id, ban)
        return True

    def send_joininfo(self, server_id, only_if_set=False):
        settings = self.db.get_server_settings(server_id)
        url = "" if settings["access_mode"] == "off" else commands.page_url(self.db, server_id, "/mitmachen")
        if url or not only_if_set:
            self._send_to_server(server_id, f"!joininfo~{url}")

    def _unregister(self, client):
        if client.server_id is None:
            return
        with self._lock:
            if self.active_connections.get(client.server_id) is not client:
                return  # replaced by a newer connection
            del self.active_connections[client.server_id]
        try:
            self.db.set_all_players_offline(client.server_id)
            self.db.set_plugin_connected(client.server_id, False)
        except Exception:
            logger.exception(f"Could not mark players of server {client.server_id} offline")

    def execute_command(self, client, data):
        command, sep, value = data.partition("~")
        if not sep:
            client.send("error|005")
            return
        try:
            if command == "!JOIN":
                player_uuid, name, is_op, first_played = (value.split("|") + ["", "", ""])[:4]
                player_uuid = parse_uuid(player_uuid)
                try:
                    first_played = int(first_played)
                    first_played = datetime.fromtimestamp(first_played / 1000, timezone.utc) if first_played > 0 else None
                except (ValueError, OverflowError, OSError):
                    first_played = None
                player_id, first = self.db.register_player_join_info(client.server_id, player_uuid, name.strip() or None,
                                                                     first_played)
                if is_op.strip() in ("0", "1"):
                    self.db.set_player_op(client.server_id, player_uuid, is_op.strip() == "1")
                client.send("success|101")
                client.send(self.prefix_message(client.server_id, player_uuid))
                self.announce_milestones(client.server_id, player_id, self.db.check_milestones(player_id))
                if self.db.get_sidebar(player_id) != "off":
                    self.update_sidebar(client.server_id, player_id)
                self.update_badge(client.server_id, player_id, force=True)
                # new on the server (not only new to MCConnect, e.g. an old player after installing the plugin)
                new_here = first and (first_played is None or first_played > datetime.now(timezone.utc) - timedelta(days=1))
                if new_here and self.db.get_server_settings(client.server_id)["rules_enabled"]:
                    self.tell(client.server_id, player_uuid, "&6Willkommen auf dem Server! &7Bitte lies zuerst die Regeln: &b"
                              + commands.page_url(self.db, client.server_id, "/regeln"))
            elif command == "!QUIT":
                ok = self.db.register_player_quit(client.server_id, parse_uuid(value))
                client.send("success|101" if ok else "error|003")
            elif command == "!STATS":
                player_uuid, sep, stats = value.partition("|")
                if not sep:
                    client.send("error|005")
                    return
                player_id = self.db.ensure_player_on_server(client.server_id, parse_uuid(player_uuid))
                self.db.update_player_stats(player_id, stats)
                client.send("success|102")
                self.stats_received(client.server_id, player_id)
            elif command == "!FEATURES":
                client.features = {f.strip() for f in value.split(",") if f.strip()}
                client.send("success|106")
                if "glyphs" in client.features:
                    # the join lines of the auth were sent with fallbacks: once more with the icons
                    client.send(self.pack_message())
                    self.sync_join_messages(client.server_id)
            elif command == "!CMD":
                self.run_command(client.server_id, value)
            elif command == "!HEALTH":
                self.db.add_health_sample(client.server_id, parse_health(value))
                client.send("success|104")
            elif command == "!WEBBANS":
                names = json.loads(value)
                if not isinstance(names, list):
                    raise ValueError("web ban list must be a list of names")
                self.db.sync_web_bans(client.server_id, [str(n) for n in names])
                client.send("success|105")
            elif command == "!BANS":
                entries = json.loads(value)
                if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
                    raise ValueError("ban list must be a list of objects")
                self.db.sync_ingame_bans(client.server_id, entries)
                client.send("success|103")
            else:
                logger.warning(f"{client.addr} unknown command {command!r}")
                client.send("error|004")
        except ValueError as e:  # bad uuid or json
            logger.warning(f"{client.addr} invalid request {data[:200]!r}: {e}")
            client.send("error|005")
        except (ConnectionError, OSError):
            raise  # the connection itself is broken
        except Exception:
            # e.g. a database error: answer this message with an error, but keep the connection
            logger.exception(f"{client.addr} request failed: {data[:200]!r}")
            client.send("error|005")


if __name__ == "__main__":
    mailer = None
    if config.SMTP_HOST:
        from database.SMTPMailer import SMTPMailer
        mailer = SMTPMailer(config.SMTP_HOST, config.SMTP_PORT, config.SMTP_USER, config.SMTP_PASSWORD)
    SocketServer(DatabaseManager(), mailer=mailer).serve_forever()
