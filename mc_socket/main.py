"""Socket server the MCDataLink minecraft plugin connects to.

Protocol: every message is a 10 byte, space padded, ascii length header followed
by the utf-8 payload.

Plugin -> server:
    !AUTH~<server_key>           authenticate (must be the first message)
    !BEAT                        heartbeat
    !JOIN~<uuid>[|<name>[|<op 0/1>]]  player joined
    !QUIT~<uuid>                 player left
    !STATS~<uuid>|<stats json>   content of world/stats/<uuid>.json
    !BANS~<json list>            the server's ban list without MCConnect bans:
                                 [{"name", "reason", "source", "created", "expires"}] (epoch ms, 0 = never)
    !HEALTH~<json>               once a minute: {"tps", "mem_used_mb", "mem_max_mb", "players", "chunks",
                                 "entities", "uptime_s", "mc_version", "plugin_version"}
    !CMD~<uuid>|<world>|<x>|<y>|<z>|<command>|<args>   in-game command (stats, top, wettbewerb, duell, report,
                                 seitenleiste; see mc_socket/commands.py), answered with !tell
    !DISCONNECT                  close the connection

Server -> plugin:
    !heartbeat                   heartbeat
    !sendAllPlayerStats          request stats of every known player
    !loginPin~<uuid>~<pin>       show the website login pin to the player
    !prefix~<uuid>|<color>|<text>  show a prefix (empty color and text: remove it)
    !ban~<uuid>|<name>|<end ms, 0 = permanent>|<reason>   ban (and kick) a player
    !unban~<uuid>|<name>         lift a ban
    !broadcast~<color>|<text>    chat message to everyone (achievements, competitions, records, streaks,
                                 anniversaries, community goals, player of the week);
                                 color is a ChatColor name, e.g. gold
    !tell~<uuid>|<text>          chat message to one player; "&" color codes (plugin 3.4)
    !sidebar~<uuid>|<title>|<line>|...   scoreboard sidebar of a player ("&" color codes); empty title hides it
    !metrics~<name>|<name>|...   metric names for the tab completion of /top and /duell (after auth)
    success|<code> / error|<code>

Error codes:
000: disconnected
001: The license key is invalid
002: No license key is provided
003: Update player status failed
004: Invalid command
005: Invalid request

Success codes:
100: Auth successful
101: updated player status successfully
102: stats saved
103: ban list synced
104: health sample stored
"""
import json
import os
import select
import socket
import sys
import threading
import time
import uuid as uuid_mod

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from colorlogx import get_logger
from database import achievements, config, metrics, motivation
from mc_socket import commands
from database.databaseManagerV2 import DatabaseManager

logger = get_logger("socket")

HEADER = 10
MAX_MESSAGE_SIZE = 16 * 1024 * 1024
HEARTBEAT_SEND_INTERVAL = 5
HEARTBEAT_TIMEOUT = 20
MAX_UNAUTHORIZED_MESSAGES = 5
# How often the "plugin alive" timestamp is written to the database.
PLUGIN_TOUCH_INTERVAL = 30
# How often started/ended competitions, reached community goals and the player of the week are checked.
COMPETITION_CHECK_INTERVAL = 60
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


