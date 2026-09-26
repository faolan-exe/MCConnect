"""Socket server the MCDataLink minecraft plugin connects to.

Protocol: every message is a 10 byte, space padded, ascii length header followed
by the utf-8 payload.

Plugin -> server:
    !AUTH~<server_key>           authenticate (must be the first message)
    !BEAT                        heartbeat
    !JOIN~<uuid>[|<name>]        player joined
    !QUIT~<uuid>                 player left
    !STATS~<uuid>|<stats json>   content of world/stats/<uuid>.json
    !DISCONNECT                  close the connection

Server -> plugin:
    !heartbeat                   heartbeat
    !sendAllPlayerStats          request stats of every known player
    !loginPin~<uuid>~<pin>       show the website login pin to the player
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
"""
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
from database import config
from database.databaseManagerV2 import DatabaseManager

logger = get_logger("socket")

HEADER = 10
MAX_MESSAGE_SIZE = 16 * 1024 * 1024
HEARTBEAT_SEND_INTERVAL = 5
HEARTBEAT_TIMEOUT = 20
MAX_UNAUTHORIZED_MESSAGES = 5
# How often the "plugin alive" timestamp is written to the database.
PLUGIN_TOUCH_INTERVAL = 30


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
                self.db.listen_for_login_pins(self.deliver_login_pin, self._stop, self.poll_interval)
            except Exception:
                logger.exception("Login pin listener failed, restarting in 5 seconds")
                self._stop.wait(5)

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
                player_uuid, _, name = value.partition("|")
                self.db.register_player_join(client.server_id, parse_uuid(player_uuid), name.strip() or None)
                client.send("success|101")
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
            else:
                client.send("error|004")
        except ValueError as e:  # bad uuid or json
            logger.warning(f"{client.addr} invalid request {data[:200]!r}: {e}")
            client.send("error|005")


if __name__ == "__main__":
    SocketServer(DatabaseManager()).serve_forever()
