"""Socket server tests with a fake plugin speaking the real wire protocol."""
import json
import socket
import ssl
import time

import pytest

from mc_socket.main import HEADER, SocketServer, encode_msg, recv_msg
from tests.conftest import OTHER_UUID, PLAYER_UUID, wait_for


def tls_connect(port, certificate):
    """TLS connection that trusts exactly the test certificate (like the plugin's tls-fingerprint)."""
    context = ssl.create_default_context(cafile=certificate[0])
    return context.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5), server_hostname="localhost")


class FakePlugin:
    """Speaks the plugin protocol. Prefix updates are collected separately (self.prefixes)."""

    def __init__(self, port, certificate):
        self.sock = tls_connect(port, certificate)
        self.prefixes = []

    def send(self, msg):
        self.sock.sendall(encode_msg(msg))

    def recv(self, skip_heartbeats=True):
        while True:
            msg = recv_msg(self.sock)
            if skip_heartbeats and msg == "!heartbeat":
                continue
            if msg.startswith("!metrics~"):
                self.metrics = msg
                continue
            if msg.startswith("!prefix~"):
                self.prefixes.append(msg)
                continue
            return msg

    def recv_prefix(self):
        """Next prefix message; fails if another message arrives first."""
        while not self.prefixes:
            msg = recv_msg(self.sock)
            if msg == "!heartbeat" or msg.startswith("!metrics~"):
                continue
            if not msg.startswith("!prefix~"):
                raise AssertionError(f"expected a prefix message, got {msg!r}")
            self.prefixes.append(msg)
        return self.prefixes.pop(0)

    def request(self, msg):
        self.send(msg)
        return self.recv()

    def auth(self, key):
        assert self.request(f"!AUTH~{key}") == "success|100"
        assert self.recv() == "!sendAllPlayerStats"
        return self

    def is_closed(self, timeout=3):
        self.sock.settimeout(timeout)
        try:
            while True:
                data = self.sock.recv(1024)
                if not data:
                    return True
        except (ConnectionError, OSError):
            return True

    def close(self):
        self.sock.close()


@pytest.fixture
def socket_server(db, certificate):
    srv = SocketServer(db, host="127.0.0.1", port=0, heartbeat_interval=0.5, heartbeat_timeout=2,
                       poll_interval=0.05, tls_cert=certificate[0], tls_key=certificate[1])
    srv.start()
    time.sleep(0.2)  # let the LISTEN connection come up
    yield srv
    srv.stop()


@pytest.fixture
def plugin(socket_server, certificate):
    clients = []

    def connect():
        client = FakePlugin(socket_server.port, certificate)
        clients.append(client)
        return client
    yield connect
    for client in clients:
        client.close()


def test_framing_roundtrip():
    assert encode_msg("äb") == b"3" + b" " * (HEADER - 1) + "äb".encode()


def test_auth_success(db, server, plugin, socket_server):
    plugin().auth(server["key"])
    assert server["id"] in socket_server.active_connections
    assert db.is_plugin_online(server["id"]) is True


def test_auth_wrong_key(server, plugin):
    assert plugin().request("!AUTH~wrong") == "error|001"


def test_commands_require_auth(server, plugin):
    assert plugin().request(f"!JOIN~{PLAYER_UUID}") == "error|002"


def test_repeated_unauthorized_messages_disconnect(server, plugin):
    client = plugin()
    for _ in range(5):
        client.send("!AUTH~wrong")
    assert client.is_closed()


def test_join_and_quit(db, server, plugin):
    client = plugin().auth(server["key"])
    assert client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444") == "success|101"
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    assert db.get_online_status_by_player_id(player_id) is True
    assert db.get_player_name_from_player_id(player_id) == "_Tobias4444"

    assert client.request(f"!QUIT~{PLAYER_UUID}") == "success|101"
    assert db.get_online_status_by_player_id(player_id) is False


def test_join_without_name_uses_lookup(db, server, plugin):
    client = plugin().auth(server["key"])
    assert client.request(f"!JOIN~{OTHER_UUID}") == "success|101"
    assert db.get_player_name_from_mojang_uuid(OTHER_UUID) == "Notch"


def test_quit_unknown_player(server, plugin):
    client = plugin().auth(server["key"])
    assert client.request(f"!QUIT~{PLAYER_UUID}") == "error|003"


