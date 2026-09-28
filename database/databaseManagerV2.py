import functools
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
import psycopg2.errors
import psycopg2.extensions
from psycopg2.pool import ThreadedConnectionPool
from colorlogx import get_logger

from . import config
from . import achievements as achievements_mod
from . import metrics as metrics_mod
from . import motivation
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
    13: [
        # streak badges (days online in a row) and anniversaries, reached once per player
        """CREATE TABLE player_milestones(
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             kind text NOT NULL CHECK (kind IN ('streak', 'anniversary')),
             value integer NOT NULL,
             reached_at timestamptz NOT NULL DEFAULT now(),
             silent boolean NOT NULL DEFAULT false,
             PRIMARY KEY (player_id, kind, value))""",
        "CREATE INDEX player_milestones_reached_idx ON player_milestones (reached_at)",
        # every change of the best player of a metric; the newest row per metric is the current record
        """CREATE TABLE record_history(
             id bigserial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             metric text NOT NULL,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             value bigint NOT NULL,
             since timestamptz NOT NULL DEFAULT now(),
             silent boolean NOT NULL DEFAULT false)""",
        "CREATE INDEX record_history_server_idx ON record_history (server_id, metric, since DESC)",
        # server-wide goals: all players together gain `target` of a metric from starts_on on
        """CREATE TABLE community_goals(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             title text NOT NULL,
             metric text NOT NULL,
             target bigint NOT NULL CHECK (target > 0),
             starts_on date NOT NULL,
             ends_on date CHECK (ends_on IS NULL OR ends_on >= starts_on),
             created_by text,
             created_at timestamptz NOT NULL DEFAULT now(),
             reached_at timestamptz)""",
        "CREATE INDEX community_goals_server_idx ON community_goals (server_id)",
        # competition places and players of the week (kept, the snapshots they come from are not)
        """CREATE TABLE trophies(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             kind text NOT NULL CHECK (kind IN ('competition', 'player_of_week')),
             ref text NOT NULL,
             place smallint NOT NULL DEFAULT 1,
             title text NOT NULL,
             detail text,
             awarded_at timestamptz NOT NULL DEFAULT now(),
             UNIQUE (server_id, kind, ref, player_id))""",
        "CREATE INDEX trophies_player_idx ON trophies (player_id)",
        "ALTER TABLE competitions ADD COLUMN awarded boolean NOT NULL DEFAULT false",
        # the last Sunday up to which the player of the week was chosen
        "ALTER TABLE servers ADD COLUMN weekly_awarded_until date",
    ],
    14: [
        # scoreboard sidebar in the game: off, standings of the running competition or the own play time
        """ALTER TABLE player_server_info ADD COLUMN sidebar text NOT NULL DEFAULT 'off'
             CHECK (sidebar IN ('off', 'competition', 'playtime'))""",
        # 1 vs 1: who gains more of a metric in `days` days after the challenge was accepted
        """CREATE TABLE duels(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             challenger_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             opponent_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             metric text NOT NULL,
             days smallint NOT NULL CHECK (days BETWEEN 1 AND 7),
             status text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'finished', 'declined', 'expired')),
             created_at timestamptz NOT NULL DEFAULT now(),
             starts_at timestamptz,
             ends_at timestamptz,
             challenger_start bigint,
             opponent_start bigint,
             challenger_gain bigint,
             opponent_gain bigint,
             CHECK (challenger_id <> opponent_id))""",
        "CREATE INDEX duels_server_idx ON duels (server_id, status)",
        # reports of players (in the game with /report or on the website) for the moderators
        """CREATE TABLE reports(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             reporter_id uuid REFERENCES player_server_info (player_id) ON DELETE SET NULL,
             target_name text,
             reason text NOT NULL,
             world text,
             x integer,
             y integer,
             z integer,
             source text NOT NULL DEFAULT 'ingame' CHECK (source IN ('ingame', 'web')),
             created_at timestamptz NOT NULL DEFAULT now(),
             handled_by text,
             handled_at timestamptz)""",
        "CREATE INDEX reports_server_idx ON reports (server_id, created_at DESC)",
    ],
    15: [
        # event calendar: reminder in the chat before the start, players sign up
        """CREATE TABLE events(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             title text NOT NULL,
             description text,
             place text,
             starts_at timestamptz NOT NULL,
             created_by text,
             created_at timestamptz NOT NULL DEFAULT now(),
             reminded boolean NOT NULL DEFAULT false,
             start_announced boolean NOT NULL DEFAULT false)""",
        "CREATE INDEX events_server_idx ON events (server_id, starts_at)",
        """CREATE TABLE event_signups(
             event_id integer NOT NULL REFERENCES events (id) ON DELETE CASCADE,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             created_at timestamptz NOT NULL DEFAULT now(),
             PRIMARY KEY (event_id, player_id))""",
        # polls: one answer per player, on the website or with /vote
        """CREATE TABLE polls(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             question text NOT NULL,
             options text[] NOT NULL CHECK (cardinality(options) BETWEEN 2 AND 8),
             ends_at timestamptz NOT NULL,
             created_by text,
             created_at timestamptz NOT NULL DEFAULT now(),
             result_announced boolean NOT NULL DEFAULT false)""",
        "CREATE INDEX polls_server_idx ON polls (server_id, ends_at)",
        """CREATE TABLE poll_votes(
             poll_id integer NOT NULL REFERENCES polls (id) ON DELETE CASCADE,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             option smallint NOT NULL,
             voted_at timestamptz NOT NULL DEFAULT now(),
             PRIMARY KEY (poll_id, player_id))""",
        # build gallery: screenshots of the players, shown after a moderator approved them
        """CREATE TABLE builds(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             title text NOT NULL,
             description text,
             coordinates text,
             filename text NOT NULL UNIQUE,
             status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved')),
             created_at timestamptz NOT NULL DEFAULT now(),
             reviewed_by text,
             reviewed_at timestamptz)""",
        "CREATE INDEX builds_server_idx ON builds (server_id, status, created_at DESC)",
        """CREATE TABLE build_likes(
             build_id integer NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             PRIMARY KEY (build_id, player_id))""",
        # guestbook on the player page
        """CREATE TABLE guestbook(
             id serial PRIMARY KEY,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             author_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             text text NOT NULL,
             created_at timestamptz NOT NULL DEFAULT now(),
             reported_at timestamptz)""",
        "CREATE INDEX guestbook_player_idx ON guestbook (player_id, created_at DESC)",
    ],
    16: [
        # whitelist access: by application, by invite code, both or off (the server's own whitelist only)
        """ALTER TABLE servers ADD COLUMN access_mode text NOT NULL DEFAULT 'off'
             CHECK (access_mode IN ('off', 'application', 'code', 'both'))""",
        # warnings: a ban of warn_ban_days days after warn_threshold warnings (0 = never)
        "ALTER TABLE servers ADD COLUMN warn_threshold smallint NOT NULL DEFAULT 3",
        "ALTER TABLE servers ADD COLUMN warn_ban_days smallint NOT NULL DEFAULT 7",
        # rules and FAQ page (optional), linked in the chat on the first join
        "ALTER TABLE servers ADD COLUMN rules_enabled boolean NOT NULL DEFAULT false",
        "ALTER TABLE servers ADD COLUMN rules text",
        "ALTER TABLE servers ADD COLUMN faq text",
        # e-mail to the owner when the server is offline or lags; *_at: when the last alert was sent
        "ALTER TABLE servers ADD COLUMN alerts_enabled boolean NOT NULL DEFAULT true",
        "ALTER TABLE servers ADD COLUMN alert_offline_at timestamptz",
        "ALTER TABLE servers ADD COLUMN alert_tps_at timestamptz",
        """CREATE TABLE access_requests(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             name text NOT NULL,
             message text,
             kind text NOT NULL CHECK (kind IN ('application', 'code')),
             status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
             created_at timestamptz NOT NULL DEFAULT now(),
             handled_by text,
             handled_at timestamptz,
             synced boolean NOT NULL DEFAULT false)""",
        "CREATE INDEX access_requests_server_idx ON access_requests (server_id, status, created_at DESC)",
        """CREATE TABLE invite_codes(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             code text NOT NULL,
             max_uses integer CHECK (max_uses IS NULL OR max_uses > 0),
             uses integer NOT NULL DEFAULT 0,
             expires_at timestamptz,
             created_by text,
             created_at timestamptz NOT NULL DEFAULT now())""",
        "CREATE UNIQUE INDEX invite_codes_code_idx ON invite_codes (server_id, upper(code))",
        """CREATE TABLE warnings(
             id serial PRIMARY KEY,
             server_id integer NOT NULL REFERENCES servers (id) ON DELETE CASCADE,
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             reason text NOT NULL,
             created_by text,
             created_at timestamptz NOT NULL DEFAULT now())""",
        "CREATE INDEX warnings_player_idx ON warnings (player_id, created_at)",
        "ALTER TABLE player_server_info ADD COLUMN muted_until timestamptz",
        "ALTER TABLE player_server_info ADD COLUMN mute_reason text",
    ],
    17: [
        # public server directory on the main domain (owners can opt out)
        "ALTER TABLE servers ADD COLUMN listed boolean NOT NULL DEFAULT true",
    ],
    18: [
        # when a website ban reached the plugin (NULL: not yet, sent on the next connect); a delivered ban
        # that is missing from the plugin's ban list was lifted in the game (/pardon)
        "ALTER TABLE banned_players ADD COLUMN delivered_at timestamptz",
        "UPDATE banned_players SET delivered_at = now() WHERE source = 'web'",
    ],
    19: [
        # A player's first snapshot is also stored as the day before (baseline): gains since the first sync
        # then count from that sync on. Before, a player first seen today had no baseline and every gain of
        # the day (today, competitions and goals starting today, sidebar) stayed 0. For existing players the
        # baseline is their current value, so gains count from now on.
        """INSERT INTO stat_snapshots (player_id, metric, day, value)
           SELECT DISTINCT ON (player_id, metric) player_id, metric, day - 1, value FROM stat_snapshots
           ORDER BY player_id, metric, day
           ON CONFLICT DO NOTHING""",
        # the challenger can withdraw a challenge that was not answered yet
        "ALTER TABLE duels DROP CONSTRAINT duels_status_check",
        """ALTER TABLE duels ADD CONSTRAINT duels_status_check
             CHECK (status IN ('pending', 'running', 'finished', 'declined', 'expired', 'cancelled'))""",
    ],
    20: [
        # join/leave messages and reward levels (database/rewards.py): levels of the server (NULL = default),
        # the player's choice, the highest level reached (never lowered) and whose messages a player mutes
        "ALTER TABLE servers ADD COLUMN rewards_enabled boolean NOT NULL DEFAULT true",
        "ALTER TABLE servers ADD COLUMN reward_levels jsonb",
        "ALTER TABLE player_server_info ADD COLUMN join_style jsonb",
        "ALTER TABLE player_server_info ADD COLUMN reward_level smallint NOT NULL DEFAULT 0",
        "ALTER TABLE player_server_info ADD COLUMN join_sounds_off boolean NOT NULL DEFAULT false",
        """CREATE TABLE join_mutes(
             player_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             muted_id uuid NOT NULL REFERENCES player_server_info (player_id) ON DELETE CASCADE,
             PRIMARY KEY (player_id, muted_id))""",
    ],
}
MAX_GALLERY_IMAGES = 12
# A session that ended less than this ago is continued on the next join (plugin reconnects).
SESSION_MERGE_SECONDS = 120
# An achievement counts as reached while playing if the player is online or left at most this
# long ago (the plugin sends the stats shortly after a quit). Otherwise it is stored silently.
ACHIEVEMENT_ACTIVE_MINUTES = 10
# A record that changes hands again within this time is not announced (two players passing
# each other while playing together); taking it straight back undoes the change.
RECORD_COOLDOWN_MINUTES = 60
# A duel challenge that is not accepted within this time expires.
DUEL_ACCEPT_HOURS = 24
# At most this many reports per player and hour.
MAX_REPORTS_PER_HOUR = 5
# The chat reminds of an event this long before it starts.
EVENT_REMINDER_MINUTES = 30
# Uploads to the build gallery per player: waiting for approval / per day.
MAX_PENDING_BUILDS = 3
MAX_BUILDS_PER_DAY = 5
# Guestbook entries a player may write per hour.
MAX_GUESTBOOK_PER_HOUR = 10
# Open applications per server (spam protection of the public form).
MAX_PENDING_APPLICATIONS = 50
# Alerts: the server counts as down after this time without plugin, as lagging after this many
# minutes with a TPS below ALERT_TPS; at most one mail of a kind per ALERT_REPEAT_HOURS.
ALERT_OFFLINE_MINUTES = 5
ALERT_TPS = 15.0
ALERT_TPS_MINUTES = 5
ALERT_REPEAT_HOURS = 6
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


