import hashlib
import json
import os
import secrets
import select
import string
import threading
from contextlib import contextmanager

import argon2
import psycopg2
import psycopg2.extensions
from psycopg2.pool import ThreadedConnectionPool
from colorlogx import get_logger

from . import config
from . import stats as stats_mod
from .minecraft import Minecraft

# Changes on top of queries/schema.sql (which is version 1). Never edit an applied
# migration, add a new one instead.
MIGRATIONS = {
    2: [
        "ALTER TABLE servers ADD COLUMN plugin_connected boolean NOT NULL DEFAULT false",
        "ALTER TABLE servers ADD COLUMN plugin_last_seen timestamptz",
    ],
    3: [
        """CREATE TABLE password_reset(
             admin_id integer PRIMARY KEY REFERENCES server_admins (id) ON DELETE CASCADE,
             token_hash text NOT NULL UNIQUE,
             created_at timestamptz NOT NULL DEFAULT now())""",
        # Sessions started before this time are invalid (set on password change).
        "ALTER TABLE server_admins ADD COLUMN password_changed_at timestamptz NOT NULL DEFAULT now()",
    ],
    4: [
        # Whitelist-only servers do not publish an address.
        "ALTER TABLE servers ADD COLUMN whitelist boolean NOT NULL DEFAULT false",
        "ALTER TABLE servers ALTER COLUMN mc_server_domain DROP NOT NULL",
    ],
}
SCHEMA_VERSION = max(MIGRATIONS, default=1)
# A plugin counts as online if it was seen within this time (it sends a heartbeat every 5 seconds).
PLUGIN_ONLINE_SECONDS = 90
MIGRATION_LOCK_ID = 7_412_001

LOWEST_WEB_ACCESS_LEVEL = 0
DEFAULT_WEB_ACCESS_LEVEL = 3
BAN_REASONS = [
    ["breaking the rules", 30],
    ["hacking", 365],
    ["spamming", 1],
    ["other", 7],
]
LOGIN_PIN_VALID_MINUTES = 5
MAX_LOGIN_ATTEMPTS = 5
EMAIL_VERIFICATION_VALID_HOURS = 24
PASSWORD_RESET_VALID_MINUTES = 60
LOGIN_PIN_CHANNEL = "login_pin"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(BASE_DIR, "queries", "schema.sql")
BLOCKS_FILE = os.path.join(BASE_DIR, "blocks.json")
ITEMS_FILE = os.path.join(BASE_DIR, "itemlist.json")

ph = argon2.PasswordHasher()
logger = get_logger("databaseManager")


def generate_secure_token(length=64):
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))


class DatabaseNotInitializedError(RuntimeError):
    pass


