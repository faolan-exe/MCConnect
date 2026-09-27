import hashlib
from datetime import datetime, timedelta, timezone
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
from . import achievements as achievements_mod
from . import metrics as metrics_mod
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
    5: [
        # Images for the server start page. The files live in the upload folder.
        """CREATE TABLE server_images(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             kind text NOT NULL CHECK (kind IN ('banner', 'gallery')),
             filename text NOT NULL UNIQUE,
             created_at timestamptz NOT NULL DEFAULT now())""",
        "CREATE INDEX server_images_server_idx ON server_images (server_id, kind)",
    ],
    6: [
        # prefixes belong to a server, have a color and a hashed password (the old column was plain text)
        "ALTER TABLE prefixes ADD COLUMN server_id integer REFERENCES servers (id) ON DELETE CASCADE",
        """UPDATE prefixes p SET server_id = psi.server_id
           FROM player_server_info psi WHERE psi.player_id = p.prefix_owner_id""",
        "ALTER TABLE prefixes ALTER COLUMN server_id SET NOT NULL",
        "ALTER TABLE prefixes ADD COLUMN color text NOT NULL DEFAULT 'gray'",
        "ALTER TABLE prefixes RENAME COLUMN password TO password_hash",
        "UPDATE prefixes SET password_hash = NULL",
        "CREATE UNIQUE INDEX prefixes_server_text_idx ON prefixes (server_id, lower(prefix_text))",
        "CREATE UNIQUE INDEX prefixes_owner_idx ON prefixes (prefix_owner_id)",
        # moderation
        "ALTER TABLE player_server_info ADD COLUMN is_op boolean NOT NULL DEFAULT false",
        "ALTER TABLE servers ADD COLUMN auto_mod_ops boolean NOT NULL DEFAULT false",
        # bans from the website (source web) and from the game (source ingame, synced by the plugin)
        "ALTER TABLE banned_players ALTER COLUMN ban_reason_id DROP NOT NULL",
        "ALTER TABLE banned_players ALTER COLUMN ban_end DROP NOT NULL",  # NULL = permanent
        "ALTER TABLE banned_players ADD COLUMN reason_text text",
        "ALTER TABLE banned_players ADD COLUMN banned_by text",
        "ALTER TABLE banned_players ADD COLUMN source text NOT NULL DEFAULT 'web' CHECK (source IN ('web', 'ingame'))",
    ],
    7: [
        # Daily value of every metric (database/metrics.py) per player, for rankings over a time range
        # and the history chart. actions only holds the current values.
        """CREATE TABLE stat_snapshots(
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             metric text NOT NULL,
             day date NOT NULL,
             value bigint NOT NULL,
             PRIMARY KEY (player_id, metric, day))""",
    ],
    8: [
        # One row per stay on the server (join to quit), for the online history and peak times.
        """CREATE TABLE player_sessions(
             id bigserial PRIMARY KEY,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             started_at timestamptz NOT NULL DEFAULT now(),
             ended_at timestamptz,
             CHECK (ended_at IS NULL OR ended_at >= started_at))""",
        "CREATE INDEX player_sessions_player_idx ON player_sessions (player_id, started_at)",
        "CREATE INDEX player_sessions_started_idx ON player_sessions (started_at)",
    ],
    9: [
        # earned tiers of the achievements in database/achievements.py
        """CREATE TABLE player_achievements(
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             achievement text NOT NULL,
             tier smallint NOT NULL,
             earned_at timestamptz NOT NULL DEFAULT now(),
             PRIMARY KEY (player_id, achievement, tier))""",
        "CREATE INDEX player_achievements_earned_idx ON player_achievements (earned_at)",
        # competitions: most gain of a metric between two days (inclusive)
        """CREATE TABLE competitions(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             title text NOT NULL,
             metric text NOT NULL,
             starts_on date NOT NULL,
             ends_on date NOT NULL CHECK (ends_on >= starts_on),
             created_by text,
             created_at timestamptz NOT NULL DEFAULT now(),
             start_announced boolean NOT NULL DEFAULT false,
             end_announced boolean NOT NULL DEFAULT false)""",
        "CREATE INDEX competitions_server_idx ON competitions (server_id, ends_on)",
    ],
    10: [
        # profile: a short text on the player page, and hiding the own stats from the public pages
        "ALTER TABLE player_server_info ADD COLUMN bio text",
        "ALTER TABLE player_server_info ADD COLUMN hide_stats boolean NOT NULL DEFAULT false",
        """CREATE TABLE player_favorites(
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             favorite_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             created_at timestamptz NOT NULL DEFAULT now(),
             PRIMARY KEY (player_id, favorite_id),
             CHECK (player_id <> favorite_id))""",
    ],
    11: [
        # Tiers reached while the player was not playing (first sync of old stats, e.g. after an
        # update) are stored silently: no feed entry and no chat announcement.
        "ALTER TABLE player_achievements ADD COLUMN silent boolean NOT NULL DEFAULT false",
        """UPDATE player_achievements pa SET silent = NOT EXISTS (
             SELECT 1 FROM player_sessions ps WHERE ps.player_id = pa.player_id
               AND ps.started_at <= pa.earned_at
               AND COALESCE(ps.ended_at, now()) >= pa.earned_at - interval '10 minutes')""",
    ],
    12: [
        # who did what in the moderation (bans, moderators, competitions, notes)
        """CREATE TABLE mod_log(
             id bigserial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             at timestamptz NOT NULL DEFAULT now(),
             actor text,
             action text NOT NULL,
             target_name text,
             details text)""",
        "CREATE INDEX mod_log_server_idx ON mod_log (server_id, at DESC)",
        # internal notes of the moderators about a player
        """CREATE TABLE player_notes(
             id serial PRIMARY KEY,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             author text,
             text text NOT NULL,
             created_at timestamptz NOT NULL DEFAULT now())""",
        "CREATE INDEX player_notes_player_idx ON player_notes (player_id, created_at)",
        # health samples the plugin sends every minute
        """CREATE TABLE server_health(
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             at timestamptz NOT NULL DEFAULT now(),
             tps real,
             mem_used_mb integer,
             mem_max_mb integer,
             players integer,
             chunks integer,
             entities integer,
             uptime_s bigint,
             mc_version text,
             plugin_version text)""",
        "CREATE INDEX server_health_server_idx ON server_health (server_id, at)",
    ],
}
MAX_GALLERY_IMAGES = 12
# A session that ended less than this ago is continued on the next join (plugin reconnects).
SESSION_MERGE_SECONDS = 120
# An achievement counts as reached while playing if the player is online or left at most this
# long ago (the plugin sends the stats shortly after a quit). Otherwise it is stored silently.
ACHIEVEMENT_ACTIVE_MINUTES = 10
# Health samples older than this are deleted.
HEALTH_RETENTION_DAYS = 7
# Snapshots older than this are deleted (the newest older one of each metric is kept as baseline).
SNAPSHOT_RETENTION_DAYS = 90
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
EVENTS_CHANNEL = "mcc_events"
MODERATOR_LEVEL = 1

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
        """
        Store a stats file (json string or dict) for the player and update today's
        metric snapshot. Returns the number of stored values.
        """
        entries = stats_mod.split_stats(stats, self.blocks, self.items)
        data = [(player_id, category, name, value) for name, category, value in entries]
        with self._cursor() as cur:
            cur.executemany("""
                INSERT INTO actions (player_id, category, object, value)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (player_id, category, object)
                DO UPDATE SET value = EXCLUDED.value
                WHERE actions.value IS DISTINCT FROM EXCLUDED.value""", data)
            # the snapshot is taken from actions, so it always matches the live values
            columns, params = metrics_mod.sql_columns()
            cur.execute(f"""SELECT {", ".join(columns)} FROM actions a
                            WHERE a.player_id = %s AND a.category = ANY(%s)""",
                        (*params, player_id, metrics_mod.METRIC_CATEGORIES))
            snapshot = [(player_id, m.key, int(v)) for m, v in zip(metrics_mod.METRICS, cur.fetchone())]
            cur.executemany("""
                INSERT INTO stat_snapshots (player_id, metric, day, value) VALUES (%s, %s, current_date, %s)
                ON CONFLICT (player_id, metric, day) DO UPDATE SET value = EXCLUDED.value
                WHERE stat_snapshots.value IS DISTINCT FROM EXCLUDED.value""", snapshot)
            # Keep the newest snapshot before today even if it is old: it is the baseline
            # for players that come back after a long break.
            cur.execute("""
                DELETE FROM stat_snapshots s
                WHERE s.player_id = %s AND s.day < current_date - %s
                  AND EXISTS (SELECT 1 FROM stat_snapshots n
                              WHERE n.player_id = s.player_id AND n.metric = s.metric
                                AND n.day > s.day AND n.day < current_date)""",
                        (player_id, SNAPSHOT_RETENTION_DAYS))
        logger.info(f'Updated stats of player "{player_id}" ({len(data)} values)')
        return len(data)

    def register_player_join(self, server_id, mojang_uuid, name=None):
        """Mark the player online on the server, creating it if needed. Returns the player_id."""
        player_id = self.ensure_player_on_server(server_id, mojang_uuid, name)
        with self._cursor() as cur:
            cur.execute("""UPDATE player_server_info
                           SET online = true, first_seen = COALESCE(first_seen, now()), last_seen = now()
                           WHERE player_id = %s""", (player_id,))
            cur.execute("SELECT 1 FROM player_sessions WHERE player_id = %s AND ended_at IS NULL", (player_id,))
            if cur.fetchone() is None:
                # continue a session that just ended (plugin reconnect), otherwise start a new one
                cur.execute("""UPDATE player_sessions SET ended_at = NULL
                               WHERE id = (SELECT id FROM player_sessions WHERE player_id = %s
                                           ORDER BY started_at DESC LIMIT 1)
                                 AND ended_at > now() - %s * interval '1 second'""",
                            (player_id, SESSION_MERGE_SECONDS))
                if cur.rowcount == 0:
                    cur.execute("INSERT INTO player_sessions (player_id) VALUES (%s)", (player_id,))
        return player_id

    def register_player_quit(self, server_id, mojang_uuid):
        """Mark the player offline. Returns False if the player is unknown on this server."""
        with self._cursor() as cur:
            cur.execute("""UPDATE player_server_info SET online = false, last_seen = now()
                           WHERE mojang_uuid = %s AND server_id = %s RETURNING player_id""",
                        (mojang_uuid, server_id))
            row = cur.fetchone()
            if row is None:
                return False
            cur.execute("UPDATE player_sessions SET ended_at = now() WHERE player_id = %s AND ended_at IS NULL",
                        (row[0],))
        return True

    def update_player_status_from_mojang_uuid_and_server_id(self, mojang_uuid, server_id, status):
        if status == "online":
            return self.register_player_join(server_id, mojang_uuid) is not None
        return self.register_player_quit(server_id, mojang_uuid)

    def set_all_players_offline(self, server_id):
        with self._cursor() as cur:
            cur.execute("""UPDATE player_sessions ps SET ended_at = now() FROM player_server_info psi
                           WHERE psi.player_id = ps.player_id AND psi.server_id = %s AND ps.ended_at IS NULL""",
                        (server_id,))
            cur.execute("""UPDATE player_server_info SET online = false, last_seen = now()
                           WHERE server_id = %s AND online""", (server_id,))
            return cur.rowcount

    def update_online_status_by_player_id(self, player_id, online_status):
        self._execute("UPDATE player_server_info SET online = %s WHERE player_id = %s",
                      (online_status, player_id))

    ################################ DELETE FUNCTIONS #################################

    def delete_login_entry(self, player_id):
        self._execute("DELETE FROM login WHERE player_id = %s", (player_id,))

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


    ###----------------------------- Prefixes ------------------------------------###

    def _prefix_dict(self, row):
        prefix_id, text, color, owner_id, owner_name, has_password, members = row
        return {"prefix_id": prefix_id, "text": text, "color": color, "owner_id": owner_id,
                "owner_name": owner_name, "has_password": has_password, "members": members}

    _PREFIX_SELECT = """
        SELECT p.prefix_id, p.prefix_text, p.color, p.prefix_owner_id, op.name, p.password_hash IS NOT NULL,
               (SELECT count(*) FROM player_server_info m WHERE m.prefix_id = p.prefix_id)
        FROM prefixes p
        JOIN player_server_info opsi ON opsi.player_id = p.prefix_owner_id
        JOIN player op ON op.uuid = opsi.mojang_uuid"""

    def list_prefixes(self, server_id):
        """All prefixes of a server, sorted by text."""
        return [self._prefix_dict(row) for row in self._fetchall(
            self._PREFIX_SELECT + " WHERE p.server_id = %s ORDER BY lower(p.prefix_text)", (server_id,))]

    def get_owned_prefix(self, player_id):
        row = self._fetchone(self._PREFIX_SELECT + " WHERE p.prefix_owner_id = %s", (player_id,))
        return self._prefix_dict(row) if row else None

    def get_player_prefix(self, player_id):
        """The prefix the player currently wears (own or joined), or None."""
        row = self._fetchone(self._PREFIX_SELECT + """
            WHERE p.prefix_id = (SELECT prefix_id FROM player_server_info WHERE player_id = %s)""", (player_id,))
        return self._prefix_dict(row) if row else None

    def save_own_prefix(self, player_id, text, color, password=None, remove_password=False):
        """
        Create or update the player's own prefix and wear it. password=None keeps the
        current password (unless remove_password). Returns the uuids of everyone wearing it.
        Raises psycopg2.errors.UniqueViolation if another prefix on the server has this text.
        """
        with self._cursor() as cur:
            cur.execute("SELECT server_id FROM player_server_info WHERE player_id = %s", (player_id,))
            server_id = cur.fetchone()[0]
            cur.execute("SELECT prefix_id FROM prefixes WHERE prefix_owner_id = %s", (player_id,))
            row = cur.fetchone()
            password_hash = ph.hash(password) if password else None
            if row is None:
                cur.execute("""INSERT INTO prefixes (prefix_owner_id, server_id, prefix_text, color, password_hash)
                               VALUES (%s, %s, %s, %s, %s) RETURNING prefix_id""",
                            (player_id, server_id, text, color, password_hash))
                prefix_id = cur.fetchone()[0]
            else:
                prefix_id = row[0]
                cur.execute("UPDATE prefixes SET prefix_text = %s, color = %s WHERE prefix_id = %s",
                            (text, color, prefix_id))
                if password or remove_password:
                    cur.execute("UPDATE prefixes SET password_hash = %s WHERE prefix_id = %s", (password_hash, prefix_id))
            cur.execute("UPDATE player_server_info SET prefix_id = %s WHERE player_id = %s", (prefix_id, player_id))
            cur.execute("SELECT mojang_uuid FROM player_server_info WHERE prefix_id = %s", (prefix_id,))
            return [str(r[0]) for r in cur.fetchall()]

    def delete_own_prefix(self, player_id):
        """Delete the player's own prefix. Returns the uuids of everyone who wore it."""
        with self._cursor() as cur:
            cur.execute("""SELECT psi.mojang_uuid FROM player_server_info psi
                           JOIN prefixes p ON p.prefix_id = psi.prefix_id WHERE p.prefix_owner_id = %s""",
                        (player_id,))
            uuids = [str(r[0]) for r in cur.fetchall()]
            cur.execute("DELETE FROM prefixes WHERE prefix_owner_id = %s", (player_id,))  # members: ON DELETE SET NULL
        return uuids

    def join_prefix(self, player_id, prefix_id, password=None):
        """Wear another prefix of the same server. Returns "ok", "not found" or "wrong password"."""
        with self._cursor() as cur:
            cur.execute("""SELECT p.password_hash FROM prefixes p
                           JOIN player_server_info psi ON psi.server_id = p.server_id
                           WHERE p.prefix_id = %s AND psi.player_id = %s""", (prefix_id, player_id))
            row = cur.fetchone()
            if row is None:
                return "not found"
            if row[0]:
                try:
                    ph.verify(row[0], password or "")
                except argon2.exceptions.VerificationError:
                    return "wrong password"
            cur.execute("UPDATE player_server_info SET prefix_id = %s WHERE player_id = %s", (prefix_id, player_id))
        return "ok"

    def leave_prefix(self, player_id):
        self._execute("UPDATE player_server_info SET prefix_id = NULL WHERE player_id = %s", (player_id,))

    def get_prefix_for_uuid(self, server_id, mojang_uuid):
        """(text, color) of the prefix a player wears on a server, or None (used by the socket server)."""
        return self._fetchone("""SELECT p.prefix_text, p.color FROM player_server_info psi
                                 JOIN prefixes p ON p.prefix_id = psi.prefix_id
                                 WHERE psi.server_id = %s AND psi.mojang_uuid = %s""", (server_id, mojang_uuid))

    def get_all_worn_prefixes(self, server_id):
        """{uuid: (text, color)} for every player on the server that wears a prefix."""
        return {str(uuid): (text, color) for uuid, text, color in self._fetchall(
            """SELECT psi.mojang_uuid, p.prefix_text, p.color FROM player_server_info psi
               JOIN prefixes p ON p.prefix_id = psi.prefix_id WHERE psi.server_id = %s""", (server_id,))}

    def notify_server_event(self, server_id, event_type, **data):
        """Tell the socket server about a change (prefix, ban, unban) on a minecraft server."""
        payload = json.dumps(dict(data, type=event_type, server_id=server_id))
        self._execute("SELECT pg_notify(%s, %s)", (EVENTS_CHANNEL, payload))

    ###----------------------------- Moderation ------------------------------------###

    def is_moderator(self, player_id):
        """Moderators have web_access_permissions <= 1, or are OPs on a server with auto_mod_ops."""
        return bool(self._fetchvalue("""
            SELECT psi.web_access_permissions <= %s OR (s.auto_mod_ops AND psi.is_op)
            FROM player_server_info psi JOIN servers s ON s.id = psi.server_id
            WHERE psi.player_id = %s""", (MODERATOR_LEVEL, player_id)))

    def set_moderator(self, server_id, player_name, moderator):
        """Grant or revoke moderator rights by player name. Returns the player's name or None if unknown."""
        return self._fetchvalue("""
            UPDATE player_server_info psi SET web_access_permissions = %s
            FROM player p WHERE p.uuid = psi.mojang_uuid AND psi.server_id = %s AND lower(p.name) = lower(%s)
            RETURNING p.name""", (MODERATOR_LEVEL if moderator else DEFAULT_WEB_ACCESS_LEVEL, server_id, player_name))

    def list_moderators(self, server_id):
        """[{"name", "uuid", "is_op", "explicit"}] of everyone with moderator rights on the server."""
        rows = self._fetchall("""
            SELECT p.name, psi.mojang_uuid, psi.is_op, psi.web_access_permissions <= %s
            FROM player_server_info psi
            JOIN player p ON p.uuid = psi.mojang_uuid
            JOIN servers s ON s.id = psi.server_id
            WHERE psi.server_id = %s AND (psi.web_access_permissions <= %s OR (s.auto_mod_ops AND psi.is_op))
            ORDER BY lower(p.name)""", (MODERATOR_LEVEL, server_id, MODERATOR_LEVEL))
        return [{"name": n, "uuid": str(u), "is_op": op, "explicit": explicit} for n, u, op, explicit in rows]

    def set_player_op(self, server_id, mojang_uuid, is_op):
        self._execute("UPDATE player_server_info SET is_op = %s WHERE server_id = %s AND mojang_uuid = %s",
                      (is_op, server_id, mojang_uuid))

    ###----------------------------- Bans ------------------------------------###

    _ACTIVE_BAN = "(bp.ban_end IS NULL OR bp.ban_end > now())"

    def get_ban_reasons(self):
        return [{"id": i, "reason": r, "days": d}
                for i, r, d in self._fetchall("SELECT id, reason, ban_duration_in_days FROM ban_reasons ORDER BY id")]

    def add_banned_player(self, banned_player_id, moderator_id, ban_reason_id, ban_end, comment=None,
                          reason_text=None, banned_by=None, source="web"):
        """ban_end None = permanent."""
        return self._fetchvalue("""
            INSERT INTO banned_players (banned_player_id, moderator_id, ban_reason_id, ban_end, comment,
                                        reason_text, banned_by, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (banned_player_id, moderator_id, ban_reason_id, ban_end, comment, reason_text, banned_by, source))

    def ban_player(self, server_id, player_name, banned_by, reason_id=None, days=None, comment=None):
        """
        Web ban by player name. days=None uses the reason's default duration, days=0 is permanent.
        Returns {"uuid", "name", "reason", "end"} or None if the player is unknown on the server.
        """
        with self._cursor() as cur:
            cur.execute("""SELECT psi.player_id, psi.mojang_uuid, p.name FROM player_server_info psi
                           JOIN player p ON p.uuid = psi.mojang_uuid
                           WHERE psi.server_id = %s AND lower(p.name) = lower(%s)""", (server_id, player_name))
            row = cur.fetchone()
            if row is None:
                return None
            player_id, uuid, name = row
            reason = "Gebannt"
            if reason_id is not None:
                cur.execute("SELECT reason, ban_duration_in_days FROM ban_reasons WHERE id = %s", (reason_id,))
                reason_row = cur.fetchone()
                if reason_row:
                    reason = reason_row[0]
                    if days is None:
                        days = reason_row[1]
            end = None if not days else datetime.now(timezone.utc) + timedelta(days=int(days))
            cur.execute("""INSERT INTO banned_players (banned_player_id, ban_reason_id, ban_end, comment, banned_by, source)
                           VALUES (%s, %s, %s, %s, %s, 'web')""", (player_id, reason_id, end, comment, banned_by))
        return {"uuid": str(uuid), "name": name, "reason": reason, "end": end}

    def unban_player(self, server_id, player_id):
        """Remove all active bans of the player. Returns {"uuid", "name"} or None."""
        with self._cursor() as cur:
            cur.execute("""SELECT psi.mojang_uuid, p.name FROM player_server_info psi
                           JOIN player p ON p.uuid = psi.mojang_uuid
                           WHERE psi.server_id = %s AND psi.player_id = %s""", (server_id, player_id))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(f"DELETE FROM banned_players bp WHERE bp.banned_player_id = %s AND {self._ACTIVE_BAN}",
                        (player_id,))
        return {"uuid": str(row[0]), "name": row[1]}

    def list_active_bans(self, server_id):
        rows = self._fetchall(f"""
            SELECT psi.player_id, p.name, psi.mojang_uuid, bp.source, COALESCE(br.reason, bp.reason_text),
                   bp.banned_by, bp.ban_start, bp.ban_end, bp.comment
            FROM banned_players bp
            JOIN player_server_info psi ON psi.player_id = bp.banned_player_id
            JOIN player p ON p.uuid = psi.mojang_uuid
            LEFT JOIN ban_reasons br ON br.id = bp.ban_reason_id
            WHERE psi.server_id = %s AND {self._ACTIVE_BAN}
            ORDER BY bp.ban_start DESC""", (server_id,))
        keys = ("player_id", "name", "uuid", "source", "reason", "banned_by", "start", "end", "comment")
        bans = [dict(zip(keys, row)) for row in rows]
        for ban in bans:
            ban["uuid"] = str(ban["uuid"])
        return bans

    def sync_ingame_bans(self, server_id, entries):
        """
        Replace the server's ingame bans with the ban list the plugin reported:
        [{"name", "reason", "source", "created" (epoch ms), "expires" (epoch ms or 0)}].
        Names that never played on the server are ignored. Returns the number of stored bans.
        """
        stored = 0
        with self._cursor() as cur:
            cur.execute("""DELETE FROM banned_players bp USING player_server_info psi, player p
                           WHERE bp.banned_player_id = psi.player_id AND p.uuid = psi.mojang_uuid
                             AND psi.server_id = %s AND bp.source = 'ingame'
                           RETURNING p.name""", (server_id,))
            before = {row[0] for row in cur.fetchall()}
            after = set()
            for entry in entries:
                cur.execute("""SELECT psi.player_id FROM player_server_info psi JOIN player p ON p.uuid = psi.mojang_uuid
                               WHERE psi.server_id = %s AND lower(p.name) = lower(%s)""",
                            (server_id, str(entry.get("name", ""))))
                row = cur.fetchone()
                if row is None:
                    continue
                cur.execute("SELECT name FROM player WHERE uuid = (SELECT mojang_uuid FROM player_server_info WHERE player_id = %s)",
                            (row[0],))
                name = cur.fetchone()[0]
                after.add(name)
                if name not in before:
                    self._log(cur, server_id, str(entry.get("source") or "")[:100] or None, "ingame_ban", name,
                              str(entry.get("reason") or "")[:500] or None)
                created = entry.get("created") or 0
                expires = entry.get("expires") or 0
                cur.execute("""INSERT INTO banned_players (banned_player_id, ban_start, ban_end, reason_text, banned_by, source)
                               VALUES (%s, COALESCE(to_timestamp(%s / 1000.0), now()), to_timestamp(%s / 1000.0),
                                       %s, %s, 'ingame')""",
                            (row[0], created or None, expires or None,
                             str(entry.get("reason") or "")[:500] or None, str(entry.get("source") or "")[:100] or None))
                stored += 1
            for name in before - after:
                self._log(cur, server_id, None, "ingame_unban", name)
        return stored

    def get_ban_reason_from_player_id(self, player_id):
        """Reason of the currently active ban (or "Gebannt" without a reason), or None if not banned."""
        row = self._fetchone(f"""SELECT COALESCE(br.reason, bp.reason_text, 'Gebannt') FROM banned_players bp
                                 LEFT JOIN ban_reasons br ON br.id = bp.ban_reason_id
                                 WHERE bp.banned_player_id = %s AND {self._ACTIVE_BAN}
                                 ORDER BY bp.ban_end DESC NULLS FIRST LIMIT 1""", (player_id,))
        return row[0] if row else None

    def get_ban_start_and_ban_end_by_player_id(self, player_id):
        """(ban_start, ban_end) of the currently active ban (ban_end None = permanent), or None."""
        return self._fetchone(f"""SELECT ban_start, ban_end FROM banned_players bp
                                  WHERE bp.banned_player_id = %s AND {self._ACTIVE_BAN}
                                  ORDER BY bp.ban_end DESC NULLS FIRST LIMIT 1""", (player_id,))

    ###----------------------------- Servers ------------------------------------###

    def get_server_id_from_subdomain(self, subdomain):
        return self._fetchvalue("SELECT id FROM servers WHERE subdomain = lower(%s)", (subdomain,))

    def get_subdomain_from_server_id(self, server_id):
        return self._fetchvalue("SELECT subdomain FROM servers WHERE id = %s", (server_id,))

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
        allowed = {"server_name", "mc_server_domain", "discord_url", "whitelist", "auto_mod_ops",
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
        """
        Delete the server with all its player data. Returns the filenames of its images
        (to be removed from disk), or None if the server does not exist / is not owned.
        """
        with self._cursor() as cur:
            cur.execute("SELECT filename FROM server_images WHERE server_id = %s", (server_id,))
            filenames = [row[0] for row in cur.fetchall()]
            cur.execute("DELETE FROM servers WHERE id = %s AND owner_id = %s", (server_id, owner_id))
            if cur.rowcount == 0:
                return None
        return filenames

    ###----------------------------- Server images ------------------------------------###

    def add_server_image(self, server_id, owner_id, kind, filename):
        """
        Register an uploaded image. A new banner replaces the old one; the gallery holds
        at most MAX_GALLERY_IMAGES. Returns (image_id, replaced_filenames), or None if the
        server is not owned by owner_id or the gallery is full.
        """
        with self._cursor() as cur:
            # lock the server row so concurrent uploads cannot exceed the limit
            cur.execute("SELECT id FROM servers WHERE id = %s AND owner_id = %s FOR UPDATE", (server_id, owner_id))
            if cur.fetchone() is None:
                return None
            replaced = []
            if kind == "banner":
                cur.execute("DELETE FROM server_images WHERE server_id = %s AND kind = 'banner' RETURNING filename",
                            (server_id,))
                replaced = [row[0] for row in cur.fetchall()]
            else:
                cur.execute("SELECT count(*) FROM server_images WHERE server_id = %s AND kind = 'gallery'", (server_id,))
                if cur.fetchone()[0] >= MAX_GALLERY_IMAGES:
                    return None
            cur.execute("INSERT INTO server_images (server_id, kind, filename) VALUES (%s, %s, %s) RETURNING id",
                        (server_id, kind, filename))
            return cur.fetchone()[0], replaced

    def get_server_images(self, server_id):
        """{"banner": filename or None, "gallery": [{"id", "filename"}, ...]} in upload order."""
        rows = self._fetchall("SELECT id, kind, filename FROM server_images WHERE server_id = %s ORDER BY id",
                              (server_id,))
        images = {"banner": None, "gallery": []}
        for image_id, kind, filename in rows:
            if kind == "banner":
                images["banner"] = filename
            else:
                images["gallery"].append({"id": image_id, "filename": filename})
        return images

    def delete_server_image(self, image_id, owner_id):
        """Delete one image of a server owned by owner_id. Returns its filename or None."""
        return self._fetchvalue("""DELETE FROM server_images si USING servers s
                                   WHERE si.id = %s AND si.server_id = s.id AND s.owner_id = %s
                                   RETURNING si.filename""", (image_id, owner_id))

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

    ###----------------------------- Rankings / comparison ------------------------------------###

    def get_server_metrics(self, server_id, days=None, include_hidden=False):
        """
        Every player of the server with all metrics (database/metrics.py):
        [{"player_id", "name", "uuid", "online", "hidden", "values": {metric key: value}}], sorted by name.
        Players who hide their stats are left out unless include_hidden (e.g. for server totals).
        With days, values are the gain since the end of the day `days` days ago (from the
        snapshots; if there is none that old, since the first snapshot).
        """
        columns, params = metrics_mod.sql_columns()
        rows = self._fetchall(f"""
            SELECT psi.player_id, p.name, psi.mojang_uuid, psi.online, psi.hide_stats, {", ".join(columns)}
            FROM player_server_info psi
            JOIN player p ON p.uuid = psi.mojang_uuid
            LEFT JOIN actions a ON a.player_id = psi.player_id AND a.category = ANY(%s)
            WHERE psi.server_id = %s AND (%s OR NOT psi.hide_stats)
            GROUP BY psi.player_id, p.name, psi.mojang_uuid, psi.online, psi.hide_stats
            ORDER BY lower(p.name)""", (*params, metrics_mod.METRIC_CATEGORIES, server_id, include_hidden))
        players = [{"player_id": str(row[0]), "name": row[1], "uuid": str(row[2]), "online": row[3], "hidden": row[4],
                    "values": {m.key: int(v) for m, v in zip(metrics_mod.METRICS, row[5:])}} for row in rows]
        if days is None:
            return players

        baselines = {}
        for player_id, metric, value in self._fetchall("""
                SELECT DISTINCT ON (s.player_id, s.metric) s.player_id, s.metric, s.value
                FROM stat_snapshots s
                JOIN player_server_info psi ON psi.player_id = s.player_id
                WHERE psi.server_id = %s
                ORDER BY s.player_id, s.metric, s.day <= current_date - %s DESC,
                         CASE WHEN s.day <= current_date - %s THEN s.day END DESC NULLS LAST, s.day""",
                                                       (server_id, days, days)):
            baselines[(str(player_id), metric)] = value
        for player in players:
            player["values"] = {key: max(0, value - baselines.get((player["player_id"], key), value))
                                for key, value in player["values"].items()}
        return players

    def get_player_list_details(self, server_id):
        """{player_id: {"first_seen", "last_seen", "is_op", "moderator", "banned"}} for the player list."""
        rows = self._fetchall(f"""
            SELECT psi.player_id, psi.first_seen, psi.last_seen, psi.is_op,
                   psi.web_access_permissions <= %s OR (s.auto_mod_ops AND psi.is_op),
                   EXISTS (SELECT 1 FROM banned_players bp
                           WHERE bp.banned_player_id = psi.player_id AND {self._ACTIVE_BAN})
            FROM player_server_info psi JOIN servers s ON s.id = psi.server_id
            WHERE psi.server_id = %s""", (MODERATOR_LEVEL, server_id))
        keys = ("first_seen", "last_seen", "is_op", "moderator", "banned")
        return {str(row[0]): dict(zip(keys, row[1:])) for row in rows}

    def get_stat_values(self, player_id, category, objects=None):
        """{object: value} of one player and category, optionally only the given objects."""
        if objects is None:
            rows = self._fetchall("SELECT object, value FROM actions WHERE player_id = %s AND category = %s",
                                  (player_id, category))
        else:
            rows = self._fetchall("""SELECT object, value FROM actions
                                     WHERE player_id = %s AND category = %s AND object = ANY(%s)""",
                                  (player_id, category, list(objects)))
        return dict(rows)

    ###----------------------------- Server statistics (sessions) ------------------------------------###

    def get_today(self):
        """Today in the database time zone (config.TIMEZONE)."""
        return self._fetchvalue("SELECT current_date")

    _SERVER_SESSIONS = """SELECT ps.player_id, ps.started_at, COALESCE(ps.ended_at, now()) AS ended_at
                          FROM player_sessions ps JOIN player_server_info psi ON psi.player_id = ps.player_id
                          WHERE psi.server_id = %s"""

    def get_online_history(self, server_id, hours, bucket_minutes):
        """[(bucket start, players online)] for the last `hours` hours. A player counts for a
        bucket if they were online at any time in it."""
        return self._fetchall(f"""
            WITH buckets AS (
                SELECT generate_series(date_trunc('hour', now()) - %s * interval '1 hour',
                                       now(), %s * interval '1 minute') AS start),
            s AS ({self._SERVER_SESSIONS} AND COALESCE(ps.ended_at, now()) > now() - %s * interval '1 hour' - interval '1 hour')
            SELECT b.start, count(DISTINCT s.player_id)
            FROM buckets b LEFT JOIN s ON s.started_at < b.start + %s * interval '1 minute' AND s.ended_at > b.start
            GROUP BY b.start ORDER BY b.start""", (hours, bucket_minutes, server_id, hours, bucket_minutes))

    def get_peak_times(self, server_id, weeks=8):
        """7x24 matrix [weekday 0=Monday][hour] of the average number of players online
        in that hour over the last `weeks` weeks (only weeks with any data count)."""
        rows = self._fetchall(f"""
            WITH hours AS (
                SELECT generate_series(date_trunc('hour', now()) - %s * interval '1 week',
                                       date_trunc('hour', now()) - interval '1 hour', interval '1 hour') AS start),
            s AS ({self._SERVER_SESSIONS} AND COALESCE(ps.ended_at, now()) > now() - %s * interval '1 week'),
            counted AS (
                SELECT h.start, count(DISTINCT s.player_id) AS online
                FROM hours h LEFT JOIN s ON s.started_at < h.start + interval '1 hour' AND s.ended_at > h.start
                WHERE h.start >= (SELECT date_trunc('hour', min(started_at)) FROM s)
                GROUP BY h.start)
            SELECT extract(isodow FROM start)::int - 1, extract(hour FROM start)::int, avg(online)
            FROM counted GROUP BY 1, 2""", (weeks, server_id, weeks))
        matrix = [[0.0] * 24 for _ in range(7)]
        for weekday, hour, online in rows:
            matrix[weekday][hour] = round(float(online), 2)
        return matrix

    def get_online_peak(self, server_id):
        """(most players online at the same time, when) over all sessions, or (0, None)."""
        row = self._fetchone(f"""
            WITH s AS ({self._SERVER_SESSIONS}),
            events AS (SELECT started_at AS at, 1 AS delta FROM s UNION ALL SELECT ended_at, -1 FROM s),
            running AS (SELECT at, sum(delta) OVER (ORDER BY at, delta ROWS UNBOUNDED PRECEDING) AS online FROM events)
            SELECT online, at FROM running ORDER BY online DESC, at DESC LIMIT 1""", (server_id,))
        return (int(row[0]), row[1]) if row else (0, None)

    def get_new_players_per_month(self, server_id, months=12):
        """[(first day of month, new players)] for the last `months` months including the current one."""
        return self._fetchall("""
            SELECT m.month::date, count(psi.player_id)
            FROM generate_series(date_trunc('month', now()) - (%s - 1) * interval '1 month',
                                 date_trunc('month', now()), interval '1 month') AS m(month)
            LEFT JOIN player_server_info psi ON psi.server_id = %s
                 AND date_trunc('month', psi.first_seen) = m.month
            GROUP BY m.month ORDER BY m.month""", (months, server_id))

    def get_server_daily_gain(self, server_id, metric, days=30):
        """[(day, gain of all players together)] of the last `days` days from the snapshots
        (None for days without any data)."""
        rows = self._fetchall("""
            SELECT s.player_id, s.day, s.value FROM stat_snapshots s
            JOIN player_server_info psi ON psi.player_id = s.player_id
            WHERE psi.server_id = %s AND s.metric = %s ORDER BY s.player_id, s.day""", (server_id, metric))
        today = self._fetchvalue("SELECT current_date")
        dates = [today - timedelta(days=offset) for offset in range(days, -1, -1)]
        per_player = {}
        for player_id, day, value in rows:
            per_player.setdefault(player_id, []).append((day, value))
        totals = [None] * len(dates)
        for points in per_player.values():
            index, last, values = 0, None, []
            for date in dates:
                while index < len(points) and points[index][0] <= date:
                    last = points[index][1]
                    index += 1
                values.append(last)
            for i in range(1, len(dates)):
                if values[i - 1] is not None and values[i] is not None:
                    totals[i] = (totals[i] or 0) + max(0, values[i] - values[i - 1])
        return list(zip(dates[1:], totals[1:]))

    def get_metrics_between(self, server_id, start, end):
        """
        {player_id: {metric: gain}} between the end of the day before `start` and the end of
        `end` (dates), from the snapshots. Players without a snapshot up to `end` are left out;
        without one before `start` the first snapshot is the baseline.
        """
        rows = self._fetchall("""
            WITH s AS (SELECT s.player_id, s.metric, s.day, s.value FROM stat_snapshots s
                       JOIN player_server_info psi ON psi.player_id = s.player_id
                       WHERE psi.server_id = %s AND s.day <= %s),
            at_end AS (SELECT DISTINCT ON (player_id, metric) player_id, metric, value FROM s
                       ORDER BY player_id, metric, day DESC),
            at_start AS (SELECT DISTINCT ON (player_id, metric) player_id, metric, value FROM s
                         ORDER BY player_id, metric, day < %s DESC,
                                  CASE WHEN day < %s THEN day END DESC NULLS LAST, day)
            SELECT e.player_id, e.metric, e.value - b.value
            FROM at_end e JOIN at_start b USING (player_id, metric)""", (server_id, end, start, start))
        result = {}
        for player_id, metric, gain in rows:
            result.setdefault(str(player_id), {})[metric] = max(0, gain)
        return result

    def get_active_players_between(self, server_id, start, end):
        """Number of players that were online between the start of `start` and the end of `end` (dates)."""
        return self._fetchvalue(f"""
            WITH s AS ({self._SERVER_SESSIONS})
            SELECT count(DISTINCT player_id) FROM s
            WHERE started_at < (%s::date + 1)::timestamptz AND ended_at > %s::date::timestamptz""",
                                (server_id, end, start))

    def get_new_players_between(self, server_id, start, end):
        return self._fetchvalue("""SELECT count(*) FROM player_server_info WHERE server_id = %s
                                   AND first_seen >= %s::date::timestamptz AND first_seen < (%s::date + 1)::timestamptz""",
                                (server_id, start, end))

    ###----------------------------- Profile, privacy, favourites ------------------------------------###

    def get_profile(self, player_id):
        """{"bio", "hide_stats"} of a player."""
        row = self._fetchone("SELECT bio, hide_stats FROM player_server_info WHERE player_id = %s", (player_id,))
        return {"bio": row[0], "hide_stats": row[1]} if row else None

    def save_profile(self, player_id, bio, hide_stats):
        self._execute("UPDATE player_server_info SET bio = %s, hide_stats = %s WHERE player_id = %s",
                      (bio or None, bool(hide_stats), player_id))

    def is_stats_hidden(self, player_id):
        return bool(self._fetchvalue("SELECT hide_stats FROM player_server_info WHERE player_id = %s", (player_id,)))

    def get_favorites(self, player_id):
        """[{"name", "uuid"}] of the favourites of a player, sorted by name."""
        return [{"name": name, "uuid": str(uuid)} for name, uuid in self._fetchall("""
            SELECT p.name, psi.mojang_uuid FROM player_favorites f
            JOIN player_server_info psi ON psi.player_id = f.favorite_id
            JOIN player p ON p.uuid = psi.mojang_uuid
            WHERE f.player_id = %s ORDER BY lower(p.name)""", (player_id,))]

    def toggle_favorite(self, player_id, favorite_name):
        """Add or remove a favourite (same server). Returns True/False (now a favourite) or None if unknown."""
        server_id = self.get_server_id_from_player_id(player_id)
        favorite_id = self.get_player_id_from_player_name_and_server_id(favorite_name, server_id)
        if favorite_id is None or str(favorite_id) == str(player_id):
            return None
        with self._cursor() as cur:
            cur.execute("DELETE FROM player_favorites WHERE player_id = %s AND favorite_id = %s", (player_id, favorite_id))
            if cur.rowcount:
                return False
            cur.execute("INSERT INTO player_favorites (player_id, favorite_id) VALUES (%s, %s)", (player_id, favorite_id))
        return True

    def get_feed(self, server_id, limit=15, days=7):
        """
        Latest events of a server, newest first: [{"at", "kind", "name", "uuid", "detail", "tier"}].
        kind: join, new, achievement, competition_start, competition_end. Players who hide their
        stats, silent achievements and bulks (more than 2 at once) are left out.
        """
        rows = self._fetchall("""
            WITH visible AS (
                SELECT psi.player_id, p.name, psi.mojang_uuid, psi.first_seen FROM player_server_info psi
                JOIN player p ON p.uuid = psi.mojang_uuid
                WHERE psi.server_id = %s AND NOT psi.hide_stats),
            achievements AS (
                SELECT pa.player_id, pa.achievement, pa.tier, pa.earned_at,
                       count(*) OVER (PARTITION BY pa.player_id, pa.earned_at) AS at_once
                FROM player_achievements pa JOIN visible v USING (player_id)
                WHERE pa.earned_at > now() - %s * interval '1 day' AND NOT pa.silent)
            SELECT at, kind, name, uuid, detail, tier FROM (
                SELECT ps.started_at AS at,
                       CASE WHEN ps.started_at - v.first_seen < interval '1 minute' THEN 'new' ELSE 'join' END AS kind,
                       v.name, v.mojang_uuid::text AS uuid, NULL AS detail, NULL::smallint AS tier
                FROM player_sessions ps JOIN visible v USING (player_id)
                WHERE ps.started_at > now() - %s * interval '1 day'
                UNION ALL
                SELECT a.earned_at, 'achievement', v.name, v.mojang_uuid::text, a.achievement, a.tier
                FROM achievements a JOIN visible v USING (player_id) WHERE a.at_once <= 2
                UNION ALL
                SELECT c.starts_on::timestamptz, 'competition_start', NULL, NULL, c.title, NULL FROM competitions c
                WHERE c.server_id = %s AND c.starts_on <= current_date AND c.starts_on > current_date - %s
                UNION ALL
                SELECT (c.ends_on + 1)::timestamptz, 'competition_end', NULL, NULL, c.title, NULL FROM competitions c
                WHERE c.server_id = %s AND c.ends_on < current_date AND c.ends_on >= current_date - %s
            ) events ORDER BY at DESC LIMIT %s""", (server_id, days, days, server_id, days, server_id, days, limit))
        return [dict(zip(("at", "kind", "name", "uuid", "detail", "tier"), row)) for row in rows]

    ###----------------------------- Moderation log and notes ------------------------------------###

    @staticmethod
    def _log(cur, server_id, actor, action, target_name=None, details=None):
        cur.execute("""INSERT INTO mod_log (server_id, actor, action, target_name, details)
                       VALUES (%s, %s, %s, %s, %s)""", (server_id, actor, action, target_name, details))

    def add_mod_log(self, server_id, actor, action, target_name=None, details=None):
        with self._cursor() as cur:
            self._log(cur, server_id, actor, action, target_name, details)

    def get_mod_log(self, server_id, limit=100, target_name=None):
        """Newest entries first: [{"at", "actor", "action", "target_name", "details"}]."""
        rows = self._fetchall("""SELECT at, actor, action, target_name, details FROM mod_log
                                 WHERE server_id = %s AND (%s::text IS NULL OR lower(target_name) = lower(%s))
                                 ORDER BY at DESC, id DESC LIMIT %s""", (server_id, target_name, target_name, limit))
        return [dict(zip(("at", "actor", "action", "target_name", "details"), row)) for row in rows]

    def add_player_note(self, player_id, author, text):
        return self._fetchvalue("INSERT INTO player_notes (player_id, author, text) VALUES (%s, %s, %s) RETURNING id",
                                (player_id, author, text))

    def get_player_notes(self, player_id):
        """[{"id", "author", "text", "created_at"}], oldest first."""
        rows = self._fetchall("""SELECT id, author, text, created_at FROM player_notes
                                 WHERE player_id = %s ORDER BY created_at, id""", (player_id,))
        return [dict(zip(("id", "author", "text", "created_at"), row)) for row in rows]

    def delete_player_note(self, server_id, note_id):
        """Delete a note of a player on the server. Returns (player name, text) or None."""
        return self._fetchone("""DELETE FROM player_notes n USING player_server_info psi, player p
                                 WHERE n.id = %s AND n.player_id = psi.player_id AND psi.server_id = %s
                                   AND p.uuid = psi.mojang_uuid
                                 RETURNING p.name, n.text""", (note_id, server_id))

    ###----------------------------- Server health ------------------------------------###

    _HEALTH_FIELDS = ("tps", "mem_used_mb", "mem_max_mb", "players", "chunks", "entities", "uptime_s",
                      "mc_version", "plugin_version")

    def add_health_sample(self, server_id, sample):
        """Store one sample of the plugin (dict with _HEALTH_FIELDS) and drop old ones."""
        values = [sample.get(field) for field in self._HEALTH_FIELDS]
        with self._cursor() as cur:
            cur.execute(f"""INSERT INTO server_health (server_id, {", ".join(self._HEALTH_FIELDS)})
                            VALUES (%s, {", ".join(["%s"] * len(values))})""", (server_id, *values))
            cur.execute("DELETE FROM server_health WHERE server_id = %s AND at < now() - %s * interval '1 day'",
                        (server_id, HEALTH_RETENTION_DAYS))

    def get_latest_health(self, server_id):
        row = self._fetchone(f"""SELECT at, {", ".join(self._HEALTH_FIELDS)} FROM server_health
                                 WHERE server_id = %s ORDER BY at DESC LIMIT 1""", (server_id,))
        return dict(zip(("at",) + self._HEALTH_FIELDS, row)) if row else None

    def get_health_history(self, server_id, hours=24, bucket_minutes=10):
        """[(bucket start, avg tps, avg used MB, max MB)] of the last `hours` hours (None without samples)."""
        return self._fetchall("""
            WITH buckets AS (SELECT generate_series(date_trunc('hour', now()) - %s * interval '1 hour', now(),
                                                    %s * interval '1 minute') AS start)
            SELECT b.start, avg(h.tps), avg(h.mem_used_mb), max(h.mem_max_mb)
            FROM buckets b LEFT JOIN server_health h ON h.server_id = %s
                 AND h.at >= b.start AND h.at < b.start + %s * interval '1 minute'
            GROUP BY b.start ORDER BY b.start""", (hours, bucket_minutes, server_id, bucket_minutes))

    def get_health_availability(self, server_id, days=7):
        """Share (0..1) of the minutes of the last `days` days with a health sample, or None without any."""
        row = self._fetchone("""SELECT count(DISTINCT date_trunc('minute', at)), min(at) FROM server_health
                                WHERE server_id = %s AND at > now() - %s * interval '1 day'""", (server_id, days))
        if not row or not row[0]:
            return None
        # measure from the first sample on (the plugin may have been installed recently)
        minutes = self._fetchvalue("SELECT greatest(1, extract(epoch FROM now() - %s) / 60)", (row[1],))
        return min(1.0, row[0] / float(minutes))

    ###----------------------------- Achievements ------------------------------------###

    def get_player_metrics(self, player_id):
        """{metric key: current value} of one player."""
        columns, params = metrics_mod.sql_columns()
        row = self._fetchone(f"SELECT {', '.join(columns)} FROM actions a WHERE a.player_id = %s AND a.category = ANY(%s)",
                             (*params, player_id, metrics_mod.METRIC_CATEGORIES))
        return {m.key: int(v) for m, v in zip(metrics_mod.METRICS, row)}

    def award_achievements(self, player_id):
        """
        Store the newly reached achievement tiers of a player. Returns [(achievement, tier)] of the
        new ones reached while playing (to announce); others are stored silently.
        """
        values = self.get_player_metrics(player_id)
        reached = [(a.key, tier) for a in achievements_mod.ACHIEVEMENTS
                   for tier in range(achievements_mod.tier_of(a, values[a.metric]) + 1)]
        if not reached:
            return []
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO player_achievements (player_id, achievement, tier, silent)
                SELECT %s, key, tier, NOT EXISTS (
                    SELECT 1 FROM player_sessions ps WHERE ps.player_id = %s
                      AND COALESCE(ps.ended_at, now()) >= now() - %s * interval '1 minute')
                FROM unnest(%s::text[], %s::smallint[]) AS r(key, tier)
                ON CONFLICT DO NOTHING RETURNING achievement, tier, silent""",
                        (player_id, player_id, ACHIEVEMENT_ACTIVE_MINUTES,
                         [k for k, _ in reached], [t for _, t in reached]))
            rows = cur.fetchall()
        return [(achievements_mod.ACHIEVEMENTS_BY_KEY[key], tier) for key, tier, silent in rows if not silent]

    def get_player_achievements(self, player_id):
        """{(achievement key, tier): earned_at} of a player."""
        return {(key, tier): at for key, tier, at in self._fetchall(
            "SELECT achievement, tier, earned_at FROM player_achievements WHERE player_id = %s", (player_id,))}

    ###----------------------------- Competitions ------------------------------------###

    _COMPETITION_COLUMNS = "id, server_id, title, metric, starts_on, ends_on, created_by, start_announced, end_announced"

    def _competition(self, row):
        keys = [c.strip() for c in self._COMPETITION_COLUMNS.split(",")]
        return dict(zip(keys, row))

    def create_competition(self, server_id, title, metric, starts_on, ends_on, created_by=None):
        return self._fetchvalue("""INSERT INTO competitions (server_id, title, metric, starts_on, ends_on, created_by)
                                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                                (server_id, title, metric, starts_on, ends_on, created_by))

    def delete_competition(self, server_id, competition_id):
        return self._execute("DELETE FROM competitions WHERE id = %s AND server_id = %s",
                             (competition_id, server_id)) > 0

    def list_competitions(self, server_id):
        """All competitions of a server, newest start first."""
        return [self._competition(row) for row in self._fetchall(
            f"SELECT {self._COMPETITION_COLUMNS} FROM competitions WHERE server_id = %s ORDER BY starts_on DESC, id DESC",
            (server_id,))]

    def get_competition(self, competition_id):
        row = self._fetchone(f"SELECT {self._COMPETITION_COLUMNS} FROM competitions WHERE id = %s", (competition_id,))
        return self._competition(row) if row else None

    def get_competition_standings(self, competition):
        """[{"player_id", "name", "uuid", "value"}] of a competition, best first (players with a gain only)."""
        today = self.get_today()
        if competition["starts_on"] > today:
            return []
        gains = self.get_metrics_between(competition["server_id"], competition["starts_on"],
                                         min(competition["ends_on"], today))
        names = {str(pid): (name, str(uuid)) for pid, name, uuid in self._fetchall(
            """SELECT psi.player_id, p.name, psi.mojang_uuid FROM player_server_info psi
               JOIN player p ON p.uuid = psi.mojang_uuid
               WHERE psi.server_id = %s AND NOT psi.hide_stats""", (competition["server_id"],))}
        rows = [{"player_id": pid, "name": names[pid][0], "uuid": names[pid][1], "value": values.get(competition["metric"], 0)}
                for pid, values in gains.items() if pid in names]
        return sorted((r for r in rows if r["value"] > 0), key=lambda r: (-r["value"], r["name"].lower()))

    def take_due_competition_announcements(self, server_ids):
        """
        Competitions of the given servers that started or ended since the last call, marked
        as announced: [("start" | "end", competition)]. A competition ends after its last day.
        """
        result = []
        with self._cursor() as cur:
            cur.execute(f"""UPDATE competitions SET start_announced = true
                            WHERE server_id = ANY(%s) AND NOT start_announced
                              AND starts_on <= current_date AND ends_on >= current_date
                            RETURNING {self._COMPETITION_COLUMNS}""", (list(server_ids),))
            result += [("start", self._competition(row)) for row in cur.fetchall()]
            cur.execute(f"""UPDATE competitions SET end_announced = true, start_announced = true
                            WHERE server_id = ANY(%s) AND NOT end_announced AND ends_on < current_date
                            RETURNING {self._COMPETITION_COLUMNS}""", (list(server_ids),))
            result += [("end", self._competition(row)) for row in cur.fetchall()]
        return result

    def get_snapshot_start(self, server_id):
        """The day of the first snapshot on the server, or None."""
        return self._fetchvalue("""SELECT min(s.day) FROM stat_snapshots s
                                   JOIN player_server_info psi ON psi.player_id = s.player_id
                                   WHERE psi.server_id = %s""", (server_id,))

    def get_metric_history(self, player_ids, days):
        """
        (dates, {player_id: {metric key: [gain per day or None]}}) for the last `days` days
        up to today. A day's value is the gain since the start of the range; None before the
        first snapshot of the player.
        """
        rows = self._fetchall("""
            SELECT player_id, metric, day, value, current_date FROM stat_snapshots
            WHERE player_id = ANY(%s::uuid[]) AND metric = ANY(%s)
            ORDER BY player_id, metric, day""", (list(player_ids), list(metrics_mod.METRICS_BY_KEY)))
        today = rows[0][4] if rows else self._fetchvalue("SELECT current_date")
        dates = [today - timedelta(days=offset) for offset in range(days, -1, -1)]
        series = {}
        for player_id, metric, day, value, _ in rows:
            series.setdefault(str(player_id), {}).setdefault(metric, []).append((day, value))

        history = {}
        for player_id in player_ids:
            history[player_id] = {}
            for key in metrics_mod.METRICS_BY_KEY:
                points = series.get(player_id, {}).get(key, [])
                values, index, last = [], 0, None
                for date in dates:
                    while index < len(points) and points[index][0] <= date:
                        last = points[index][1]
                        index += 1
                    values.append(last)
                base = next((v for v in values if v is not None), None)
                history[player_id][key] = [None if v is None else max(0, v - base) for v in values]
        return dates, history

    def get_top_objects(self, player_ids, category, limit=10):
        """
        The objects of a category with the highest sum over the given players:
        [(object, {player_id: value})], e.g. the most mined blocks of the compared players.
        """
        rows = self._fetchall("""
            WITH top AS (
                SELECT object FROM actions
                WHERE player_id = ANY(%s::uuid[]) AND category = %s
                GROUP BY object ORDER BY sum(value) DESC, object LIMIT %s)
            SELECT a.object, a.player_id, a.value FROM actions a JOIN top USING (object)
            WHERE a.player_id = ANY(%s::uuid[]) AND a.category = %s""",
                              (list(player_ids), category, limit, list(player_ids), category))
        totals = {}
        for obj, player_id, value in rows:
            totals.setdefault(obj, {})[str(player_id)] = value
        return sorted(totals.items(), key=lambda item: (-sum(item[1].values()), item[0]))

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

    def listen_for_login_pins(self, callback, stop_event=None, poll_timeout=1.0, event_callback=None):
        """
        Block and call callback(server_id, mojang_uuid, pin) for every new login pin, and
        event_callback(payload_dict) for server events (prefix, ban, unban), until stop_event
        is set. Uses a dedicated connection (LISTEN needs autocommit).
        """
        stop_event = stop_event or threading.Event()
        conn = psycopg2.connect(**self.db_config)
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        try:
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {LOGIN_PIN_CHANNEL};")
                if event_callback:
                    cur.execute(f"LISTEN {EVENTS_CHANNEL};")
            logger.info("Listening for login pins and server events")
            while not stop_event.is_set():
                if select.select([conn], [], [], poll_timeout) == ([], [], []):
                    continue
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    try:
                        payload = json.loads(notify.payload)
                        if notify.channel == LOGIN_PIN_CHANNEL:
                            callback(payload["server_id"], payload["mojang_uuid"], payload["pin"])
                        elif event_callback:
                            event_callback(payload)
                    except Exception:
                        logger.exception(f"Failed to handle notification on {notify.channel}: {notify.payload}")
        finally:
            conn.close()

    ################################# helper functions #######################################

    def format_time(self, seconds):
        return stats_mod.format_time(seconds)


if __name__ == "__main__":
    db = DatabaseManager()
    print(f"Connected, schema version {db.get_schema_version()}")
