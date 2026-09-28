"""The plugin connection is TLS only (plugin 3.12), on the one socket port."""
import os
import socket
import time

from mc_socket import devcert
from mc_socket.main import encode_msg, recv_msg
from tests.test_socket import plugin, socket_server, tls_connect  # noqa: F401 (fixtures)


def test_plugin_connects_with_tls(db, server, plugin):
    connection = plugin().auth(server["key"])
    assert connection.request("!JOIN~4ebe5f6f-c231-4315-9d60-097c48cc6d30|_Tobias4444") == "success|101"
    assert db.is_plugin_online(server["id"])


def test_plain_connection_is_refused(db, server, socket_server):
    conn = socket.create_connection(("127.0.0.1", socket_server.port), timeout=5)
    try:
        conn.sendall(encode_msg(f"!AUTH~{server['key']}"))
        assert recv_msg(conn) == "error|006"  # old plugins log the error code
        try:
            assert conn.recv(100) == b""  # and the connection is closed
        except ConnectionResetError:
            pass  # closed with the unread request still in the buffer
        assert not db.is_plugin_online(server["id"])
    finally:
        conn.close()


def test_silent_client_does_not_block_others(db, server, socket_server, certificate):
    silent = socket.create_connection(("127.0.0.1", socket_server.port), timeout=5)
    try:
        conn = tls_connect(socket_server.port, certificate)
        conn.sendall(encode_msg(f"!AUTH~{server['key']}"))
        assert recv_msg(conn) == "success|100"
        conn.close()
    finally:
        silent.close()


def test_renewed_certificate_is_reloaded(socket_server, certificate):
    first = socket_server.tls_context()
    assert socket_server.tls_context() is first
    os.utime(certificate[0], (time.time() + 5, time.time() + 5))
    assert socket_server.tls_context() is not first


def test_dev_certificate_fingerprint(tmp_path):
    cert, key = devcert.ensure_dev_certificate(str(tmp_path))
    assert devcert.ensure_dev_certificate(str(tmp_path)) == (cert, key)  # created once
    assert len(devcert.fingerprint(cert).split(":")) == 32
