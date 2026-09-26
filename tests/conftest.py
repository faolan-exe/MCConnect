"""Shared fixtures. Tests run against a real postgres (docker/db) in a separate
database (default: mcconnect_test), which is created and reset automatically."""
import os
import socket
import sys
import time

import psycopg2
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from database import config
from database.databaseManagerV2 import DatabaseManager

TEST_DB_CONFIG = dict(config.DB_CONFIG, dbname=os.environ.get("MCC_TEST_DB_NAME", "mcconnect_test"))

DATA_TABLES = ("actions", "login", "banned_players", "prefixes", "player_server_info",
               "servers", "email_verification", "server_admins", "player")

PLAYER_UUID = "4ebe5f6f-c231-4315-9d60-097c48cc6d30"
OTHER_UUID = "069a79f4-44e9-4726-a5be-fca90e38aaf5"


class FakeMinecraft:
    """Offline replacement for the playerdb.co lookup."""
    names = {PLAYER_UUID: "_Tobias4444", OTHER_UUID: "Notch"}

    def get_player_name_from_mojang_uuid_online(self, uuid):
        return self.names.get(str(uuid))


def _ensure_test_database():
    admin_config = dict(TEST_DB_CONFIG, dbname="postgres")
    try:
        conn = psycopg2.connect(**admin_config, connect_timeout=3)
    except psycopg2.OperationalError as e:
        pytest.exit(f"Postgres not reachable ({e}). Start it with: docker compose -f docker/db/docker-compose.yml "
                    "-p mcconnect up -d", returncode=2)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_CONFIG["dbname"],))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{TEST_DB_CONFIG["dbname"]}"')
    conn.close()


@pytest.fixture(scope="session")
def db_manager():
    _ensure_test_database()
    manager = DatabaseManager(db_config=TEST_DB_CONFIG, minecraft=FakeMinecraft(), check_schema=False)
    manager.reset_database()
    yield manager
    manager.close()


@pytest.fixture
def db(db_manager):
    """The shared manager with all data tables emptied before the test."""
    with db_manager._cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE")
    return db_manager


@pytest.fixture
def admin_id(db):
    return db.add_server_admin("tobi", "testPassword", "tobi@example.com", email_verified=True)


@pytest.fixture
def server(db, admin_id):
    """A registered minecraft server: dict with id, key and subdomain."""
    server_id = db.add_server(admin_id, "testDomain", "mc.example.com", "Test Server",
                              server_key="k" * 64, server_description_short="short",
                              server_description_long="long")
    return {"id": server_id, "key": "k" * 64, "subdomain": "testdomain"}


@pytest.fixture
def other_server(db, admin_id):
    server_id = db.add_server(admin_id, "other", "other.example.com", "Other Server", server_key="o" * 64)
    return {"id": server_id, "key": "o" * 64, "subdomain": "other"}


def wait_for(condition, timeout=5.0, interval=0.02):
    """Poll condition() until it is truthy; return its value or fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