def recv_msg(conn):
    """Read one framed message. Raises ConnectionError / ProtocolError."""
    header = _recv_exactly(conn, HEADER).decode("utf-8", errors="replace").strip()
    try:
        length = int(header)
    except ValueError:
        raise ProtocolError(f"invalid header {header!r}")
    if length < 0 or length > MAX_MESSAGE_SIZE:
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
                 poll_interval=1.0):
        self.db = db_manager
        self.host = host
        self.port = port
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.poll_interval = poll_interval  # how quickly threads notice stop()
        self.active_connections = {}  # server_id -> ClientConnection
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = None
        self._threads = []

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        """Bind and start accepting connections and login pin notifications in background threads."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen()
        self._sock.settimeout(self.poll_interval)
        self.port = self._sock.getsockname()[1]
        logger.info(f"Socket server listening on {self.host}:{self.port}")
        self._spawn(self._accept_loop, name="socket-accept")
        self._spawn(self._login_pin_loop, name="login-pins")
        self._spawn(self._competition_loop, name="competitions")

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

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(None)
            self._spawn(self.handle_client_connection, conn, addr, name=f"client-{addr[0]}:{addr[1]}")

    def _login_pin_loop(self):
        while not self._stop.is_set():
            try:
                self.db.listen_for_login_pins(self.deliver_login_pin, self._stop, self.poll_interval,
                                              event_callback=self.handle_server_event)
            except Exception:
                logger.exception("Login pin listener failed, restarting in 5 seconds")
                self._stop.wait(5)

    def _competition_loop(self):
        while not self._stop.is_set():
            try:
                self.periodic_checks()
            except Exception:
                logger.exception("Periodic check failed")
            self._stop.wait(COMPETITION_CHECK_INTERVAL)

    # ------------------------------------------------------------------ announcements
    def broadcast(self, server_id, color, text):
        return self._send_to_server(server_id, f"!broadcast~{color}|{self._clean(text)}")

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
        """Achievements, records and milestones after new stats of a player."""
        self.announce_achievements(server_id, player_id, self.db.award_achievements(player_id))
        self.announce_records(server_id, player_id, self.db.update_records(player_id))
        self.announce_milestones(server_id, player_id, self.db.check_milestones(player_id))

    def periodic_checks(self):
        """Competitions, community goals and the player of the week on the connected servers (the others later)."""
        with self._lock:
            connected = list(self.active_connections)
        if not connected:
            return
        self.announce_competitions(connected)
        self.db.award_finished_competitions(connected)
        for goal in self.db.take_reached_goals(connected):
            self.broadcast(goal["server_id"], *motivation.goal_announcement(goal))
        for server_id in connected:
            trophy = self.db.settle_player_of_week(server_id)
            if trophy:
                self.broadcast(server_id, *motivation.player_of_week_announcement(trophy))
        for duel in self.db.take_finished_duels(connected):
            self.broadcast(duel["server_id"], *commands.duel_result(duel))
        for server_id, player_id, uuid, mode in self.db.get_sidebar_players(connected):
            self.send_sidebar(server_id, uuid, commands.sidebar(self.db, player_id, mode))

    # ------------------------------------------------------------------ in-game commands
    def tell(self, server_id, mojang_uuid, text):
        return self._send_to_server(server_id, f"!tell~{mojang_uuid}|{text.replace('|', '/').replace(chr(10), ' ')}")

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
            update_sidebar=lambda player_id: self.update_sidebar(server_id, player_id))
        for line in commands.handle(ctx, parts[5], parts[6] if len(parts) > 6 else ""):
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

    def _send_to_server(self, server_id, msg):
        with self._lock:
            client = self.active_connections.get(server_id)
        if client is None:
            return False
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
            self._send_to_server(server_id, f"!ban~{event['uuid']}|{self._clean(event['name'])}|{end_ms}|"
                                            f"{self._clean(event.get('reason'))}")
        elif kind == "unban":
            self._send_to_server(server_id, f"!unban~{event['uuid']}|{self._clean(event['name'])}")
        elif kind == "broadcast":
            self.broadcast(server_id, event.get("color") or "gold", event.get("text"))
        elif kind == "tell":
            for mojang_uuid in event.get("uuids", []):
                self.tell(server_id, mojang_uuid, str(event.get("text") or ""))
        elif kind == "sidebar":
            self.update_sidebar(server_id, event["player_id"])
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

                ready, _, _ = select.select([conn], [], [], self.poll_interval)
                if not ready:
                    continue
                data = recv_msg(conn)
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
        logger.info(f"{client.addr} authenticated as server {server_id}")
        client.send("success|100")
        client.send("!sendAllPlayerStats")
        for mojang_uuid, (text, color) in self.db.get_all_worn_prefixes(server_id).items():
            client.send(f"!prefix~{mojang_uuid}|{color}|{self._clean(text)}")
        client.send("!metrics~" + "|".join(commands.METRIC_NAMES))
        return True

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
                player_uuid, name, is_op = (value.split("|") + ["", ""])[:3]
                player_uuid = parse_uuid(player_uuid)
                player_id = self.db.register_player_join(client.server_id, player_uuid, name.strip() or None)
                if is_op.strip() in ("0", "1"):
                    self.db.set_player_op(client.server_id, player_uuid, is_op.strip() == "1")
                client.send("success|101")
                client.send(self.prefix_message(client.server_id, player_uuid))
                self.announce_milestones(client.server_id, player_id, self.db.check_milestones(player_id))
                if self.db.get_sidebar(player_id) != "off":
                    self.update_sidebar(client.server_id, player_id)
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
                self.after_stats(client.server_id, player_id)
            elif command == "!CMD":
                self.run_command(client.server_id, value)
            elif command == "!HEALTH":
                self.db.add_health_sample(client.server_id, parse_health(value))
                client.send("success|104")
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


if __name__ == "__main__":
    SocketServer(DatabaseManager()).serve_forever()