class DatabaseManager:
    """Thread-safe access to the MCConnect database.

    Every method borrows its own connection from a pool, so one instance can
    be shared between Flask request threads, SSE streams and socket threads.
    """

    def __init__(self, db_config=None, minecraft=None, auto_init=True, check_schema=True, timezone=None):
        """
        auto_init: create the schema if the database is empty and apply pending migrations.
        check_schema: raise DatabaseNotInitializedError unless the schema is up to date.
        """
        self.db_config = dict(db_config or config.DB_CONFIG)
        self.timezone = timezone or config.TIMEZONE
        self.minecraft = minecraft or Minecraft()
        self.blocks, self.items = set(), set()
        self.pool = ThreadedConnectionPool(
            config.DB_POOL_MIN, config.DB_POOL_MAX,
            options=f"-c timezone={self.timezone}", **self.db_config,
        )
        logger.info("Established connection pool to the database")
        if not check_schema:
            return

        version = self.get_schema_version()
        if version is None and self._has_tables():
            raise DatabaseNotInitializedError(
                "The database contains tables but no MCConnect schema version. "
                "Use an empty database or `python -m database.manage reset --yes` for a dev database.")
        if version is not None and version > SCHEMA_VERSION:
            raise DatabaseNotInitializedError(
                f"Database schema version {version} is newer than this code ({SCHEMA_VERSION}).")
        if version != SCHEMA_VERSION:
            if not auto_init:
                raise DatabaseNotInitializedError(
                    f"Database schema version is {version}, expected {SCHEMA_VERSION}. "
                    "Run `python -m database.manage init`.")
            self.migrate()
        self._load_lookups()

    def close(self):
        self.pool.closeall()

    @contextmanager
    def _cursor(self):
        """Borrow a connection, yield a cursor, commit on success and roll back on error."""
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.pool.putconn(conn)

    def _fetchone(self, query, data=()):
        with self._cursor() as cur:
            cur.execute(query, data)
            return cur.fetchone()

    def _fetchvalue(self, query, data=()):
        row = self._fetchone(query, data)
        return row[0] if row else None

    def _fetchall(self, query, data=()):
        with self._cursor() as cur:
            cur.execute(query, data)
            return cur.fetchall()

    def _execute(self, query, data=()):
        """Execute a statement and return the number of affected rows."""
        with self._cursor() as cur:
            cur.execute(query, data)
            return cur.rowcount

    ################################ DB INIT FUNCTIONS ###################################

    def _has_tables(self):
        return bool(self._fetchvalue(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = 'public')"
        ))

    def get_schema_version(self):
        exists = self._fetchvalue("SELECT to_regclass('public.schema_version') IS NOT NULL")
        if not exists:
            return None
        return self._fetchvalue("SELECT max(version) FROM schema_version")

    def migrate(self):
        """
        Create the schema in an empty database and/or apply pending migrations.
        Safe to call from several processes at once (advisory lock).
        """
        conn = self.pool.getconn()
        try:
            # Session-level lock in its own transaction: the migration below then starts a
            # fresh transaction and sees the tables another process may have just created
            # (an advisory lock alone does not refresh the catalog snapshot).
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
            conn.commit()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.schema_version') IS NOT NULL")
                    version = None
                    if cur.fetchone()[0]:
                        cur.execute("SELECT max(version) FROM schema_version")
                        version = cur.fetchone()[0]
                    if version is None:
                        self._create_base_schema(cur)
                        version = 1
                    for target in range(version + 1, SCHEMA_VERSION + 1):
                        for statement in MIGRATIONS[target]:
                            cur.execute(statement)
                        cur.execute("UPDATE schema_version SET version = %s", (target,))
                        logger.info(f"Migrated database to schema version {target}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
                conn.commit()
        finally:
            self.pool.putconn(conn)
        self._load_lookups()

    def init_database(self):
        """Create all tables and fill the static lookup data. The database must be empty."""
        self.migrate()

    def _create_base_schema(self, cur):
        with open(SCHEMA_FILE, "r") as f:
            schema = f.read()
        with open(BLOCKS_FILE, "r") as f:
            blocks = [block["name"] for block in json.load(f)]
        with open(ITEMS_FILE, "r") as f:
            items = [item["id"] for item in json.load(f)]

        block_set = set(blocks)
        cur.execute(schema)
        cur.executemany("INSERT INTO block_lookup (name) VALUES (%s) ON CONFLICT DO NOTHING",
                        [(b,) for b in blocks])
        cur.executemany("INSERT INTO item_lookup (name) VALUES (%s) ON CONFLICT DO NOTHING",
                        [(i,) for i in items if i not in block_set])
        cur.executemany("INSERT INTO ban_reasons (reason, ban_duration_in_days) VALUES (%s, %s)",
                        BAN_REASONS)
        cur.execute("INSERT INTO schema_version (version) VALUES (1)")
        logger.info("Created database schema (version 1)")

    def reset_database(self):
        """WARNING: drops every table and recreates an empty schema."""
        logger.warning("Dropping the whole database schema")
        with self._cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        self.init_database()

    def _load_lookups(self):
        self.blocks = {row[0] for row in self._fetchall("SELECT name FROM block_lookup")}
        self.items = {row[0] for row in self._fetchall("SELECT name FROM item_lookup")}

    ################################ ADD FUNCTIONS ####################################

    def add_player(self, mojang_uuid, name=None):
        """
        Make sure the player exists. A given name always overwrites the stored
        one (names can change); without a name it is looked up online once.
        """
        if name:
            self._execute("""INSERT INTO player (uuid, name) VALUES (%s, %s)
                             ON CONFLICT (uuid) DO UPDATE SET name = EXCLUDED.name""",
                          (mojang_uuid, name))
            return
        if self._fetchvalue("SELECT 1 FROM player WHERE uuid = %s", (mojang_uuid,)):
            return
        name = self.minecraft.get_player_name_from_mojang_uuid_online(mojang_uuid) or str(mojang_uuid)
        self._execute("INSERT INTO player (uuid, name) VALUES (%s, %s) ON CONFLICT (uuid) DO NOTHING",
                      (mojang_uuid, name))

    def add_player_server_info(self, server_id, mojang_uuid, web_access_permissions=DEFAULT_WEB_ACCESS_LEVEL):
        """Add the player to the server (if not already there) and return the player_id."""
        return self._fetchvalue("""
            INSERT INTO player_server_info (mojang_uuid, server_id, web_access_permissions)
            VALUES (%s, %s, %s)
            ON CONFLICT (server_id, mojang_uuid) DO UPDATE SET server_id = EXCLUDED.server_id
            RETURNING player_id""", (mojang_uuid, server_id, web_access_permissions))

    def ensure_player_on_server(self, server_id, mojang_uuid, name=None):
        self.add_player(mojang_uuid, name)
        return self.add_player_server_info(server_id, mojang_uuid)

    def add_server(self, owner_id, subdomain, mc_server_domain, server_name, server_key=None,
                   server_description_short="SHORT DESCR", server_description_long="LONG DESCR",
                   discord_url=None, whitelist=False):
        """
        Add a server and return its id. A server key is generated if none is given.
        mc_server_domain may be None for whitelist-only servers.
        """
        server_key = server_key or generate_secure_token(64)
        return self._fetchvalue("""
            INSERT INTO servers (owner_id, subdomain, mc_server_domain, server_name, server_key,
                                 server_description_short, server_description_long, discord_url, whitelist)
            VALUES (%s, lower(%s), %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (owner_id, subdomain, mc_server_domain, server_name, server_key,
             server_description_short, server_description_long, discord_url, whitelist))

    def add_prefix(self, player_id, prefix_text, password=None):
        return self._fetchvalue("""INSERT INTO prefixes (prefix_owner_id, prefix_text, password)
                                   VALUES (%s, %s, %s) RETURNING prefix_id""",
                                (player_id, prefix_text, password))

    def add_server_admin(self, username, password, email, email_verified=False, replace_unverified=False):
        """
        Add a server admin with an argon2 hashed password and return the id.

        replace_unverified: first delete unverified accounts (without servers) that use the
        same username or email, so a typo during signup can be fixed by signing up again.
        Verified accounts are never replaced (the insert then raises UniqueViolation).
        """
        with self._cursor() as cur:
            if replace_unverified:
                cur.execute("""DELETE FROM server_admins sa
                               WHERE NOT sa.email_verified
                                 AND (lower(sa.username) = lower(%s) OR lower(sa.email) = lower(%s))
                                 AND NOT EXISTS (SELECT 1 FROM servers s WHERE s.owner_id = sa.id)""",
                            (username, email))
            cur.execute("""INSERT INTO server_admins (username, password, email, email_verified)
                           VALUES (%s, %s, %s, %s) RETURNING id""",
                        (username, ph.hash(password), email, email_verified))
            return cur.fetchone()[0]

    def create_email_verification(self, admin_id):
        """Create (or replace) the email verification token for an admin and return it."""
        token = generate_secure_token(48)
        self._execute("""INSERT INTO email_verification (admin_id, token) VALUES (%s, %s)
                         ON CONFLICT (admin_id) DO UPDATE SET token = EXCLUDED.token, created_at = now()""",
                      (admin_id, token))
        return token

    def renew_email_verification(self, email, min_interval_seconds=60):
        """
        New verification token for the unverified admin with this email.
        Returns (username, token), or None if there is no such admin or the last
        mail was sent less than min_interval_seconds ago.
        """
        with self._cursor() as cur:
            cur.execute("""SELECT sa.id, sa.username FROM server_admins sa
                           LEFT JOIN email_verification ev ON ev.admin_id = sa.id
                           WHERE lower(sa.email) = lower(%s) AND NOT sa.email_verified
                             AND (ev.created_at IS NULL OR ev.created_at < now() - make_interval(secs => %s))
                           FOR UPDATE OF sa""", (email, min_interval_seconds))
            row = cur.fetchone()
            if row is None:
                return None
            admin_id, username = row
            token = generate_secure_token(48)
            cur.execute("""INSERT INTO email_verification (admin_id, token) VALUES (%s, %s)
                           ON CONFLICT (admin_id) DO UPDATE SET token = EXCLUDED.token, created_at = now()""",
                        (admin_id, token))
        return username, token

    ###----------------------------- Password reset ------------------------------------###

    @staticmethod
    def _hash_token(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def create_password_reset(self, email, min_interval_seconds=60):
        """
        Reset token for the verified admin with this email. Only a hash is stored.
        Returns (username, token), or None if there is no such admin or the last
        reset mail was sent less than min_interval_seconds ago.
        """
        with self._cursor() as cur:
            cur.execute("""SELECT sa.id, sa.username FROM server_admins sa
                           LEFT JOIN password_reset pr ON pr.admin_id = sa.id
                           WHERE lower(sa.email) = lower(%s) AND sa.email_verified
                             AND (pr.created_at IS NULL OR pr.created_at < now() - make_interval(secs => %s))
                           FOR UPDATE OF sa""", (email, min_interval_seconds))
            row = cur.fetchone()
            if row is None:
                return None
            admin_id, username = row
            token = generate_secure_token(48)
            cur.execute("""INSERT INTO password_reset (admin_id, token_hash) VALUES (%s, %s)
                           ON CONFLICT (admin_id) DO UPDATE SET token_hash = EXCLUDED.token_hash, created_at = now()""",
                        (admin_id, self._hash_token(token)))
        return username, token

    def get_password_reset_username(self, token):
        """Username for a valid (unexpired) reset token, or None."""
        return self._fetchvalue("""SELECT sa.username FROM password_reset pr
                                   JOIN server_admins sa ON sa.id = pr.admin_id
                                   WHERE pr.token_hash = %s
                                     AND pr.created_at > now() - make_interval(mins => %s)""",
                                (self._hash_token(token), PASSWORD_RESET_VALID_MINUTES))

    def reset_password(self, token, new_password):
        """Set a new password with a reset token (single use). Returns the username or None."""
        with self._cursor() as cur:
            cur.execute("""DELETE FROM password_reset
                           WHERE token_hash = %s AND created_at > now() - make_interval(mins => %s)
                           RETURNING admin_id""", (self._hash_token(token), PASSWORD_RESET_VALID_MINUTES))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute("""UPDATE server_admins SET password = %s, password_changed_at = now()
                           WHERE id = %s RETURNING username""", (ph.hash(new_password), row[0]))
            username = cur.fetchone()[0]
        logger.info(f'Password of admin "{username}" was reset')
        return username

    def get_admin_password_changed_at(self, admin_id):
        """Unix time of the last password change, or None if the admin does not exist."""
        value = self._fetchvalue("SELECT extract(epoch FROM password_changed_at) FROM server_admins WHERE id = %s",
                                 (admin_id,))
        return float(value) if value is not None else None

    def add_ban_reason(self, reason, duration_in_days):
        return self._fetchvalue("""INSERT INTO ban_reasons (reason, ban_duration_in_days)
                                   VALUES (%s, %s) RETURNING id""", (reason, duration_in_days))

    def add_banned_player(self, banned_player_id, moderator_id, ban_reason_id, ban_end, comment=None):
        return self._fetchvalue("""
            INSERT INTO banned_players (banned_player_id, moderator_id, ban_reason_id, ban_end, comment)
            VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (banned_player_id, moderator_id, ban_reason_id, ban_end, comment))

    def add_login_entry_from_player_id(self, player_id, pin):
        """
        Store a login pin for the player and notify the socket server
        (channel login_pin) so it can deliver the pin in-game.
        """
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO login (player_id, pin) VALUES (%s, %s)
                ON CONFLICT (player_id) DO UPDATE SET pin = EXCLUDED.pin, attempts = 0, created_at = now()""",
                (player_id, pin))
            cur.execute("SELECT server_id, mojang_uuid FROM player_server_info WHERE player_id = %s",
                        (player_id,))
            server_id, mojang_uuid = cur.fetchone()
            payload = json.dumps({"server_id": server_id, "mojang_uuid": str(mojang_uuid), "pin": pin})
            cur.execute("SELECT pg_notify(%s, %s)", (LOGIN_PIN_CHANNEL, payload))
        logger.info(f'Added login entry for player: "{player_id}"')
        return True

    ###----------------------------- Update Functions ------------------------------------###

    def update_player_stats(self, player_id, stats):
        """Store a stats file (json string or dict) for the player. Returns the number of stored values."""
        entries = stats_mod.split_stats(stats, self.blocks, self.items)
        data = [(player_id, category, name, value) for name, category, value in entries]
        with self._cursor() as cur:
            cur.executemany("""
                INSERT INTO actions (player_id, category, object, value)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (player_id, category, object)
                DO UPDATE SET value = EXCLUDED.value
                WHERE actions.value IS DISTINCT FROM EXCLUDED.value""", data)
        logger.info(f'Updated stats of player "{player_id}" ({len(data)} values)')
        return len(data)

    def register_player_join(self, server_id, mojang_uuid, name=None):
        """Mark the player online on the server, creating it if needed. Returns the player_id."""
        player_id = self.ensure_player_on_server(server_id, mojang_uuid, name)
        self._execute("""UPDATE player_server_info
                         SET online = true, first_seen = COALESCE(first_seen, now()), last_seen = now()
                         WHERE player_id = %s""", (player_id,))
        return player_id

    def register_player_quit(self, server_id, mojang_uuid):
        """Mark the player offline. Returns False if the player is unknown on this server."""
        return self._execute("""UPDATE player_server_info SET online = false, last_seen = now()
                                WHERE mojang_uuid = %s AND server_id = %s""",
                             (mojang_uuid, server_id)) > 0

    def update_player_status_from_mojang_uuid_and_server_id(self, mojang_uuid, server_id, status):
        if status == "online":
            return self.register_player_join(server_id, mojang_uuid) is not None
        return self.register_player_quit(server_id, mojang_uuid)

    def set_all_players_offline(self, server_id):
        return self._execute("""UPDATE player_server_info SET online = false, last_seen = now()
                                WHERE server_id = %s AND online""", (server_id,))

    def update_online_status_by_player_id(self, player_id, online_status):
        self._execute("UPDATE player_server_info SET online = %s WHERE player_id = %s",
                      (online_status, player_id))

    def update_player_prefix_by_player_id(self, player_id, prefix_id):
        self._execute("UPDATE player_server_info SET prefix_id = %s WHERE player_id = %s",
                      (prefix_id, player_id))

    def update_prefix_text_by_prefix_id(self, prefix_id, prefix_text):
        self._execute("UPDATE prefixes SET prefix_text = %s WHERE prefix_id = %s", (prefix_text, prefix_id))

    def update_prefix_password_by_prefix_id(self, prefix_id, prefix_password):
        self._execute("UPDATE prefixes SET password = %s WHERE prefix_id = %s", (prefix_password, prefix_id))

    ################################ DELETE FUNCTIONS #################################

    def delete_login_entry(self, player_id):
        self._execute("DELETE FROM login WHERE player_id = %s", (player_id,))

    def delete_banned_player(self, banned_player_id):
        self._execute("DELETE FROM banned_players WHERE banned_player_id = %s", (banned_player_id,))

    ################################ GET FUNCTIONS ####################################

    ###----------------------------- Players ------------------------------------###
    def get_player_id_from_mojang_uuid_and_server_id(self, mojang_uuid, server_id):
        return self._fetchvalue("SELECT player_id FROM player_server_info WHERE mojang_uuid = %s AND server_id = %s",
                                (mojang_uuid, server_id))

    def get_player_id_from_mojang_uuid_and_subdomain(self, mojang_uuid, subdomain):
        return self._fetchvalue("""SELECT psi.player_id
                                   FROM player_server_info psi
                                   JOIN servers s ON psi.server_id = s.id
                                   WHERE psi.mojang_uuid = %s AND s.subdomain = lower(%s)""",
                                (mojang_uuid, subdomain))

    def get_player_id_from_player_name_and_server_id(self, player_name, server_id):
        """Case-insensitive name lookup, scoped to one server (names are not globally unique over time)."""
        return self._fetchvalue("""SELECT psi.player_id
                                   FROM player_server_info psi
                                   JOIN player p ON p.uuid = psi.mojang_uuid
                                   WHERE lower(p.name) = lower(%s) AND psi.server_id = %s""",
                                (player_name, server_id))

    def get_mojang_uuid_from_player_id(self, player_id):
        return self._fetchvalue("SELECT mojang_uuid FROM player_server_info WHERE player_id = %s", (player_id,))

    def get_server_id_from_player_id(self, player_id):
        return self._fetchvalue("SELECT server_id FROM player_server_info WHERE player_id = %s", (player_id,))

    def get_mojang_uuid_from_player_name(self, player_name):
        return self._fetchvalue("SELECT uuid FROM player WHERE lower(name) = lower(%s)", (player_name,))

    def get_player_name_from_mojang_uuid(self, mojang_uuid):
        return self._fetchvalue("SELECT name FROM player WHERE uuid = %s", (mojang_uuid,))

    def get_player_name_from_player_id(self, player_id):
        return self._fetchvalue("""SELECT p.name FROM player p
                                   JOIN player_server_info psi ON p.uuid = psi.mojang_uuid
                                   WHERE psi.player_id = %s""", (player_id,))

    def get_player_info_by_player_id(self, player_id):
        """All player_server_info columns plus name, as dict (or None)."""
        with self._cursor() as cur:
            cur.execute("""SELECT psi.*, p.name FROM player_server_info psi
                           JOIN player p ON p.uuid = psi.mojang_uuid
                           WHERE psi.player_id = %s""", (player_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip([d[0] for d in cur.description], row))

    def get_all_player_ids_from_subdomain(self, subdomain):
        return [row[0] for row in self._fetchall("""
            SELECT psi.player_id FROM player_server_info psi
            JOIN servers s ON psi.server_id = s.id WHERE s.subdomain = lower(%s)""", (subdomain,))]

    def get_all_mojang_uuids_from_subdomain(self, subdomain):
        return [row[0] for row in self._fetchall("""
            SELECT psi.mojang_uuid FROM player_server_info psi
            JOIN player p ON p.uuid = psi.mojang_uuid
            JOIN servers s ON psi.server_id = s.id
            WHERE s.subdomain = lower(%s) ORDER BY lower(p.name)""", (subdomain,))]

    def get_players_overview_from_subdomain(self, subdomain):
        """[{"name", "uuid", "online"}, ...] for all players of a server, sorted by name."""
        rows = self._fetchall("""
            SELECT p.name, psi.mojang_uuid, psi.online FROM player_server_info psi
            JOIN player p ON p.uuid = psi.mojang_uuid
            JOIN servers s ON psi.server_id = s.id
            WHERE s.subdomain = lower(%s) ORDER BY lower(p.name)""", (subdomain,))
        return [{"name": name, "uuid": str(uuid), "online": online} for name, uuid, online in rows]

    def get_members_from_prefix_id(self, prefix_id):
        return [row[0] for row in self._fetchall("SELECT player_id FROM player_server_info WHERE prefix_id = %s",
                                                 (prefix_id,))]

    ###----------------------------- Player Statuses ------------------------------------###

    def get_online_player_count_from_subdomain(self, subdomain):
        return self._fetchvalue("""SELECT count(*) FROM player_server_info psi
                                   JOIN servers s ON psi.server_id = s.id
                                   WHERE s.subdomain = lower(%s) AND psi.online""", (subdomain,))

    def get_online_player_count_total(self):
        return self._fetchvalue("SELECT count(*) FROM player_server_info WHERE online")

    def get_online_status_by_player_uuid_and_subdomain(self, uuid, subdomain):
        return self._fetchvalue("""SELECT psi.online FROM player_server_info psi
                                   JOIN servers s ON psi.server_id = s.id
                                   WHERE psi.mojang_uuid = %s AND s.subdomain = lower(%s)""", (uuid, subdomain))

    def get_online_status_by_player_id(self, player_id):
        return self._fetchvalue("SELECT online FROM player_server_info WHERE player_id = %s", (player_id,))

    def get_first_seen_by_player_id(self, player_id):
        return self._fetchvalue("SELECT first_seen FROM player_server_info WHERE player_id = %s", (player_id,))

    def get_last_seen_by_player_id(self, player_id):
        return self._fetchvalue("SELECT last_seen FROM player_server_info WHERE player_id = %s", (player_id,))

    def get_web_access_permission_from_player_id(self, player_id):
        return self._fetchvalue("SELECT web_access_permissions FROM player_server_info WHERE player_id = %s",
                                (player_id,))

    ###----------------------------- Prefixes & Bans ------------------------------------###

    def get_prefix_id_by_player_id(self, player_id):
        return self._fetchvalue("SELECT prefix_id FROM player_server_info WHERE player_id = %s", (player_id,))

    def get_prefix_text_by_prefix_id(self, prefix_id):
        return self._fetchvalue("SELECT prefix_text FROM prefixes WHERE prefix_id = %s", (prefix_id,))

    def get_ban_reason_from_player_id(self, player_id):
        """Reason of the currently active ban, or None."""
        return self._fetchvalue("""SELECT br.reason FROM banned_players bp
                                   JOIN ban_reasons br ON br.id = bp.ban_reason_id
                                   WHERE bp.banned_player_id = %s AND bp.ban_end > now()
                                   ORDER BY bp.ban_end DESC LIMIT 1""", (player_id,))

    def get_ban_start_and_ban_end_by_player_id(self, player_id):
        """(ban_start, ban_end) of the currently active ban, or None."""
        return self._fetchone("""SELECT ban_start, ban_end FROM banned_players
                                 WHERE banned_player_id = %s AND ban_end > now()
                                 ORDER BY ban_end DESC LIMIT 1""", (player_id,))

    ###----------------------------- Servers ------------------------------------###

    def get_server_id_from_subdomain(self, subdomain):
        return self._fetchvalue("SELECT id FROM servers WHERE subdomain = lower(%s)", (subdomain,))

    def get_server_id_by_auth_key(self, auth_key):
        return self._fetchvalue("SELECT id FROM servers WHERE server_key = %s", (auth_key,))

    def get_server_information_dict(self, subdomain):
        """All columns of the server except the secret key, as dict (or None)."""
        if not subdomain:
            return None
        with self._cursor() as cur:
            cur.execute("SELECT * FROM servers WHERE subdomain = lower(%s)", (subdomain,))
            row = cur.fetchone()
            if row is None:
                return None
            info = dict(zip([d[0] for d in cur.description], row))
        info.pop("server_key", None)
        return info

    def set_plugin_connected(self, server_id, connected):
        self._execute("UPDATE servers SET plugin_connected = %s, plugin_last_seen = now() WHERE id = %s",
                      (connected, server_id))

    def touch_plugin(self, server_id):
        """Record that the plugin of the server is still alive."""
        self._execute("UPDATE servers SET plugin_last_seen = now() WHERE id = %s", (server_id,))

    def is_plugin_online(self, server_id):
        return bool(self._fetchvalue(
            """SELECT plugin_connected AND plugin_last_seen > now() - make_interval(secs => %s)
               FROM servers WHERE id = %s""", (PLUGIN_ONLINE_SECONDS, server_id)))

    def get_servers_by_owner(self, owner_id):
        """All servers of an admin (including the key) as dicts, plus plugin_online and player_count."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT s.*,
                       s.plugin_connected AND s.plugin_last_seen > now() - make_interval(secs => %s) AS plugin_online,
                       (SELECT count(*) FROM player_server_info psi WHERE psi.server_id = s.id) AS player_count
                FROM servers s WHERE s.owner_id = %s ORDER BY s.created_at""", (PLUGIN_ONLINE_SECONDS, owner_id))
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def update_server(self, server_id, owner_id, **fields):
        """Update editable server fields; only succeeds for the owner. Returns True if updated."""
        allowed = {"server_name", "mc_server_domain", "discord_url", "whitelist",
                   "server_description_short", "server_description_long"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return False
        assignments = ", ".join(f"{column} = %s" for column in fields)
        return self._execute(f"UPDATE servers SET {assignments} WHERE id = %s AND owner_id = %s",
                             (*fields.values(), server_id, owner_id)) > 0

    def regenerate_server_key(self, server_id, owner_id):
        """Give the server a new key (the old plugin config stops working). Returns the key or None."""
        return self._fetchvalue("UPDATE servers SET server_key = %s WHERE id = %s AND owner_id = %s RETURNING server_key",
                                (generate_secure_token(64), server_id, owner_id))

    def delete_server(self, server_id, owner_id):
        """Delete the server with all its player data. Returns True if deleted."""
        return self._execute("DELETE FROM servers WHERE id = %s AND owner_id = %s", (server_id, owner_id)) > 0

    ###----------------------------- Minecraft stats ------------------------------------###

    def get_value_from_unique_object_from_action_table_with_player_id(self, object, player_id,
                                                                     category=stats_mod.CUSTOM):
        """Value of one stat object (by default from the custom category, e.g. minecraft:deaths)."""
        return self._fetchvalue("SELECT value FROM actions WHERE object = %s AND player_id = %s AND category = %s",
                                (object, player_id, category))

    def _get_grouped_stats(self, player_id, categories):
        """
        [[{"object", "value"}, ...], ...] with one inner list per category, ordered by
        category descending. The web templates map columns by position, so categories
        without values are included as empty lists.
        """
        rows = self._fetchall("""
            SELECT category, jsonb_agg(jsonb_build_object('object', object, 'value', value))
            FROM actions
            WHERE category = ANY(%s) AND player_id = %s
            GROUP BY category""", (list(categories), player_id))
        by_category = dict(rows)
        return [by_category.get(category, []) for category in sorted(categories, reverse=True)]

    def get_all_armor_stats(self, player_id):
        return self._get_grouped_stats(player_id, stats_mod.ARMOR_CATEGORIES)

    def get_all_tools_stats(self, player_id):
        return self._get_grouped_stats(player_id, stats_mod.TOOL_CATEGORIES)

    def get_all_items_stats(self, player_id):
        return self._get_grouped_stats(player_id, stats_mod.ITEM_CATEGORIES)

    def get_all_blocks_stats(self, player_id):
        return self._get_grouped_stats(player_id, stats_mod.BLOCK_CATEGORIES)

    def get_all_mobs_stats(self, player_id):
        return self._get_grouped_stats(player_id, stats_mod.MOB_CATEGORIES)

    def get_all_custom_stats(self, player_id):
        return self._get_grouped_stats(player_id, stats_mod.CUSTOM_CATEGORIES)

    ################################# Verify Functions #######################################

    def verify_player_login(self, player_id, pin):
        """
        Check a login pin. Returns [True] on success, otherwise [False, reason] with reason
        "no entry found in the database", "timeout reached", "too many attempts" or "wrong pin provided".
        A successful, expired or exhausted login entry is deleted.
        """
        with self._cursor() as cur:
            cur.execute("""SELECT pin, attempts, created_at > now() - make_interval(mins => %s)
                           FROM login WHERE player_id = %s FOR UPDATE""",
                        (LOGIN_PIN_VALID_MINUTES, player_id))
            row = cur.fetchone()
            if row is None:
                return [False, "no entry found in the database"]
            stored_pin, attempts, still_valid = row
            if not still_valid:
                cur.execute("DELETE FROM login WHERE player_id = %s", (player_id,))
                return [False, "timeout reached"]
            if pin == stored_pin:
                cur.execute("DELETE FROM login WHERE player_id = %s", (player_id,))
                return [True]
            if attempts + 1 >= MAX_LOGIN_ATTEMPTS:
                cur.execute("DELETE FROM login WHERE player_id = %s", (player_id,))
                return [False, "too many attempts"]
            cur.execute("UPDATE login SET attempts = attempts + 1 WHERE player_id = %s", (player_id,))
            return [False, "wrong pin provided"]

    def get_admin_id_by_username(self, username):
        return self._fetchvalue("SELECT id FROM server_admins WHERE username = %s", (username,))

    def authenticate_admin(self, login, password):
        """
        Check the credentials of an admin with verified email. login is the username or
        the email address (case-insensitive; usernames cannot contain "@").
        Returns (admin_id, username) or None.
        """
        column = "lower(email) = lower(%s)" if "@" in login else "username = %s"
        rows = self._fetchall(f"SELECT id, username, password FROM server_admins WHERE {column} AND email_verified",
                              (login,))
        for admin_id, username, stored_hash in rows:
            try:
                if ph.verify(stored_hash, password):
                    return admin_id, username
            except argon2.exceptions.VerificationError:
                continue
            except argon2.exceptions.InvalidHashError:
                logger.error(f'Stored password of admin "{username}" is not a valid argon2 hash')
        return None

    def verify_admin_login(self, login, password):
        """True if the credentials (username or email) are correct and the email address is verified."""
        return self.authenticate_admin(login, password) is not None

    def verify_signupcode(self, username, token):
        """Mark the admin's email as verified if the token matches and has not expired."""
        with self._cursor() as cur:
            cur.execute("""
                UPDATE server_admins sa SET email_verified = true
                FROM email_verification ev
                WHERE ev.admin_id = sa.id AND sa.username = %s AND ev.token = %s
                  AND ev.created_at > now() - make_interval(hours => %s)
                RETURNING sa.id""", (username, token, EMAIL_VERIFICATION_VALID_HOURS))
            row = cur.fetchone()
            if row is None:
                return False
            cur.execute("DELETE FROM email_verification WHERE admin_id = %s", (row[0],))
        logger.info(f'Verified email of admin "{username}"')
        return True

    ################################# Notifications #######################################

    def listen_for_login_pins(self, callback, stop_event=None, poll_timeout=1.0):
        """
        Block and call callback(server_id, mojang_uuid, pin) for every new login pin
        until stop_event is set. Uses a dedicated connection (LISTEN needs autocommit).
        """
        stop_event = stop_event or threading.Event()
        conn = psycopg2.connect(**self.db_config)
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        try:
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {LOGIN_PIN_CHANNEL};")
            logger.info("Listening for login pins")
            while not stop_event.is_set():
                if select.select([conn], [], [], poll_timeout) == ([], [], []):
                    continue
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    try:
                        payload = json.loads(notify.payload)
                        callback(payload["server_id"], payload["mojang_uuid"], payload["pin"])
                    except Exception:
                        logger.exception(f"Failed to handle login pin notification: {notify.payload}")
        finally:
            conn.close()

    ################################# helper functions #######################################

    def format_time(self, seconds):
        return stats_mod.format_time(seconds)


if __name__ == "__main__":
    db = DatabaseManager()
    print(f"Connected, schema version {db.get_schema_version()}")
