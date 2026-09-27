"""The optional TLS port of the socket server (plugin 3.8 with "tls: true")."""
import socket
import ssl
import subprocess
import time

import pytest

from mc_socket.main import SocketServer, encode_msg, recv_msg


@pytest.fixture
def certificate(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return str(cert), str(key)


@pytest.fixture
def tls_server(db, certificate):
    srv = SocketServer(db, host="127.0.0.1", port=0, heartbeat_interval=0.5, heartbeat_timeout=2, poll_interval=0.05,
                       tls_cert=certificate[0], tls_key=certificate[1], tls_port=0)
    srv.start()
    yield srv
    srv.stop()


def tls_connect(port):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # self-signed test certificate
    return context.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5), server_hostname="localhost")


def next_message(conn):
    while True:
        msg = recv_msg(conn)
        if msg != "!heartbeat" and not msg.startswith("!metrics~"):
            return msg


def test_plugin_connects_with_tls(db, server, tls_server):
    conn = tls_connect(tls_server.tls_port)
    try:
        conn.sendall(encode_msg(f"!AUTH~{server['key']}"))
        assert next_message(conn) == "success|100"
        assert next_message(conn) == "!sendAllPlayerStats"
        conn.sendall(encode_msg("!JOIN~4ebe5f6f-c231-4315-9d60-097c48cc6d30|_Tobias4444"))
        assert next_message(conn) == "success|101"
        assert db.is_plugin_online(server["id"])
    finally:
        conn.close()


def test_plain_port_still_works_next_to_tls(db, server, tls_server):
    conn = socket.create_connection(("127.0.0.1", tls_server.port), timeout=5)
    try:
        conn.sendall(encode_msg(f"!AUTH~{server['key']}"))
        assert next_message(conn) == "success|100"
    finally:
        conn.close()


def test_plain_client_on_the_tls_port_is_dropped(db, server, tls_server):
    conn = socket.create_connection(("127.0.0.1", tls_server.tls_port), timeout=5)
    try:
        conn.sendall(encode_msg(f"!AUTH~{server['key']}"))
        time.sleep(0.3)
        try:
            conn.recv(100)  # closed or a TLS alert, never an answer
        except ConnectionResetError:
            pass
        assert not db.is_plugin_online(server["id"])
    finally:
        conn.close()


def test_renewed_certificate_is_reloaded(db, tls_server, certificate):
    first = tls_server.tls_context()
    assert tls_server.tls_context() is first
    import os
    os.utime(certificate[0], (time.time() + 5, time.time() + 5))
    assert tls_server.tls_context() is not first