@functools.lru_cache(maxsize=1)
def _dummy_hash():
    return ph.hash(secrets.token_hex(16))
logger = get_logger("databaseManager")


def generate_secure_token(length=64):
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))


class DatabaseNotInitializedError(RuntimeError):
    pass


class DatabaseManager:
    """Thread-safe access to the MCConnect database.

    Every method borrows its own connection from a pool, so one instance can
    be shared between Flask request threads and socket threads.
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
            cur.execute("SELECT NOT EXISTS (SELECT 1 FROM stat_snapshots WHERE player_id = %s)", (player_id,))
            if cur.fetchone()[0]:
                # first sync: the same values as the day before are the baseline, so everything gained
                # from now on counts (today, and in competitions and goals that start today)
                cur.executemany("""INSERT INTO stat_snapshots (player_id, metric, day, value)
                                   VALUES (%s, %s, current_date - 1, %s) ON CONFLICT DO NOTHING""", snapshot)
            cur.executemany("""
                INSERT INTO stat_snapshots (player_id, metric, day, value) VALUES (%s, %s, current_date, %s)
                ON CONFLICT (player_id, metric, day) DO UPDATE SET value = EXCLUDED.value
                WHERE stat_snapshots.value IS DISTINCT FROM EXCLUDED.value""", snapshot)
            # Keep the newest snapshot before today even if it is old: it is the baseline
            # for players that come back after a long break. The first and the last snapshot of
            # every year are kept for the year in review.
            cur.execute("""
                DELETE FROM stat_snapshots s
                WHERE s.player_id = %s AND s.day < current_date - %s
                  AND EXISTS (SELECT 1 FROM stat_snapshots n
                              WHERE n.player_id = s.player_id AND n.metric = s.metric
                                AND n.day > s.day AND n.day < current_date)
                  AND EXISTS (SELECT 1 FROM stat_snapshots e WHERE e.player_id = s.player_id AND e.metric = s.metric
                                AND e.day < s.day AND date_trunc('year', e.day) = date_trunc('year', s.day))
                  AND EXISTS (SELECT 1 FROM stat_snapshots l WHERE l.player_id = s.player_id AND l.metric = s.metric
                                AND l.day > s.day AND date_trunc('year', l.day) = date_trunc('year', s.day))""",
                        (player_id, SNAPSHOT_RETENTION_DAYS))
        logger.info(f'Updated stats of player "{player_id}" ({len(data)} values)')
        return len(data)

    def register_player_join(self, server_id, mojang_uuid, name=None):
        """Mark the player online on the server, creating it if needed. Returns the player_id."""
        return self.register_player_join_info(server_id, mojang_uuid, name)[0]

    def register_player_join_info(self, server_id, mojang_uuid, name=None):
        """Like register_player_join, returns (player_id, first join on this server)."""
        player_id = self.ensure_player_on_server(server_id, mojang_uuid, name)
        with self._cursor() as cur:
            cur.execute("SELECT first_seen IS NULL FROM player_server_info WHERE player_id = %s", (player_id,))
            first = cur.fetchone()[0]
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
        return player_id, first

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

    def ban_player(self, server_id, player_name, banned_by, reason_id=None, days=None, comment=None, reason_text=None):
        """
        Web ban by player name. days=None uses the reason's default duration, days=0 is permanent.
        reason_text: own reason shown to the player instead of the reason's name.
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
            cur.execute("""INSERT INTO banned_players (banned_player_id, ban_reason_id, ban_end, comment, banned_by, source,
                                                       reason_text)
                           VALUES (%s, %s, %s, %s, %s, 'web', %s)""",
                        (player_id, reason_id, end, comment, banned_by, reason_text))
        return {"uuid": str(uuid), "name": name, "reason": reason_text or reason, "end": end}

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
            SELECT psi.player_id, p.name, psi.mojang_uuid, bp.source, COALESCE(bp.reason_text, br.reason),
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

    # A delivered ban must be missing from the plugin's list for this long before it counts as lifted
    # in the game (the plugin may not have applied a brand-new ban yet).
    PARDON_GRACE_SECONDS = 120

    def mark_ban_delivered(self, server_id, mojang_uuid):
        self._execute(f"""UPDATE banned_players bp SET delivered_at = now() FROM player_server_info psi
                          WHERE psi.player_id = bp.banned_player_id AND psi.server_id = %s AND psi.mojang_uuid = %s
                            AND bp.source = 'web' AND bp.delivered_at IS NULL AND {self._ACTIVE_BAN}""",
                      (server_id, mojang_uuid))

    def get_undelivered_web_bans(self, server_id):
        """[{"uuid", "name", "reason", "end"}] of active website bans the plugin has not received yet."""
        rows = self._fetchall(f"""
            SELECT psi.mojang_uuid, p.name, COALESCE(bp.reason_text, br.reason, 'Gebannt'), bp.ban_end
            FROM banned_players bp JOIN player_server_info psi ON psi.player_id = bp.banned_player_id
            JOIN player p ON p.uuid = psi.mojang_uuid LEFT JOIN ban_reasons br ON br.id = bp.ban_reason_id
            WHERE psi.server_id = %s AND bp.source = 'web' AND bp.delivered_at IS NULL AND {self._ACTIVE_BAN}""",
                              (server_id,))
        return [{"uuid": str(u), "name": n, "reason": r, "end": e} for u, n, r, e in rows]

    def sync_web_bans(self, server_id, names):
        """
        The plugin reported the names of MCConnect bans still in the server's ban list. Delivered active
        website bans of other players were lifted in the game: they are removed here as well.
        Returns the names of the players pardoned that way.
        """
        with self._cursor() as cur:
            cur.execute(f"""DELETE FROM banned_players bp USING player_server_info psi, player p
                            WHERE bp.banned_player_id = psi.player_id AND p.uuid = psi.mojang_uuid
                              AND psi.server_id = %s AND bp.source = 'web' AND {self._ACTIVE_BAN}
                              AND bp.delivered_at < now() - %s * interval '1 second'
                              AND NOT (lower(p.name) = ANY(%s))
                            RETURNING p.name""", (server_id, self.PARDON_GRACE_SECONDS, [n.lower() for n in names]))
            pardoned = sorted({row[0] for row in cur.fetchall()})
            for name in pardoned:
                self._log(cur, server_id, None, "ingame_pardon", name)
        return pardoned

    def get_ban_reason_from_player_id(self, player_id):
        """Reason of the currently active ban (or "Gebannt" without a reason), or None if not banned."""
        row = self._fetchone(f"""SELECT COALESCE(bp.reason_text, br.reason, 'Gebannt') FROM banned_players bp
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
        allowed = {"server_name", "mc_server_domain", "discord_url", "whitelist", "auto_mod_ops", "alerts_enabled", "listed",
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

    def get_now(self):
        return self._fetchvalue("SELECT now()")

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
        kind: join, new, achievement, competition_start, competition_end, record (detail: metric),
        streak / anniversary (detail: days / years), goal (detail: title), player_of_week (detail: week).
        Players who hide their stats, silent achievements, records and milestones and bulks of
        achievements (more than 2 at once) are left out.
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
                UNION ALL
                SELECT rh.since, 'record', v.name, v.mojang_uuid::text, rh.metric, NULL FROM record_history rh
                JOIN visible v USING (player_id) WHERE rh.server_id = %s AND NOT rh.silent
                  AND rh.since > now() - %s * interval '1 day'
                UNION ALL
                SELECT pm.reached_at, pm.kind, v.name, v.mojang_uuid::text, pm.value::text, NULL FROM player_milestones pm
                JOIN visible v USING (player_id) WHERE NOT pm.silent AND pm.reached_at > now() - %s * interval '1 day'
                UNION ALL
                SELECT g.reached_at, 'goal', NULL, NULL, g.title, NULL FROM community_goals g
                WHERE g.server_id = %s AND g.reached_at > now() - %s * interval '1 day'
                UNION ALL
                SELECT t.awarded_at, 'player_of_week', v.name, v.mojang_uuid::text, t.title, NULL FROM trophies t
                JOIN visible v USING (player_id) WHERE t.kind = 'player_of_week' AND t.awarded_at > now() - %s * interval '1 day'
            ) events ORDER BY at DESC LIMIT %s""", (server_id, days, days, server_id, days, server_id, days,
                                                   server_id, days, days, server_id, days, days, limit))
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

    def get_new_achievements(self, player_id, since):
        """[(achievement key, tier)] reached while playing after `since` (not silent), oldest first."""
        return self._fetchall("""SELECT achievement, tier FROM player_achievements
                                 WHERE player_id = %s AND earned_at > %s AND NOT silent
                                 ORDER BY earned_at, tier LIMIT 5""", (player_id, since))

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

    ###----------------------------- Streaks and milestones ------------------------------------###

    # Runs of days in a row with a session (a session over midnight counts for both days).
    _STREAK_RUNS = """
        WITH days AS (
            SELECT DISTINCT ps.player_id, d::date AS day
            FROM player_sessions ps JOIN player_server_info psi ON psi.player_id = ps.player_id
            CROSS JOIN LATERAL generate_series(ps.started_at::date, COALESCE(ps.ended_at, now())::date,
                                               interval '1 day') AS d
            WHERE {where}),
        runs AS (
            SELECT player_id, min(day) AS first, max(day) AS last, count(*)::int AS length
            FROM (SELECT player_id, day, day - (row_number() OVER (PARTITION BY player_id ORDER BY day))::int AS grp
                  FROM days) numbered
            GROUP BY player_id, grp)
        SELECT player_id, max(length),
               COALESCE(max(length) FILTER (WHERE last >= current_date - 1), 0),
               (array_agg(first ORDER BY length DESC, last DESC))[1],
               (array_agg(last ORDER BY length DESC, last DESC))[1]
        FROM runs GROUP BY player_id"""

    @staticmethod
    def _streak(row):
        return {"best": row[1], "current": row[2], "best_first": row[3], "best_last": row[4]}

    def get_streaks(self, server_id, include_hidden=False):
        """
        {player_id: {"best", "current", "best_first", "best_last"}} of the players with any session.
        current counts while the player was online today or yesterday (the streak can still go on).
        """
        rows = self._fetchall(self._STREAK_RUNS.format(where="psi.server_id = %s AND (%s OR NOT psi.hide_stats)"),
                              (server_id, include_hidden))
        return {str(row[0]): self._streak(row) for row in rows}

    def get_player_streak(self, player_id):
        row = self._fetchone(self._STREAK_RUNS.format(where="ps.player_id = %s"), (player_id,))
        return self._streak(row) if row else {"best": 0, "current": 0, "best_first": None, "best_last": None}

    def _played_recently(self, cur, player_id):
        """Online now or left at most ACHIEVEMENT_ACTIVE_MINUTES ago (news worth announcing)."""
        cur.execute("""SELECT EXISTS (SELECT 1 FROM player_sessions WHERE player_id = %s
                                        AND COALESCE(ended_at, now()) >= now() - %s * interval '1 minute')""",
                    (player_id, ACHIEVEMENT_ACTIVE_MINUTES))
        return cur.fetchone()[0]

    def check_milestones(self, player_id):
        """
        Store the streak badges and the anniversary the player has reached. Returns [(kind, value)]
        of the new ones to announce: streak badges reached with the current streak and anniversaries,
        both only while playing. Badges of an older streak are stored silently.
        """
        streak = self.get_player_streak(player_id)
        with self._cursor() as cur:
            cur.execute("""SELECT extract(year FROM age(current_date, first_seen::date))::int
                           FROM player_server_info WHERE player_id = %s""", (player_id,))
            row = cur.fetchone()
            years = (row[0] or 0) if row else 0
            active = self._played_recently(cur, player_id)
            reached = [("streak", n, not active or streak["current"] < n)
                       for n in motivation.STREAK_MILESTONES if streak["best"] >= n]
            if years >= 1:
                reached.append(("anniversary", years, not active))
            if not reached:
                return []
            cur.execute("""INSERT INTO player_milestones (player_id, kind, value, silent)
                           SELECT %s, kind, value, silent
                           FROM unnest(%s::text[], %s::int[], %s::boolean[]) AS r(kind, value, silent)
                           ON CONFLICT DO NOTHING RETURNING kind, value, silent""",
                        (player_id, [r[0] for r in reached], [r[1] for r in reached], [r[2] for r in reached]))
            rows = cur.fetchall()
        return sorted((kind, value) for kind, value, silent in rows if not silent)

    def get_player_milestones(self, player_id):
        """[{"kind", "value", "reached_at"}] of a player, streaks first."""
        rows = self._fetchall("""SELECT kind, value, reached_at FROM player_milestones WHERE player_id = %s
                                 ORDER BY kind DESC, value""", (player_id,))
        return [dict(zip(("kind", "value", "reached_at"), row)) for row in rows]

    def get_server_milestones(self, server_id):
        """{player_id: {"streak": highest badge, "anniversary": years}} of the visible players."""
        result = {}
        for player_id, kind, value in self._fetchall("""
                SELECT pm.player_id, pm.kind, max(pm.value) FROM player_milestones pm
                JOIN player_server_info psi ON psi.player_id = pm.player_id
                WHERE psi.server_id = %s AND NOT psi.hide_stats GROUP BY pm.player_id, pm.kind""", (server_id,)):
            result.setdefault(str(player_id), {})[kind] = value
        return result

    ###----------------------------- Records ------------------------------------###

    def update_records(self, player_id):
        """
        Check the player's values against the records of the server (record_history) after new stats.
        Returns [(metric, previous holder's name, value)] of the records the player took while playing
        (to announce). The first record of a metric is taken from all players and stored silently.
        Players who hide their stats take no records.
        """
        info = self._fetchone("SELECT server_id, hide_stats FROM player_server_info WHERE player_id = %s", (player_id,))
        if info is None or info[1]:
            return []
        server_id = info[0]
        values = self.get_player_metrics(player_id)
        keys = [m.key for m in motivation.RECORD_METRICS]
        events = []
        with self._cursor() as cur:
            cur.execute("SELECT id FROM servers WHERE id = %s FOR UPDATE", (server_id,))  # one check at a time
            current = self._current_records(cur, server_id)
            missing = [key for key in keys if key not in current]
            if missing:
                best = {}
                for p in self.get_server_metrics(server_id):
                    for key in missing:
                        if p["values"][key] > best.get(key, (None, 0))[1]:
                            best[key] = (p["player_id"], p["values"][key])
                for key, (holder, value) in best.items():
                    cur.execute("""INSERT INTO record_history (server_id, metric, player_id, value, silent)
                                   VALUES (%s, %s, %s, %s, true)""", (server_id, key, holder, value))
                current = self._current_records(cur, server_id)
            active = None
            for key in keys:
                value, record = values[key], current.get(key)
                if record is None or value <= record["value"]:
                    continue
                if record["player_id"] == str(player_id):
                    cur.execute("UPDATE record_history SET value = %s WHERE id = %s", (value, record["id"]))
                    continue
                cur.execute("""SELECT id, player_id FROM record_history
                               WHERE server_id = %s AND metric = %s AND (since, id) < (%s, %s)
                               ORDER BY since DESC, id DESC LIMIT 1""", (server_id, key, record["since"], record["id"]))
                before = cur.fetchone()
                recent = record["age_minutes"] < RECORD_COOLDOWN_MINUTES and before is not None
                if recent and str(before[1]) == str(player_id):
                    # taken straight back: the short change in between did not happen
                    cur.execute("DELETE FROM record_history WHERE id = %s", (record["id"],))
                    cur.execute("UPDATE record_history SET value = %s WHERE id = %s", (value, before[0]))
                    continue
                if active is None:
                    active = self._played_recently(cur, player_id)
                silent = recent or not active
                cur.execute("""INSERT INTO record_history (server_id, metric, player_id, value, silent)
                               VALUES (%s, %s, %s, %s, %s)""", (server_id, key, player_id, value, silent))
                if not silent:
                    cur.execute("""SELECT p.name FROM player p JOIN player_server_info psi ON psi.mojang_uuid = p.uuid
                                   WHERE psi.player_id = %s""", (record["player_id"],))
                    events.append((metrics_mod.METRICS_BY_KEY[key], cur.fetchone()[0], value))
        return events

    @staticmethod
    def _current_records(cur, server_id):
        cur.execute("""SELECT DISTINCT ON (metric) id, metric, player_id, value, since,
                              extract(epoch FROM now() - since) / 60
                       FROM record_history WHERE server_id = %s ORDER BY metric, since DESC, id DESC""", (server_id,))
        return {metric: {"id": rid, "player_id": str(pid), "value": value, "since": since, "age_minutes": float(age)}
                for rid, metric, pid, value, since, age in cur.fetchall()}

    def get_current_records(self, server_id):
        """
        {metric: {"player_id", "name", "uuid", "value", "since", "first"}} of the current record holders
        (players who hide their stats are left out). first: the record is the first one recorded.
        """
        rows = self._fetchall("""
            WITH cur AS (SELECT DISTINCT ON (metric) id, metric, player_id, value, since FROM record_history
                         WHERE server_id = %s ORDER BY metric, since DESC, id DESC)
            SELECT cur.metric, cur.player_id, p.name, psi.mojang_uuid, cur.value, cur.since,
                   NOT EXISTS (SELECT 1 FROM record_history e WHERE e.server_id = %s AND e.metric = cur.metric
                                                              AND (e.since, e.id) < (cur.since, cur.id))
            FROM cur JOIN player_server_info psi ON psi.player_id = cur.player_id
            JOIN player p ON p.uuid = psi.mojang_uuid
            WHERE NOT psi.hide_stats""", (server_id, server_id))
        return {row[0]: {"player_id": str(row[1]), "name": row[2], "uuid": str(row[3]), "value": row[4],
                         "since": row[5], "first": row[6]} for row in rows}

    def get_record_history(self, server_id, limit=30):
        """
        Record changes, newest first: [{"at", "metric", "name", "uuid", "value", "previous"}]
        (without the first record of each metric; previous is None if that player hides the stats).
        """
        rows = self._fetchall("""
            WITH h AS (SELECT rh.*, lag(rh.player_id) OVER (PARTITION BY rh.metric ORDER BY rh.since, rh.id) AS prev_id
                       FROM record_history rh WHERE rh.server_id = %s)
            SELECT h.since, h.metric, p.name, psi.mojang_uuid, h.value, CASE WHEN ppsi.hide_stats THEN NULL ELSE pp.name END
            FROM h JOIN player_server_info psi ON psi.player_id = h.player_id
            JOIN player p ON p.uuid = psi.mojang_uuid
            JOIN player_server_info ppsi ON ppsi.player_id = h.prev_id
            JOIN player pp ON pp.uuid = ppsi.mojang_uuid
            WHERE NOT psi.hide_stats
            ORDER BY h.since DESC, h.id DESC LIMIT %s""", (server_id, limit))
        return [{"at": at, "metric": metric, "name": name, "uuid": str(uuid), "value": value, "previous": previous}
                for at, metric, name, uuid, value, previous in rows]

    ###----------------------------- Community goals ------------------------------------###

    _GOAL_COLUMNS = "id, server_id, title, metric, target, starts_on, ends_on, created_by, reached_at"

    def _goal(self, row):
        return dict(zip([c.strip() for c in self._GOAL_COLUMNS.split(",")], row))

    def create_goal(self, server_id, title, metric, target, starts_on, ends_on=None, created_by=None):
        return self._fetchvalue("""INSERT INTO community_goals (server_id, title, metric, target, starts_on, ends_on, created_by)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                                (server_id, title, metric, target, starts_on, ends_on, created_by))

    def delete_goal(self, server_id, goal_id):
        """Returns the title of the deleted goal, or None."""
        return self._fetchvalue("DELETE FROM community_goals WHERE id = %s AND server_id = %s RETURNING title",
                                (goal_id, server_id))

    def list_goals(self, server_id):
        """All goals of a server, newest start first."""
        return [self._goal(row) for row in self._fetchall(
            f"SELECT {self._GOAL_COLUMNS} FROM community_goals WHERE server_id = %s ORDER BY starts_on DESC, id DESC",
            (server_id,))]

    def get_goal_progress(self, goal, today=None):
        """(total gain of all players, {player_id: gain}) of a goal up to today (or its end)."""
        today = today or self.get_today()
        if goal["starts_on"] > today:
            return 0, {}
        end = min(goal["ends_on"], today) if goal["ends_on"] else today
        gains = self.get_metrics_between(goal["server_id"], goal["starts_on"], end)
        per_player = {pid: values.get(goal["metric"], 0) for pid, values in gains.items()}
        return sum(per_player.values()), per_player

    def take_reached_goals(self, server_ids):
        """Goals of the given servers that reached their target since the last call, marked as reached."""
        today = self.get_today()
        reached = []
        for row in self._fetchall(f"""SELECT {self._GOAL_COLUMNS} FROM community_goals
                                      WHERE server_id = ANY(%s) AND reached_at IS NULL AND starts_on <= current_date
                                        AND (ends_on IS NULL OR ends_on >= current_date - 1)""", (list(server_ids),)):
            goal = self._goal(row)
            if self.get_goal_progress(goal, today)[0] >= goal["target"]:
                row = self._fetchone(f"""UPDATE community_goals SET reached_at = now()
                                         WHERE id = %s AND reached_at IS NULL RETURNING {self._GOAL_COLUMNS}""",
                                     (goal["id"],))
                if row:
                    reached.append(self._goal(row))
        return reached

    ###----------------------------- Trophies ------------------------------------###

    def award_finished_competitions(self, server_ids):
        """Store the places 1-3 of competitions that ended as trophies (once). Returns the number of trophies."""
        count = 0
        for row in self._fetchall(f"""SELECT {self._COMPETITION_COLUMNS} FROM competitions
                                      WHERE server_id = ANY(%s) AND NOT awarded AND ends_on < current_date""",
                                  (list(server_ids),)):
            competition = self._competition(row)
            metric = metrics_mod.METRICS_BY_KEY.get(competition["metric"])
            standings = self.get_competition_standings(competition)[:3] if metric else []
            with self._cursor() as cur:
                cur.execute("UPDATE competitions SET awarded = true WHERE id = %s AND NOT awarded", (competition["id"],))
                if cur.rowcount == 0:
                    continue
                for place, entry in enumerate(standings, 1):
                    cur.execute("""INSERT INTO trophies (server_id, player_id, kind, ref, place, title, detail, awarded_at)
                                   VALUES (%s, %s, 'competition', %s, %s, %s, %s, (%s + 1)::timestamptz)
                                   ON CONFLICT DO NOTHING""",
                                (competition["server_id"], entry["player_id"], str(competition["id"]), place,
                                 competition["title"], metrics_mod.format_value(metric, entry["value"]),
                                 competition["ends_on"]))
                    count += cur.rowcount
        return count

    def settle_player_of_week(self, server_id):
        """
        Choose the player of the last full week (Monday to Sunday: most play time, visible players only)
        once. Returns {"name", "uuid", "title", "detail"} of the new trophy, or None (already chosen,
        nobody played, or the snapshots do not reach back before the week yet).
        """
        today = self.get_today()
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
        done = self._fetchvalue("SELECT weekly_awarded_until FROM servers WHERE id = %s", (server_id,))
        if done is not None and done >= end:
            return None
        first = self.get_snapshot_start(server_id)
        if first is None or first >= start:
            return None
        gains = self.get_metrics_between(server_id, start, end)
        visible = {str(pid): (name, str(uuid)) for pid, name, uuid in self._fetchall(
            """SELECT psi.player_id, p.name, psi.mojang_uuid FROM player_server_info psi
               JOIN player p ON p.uuid = psi.mojang_uuid WHERE psi.server_id = %s AND NOT psi.hide_stats""", (server_id,))}
        best = max(((pid, values.get("play_time", 0)) for pid, values in gains.items() if pid in visible),
                   key=lambda item: (item[1], visible[item[0]][0].lower()), default=None)
        year, week, _ = start.isocalendar()
        trophy = None
        with self._cursor() as cur:
            cur.execute("""UPDATE servers SET weekly_awarded_until = %s WHERE id = %s
                             AND (weekly_awarded_until IS NULL OR weekly_awarded_until < %s)""", (end, server_id, end))
            if cur.rowcount == 0:
                return None
            if best and best[1] > 0:
                trophy = {"name": visible[best[0]][0], "uuid": visible[best[0]][1], "title": f"KW {week}",
                          "detail": f"{metrics_mod.format_value(metrics_mod.METRICS_BY_KEY['play_time'], best[1])} Spielzeit"}
                cur.execute("""INSERT INTO trophies (server_id, player_id, kind, ref, title, detail)
                               VALUES (%s, %s, 'player_of_week', %s, %s, %s) ON CONFLICT DO NOTHING""",
                            (server_id, best[0], f"{year}-W{week:02d}", trophy["title"], trophy["detail"]))
        return trophy

    _TROPHY_SELECT = """SELECT t.kind, t.ref, t.place, t.title, t.detail, t.awarded_at, p.name, psi.mojang_uuid
                        FROM trophies t JOIN player_server_info psi ON psi.player_id = t.player_id
                        JOIN player p ON p.uuid = psi.mojang_uuid"""

    @staticmethod
    def _trophy(row):
        trophy = dict(zip(("kind", "ref", "place", "title", "detail", "awarded_at", "name", "uuid"), row))
        trophy["uuid"] = str(trophy["uuid"])
        return trophy

    def get_player_trophies(self, player_id):
        """[{"kind", "ref", "place", "title", "detail", "awarded_at", "name", "uuid"}] newest first."""
        return [self._trophy(row) for row in self._fetchall(
            self._TROPHY_SELECT + " WHERE t.player_id = %s ORDER BY t.awarded_at DESC, t.place", (player_id,))]

    def get_server_trophies(self, server_id, kind, limit=50):
        """Trophies of one kind on the server (visible players only), newest first."""
        return [self._trophy(row) for row in self._fetchall(
            self._TROPHY_SELECT + """ WHERE t.server_id = %s AND t.kind = %s AND NOT psi.hide_stats
                                      ORDER BY t.awarded_at DESC, t.ref DESC, t.place LIMIT %s""",
            (server_id, kind, limit))]

    ###----------------------------- In-game: sidebar, duels, reports ------------------------------------###

    def get_sidebar(self, player_id):
        return self._fetchvalue("SELECT sidebar FROM player_server_info WHERE player_id = %s", (player_id,))

    def set_sidebar(self, player_id, mode):
        self._execute("UPDATE player_server_info SET sidebar = %s WHERE player_id = %s", (mode, player_id))

    def get_sidebar_players(self, server_ids):
        """[(server_id, player_id, uuid, mode)] of the online players who switched the sidebar on."""
        return [(sid, str(pid), str(uuid), mode) for sid, pid, uuid, mode in self._fetchall(
            """SELECT server_id, player_id, mojang_uuid, sidebar FROM player_server_info
               WHERE server_id = ANY(%s) AND online AND sidebar <> 'off'""", (list(server_ids),))]

    def get_player_gain(self, player_id, metric, since_day):
        """Gain of one metric since the end of the day before since_day (the first snapshot if none is older)."""
        value = self.get_player_metrics(player_id)[metric]
        base = self._fetchvalue("""SELECT value FROM stat_snapshots WHERE player_id = %s AND metric = %s
                                   ORDER BY day < %s DESC, CASE WHEN day < %s THEN day END DESC NULLS LAST, day
                                   LIMIT 1""", (player_id, metric, since_day, since_day))
        return max(0, value - base) if base is not None else 0

    _DUEL_COLUMNS = """d.id, d.server_id, d.challenger_id, cp.name, cpsi.mojang_uuid, d.opponent_id, op.name, opsi.mojang_uuid,
                       d.metric, d.days, d.status, d.created_at, d.starts_at, d.ends_at, d.challenger_start,
                       d.opponent_start, d.challenger_gain, d.opponent_gain"""
    _DUEL_FROM = """FROM duels d
                    JOIN player_server_info cpsi ON cpsi.player_id = d.challenger_id JOIN player cp ON cp.uuid = cpsi.mojang_uuid
                    JOIN player_server_info opsi ON opsi.player_id = d.opponent_id JOIN player op ON op.uuid = opsi.mojang_uuid"""
    _DUEL_KEYS = ("id", "server_id", "challenger_id", "challenger", "challenger_uuid", "opponent_id", "opponent",
                  "opponent_uuid", "metric", "days", "status", "created_at", "starts_at", "ends_at", "challenger_start",
                  "opponent_start", "challenger_gain", "opponent_gain")

    def _duel(self, row):
        duel = dict(zip(self._DUEL_KEYS, row))
        for key in ("challenger_id", "opponent_id", "challenger_uuid", "opponent_uuid"):
            duel[key] = str(duel[key])
        return duel

    def _duels(self, where, params):
        return [self._duel(row) for row in self._fetchall(
            f"SELECT {self._DUEL_COLUMNS} {self._DUEL_FROM} WHERE {where} ORDER BY d.created_at DESC, d.id DESC", params)]

    def get_duel(self, duel_id):
        duels = self._duels("d.id = %s", (duel_id,))
        return duels[0] if duels else None

    def list_duels(self, server_id, limit=50):
        """Duels of a server, newest first (pending and running ones first)."""
        duels = self._duels("d.server_id = %s AND d.created_at > now() - interval '60 days'", (server_id,))[:limit]
        order = {"running": 0, "pending": 1}
        return sorted(duels, key=lambda d: order.get(d["status"], 2))

    def get_player_duels(self, player_id, statuses=("pending", "running")):
        return self._duels("(d.challenger_id = %s OR d.opponent_id = %s) AND d.status = ANY(%s)",
                           (player_id, player_id, list(statuses)))

    def create_duel(self, server_id, challenger_id, opponent_id, metric, days):
        """
        Challenge a player. Returns (duel_id, None) or (None, error) with error "self", "hidden"
        (one of them hides the stats) or "open" (the two already have an open duel).
        """
        if str(challenger_id) == str(opponent_id):
            return None, "self"
        with self._cursor() as cur:
            cur.execute("""SELECT count(*) FROM player_server_info WHERE player_id IN (%s, %s)
                           AND server_id = %s AND NOT hide_stats""", (challenger_id, opponent_id, server_id))
            if cur.fetchone()[0] != 2:
                return None, "hidden"
            self._expire_duels(cur)
            cur.execute("""SELECT 1 FROM duels WHERE status IN ('pending', 'running')
                             AND ((challenger_id = %s AND opponent_id = %s) OR (challenger_id = %s AND opponent_id = %s))""",
                        (challenger_id, opponent_id, opponent_id, challenger_id))
            if cur.fetchone():
                return None, "open"
            cur.execute("""INSERT INTO duels (server_id, challenger_id, opponent_id, metric, days)
                           VALUES (%s, %s, %s, %s, %s) RETURNING id""", (server_id, challenger_id, opponent_id, metric, days))
            return cur.fetchone()[0], None

    @staticmethod
    def _expire_duels(cur):
        cur.execute("""UPDATE duels SET status = 'expired' WHERE status = 'pending'
                       AND created_at < now() - %s * interval '1 hour'""", (DUEL_ACCEPT_HOURS,))

    def respond_duel(self, duel_id, player_id, accept):
        """The challenged player accepts or declines. Returns the updated duel, or None if not possible."""
        with self._cursor() as cur:
            self._expire_duels(cur)
            cur.execute("SELECT metric, challenger_id FROM duels WHERE id = %s AND opponent_id = %s AND status = 'pending' "
                        "FOR UPDATE", (duel_id, player_id))
            row = cur.fetchone()
            if row is None:
                return None
            if accept:
                metric, challenger_id = row
                start_c = self.get_player_metrics(challenger_id).get(metric, 0)
                start_o = self.get_player_metrics(player_id).get(metric, 0)
                cur.execute("""UPDATE duels SET status = 'running', starts_at = now(), ends_at = now() + days * interval '1 day',
                               challenger_start = %s, opponent_start = %s WHERE id = %s""", (start_c, start_o, duel_id))
            else:
                cur.execute("UPDATE duels SET status = 'declined' WHERE id = %s", (duel_id,))
        return self.get_duel(duel_id)

    def cancel_duel(self, duel_id, player_id):
        """The challenger withdraws a challenge that was not answered yet. Returns the duel or None."""
        if not self._execute("""UPDATE duels SET status = 'cancelled' WHERE id = %s AND challenger_id = %s
                                AND status = 'pending'""", (duel_id, player_id)):
            return None
        return self.get_duel(duel_id)

    def duel_gains(self, duel):
        """(challenger gain, opponent gain): final values of a finished duel, live values of a running one."""
        if duel["status"] == "finished":
            return duel["challenger_gain"], duel["opponent_gain"]
        if duel["status"] != "running":
            return 0, 0
        return (max(0, self.get_player_metrics(duel["challenger_id"]).get(duel["metric"], 0) - duel["challenger_start"]),
                max(0, self.get_player_metrics(duel["opponent_id"]).get(duel["metric"], 0) - duel["opponent_start"]))

    def take_finished_duels(self, server_ids):
        """Running duels of the given servers whose time is up, now finished with their final gains."""
        finished = []
        for duel in self._duels("d.server_id = ANY(%s) AND d.status = 'running' AND d.ends_at <= now()", (list(server_ids),)):
            gain_c, gain_o = self.duel_gains(duel)
            if self._execute("""UPDATE duels SET status = 'finished', challenger_gain = %s, opponent_gain = %s
                                WHERE id = %s AND status = 'running'""", (gain_c, gain_o, duel["id"])):
                finished.append(dict(duel, status="finished", challenger_gain=gain_c, opponent_gain=gain_o))
        with self._cursor() as cur:
            self._expire_duels(cur)
        return finished

    def create_report(self, server_id, reporter_id, target_name, reason, location=None, source="ingame"):
        """Store a report. location: (world, x, y, z) or None. Returns the id, or None if the reporter sent too many."""
        with self._cursor() as cur:
            cur.execute("""SELECT count(*) FROM reports WHERE reporter_id = %s AND created_at > now() - interval '1 hour'""",
                        (reporter_id,))
            if cur.fetchone()[0] >= MAX_REPORTS_PER_HOUR:
                return None
            world, x, y, z = location or (None, None, None, None)
            cur.execute("""INSERT INTO reports (server_id, reporter_id, target_name, reason, world, x, y, z, source)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (server_id, reporter_id, target_name, reason, world, x, y, z, source))
            return cur.fetchone()[0]

    def list_reports(self, server_id, include_handled=False, limit=100):
        """[{"id", "reporter", "target_name", "reason", "world", "x", "y", "z", "source", "created_at",
        "handled_by", "handled_at"}], open ones first, newest first."""
        rows = self._fetchall("""
            SELECT r.id, p.name, r.target_name, r.reason, r.world, r.x, r.y, r.z, r.source, r.created_at,
                   r.handled_by, r.handled_at
            FROM reports r LEFT JOIN player_server_info psi ON psi.player_id = r.reporter_id
            LEFT JOIN player p ON p.uuid = psi.mojang_uuid
            WHERE r.server_id = %s AND (%s OR r.handled_at IS NULL)
            ORDER BY r.handled_at IS NOT NULL, r.created_at DESC LIMIT %s""", (server_id, include_handled, limit))
        keys = ("id", "reporter", "target_name", "reason", "world", "x", "y", "z", "source", "created_at",
                "handled_by", "handled_at")
        return [dict(zip(keys, row)) for row in rows]

    def resolve_report(self, server_id, report_id, handled_by):
        """Mark a report as handled. Returns (target_name, reason) or None."""
        return self._fetchone("""UPDATE reports SET handled_by = %s, handled_at = now()
                                 WHERE id = %s AND server_id = %s AND handled_at IS NULL
                                 RETURNING target_name, reason""", (handled_by, report_id, server_id))

    def get_online_moderator_uuids(self, server_id):
        return [str(row[0]) for row in self._fetchall("""
            SELECT psi.mojang_uuid FROM player_server_info psi JOIN servers s ON s.id = psi.server_id
            WHERE psi.server_id = %s AND psi.online
              AND (psi.web_access_permissions <= %s OR (s.auto_mod_ops AND psi.is_op))""", (server_id, MODERATOR_LEVEL))]

    ###----------------------------- Events ------------------------------------###

    _EVENT_COLUMNS = "e.id, e.server_id, e.title, e.description, e.place, e.starts_at, e.created_by"

    def _event(self, row):
        keys = ("id", "server_id", "title", "description", "place", "starts_at", "created_by", "signups")
        return dict(zip(keys, row))

    def create_event(self, server_id, title, starts_at, description=None, place=None, created_by=None):
        return self._fetchvalue("""INSERT INTO events (server_id, title, description, place, starts_at, created_by)
                                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                                (server_id, title, description, place, starts_at, created_by))

    def get_event(self, event_id):
        row = self._fetchone(f"""SELECT {self._EVENT_COLUMNS}, (SELECT count(*) FROM event_signups s WHERE s.event_id = e.id)
                                 FROM events e WHERE e.id = %s""", (event_id,))
        return self._event(row) if row else None

    def mark_event_reminded_if_soon(self, event_id):
        """Mark an event as reminded when it starts within EVENT_REMINDER_MINUTES. True if it does."""
        return self._execute("""UPDATE events SET reminded = true WHERE id = %s AND NOT reminded
                                AND starts_at <= now() + %s * interval '1 minute'""",
                             (event_id, EVENT_REMINDER_MINUTES)) > 0

    def delete_event(self, server_id, event_id):
        """Returns the title of the deleted event, or None."""
        return self._fetchvalue("DELETE FROM events WHERE id = %s AND server_id = %s RETURNING title", (event_id, server_id))

    def list_events(self, server_id, upcoming=True, limit=20):
        """Upcoming events (soonest first; started up to 3 hours ago count as upcoming) or past ones (newest first),
        with the number of sign ups."""
        where = "e.starts_at > now() - interval '3 hours'" if upcoming else "e.starts_at <= now() - interval '3 hours'"
        order = "e.starts_at" if upcoming else "e.starts_at DESC"
        return [self._event(row) for row in self._fetchall(f"""
            SELECT {self._EVENT_COLUMNS}, (SELECT count(*) FROM event_signups s WHERE s.event_id = e.id)
            FROM events e WHERE e.server_id = %s AND {where} ORDER BY {order} LIMIT %s""", (server_id, limit))]

    def get_event_signups(self, event_id):
        """[{"name", "uuid"}] of the players signed up, in order of sign up."""
        return [{"name": n, "uuid": str(u)} for n, u in self._fetchall("""
            SELECT p.name, psi.mojang_uuid FROM event_signups s
            JOIN player_server_info psi ON psi.player_id = s.player_id JOIN player p ON p.uuid = psi.mojang_uuid
            WHERE s.event_id = %s ORDER BY s.created_at""", (event_id,))]

    def toggle_event_signup(self, server_id, event_id, player_id, signed_up=None):
        """Sign up or off (signed_up None: toggle). Returns True/False (now signed up) or None if the event is unknown/over."""
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM events WHERE id = %s AND server_id = %s AND starts_at > now() - interval '3 hours'",
                        (event_id, server_id))
            if cur.fetchone() is None:
                return None
            cur.execute("SELECT 1 FROM event_signups WHERE event_id = %s AND player_id = %s", (event_id, player_id))
            current = cur.fetchone() is not None
            wanted = (not current) if signed_up is None else signed_up
            if wanted and not current:
                cur.execute("INSERT INTO event_signups (event_id, player_id) VALUES (%s, %s)", (event_id, player_id))
            elif current and not wanted:
                cur.execute("DELETE FROM event_signups WHERE event_id = %s AND player_id = %s", (event_id, player_id))
            return wanted

    def get_player_event_ids(self, player_id):
        return {row[0] for row in self._fetchall("SELECT event_id FROM event_signups WHERE player_id = %s", (player_id,))}

    def take_due_event_announcements(self, server_ids):
        """[("reminder" | "start", event)] of the given servers, each announced once."""
        result = []
        with self._cursor() as cur:
            cur.execute(f"""UPDATE events e SET reminded = true
                            WHERE e.server_id = ANY(%s) AND NOT e.reminded AND NOT e.start_announced
                              AND e.starts_at <= now() + %s * interval '1 minute' AND e.starts_at > now()
                            RETURNING {self._EVENT_COLUMNS}, 0""", (list(server_ids), EVENT_REMINDER_MINUTES))
            result += [("reminder", self._event(row)) for row in cur.fetchall()]
            cur.execute(f"""UPDATE events e SET start_announced = true, reminded = true
                            WHERE e.server_id = ANY(%s) AND NOT e.start_announced
                              AND e.starts_at <= now() AND e.starts_at > now() - interval '15 minutes'
                            RETURNING {self._EVENT_COLUMNS}, 0""", (list(server_ids),))
            result += [("start", self._event(row)) for row in cur.fetchall()]
        return result

    ###----------------------------- Polls ------------------------------------###

    def create_poll(self, server_id, question, options, ends_at, created_by=None):
        return self._fetchvalue("""INSERT INTO polls (server_id, question, options, ends_at, created_by)
                                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                                (server_id, question, list(options), ends_at, created_by))

    def delete_poll(self, server_id, poll_id):
        return self._fetchvalue("DELETE FROM polls WHERE id = %s AND server_id = %s RETURNING question", (poll_id, server_id))

    def list_polls(self, server_id, open_only=False, limit=30):
        """Polls with the votes per option: [{"id", "question", "options", "votes", "total", "ends_at", "open", ...}],
        open ones first (ending soonest first), then closed ones (newest first)."""
        rows = self._fetchall(f"""
            SELECT p.id, p.server_id, p.question, p.options, p.ends_at, p.created_by, p.ends_at > now(),
                   COALESCE((SELECT array_agg(n ORDER BY o) FROM (
                       SELECT o, (SELECT count(*) FROM poll_votes v WHERE v.poll_id = p.id AND v.option = o) AS n
                       FROM generate_series(0, cardinality(p.options) - 1) AS o) counts), '{{}}')
            FROM polls p WHERE p.server_id = %s {"AND p.ends_at > now()" if open_only else ""}
            ORDER BY p.ends_at > now() DESC, CASE WHEN p.ends_at > now() THEN p.ends_at END, p.ends_at DESC LIMIT %s""",
                              (server_id, limit))
        keys = ("id", "server_id", "question", "options", "ends_at", "created_by", "open", "votes")
        polls = [dict(zip(keys, row)) for row in rows]
        for poll in polls:
            poll["votes"] = [int(v) for v in poll["votes"]]
            poll["total"] = sum(poll["votes"])
        return polls

    def get_poll(self, poll_id):
        row = self._fetchone("SELECT server_id FROM polls WHERE id = %s", (poll_id,))
        return next((p for p in self.list_polls(row[0], limit=1000) if p["id"] == poll_id), None) if row else None

    def vote(self, server_id, poll_id, player_id, option):
        """Vote (or change the vote). Returns "ok", "closed" or "invalid"."""
        with self._cursor() as cur:
            cur.execute("SELECT cardinality(options), ends_at > now() FROM polls WHERE id = %s AND server_id = %s",
                        (poll_id, server_id))
            row = cur.fetchone()
            if row is None or not 0 <= option < row[0]:
                return "invalid"
            if not row[1]:
                return "closed"
            cur.execute("""INSERT INTO poll_votes (poll_id, player_id, option) VALUES (%s, %s, %s)
                           ON CONFLICT (poll_id, player_id) DO UPDATE SET option = EXCLUDED.option, voted_at = now()""",
                        (poll_id, player_id, option))
        return "ok"

    def get_player_votes(self, player_id):
        """{poll_id: option} of a player."""
        return dict(self._fetchall("SELECT poll_id, option FROM poll_votes WHERE player_id = %s", (player_id,)))

    def take_finished_polls(self, server_ids):
        """Polls of the given servers that ended since the last call (with their votes), marked as announced."""
        ids = [row[0] for row in self._fetchall("""UPDATE polls SET result_announced = true
                                                   WHERE server_id = ANY(%s) AND NOT result_announced AND ends_at <= now()
                                                   RETURNING id""", (list(server_ids),))]
        return [self.get_poll(poll_id) for poll_id in ids]

    ###----------------------------- Build gallery ------------------------------------###

    _BUILD_SELECT = """SELECT b.id, b.server_id, b.player_id, p.name, psi.mojang_uuid, b.title, b.description, b.coordinates,
                              b.filename, b.status, b.created_at, b.reviewed_by,
                              (SELECT count(*) FROM build_likes l WHERE l.build_id = b.id)
                       FROM builds b JOIN player_server_info psi ON psi.player_id = b.player_id
                       JOIN player p ON p.uuid = psi.mojang_uuid"""

    @staticmethod
    def _build(row):
        keys = ("id", "server_id", "player_id", "name", "uuid", "title", "description", "coordinates", "filename",
                "status", "created_at", "reviewed_by", "likes")
        build = dict(zip(keys, row))
        build["player_id"], build["uuid"] = str(build["player_id"]), str(build["uuid"])
        return build

    def add_build(self, server_id, player_id, title, filename, description=None, coordinates=None, approved=False):
        """Store an uploaded build. Returns the id, or None if the player has too many waiting or uploaded today."""
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM player_server_info WHERE player_id = %s FOR UPDATE", (player_id,))
            cur.execute("""SELECT count(*) FILTER (WHERE status = 'pending'),
                                  count(*) FILTER (WHERE created_at > now() - interval '1 day')
                           FROM builds WHERE player_id = %s""", (player_id,))
            pending, today = cur.fetchone()
            if not approved and (pending >= MAX_PENDING_BUILDS or today >= MAX_BUILDS_PER_DAY):
                return None
            cur.execute("""INSERT INTO builds (server_id, player_id, title, description, coordinates, filename, status,
                                               reviewed_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id""",
                        (server_id, player_id, title, description, coordinates, filename,
                         "approved" if approved else "pending", approved))
            return cur.fetchone()[0]

    def list_builds(self, server_id, status="approved", player_id=None, order="new", limit=60):
        """Builds of a server (optionally of one player), newest or most liked first."""
        order_sql = "13 DESC, b.created_at DESC" if order == "top" else "b.created_at DESC"
        where = "b.server_id = %s AND b.status = %s" + (" AND b.player_id = %s" if player_id else "")
        params = (server_id, status) + ((player_id,) if player_id else ())
        return [self._build(row) for row in self._fetchall(
            f"{self._BUILD_SELECT} WHERE {where} ORDER BY {order_sql} LIMIT %s", params + (limit,))]

    def get_build(self, build_id):
        row = self._fetchone(self._BUILD_SELECT + " WHERE b.id = %s", (build_id,))
        return self._build(row) if row else None

    def approve_build(self, server_id, build_id, reviewed_by):
        """Returns the build, or None if there is no waiting build with this id."""
        if not self._execute("""UPDATE builds SET status = 'approved', reviewed_by = %s, reviewed_at = now()
                                WHERE id = %s AND server_id = %s AND status = 'pending'""", (reviewed_by, build_id, server_id)):
            return None
        return self.get_build(build_id)

    def delete_build(self, server_id, build_id, player_id=None):
        """Delete a build (only the player's own if player_id is given). Returns (title, filename, owner name) or None."""
        return self._fetchone("""DELETE FROM builds b USING player_server_info psi, player p
                                 WHERE b.id = %s AND b.server_id = %s AND (%s::uuid IS NULL OR b.player_id = %s::uuid)
                                   AND psi.player_id = b.player_id AND p.uuid = psi.mojang_uuid
                                 RETURNING b.title, b.filename, p.name""", (build_id, server_id, player_id, player_id))

    def toggle_build_like(self, server_id, build_id, player_id):
        """Returns True/False (now liked) or None if the build is unknown, not approved or the player's own."""
        with self._cursor() as cur:
            cur.execute("SELECT player_id FROM builds WHERE id = %s AND server_id = %s AND status = 'approved'",
                        (build_id, server_id))
            row = cur.fetchone()
            if row is None or str(row[0]) == str(player_id):
                return None
            cur.execute("DELETE FROM build_likes WHERE build_id = %s AND player_id = %s", (build_id, player_id))
            if cur.rowcount:
                return False
            cur.execute("INSERT INTO build_likes (build_id, player_id) VALUES (%s, %s)", (build_id, player_id))
            return True

    def get_liked_builds(self, player_id):
        return {row[0] for row in self._fetchall("SELECT build_id FROM build_likes WHERE player_id = %s", (player_id,))}

    ###----------------------------- Guestbook ------------------------------------###

    def add_guestbook_entry(self, player_id, author_id, text):
        """Returns the id, or None if the author wrote too many entries in the last hour."""
        with self._cursor() as cur:
            cur.execute("SELECT count(*) FROM guestbook WHERE author_id = %s AND created_at > now() - interval '1 hour'",
                        (author_id,))
            if cur.fetchone()[0] >= MAX_GUESTBOOK_PER_HOUR:
                return None
            cur.execute("INSERT INTO guestbook (player_id, author_id, text) VALUES (%s, %s, %s) RETURNING id",
                        (player_id, author_id, text))
            return cur.fetchone()[0]

    _GUESTBOOK_SELECT = """SELECT g.id, g.player_id, g.author_id, a.name, apsi.mojang_uuid, g.text, g.created_at, g.reported_at,
                                  o.name
                           FROM guestbook g JOIN player_server_info apsi ON apsi.player_id = g.author_id
                           JOIN player a ON a.uuid = apsi.mojang_uuid
                           JOIN player_server_info opsi ON opsi.player_id = g.player_id
                           JOIN player o ON o.uuid = opsi.mojang_uuid"""

    @staticmethod
    def _guestbook(row):
        keys = ("id", "player_id", "author_id", "author", "author_uuid", "text", "created_at", "reported_at", "owner")
        entry = dict(zip(keys, row))
        for key in ("player_id", "author_id", "author_uuid"):
            entry[key] = str(entry[key])
        return entry

    def get_guestbook(self, player_id, limit=50):
        return [self._guestbook(row) for row in self._fetchall(
            self._GUESTBOOK_SELECT + " WHERE g.player_id = %s ORDER BY g.created_at DESC LIMIT %s", (player_id, limit))]

    def get_reported_guestbook_entries(self, server_id):
        return [self._guestbook(row) for row in self._fetchall(
            self._GUESTBOOK_SELECT + " WHERE opsi.server_id = %s AND g.reported_at IS NOT NULL ORDER BY g.reported_at DESC",
            (server_id,))]

    def report_guestbook_entry(self, server_id, entry_id):
        return self._execute("""UPDATE guestbook g SET reported_at = COALESCE(g.reported_at, now())
                                FROM player_server_info psi WHERE g.id = %s AND psi.player_id = g.player_id
                                  AND psi.server_id = %s""", (entry_id, server_id)) > 0

    def keep_guestbook_entry(self, server_id, entry_id):
        """A moderator decided the reported entry is fine."""
        return self._execute("""UPDATE guestbook g SET reported_at = NULL FROM player_server_info psi
                                WHERE g.id = %s AND psi.player_id = g.player_id AND psi.server_id = %s""",
                             (entry_id, server_id)) > 0

    def delete_guestbook_entry(self, server_id, entry_id, player_id=None):
        """Delete an entry; with player_id only if that player wrote it or owns the page. Returns the entry or None."""
        row = self._fetchone(self._GUESTBOOK_SELECT + " WHERE g.id = %s AND opsi.server_id = %s", (entry_id, server_id))
        if row is None:
            return None
        entry = self._guestbook(row)
        if player_id is not None and str(player_id) not in (entry["author_id"], entry["player_id"]):
            return None
        self._execute("DELETE FROM guestbook WHERE id = %s", (entry_id,))
        return entry

    ###----------------------------- Server settings (moderation) ------------------------------------###

    _SETTINGS = ("access_mode", "warn_threshold", "warn_ban_days", "rules_enabled", "rules", "faq", "alerts_enabled")

    def get_server_settings(self, server_id):
        row = self._fetchone(f"SELECT {', '.join(self._SETTINGS)} FROM servers WHERE id = %s", (server_id,))
        return dict(zip(self._SETTINGS, row)) if row else None

    def update_server_settings(self, server_id, **fields):
        fields = {k: v for k, v in fields.items() if k in self._SETTINGS}
        if fields:
            self._execute(f"UPDATE servers SET {', '.join(f'{k} = %s' for k in fields)} WHERE id = %s",
                          (*fields.values(), server_id))

    ###----------------------------- Whitelist access ------------------------------------###

    _REQUEST_COLUMNS = "id, server_id, name, message, kind, status, created_at, handled_by, handled_at, synced"

    def _request(self, row):
        return dict(zip([c.strip() for c in self._REQUEST_COLUMNS.split(",")], row))

    def add_application(self, server_id, name, message):
        """
        Store an application. Returns (id, None) or (None, error): "pending" (this name already has
        an open application), "accepted" (already accepted) or "full" (too many open applications).
        """
        with self._cursor() as cur:
            cur.execute("SELECT id FROM servers WHERE id = %s FOR UPDATE", (server_id,))
            cur.execute("""SELECT status FROM access_requests WHERE server_id = %s AND lower(name) = lower(%s)
                             AND status IN ('pending', 'accepted') ORDER BY created_at DESC LIMIT 1""", (server_id, name))
            row = cur.fetchone()
            if row:
                return None, row[0]
            cur.execute("SELECT count(*) FROM access_requests WHERE server_id = %s AND status = 'pending'", (server_id,))
            if cur.fetchone()[0] >= MAX_PENDING_APPLICATIONS:
                return None, "full"
            cur.execute("""INSERT INTO access_requests (server_id, name, message, kind) VALUES (%s, %s, %s, 'application')
                           RETURNING id""", (server_id, name, message))
            return cur.fetchone()[0], None

    def list_access_requests(self, server_id, limit=100):
        """Open applications first, then the handled ones and code redemptions, newest first."""
        return [self._request(row) for row in self._fetchall(f"""
            SELECT {self._REQUEST_COLUMNS} FROM access_requests WHERE server_id = %s
            ORDER BY status <> 'pending', created_at DESC LIMIT %s""", (server_id, limit))]

    def handle_application(self, server_id, request_id, accept, handled_by):
        """Accept or reject an open application. Returns the request or None."""
        row = self._fetchone(f"""UPDATE access_requests SET status = %s, handled_by = %s, handled_at = now()
                                 WHERE id = %s AND server_id = %s AND status = 'pending' AND kind = 'application'
                                 RETURNING {self._REQUEST_COLUMNS}""",
                             ("accepted" if accept else "rejected", handled_by, request_id, server_id))
        return self._request(row) if row else None

    def create_invite_code(self, server_id, code, max_uses=None, expires_at=None, created_by=None):
        """Returns the id, or None if the code exists on the server already."""
        try:
            return self._fetchvalue("""INSERT INTO invite_codes (server_id, code, max_uses, expires_at, created_by)
                                       VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                                    (server_id, code, max_uses, expires_at, created_by))
        except psycopg2.errors.UniqueViolation:
            return None

    def list_invite_codes(self, server_id):
        keys = ("id", "code", "max_uses", "uses", "expires_at", "created_by", "created_at", "valid")
        return [dict(zip(keys, row)) for row in self._fetchall("""
            SELECT id, code, max_uses, uses, expires_at, created_by, created_at,
                   (max_uses IS NULL OR uses < max_uses) AND (expires_at IS NULL OR expires_at > now())
            FROM invite_codes WHERE server_id = %s ORDER BY created_at DESC""", (server_id,))]

    def delete_invite_code(self, server_id, code_id):
        return self._fetchvalue("DELETE FROM invite_codes WHERE id = %s AND server_id = %s RETURNING code",
                                (code_id, server_id))

    def redeem_invite_code(self, server_id, name, code):
        """Whitelist a name with an invite code. Returns "ok", "invalid" or "already"."""
        with self._cursor() as cur:
            cur.execute("""SELECT id FROM invite_codes WHERE server_id = %s AND upper(code) = upper(%s)
                             AND (max_uses IS NULL OR uses < max_uses) AND (expires_at IS NULL OR expires_at > now())
                           FOR UPDATE""", (server_id, code.strip()))
            row = cur.fetchone()
            if row is None:
                return "invalid"
            cur.execute("""SELECT 1 FROM access_requests WHERE server_id = %s AND lower(name) = lower(%s)
                             AND status = 'accepted'""", (server_id, name))
            if cur.fetchone():
                return "already"
            cur.execute("UPDATE invite_codes SET uses = uses + 1 WHERE id = %s", (row[0],))
            cur.execute("""UPDATE access_requests SET status = 'rejected', handled_by = 'Einladungscode', handled_at = now()
                           WHERE server_id = %s AND lower(name) = lower(%s) AND status = 'pending'""", (server_id, name))
            cur.execute("""INSERT INTO access_requests (server_id, name, message, kind, status, handled_by, handled_at)
                           VALUES (%s, %s, %s, 'code', 'accepted', 'Einladungscode', now())""",
                        (server_id, name, f"Code {code.strip().upper()}"))
        return "ok"

    def get_unsynced_whitelist(self, server_id):
        """Names accepted but not yet sent to the plugin."""
        return [row[0] for row in self._fetchall("""SELECT name FROM access_requests
                                                   WHERE server_id = %s AND status = 'accepted' AND NOT synced
                                                   ORDER BY handled_at""", (server_id,))]

    def mark_whitelist_synced(self, server_id, names):
        self._execute("""UPDATE access_requests SET synced = true WHERE server_id = %s AND status = 'accepted'
                           AND lower(name) = ANY(%s)""", (server_id, [n.lower() for n in names]))

    ###----------------------------- Warnings and mutes ------------------------------------###

    def warn_player(self, server_id, player_id, reason, created_by):
        """
        Store a warning. Returns {"count", "threshold", "ban"}: ban is the result of ban_player when
        this warning reached the server's threshold, otherwise None.
        """
        settings = self.get_server_settings(server_id)
        with self._cursor() as cur:
            cur.execute("INSERT INTO warnings (server_id, player_id, reason, created_by) VALUES (%s, %s, %s, %s)",
                        (server_id, player_id, reason, created_by))
            cur.execute("SELECT count(*) FROM warnings WHERE player_id = %s", (player_id,))
            count = cur.fetchone()[0]
        threshold = settings["warn_threshold"]
        ban = None
        if threshold and count % threshold == 0:
            ban = self.ban_player(server_id, self.get_player_name_from_player_id(player_id), created_by,
                                  days=settings["warn_ban_days"], comment=f"automatisch nach {count} Verwarnungen: {reason}")
        return {"count": count, "threshold": threshold, "ban": ban}

    def get_warnings(self, player_id):
        return [dict(zip(("id", "reason", "created_by", "created_at"), row)) for row in self._fetchall(
            "SELECT id, reason, created_by, created_at FROM warnings WHERE player_id = %s ORDER BY created_at", (player_id,))]

    def delete_warning(self, server_id, warning_id):
        """Returns (player name, reason) or None."""
        return self._fetchone("""DELETE FROM warnings w USING player_server_info psi, player p
                                 WHERE w.id = %s AND w.server_id = %s AND psi.player_id = w.player_id
                                   AND p.uuid = psi.mojang_uuid RETURNING p.name, w.reason""", (warning_id, server_id))

    def set_mute(self, player_id, until, reason=None):
        """until: datetime or None to unmute."""
        self._execute("UPDATE player_server_info SET muted_until = %s, mute_reason = %s WHERE player_id = %s",
                      (until, reason if until else None, player_id))

    def get_mutes(self, server_id):
        """{uuid: (until, reason)} of the players muted right now."""
        return {str(uuid): (until, reason) for uuid, until, reason in self._fetchall(
            """SELECT mojang_uuid, muted_until, mute_reason FROM player_server_info
               WHERE server_id = %s AND muted_until > now()""", (server_id,))}

    ###----------------------------- X-ray hints ------------------------------------###

    def get_mining_ratios(self, server_id):
        """[{"player_id", "name", "uuid", "stone", "diamonds", "netherrack", "debris", "hours"}] of all players (raw counts)."""
        rows = self._fetchall("""
            SELECT psi.player_id, p.name, psi.mojang_uuid,
                   COALESCE(sum(a.value) FILTER (WHERE a.category = %s AND a.object = ANY(%s)), 0),
                   COALESCE(sum(a.value) FILTER (WHERE a.category = %s AND a.object = ANY(%s)), 0),
                   COALESCE(sum(a.value) FILTER (WHERE a.category = %s AND a.object = 'minecraft:netherrack'), 0),
                   COALESCE(sum(a.value) FILTER (WHERE a.category = %s AND a.object = 'minecraft:ancient_debris'), 0),
                   COALESCE(sum(a.value) FILTER (WHERE a.category = %s AND a.object = ANY(%s)), 0)
            FROM player_server_info psi JOIN player p ON p.uuid = psi.mojang_uuid
            LEFT JOIN actions a ON a.player_id = psi.player_id
            WHERE psi.server_id = %s
            GROUP BY psi.player_id, p.name, psi.mojang_uuid""",
                              (stats_mod.BLOCK_MINED, ["minecraft:stone", "minecraft:deepslate", "minecraft:tuff"],
                               stats_mod.BLOCK_MINED, ["minecraft:diamond_ore", "minecraft:deepslate_diamond_ore"],
                               stats_mod.BLOCK_MINED, stats_mod.BLOCK_MINED,
                               stats_mod.CUSTOM, ["minecraft:play_time", "minecraft:play_one_minute"], server_id))
        keys = ("player_id", "name", "uuid", "stone", "diamonds", "netherrack", "debris", "ticks")
        result = []
        for row in rows:
            entry = dict(zip(keys, row))
            entry["player_id"], entry["uuid"] = str(entry["player_id"]), str(entry["uuid"])
            for key in ("stone", "diamonds", "netherrack", "debris", "ticks"):
                entry[key] = int(entry[key])
            entry["hours"] = entry.pop("ticks") / 72000
            result.append(entry)
        return result

    ###----------------------------- Alerts ------------------------------------###

    def get_alert_candidates(self):
        """
        Servers with alerts on that need a mail: [{"server_id", "server_name", "subdomain", "email", "kind",
        "since"}], kind "offline" (plugin gone for ALERT_OFFLINE_MINUTES) or "tps" (TPS below ALERT_TPS in
        every sample of the last ALERT_TPS_MINUTES). Only servers that had a plugin before are checked.
        """
        rows = self._fetchall("""
            SELECT s.id, s.server_name, s.subdomain, sa.email, 'offline', s.plugin_last_seen
            FROM servers s JOIN server_admins sa ON sa.id = s.owner_id
            WHERE s.alerts_enabled AND s.plugin_last_seen IS NOT NULL
              AND (NOT s.plugin_connected OR s.plugin_last_seen < now() - make_interval(secs => %s))
              AND s.plugin_last_seen < now() - %s * interval '1 minute'
              AND s.plugin_last_seen > now() - interval '7 days'
              AND (s.alert_offline_at IS NULL OR s.alert_offline_at < s.plugin_last_seen)
            UNION ALL
            SELECT s.id, s.server_name, s.subdomain, sa.email, 'tps', min(h.at)
            FROM servers s JOIN server_admins sa ON sa.id = s.owner_id
            JOIN server_health h ON h.server_id = s.id AND h.at > now() - %s * interval '1 minute'
            WHERE s.alerts_enabled
              AND (s.alert_tps_at IS NULL OR s.alert_tps_at < now() - %s * interval '1 hour')
            GROUP BY s.id, s.server_name, s.subdomain, sa.email
            HAVING count(*) >= %s AND max(h.tps) < %s""",
                              (PLUGIN_ONLINE_SECONDS, ALERT_OFFLINE_MINUTES, ALERT_TPS_MINUTES, ALERT_REPEAT_HOURS,
                               ALERT_TPS_MINUTES - 1, ALERT_TPS))
        keys = ("server_id", "server_name", "subdomain", "email", "kind", "since")
        return [dict(zip(keys, row)) for row in rows]

    def mark_alert_sent(self, server_id, kind):
        column = "alert_offline_at" if kind == "offline" else "alert_tps_at"
        self._execute(f"UPDATE servers SET {column} = now() WHERE id = %s", (server_id,))

    ###----------------------------- Join messages and rewards ------------------------------------###

    def get_reward_settings(self, server_id):
        """(enabled, levels or None for the default levels)."""
        row = self._fetchone("SELECT rewards_enabled, reward_levels FROM servers WHERE id = %s", (server_id,))
        return (row[0], row[1]) if row else (False, None)

    def set_reward_settings(self, server_id, enabled=None, levels=False):
        """enabled: True/False or None (unchanged); levels: a list, None (back to the default) or False (unchanged)."""
        if enabled is not None:
            self._execute("UPDATE servers SET rewards_enabled = %s WHERE id = %s", (bool(enabled), server_id))
        if levels is not False:
            self._execute("UPDATE servers SET reward_levels = %s WHERE id = %s",
                          (json.dumps(levels) if levels is not None else None, server_id))

    def get_reward_facts(self, player_id):
        """What the reward conditions look at: {"streak" (best), "tiers", "play_hours", "days", "trophies", "metrics"}."""
        values = self.get_player_metrics(player_id)
        first_seen = self._fetchvalue("SELECT first_seen FROM player_server_info WHERE player_id = %s", (player_id,))
        return {
            "streak": self.get_player_streak(player_id)["best"],
            "tiers": len(self.get_player_achievements(player_id)),
            "play_hours": values.get("play_time", 0) / 20 / 3600,
            "days": (datetime.now(timezone.utc) - first_seen).days if first_seen else 0,
            "trophies": len(self.get_player_trophies(player_id)),
            "metrics": values,
        }

    def get_join_settings(self, player_id):
        """{"style" (dict or None: the vanilla message), "level", "sounds_off"} of a player."""
        row = self._fetchone("SELECT join_style, reward_level, join_sounds_off FROM player_server_info WHERE player_id = %s",
                             (player_id,))
        return {"style": row[0], "level": row[1], "sounds_off": row[2]} if row else None

    def set_join_style(self, player_id, style):
        """style: dict or None (back to the normal message of the game)."""
        self._execute("UPDATE player_server_info SET join_style = %s WHERE player_id = %s",
                      (json.dumps(style) if style is not None else None, player_id))

    def raise_reward_level(self, player_id, level):
        """Store a newly reached level. True if it is higher than the stored one (levels are never lowered)."""
        return self._execute("UPDATE player_server_info SET reward_level = %s WHERE player_id = %s AND reward_level < %s",
                             (level, player_id, level)) > 0

    def set_join_sounds_off(self, player_id, off):
        self._execute("UPDATE player_server_info SET join_sounds_off = %s WHERE player_id = %s", (bool(off), player_id))

    def get_join_mutes(self, player_id):
        """[{"name", "uuid"}] of the players whose join/leave messages the player does not want to see."""
        return [{"name": n, "uuid": str(u)} for n, u in self._fetchall("""
            SELECT p.name, psi.mojang_uuid FROM join_mutes m JOIN player_server_info psi ON psi.player_id = m.muted_id
            JOIN player p ON p.uuid = psi.mojang_uuid WHERE m.player_id = %s ORDER BY lower(p.name)""", (player_id,))]

    def set_join_mute(self, player_id, muted_name, muted=True):
        """Mute (or unmute) the messages of a player of the same server. Returns the name, or None if unknown."""
        server_id = self.get_server_id_from_player_id(player_id)
        muted_id = self.get_player_id_from_player_name_and_server_id(muted_name, server_id)
        if muted_id is None or str(muted_id) == str(player_id):
            return None
        if muted:
            self._execute("INSERT INTO join_mutes (player_id, muted_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                          (player_id, muted_id))
        else:
            self._execute("DELETE FROM join_mutes WHERE player_id = %s AND muted_id = %s", (player_id, muted_id))
        return self.get_player_name_from_player_id(muted_id)

    def get_join_sync(self, server_id):
        """For the plugin: ([player_id of players with an own message], {uuid: (sounds_off, [muted uuids])})."""
        styled = [str(row[0]) for row in self._fetchall(
            "SELECT player_id FROM player_server_info WHERE server_id = %s AND join_style IS NOT NULL", (server_id,))]
        mutes = {}
        for uuid, sounds_off, muted in self._fetchall("""
                SELECT psi.mojang_uuid, psi.join_sounds_off,
                       COALESCE(array_agg(m.mojang_uuid::text) FILTER (WHERE m.mojang_uuid IS NOT NULL), '{}')
                FROM player_server_info psi LEFT JOIN join_mutes j ON j.player_id = psi.player_id
                LEFT JOIN player_server_info m ON m.player_id = j.muted_id
                WHERE psi.server_id = %s GROUP BY psi.mojang_uuid, psi.join_sounds_off
                HAVING psi.join_sounds_off OR count(j.muted_id) > 0""", (server_id,)):
            mutes[str(uuid)] = (sounds_off, [str(u) for u in muted])
        return styled, mutes

    ###----------------------------- Year in review ------------------------------------###

    def get_year_gains(self, server_id, year):
        """
        ({player_id: {metric: gain}}, first day) of a calendar year: the last minus the first snapshot
        within the year (a player's first snapshot is the start of the recording for them).
        """
        rows = self._fetchall("""
            WITH s AS (SELECT s.player_id, s.metric, s.day, s.value FROM stat_snapshots s
                       JOIN player_server_info psi ON psi.player_id = s.player_id
                       WHERE psi.server_id = %s AND extract(year FROM s.day) = %s)
            SELECT player_id, metric, (array_agg(value ORDER BY day DESC))[1] - (array_agg(value ORDER BY day))[1], min(day)
            FROM s GROUP BY player_id, metric""", (server_id, year))
        gains, first = {}, None
        for player_id, metric, gain, day in rows:
            gains.setdefault(str(player_id), {})[metric] = max(0, int(gain))
            first = day if first is None or day < first else first
        return gains, first

    def get_year_sessions(self, player_id, year):
        """Session facts of a player in a year: days, count, longest (seconds), per weekday/hour counts, first day."""
        days = self._fetchall("""
            SELECT DISTINCT d::date FROM player_sessions ps
            CROSS JOIN LATERAL generate_series(ps.started_at::date, COALESCE(ps.ended_at, now())::date, interval '1 day') d
            WHERE ps.player_id = %s AND extract(year FROM d) = %s ORDER BY 1""", (player_id, year))
        row = self._fetchone("""
            SELECT count(*), max(extract(epoch FROM COALESCE(ended_at, now()) - started_at)),
                   mode() WITHIN GROUP (ORDER BY extract(isodow FROM started_at)),
                   mode() WITHIN GROUP (ORDER BY extract(hour FROM started_at))
            FROM player_sessions WHERE player_id = %s AND extract(year FROM started_at) = %s""", (player_id, year))
        return {"days": [d[0] for d in days], "sessions": row[0] or 0, "longest": float(row[1] or 0),
                "weekday": int(row[2]) - 1 if row[2] is not None else None, "hour": int(row[3]) if row[3] is not None else None}

    def get_year_events(self, player_id, year):
        """Achievement tiers, trophies, records taken and milestones of a player in a year."""
        achievements = self._fetchall("""SELECT achievement, tier, earned_at FROM player_achievements
                                        WHERE player_id = %s AND extract(year FROM earned_at) = %s ORDER BY earned_at""",
                                      (player_id, year))
        trophies = [t for t in self.get_player_trophies(player_id) if t["awarded_at"].year == year]
        records = self._fetchall("""
            SELECT h.metric, h.value, h.since FROM (
                SELECT rh.*, lag(rh.id) OVER (PARTITION BY rh.server_id, rh.metric ORDER BY rh.since, rh.id) AS prev
                FROM record_history rh
                WHERE rh.server_id = (SELECT server_id FROM player_server_info WHERE player_id = %s)) h
            WHERE h.player_id = %s AND h.prev IS NOT NULL AND extract(year FROM h.since) = %s""", (player_id, player_id, year))
        milestones = [m for m in self.get_player_milestones(player_id) if m["reached_at"].year == year]
        return {"achievements": achievements, "trophies": trophies, "records": records, "milestones": milestones}

    ###----------------------------- Server directory ------------------------------------###

    def get_directory(self):
        """Listed servers that have been connected at least once, most players online first."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT s.id, s.subdomain, s.server_name, s.server_description_short, s.mc_server_domain, s.whitelist,
                       s.discord_url,
                       s.plugin_connected AND s.plugin_last_seen > now() - make_interval(secs => %s) AS online,
                       (SELECT count(*) FROM player_server_info psi WHERE psi.server_id = s.id AND psi.online) AS players_online,
                       (SELECT count(*) FROM player_server_info psi WHERE psi.server_id = s.id) AS players,
                       (SELECT count(DISTINCT ps.player_id) FROM player_sessions ps JOIN player_server_info psi
                          ON psi.player_id = ps.player_id WHERE psi.server_id = s.id
                          AND ps.started_at > now() - interval '7 days') AS active_week,
                       (SELECT filename FROM server_images si WHERE si.server_id = s.id AND si.kind = 'banner' LIMIT 1) AS banner
                FROM servers s WHERE s.listed AND s.plugin_last_seen IS NOT NULL
                ORDER BY online DESC, players_online DESC, active_week DESC, lower(s.server_name)""", (PLUGIN_ONLINE_SECONDS,))
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    ###----------------------------- Hall of fame ------------------------------------###

    def get_veterans(self, server_id, limit=10):
        """[{"name", "uuid", "first_seen"}] of the visible players who joined first."""
        return [{"name": n, "uuid": str(u), "first_seen": f} for n, u, f in self._fetchall("""
            SELECT p.name, psi.mojang_uuid, psi.first_seen FROM player_server_info psi
            JOIN player p ON p.uuid = psi.mojang_uuid
            WHERE psi.server_id = %s AND NOT psi.hide_stats AND psi.first_seen IS NOT NULL
            ORDER BY psi.first_seen LIMIT %s""", (server_id, limit))]

    def get_achievement_leaders(self, server_id, limit=10):
        """[{"name", "uuid", "tiers", "diamond"}] of the visible players with the most achievement tiers."""
        return [{"name": n, "uuid": str(u), "tiers": t, "diamond": d} for n, u, t, d in self._fetchall("""
            SELECT p.name, psi.mojang_uuid, count(*)::int, (count(*) FILTER (WHERE pa.tier = 3))::int
            FROM player_achievements pa JOIN player_server_info psi ON psi.player_id = pa.player_id
            JOIN player p ON p.uuid = psi.mojang_uuid
            WHERE psi.server_id = %s AND NOT psi.hide_stats
            GROUP BY p.name, psi.mojang_uuid ORDER BY 3 DESC, 4 DESC, lower(p.name) LIMIT %s""", (server_id, limit))]

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
        if not rows:  # hash anyway, so the response time does not tell whether the account exists
            try:
                ph.verify(_dummy_hash(), password)
            except argon2.exceptions.VerificationError:
                pass
            return None
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