def test_stats_create_player_and_store_values(db, server, plugin):
    client = plugin().auth(server["key"])
    stats = {"stats": {"minecraft:custom": {"minecraft:deaths": 3}}}
    assert client.request(f"!STATS~{PLAYER_UUID}|{json.dumps(stats)}") == "success|102"
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    assert db.get_value_from_unique_object_from_action_table_with_player_id("minecraft:deaths", player_id) == 3


def test_large_stats_message(db, server, plugin):
    client = plugin().auth(server["key"])
    stats = {"stats": {"minecraft:mined": {f"minecraft:block_{i}": i + 1 for i in range(3000)}}}
    assert client.request(f"!STATS~{PLAYER_UUID}|{json.dumps(stats)}") == "success|102"


@pytest.mark.parametrize("msg", [
    "!JOIN~not-a-uuid",
    f"!STATS~{PLAYER_UUID}",
    f"!STATS~{PLAYER_UUID}|not json",
    "no tilde",
])
def test_invalid_requests(server, plugin, msg):
    client = plugin().auth(server["key"])
    assert client.request(msg) == "error|005"
    # the connection stays usable
    assert client.request(f"!JOIN~{PLAYER_UUID}") == "success|101"


def test_unknown_command(server, plugin):
    assert plugin().auth(server["key"]).request("!FOO~bar") == "error|004"


def test_invalid_header_disconnects(server, plugin):
    client = plugin()
    client.sock.sendall(b"garbage!!!")
    assert client.is_closed()


def test_heartbeat_timeout_disconnects(server, plugin):
    client = plugin().auth(server["key"])
    assert client.is_closed(timeout=5)


def test_heartbeats_keep_connection_alive(server, plugin):
    client = plugin().auth(server["key"])
    deadline = time.monotonic() + 3  # longer than heartbeat_timeout
    while time.monotonic() < deadline:
        client.send("!BEAT")
        time.sleep(0.3)
    assert client.request(f"!JOIN~{PLAYER_UUID}") == "success|101"


def test_disconnect_marks_players_offline(db, server, plugin, socket_server):
    client = plugin().auth(server["key"])
    client.request(f"!JOIN~{PLAYER_UUID}")
    client.send("!DISCONNECT")
    assert client.is_closed()
    wait_for(lambda: db.get_online_player_count_from_subdomain("testdomain") == 0)
    wait_for(lambda: server["id"] not in socket_server.active_connections)
    wait_for(lambda: db.is_plugin_online(server["id"]) is False)


def test_reconnect_replaces_old_connection(db, server, plugin, socket_server):
    first = plugin().auth(server["key"])
    second = plugin().auth(server["key"])
    assert first.is_closed()
    assert socket_server.active_connections[server["id"]] is not None
    assert second.request(f"!JOIN~{PLAYER_UUID}") == "success|101"
    # the closed old connection must not mark players offline or unregister the new one
    time.sleep(0.3)
    assert db.get_online_player_count_from_subdomain("testdomain") == 1
    assert server["id"] in socket_server.active_connections


def test_login_pin_is_delivered_via_notify(db, server, plugin):
    client = plugin().auth(server["key"])
    client.request(f"!JOIN~{PLAYER_UUID}")
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    db.add_login_entry_from_player_id(player_id, 424242)
    assert client.recv() == f"!loginPin~{PLAYER_UUID}~424242"


def test_login_pin_goes_to_the_right_server(db, server, other_server, plugin):
    first = plugin().auth(server["key"])
    second = plugin().auth(other_server["key"])
    second.request(f"!JOIN~{PLAYER_UUID}")
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, other_server["id"])
    db.add_login_entry_from_player_id(player_id, 111222)
    assert second.recv() == f"!loginPin~{PLAYER_UUID}~111222"
    first.sock.settimeout(0.5)
    with pytest.raises(socket.timeout):
        first.recv()



# ------------------------------------------------------------------ prefixes, ops, bans

def test_join_sends_prefix_and_op_status(db, server, plugin):
    client = plugin().auth(server["key"])
    assert client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444|1") == "success|101"
    assert client.recv_prefix() == f"!prefix~{PLAYER_UUID}||"  # no prefix yet
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    assert db._fetchvalue("SELECT is_op FROM player_server_info WHERE player_id = %s", (player_id,)) is True

    db.save_own_prefix(player_id, "Bauteam", "gold")
    client.request(f"!QUIT~{PLAYER_UUID}")
    assert client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444|0") == "success|101"
    assert client.recv_prefix() == f"!prefix~{PLAYER_UUID}|gold|Bauteam"
    assert db._fetchvalue("SELECT is_op FROM player_server_info WHERE player_id = %s", (player_id,)) is False


