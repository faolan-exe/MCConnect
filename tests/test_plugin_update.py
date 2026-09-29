"""Automatic plugin updates (database/plugin_jar.py, !update after the "update" feature)."""
import hashlib
import zipfile

import pytest

from database import config, plugin_jar
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import app, client  # noqa: F401 (fixtures)


@pytest.fixture
def jar(tmp_path, monkeypatch):
    path = tmp_path / "MCDataLink-9.1.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.yml", "name: MCDataLink\nversion: '9.1'\nmain: x\n")
    monkeypatch.setattr(config, "PLUGIN_JAR", str(path))
    return path


def test_jar_version_and_checksum(jar):
    info = plugin_jar.info()
    assert info["version"] == "9.1" and info["sha256"] == hashlib.sha256(jar.read_bytes()).hexdigest()
    assert plugin_jar.update_message().endswith(f"/download/MCDataLink.jar|{info['sha256']}")
    assert plugin_jar.version_key("3.17") > plugin_jar.version_key("3.9")


def test_update_is_offered_to_plugins_that_can_install_it(jar, server, plugin):
    connection = plugin().auth(server["key"])
    connection.send("!FEATURES~click,glyphs,update")
    assert connection.recv() == "success|106"
    message = connection.recv()
    assert message == plugin_jar.update_message() and message.startswith("!update~9.1|http")


def test_no_update_without_the_feature(jar, server, plugin):
    connection = plugin().auth(server["key"])
    connection.send("!FEATURES~click")
    assert connection.recv() == "success|106"
    assert connection.recv(skip_heartbeats=False) == "!heartbeat"  # nothing in between: no !update


def test_download_route(jar, client):
    response = client.get("/download/MCDataLink.jar", base_url="http://mc.test")
    assert response.status_code == 200 and response.data == jar.read_bytes()