def test_auth_sends_all_worn_prefixes(db, server, plugin):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.save_own_prefix(player_id, "Clan", "red")
    client = plugin().auth(server["key"])
    assert client.recv_prefix() == f"!prefix~{PLAYER_UUID}|red|Clan"


def test_prefix_change_is_pushed_to_plugin(db, server, plugin):
    client = plugin().auth(server["key"])
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    uuids = db.save_own_prefix(player_id, "Neu", "aqua")
    db.notify_server_event(server["id"], "prefix", uuids=uuids)
    assert client.recv_prefix() == f"!prefix~{PLAYER_UUID}|aqua|Neu"


def test_web_ban_and_unban_are_pushed_to_plugin(db, server, plugin):
    client = plugin().auth(server["key"])
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.notify_server_event(server["id"], "ban", uuid=PLAYER_UUID, name="_Tobias4444", end_ms=0, reason="hacking|x~y")
    assert client.recv() == f"!ban~{PLAYER_UUID}|_Tobias4444|0|hacking/x-y|dauerhaft"
    db.notify_server_event(server["id"], "unban", uuid=PLAYER_UUID, name="_Tobias4444")
    assert client.recv() == f"!unban~{PLAYER_UUID}|_Tobias4444"


def test_ingame_ban_list_sync(db, server, plugin):
    client = plugin().auth(server["key"])
    client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444")
    bans = [{"name": "_Tobias4444", "reason": "Griefing", "source": "Console", "created": 1700000000000, "expires": 0},
            {"name": "NeverPlayedHere", "reason": "x", "source": "Console", "created": 0, "expires": 0}]
    assert client.request("!BANS~" + json.dumps(bans)) == "success|103"
    active = db.list_active_bans(server["id"])
    assert [(b["name"], b["source"], b["reason"], b["end"]) for b in active] == [("_Tobias4444", "ingame", "Griefing", None)]
    assert client.request("!BANS~[]") == "success|103"  # pardoned ingame
    assert db.list_active_bans(server["id"]) == []
    assert client.request("!BANS~{}") == "error|005"


# ------------------------------------------------------------------ website -> plugin, end to end

@pytest.fixture
def web_client(db):
    from web.main import create_app
    return create_app(db, {"TESTING": True, "SECRET_KEY": "test", "SERVER_NAME": "mc.test"}).test_client()


def test_web_ban_reaches_plugin(db, server, plugin, web_client, admin_id):
    client = plugin().auth(server["key"])
    client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444")
    web_client.post("/api/login", json={"username": "tobi", "password": "testPassword"}, base_url="http://mc.test")
    response = web_client.post(f"/api/servers/{server['id']}/ban", json={"name": "_Tobias4444", "days": 2},
                               base_url="http://mc.test")
    assert response.status_code == 200
    command, _, value = client.recv().partition("~")
    uuid, name, end_ms, reason, until = value.split("|")
    assert (command, uuid, name, reason) == ("!ban", PLAYER_UUID, "_Tobias4444", "Gebannt")
    assert until.startswith("bis ") and until.endswith(" Uhr")  # local time for the kick message
    assert int(end_ms) > time.time() * 1000


def test_web_prefix_reaches_plugin(db, server, plugin, web_client):
    client = plugin().auth(server["key"])
    client.request(f"!JOIN~{PLAYER_UUID}|_Tobias4444")
    assert client.recv_prefix() == f"!prefix~{PLAYER_UUID}||"
    player_id = db.get_player_id_from_mojang_uuid_and_server_id(PLAYER_UUID, server["id"])
    db.set_plugin_connected(server["id"], True)
    # log in on the server page like a player (pin comes through the socket)
    web_client.post("/api/login", json={"username": "_Tobias4444", "pin": None}, base_url="http://testdomain.mc.test")
    pin = client.recv().split("~")[2]
    web_client.post("/api/login", json={"username": None, "pin": pin}, base_url="http://testdomain.mc.test")
    web_client.post("/api/prefix/save", json={"text": "Bauteam", "color": "aqua"}, base_url="http://testdomain.mc.test")
    assert client.recv_prefix() == f"!prefix~{PLAYER_UUID}|aqua|Bauteam"
    assert db.get_player_prefix(player_id)["text"] == "Bauteam"
