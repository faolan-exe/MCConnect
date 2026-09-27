import json
from datetime import datetime, timedelta, timezone

import pytest

from database import stats
from database.databaseManagerV2 import (DatabaseManager, DatabaseNotInitializedError, MAX_LOGIN_ATTEMPTS,
                                         SCHEMA_VERSION)
from tests.conftest import OTHER_UUID, PLAYER_UUID, TEST_DB_CONFIG, FakeMinecraft


def test_schema_version_mismatch_is_rejected(db):
    with db._cursor() as cur:
        cur.execute("UPDATE schema_version SET version = 999")
    try:
        with pytest.raises(DatabaseNotInitializedError):
            DatabaseManager(db_config=TEST_DB_CONFIG, minecraft=FakeMinecraft()).close()
    finally:
        with db._cursor() as cur:
            cur.execute("UPDATE schema_version SET version = %s", (SCHEMA_VERSION,))


def test_lookup_tables_are_filled(db):
    assert "stone" in db.blocks
    assert "stick" in db.items
    assert not db.blocks & db.items


# ------------------------------------------------------------------ players & servers

def test_add_player_looks_up_name_once(db):
    db.add_player(PLAYER_UUID)
    assert db.get_player_name_from_mojang_uuid(PLAYER_UUID) == "_Tobias4444"
    assert str(db.get_mojang_uuid_from_player_name("_tobias4444")) == PLAYER_UUID


def test_add_player_with_name_updates_name(db):
    db.add_player(PLAYER_UUID)
    db.add_player(PLAYER_UUID, "NewName")
    assert db.get_player_name_from_mojang_uuid(PLAYER_UUID) == "NewName"


def test_add_player_falls_back_to_uuid_if_lookup_fails(db):
    unknown = "11111111-2222-3333-4444-555555555555"
    db.add_player(unknown)
    assert db.get_player_name_from_mojang_uuid(unknown) == unknown


def test_player_can_be_on_multiple_servers(db, server, other_server):
    first = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    second = db.ensure_player_on_server(other_server["id"], PLAYER_UUID)
    assert first != second
    assert db.get_server_id_from_player_id(first) == server["id"]
    assert db.get_server_id_from_player_id(second) == other_server["id"]
    # adding again returns the existing id
    assert db.add_player_server_info(server["id"], PLAYER_UUID) == first


def test_admin_can_own_multiple_servers(db, server, other_server):
    assert server["id"] != other_server["id"]


def test_server_information_hides_key_and_is_case_insensitive(db, server):
    info = db.get_server_information_dict("TestDomain")
    assert info["id"] == server["id"]
    assert info["server_name"] == "Test Server"
    assert "server_key" not in info
    assert db.get_server_information_dict("missing") is None
    assert db.get_server_information_dict(None) is None


def test_server_id_by_auth_key(db, server):
    assert db.get_server_id_by_auth_key(server["key"]) == server["id"]
    assert db.get_server_id_by_auth_key("wrong") is None


def test_generated_server_key(db, admin_id):
    server_id = db.add_server(admin_id, "gen", "gen.example.com", "Gen")
    key = db._fetchvalue("SELECT server_key FROM servers WHERE id = %s", (server_id,))
    assert len(key) == 64


def test_join_and_quit(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID, "_Tobias4444")
    info = db.get_player_info_by_player_id(player_id)
    assert info["online"] is True
    assert info["first_seen"] is not None
    first_seen = info["first_seen"]

    assert db.register_player_quit(server["id"], PLAYER_UUID) is True
    assert db.get_online_status_by_player_id(player_id) is False

    db.register_player_join(server["id"], PLAYER_UUID)
    assert db.get_first_seen_by_player_id(player_id) == first_seen
    assert db.get_last_seen_by_player_id(player_id) >= first_seen


def test_quit_of_unknown_player(db, server):
    assert db.register_player_quit(server["id"], PLAYER_UUID) is False


def test_timestamps_are_timezone_aware(db, server):
    player_id = db.register_player_join(server["id"], PLAYER_UUID)
    first_seen = db.get_first_seen_by_player_id(player_id)
    assert first_seen.tzinfo is not None
    assert abs(first_seen - datetime.now(timezone.utc)) < timedelta(minutes=1)


def test_online_counts_and_overview(db, server, other_server):
    db.register_player_join(server["id"], PLAYER_UUID)
    db.ensure_player_on_server(server["id"], OTHER_UUID)
    db.register_player_join(other_server["id"], OTHER_UUID)

    assert db.get_online_player_count_from_subdomain("testdomain") == 1
    assert db.get_online_player_count_total() == 2
    assert db.get_players_overview_from_subdomain("testdomain") == [
        {"name": "Notch", "uuid": OTHER_UUID, "online": False},
        {"name": "_Tobias4444", "uuid": PLAYER_UUID, "online": True},
    ]
    assert [str(u) for u in db.get_all_mojang_uuids_from_subdomain("testdomain")] == [OTHER_UUID, PLAYER_UUID]
    assert db.get_online_status_by_player_uuid_and_subdomain(PLAYER_UUID, "testdomain") is True


def test_set_all_players_offline(db, server, other_server):
    db.register_player_join(server["id"], PLAYER_UUID)
    db.register_player_join(other_server["id"], PLAYER_UUID)
    assert db.set_all_players_offline(server["id"]) == 1
    assert db.get_online_player_count_from_subdomain("testdomain") == 0
    assert db.get_online_player_count_from_subdomain("other") == 1


def test_player_name_lookup_is_scoped_to_server(db, server, other_server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    assert db.get_player_id_from_player_name_and_server_id("_TOBIAS4444", server["id"]) == player_id
    assert db.get_player_id_from_player_name_and_server_id("_Tobias4444", other_server["id"]) is None
    assert db.get_player_id_from_mojang_uuid_and_subdomain(PLAYER_UUID, "TESTDOMAIN") == player_id


# ------------------------------------------------------------------ stats

STATS = {"stats": {
    "minecraft:mined": {"minecraft:stone": 10},
    "minecraft:used": {"minecraft:stone": 3, "minecraft:diamond_pickaxe": 7},
    "minecraft:custom": {"minecraft:deaths": 2, "minecraft:play_time": 72000},
    "minecraft:killed": {"minecraft:zombie": 4},
}}


def test_update_player_stats_and_read_back(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    assert db.update_player_stats(player_id, json.dumps(STATS)) == 6

    STATS["stats"]["minecraft:mined"]["minecraft:stone"] = 11
    try:
        db.update_player_stats(player_id, STATS)
    finally:
        STATS["stats"]["minecraft:mined"]["minecraft:stone"] = 10

    blocks = db.get_all_blocks_stats(player_id)
    # one list per block category, descending: mined, used, dropped, picked_up, crafted
    assert len(blocks) == len(stats.BLOCK_CATEGORIES)
    assert blocks[0] == [{"object": "minecraft:stone", "value": 11}]
    assert blocks[1] == [{"object": "minecraft:stone", "value": 3}]
    assert blocks[2:] == [[], [], []]

    tools = db.get_all_tools_stats(player_id)
    assert tools[0] == [{"object": "minecraft:diamond_pickaxe", "value": 7}]
    assert db.get_all_mobs_stats(player_id) == [[{"object": "minecraft:zombie", "value": 4}], []]
    assert db.get_all_armor_stats(player_id) == [[], [], [], [], []]
    assert db.get_value_from_unique_object_from_action_table_with_player_id("minecraft:deaths", player_id) == 2
    assert db.get_value_from_unique_object_from_action_table_with_player_id("minecraft:missing", player_id) is None


def test_stats_are_separated_per_server(db, server, other_server):
    first = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    second = db.ensure_player_on_server(other_server["id"], PLAYER_UUID)
    db.update_player_stats(first, STATS)
    assert db.get_all_custom_stats(second) == [[]]


def test_update_player_stats_rejects_invalid_json(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    with pytest.raises(ValueError):
        db.update_player_stats(player_id, "not json")


# ------------------------------------------------------------------ player login

@pytest.fixture
def player_id(db, server):
    return db.register_player_join(server["id"], PLAYER_UUID)


def test_login_pin_success_deletes_entry(db, player_id):
    db.add_login_entry_from_player_id(player_id, 123456)
    assert db.verify_player_login(player_id, 123456) == [True]
    assert db.verify_player_login(player_id, 123456) == [False, "no entry found in the database"]


def test_login_pin_wrong_then_right(db, player_id):
    db.add_login_entry_from_player_id(player_id, 123456)
    assert db.verify_player_login(player_id, 111111) == [False, "wrong pin provided"]
    assert db.verify_player_login(player_id, 123456) == [True]


def test_login_pin_too_many_attempts(db, player_id):
    db.add_login_entry_from_player_id(player_id, 123456)
    for _ in range(MAX_LOGIN_ATTEMPTS - 1):
        assert db.verify_player_login(player_id, 1)[1] == "wrong pin provided"
    assert db.verify_player_login(player_id, 1) == [False, "too many attempts"]
    assert db.verify_player_login(player_id, 123456) == [False, "no entry found in the database"]


def test_login_pin_timeout(db, player_id):
    db.add_login_entry_from_player_id(player_id, 123456)
    with db._cursor() as cur:
        cur.execute("UPDATE login SET created_at = now() - interval '6 minutes'")
    assert db.verify_player_login(player_id, 123456) == [False, "timeout reached"]


def test_new_login_pin_resets_attempts(db, player_id):
    db.add_login_entry_from_player_id(player_id, 123456)
    db.verify_player_login(player_id, 1)
    db.add_login_entry_from_player_id(player_id, 654321)
    assert db._fetchvalue("SELECT attempts FROM login WHERE player_id = %s", (player_id,)) == 0
    assert db.verify_player_login(player_id, 654321) == [True]


# ------------------------------------------------------------------ server admins

def test_admin_password_is_hashed(db):
    db.add_server_admin("alice", "secret123", "alice@example.com", email_verified=True)
    stored = db._fetchvalue("SELECT password FROM server_admins WHERE username = 'alice'")
    assert stored != "secret123" and stored.startswith("$argon2")
    assert db.verify_admin_login("alice", "secret123") is True
    assert db.verify_admin_login("alice", "wrong") is False
    assert db.verify_admin_login("nobody", "secret123") is False


def test_admin_login_requires_verified_email(db):
    admin_id = db.add_server_admin("bob", "secret123", "bob@example.com")
    assert db.verify_admin_login("bob", "secret123") is False
    token = db.create_email_verification(admin_id)
    assert db.verify_signupcode("bob", "wrong-token") is False
    assert db.verify_signupcode("bob", token) is True
    assert db.verify_admin_login("bob", "secret123") is True
    # tokens are single use
    assert db.verify_signupcode("bob", token) is False


def test_email_verification_expires(db):
    admin_id = db.add_server_admin("carl", "secret123", "carl@example.com")
    token = db.create_email_verification(admin_id)
    with db._cursor() as cur:
        cur.execute("UPDATE email_verification SET created_at = now() - interval '25 hours'")
    assert db.verify_signupcode("carl", token) is False


# ------------------------------------------------------------------ bans & prefixes






# ------------------------------------------------------------------ migrations & server admin

def test_migration_from_version_1(db):
    with db._cursor() as cur:
        # back to schema version 1
        cur.execute("DROP TABLE events, event_signups, polls, poll_votes, builds, build_likes, guestbook")
        cur.execute("DROP TABLE duels, reports")
        cur.execute("ALTER TABLE player_server_info DROP COLUMN sidebar")
        cur.execute("DROP TABLE player_milestones, record_history, community_goals, trophies")
        cur.execute("ALTER TABLE competitions DROP COLUMN awarded")
        cur.execute("ALTER TABLE servers DROP COLUMN weekly_awarded_until")
        cur.execute("DROP TABLE mod_log, player_notes, server_health")
        cur.execute("ALTER TABLE player_achievements DROP COLUMN silent")
        cur.execute("DROP TABLE player_favorites")
        cur.execute("ALTER TABLE player_server_info DROP COLUMN bio, DROP COLUMN hide_stats")
        cur.execute("DROP TABLE player_achievements, competitions")
        cur.execute("DROP TABLE player_sessions")
        cur.execute("DROP TABLE stat_snapshots")
        cur.execute("DROP INDEX prefixes_server_text_idx; DROP INDEX prefixes_owner_idx")
        cur.execute("ALTER TABLE prefixes DROP COLUMN server_id, DROP COLUMN color")
        cur.execute("ALTER TABLE prefixes RENAME COLUMN password_hash TO password")
        cur.execute("ALTER TABLE player_server_info DROP COLUMN is_op")
        cur.execute("ALTER TABLE servers DROP COLUMN auto_mod_ops")
        cur.execute("ALTER TABLE banned_players DROP COLUMN reason_text, DROP COLUMN banned_by, DROP COLUMN source")
        cur.execute("DROP TABLE server_images")
        cur.execute("ALTER TABLE servers DROP COLUMN whitelist")
        cur.execute("ALTER TABLE servers ALTER COLUMN mc_server_domain SET NOT NULL")
        cur.execute("DROP TABLE password_reset")
        cur.execute("ALTER TABLE server_admins DROP COLUMN password_changed_at")
        cur.execute("ALTER TABLE servers DROP COLUMN plugin_connected, DROP COLUMN plugin_last_seen")
        cur.execute("UPDATE schema_version SET version = 1")
    db.migrate()
    assert db.get_schema_version() == SCHEMA_VERSION
    assert db._fetchvalue("SELECT count(*) FROM information_schema.columns "
                          "WHERE table_name = 'servers' AND column_name LIKE 'plugin_%%'") == 2
    assert db._fetchvalue("SELECT to_regclass('public.password_reset') IS NOT NULL")
    db.migrate()  # nothing left to do
    assert db.get_schema_version() == SCHEMA_VERSION


def test_foreign_tables_without_version_are_rejected(db):
    with db._cursor() as cur:
        cur.execute("ALTER TABLE schema_version RENAME TO schema_version_tmp")
    try:
        with pytest.raises(DatabaseNotInitializedError):
            DatabaseManager(db_config=TEST_DB_CONFIG, minecraft=FakeMinecraft()).close()
    finally:
        with db._cursor() as cur:
            cur.execute("ALTER TABLE schema_version_tmp RENAME TO schema_version")


def test_plugin_online_status(db, server):
    assert db.is_plugin_online(server["id"]) is False
    db.set_plugin_connected(server["id"], True)
    assert db.is_plugin_online(server["id"]) is True
    with db._cursor() as cur:
        cur.execute("UPDATE servers SET plugin_last_seen = now() - interval '5 minutes'")
    assert db.is_plugin_online(server["id"]) is False  # connected flag is stale
    db.touch_plugin(server["id"])
    assert db.is_plugin_online(server["id"]) is True
    db.set_plugin_connected(server["id"], False)
    assert db.is_plugin_online(server["id"]) is False


def test_servers_by_owner(db, admin_id, server, other_server):
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    servers = db.get_servers_by_owner(admin_id)
    assert [s["subdomain"] for s in servers] == ["testdomain", "other"]
    assert servers[0]["server_key"] == server["key"]
    assert servers[0]["player_count"] == 1
    assert servers[0]["plugin_online"] is False
    other_admin = db.add_server_admin("eve", "secret123", "eve@example.com")
    assert db.get_servers_by_owner(other_admin) == []


def test_update_regenerate_delete_only_for_owner(db, admin_id, server):
    intruder = db.add_server_admin("eve", "secret123", "eve@example.com")
    assert db.update_server(server["id"], intruder, server_name="Hacked") is False
    assert db.regenerate_server_key(server["id"], intruder) is None
    assert db.delete_server(server["id"], intruder) is None

    assert db.update_server(server["id"], admin_id, server_name="New", server_key="x" * 64) is True
    info = db.get_server_information_dict("testdomain")
    assert info["server_name"] == "New"
    assert db.get_server_id_by_auth_key(server["key"]) == server["id"]  # key is not an editable field

    new_key = db.regenerate_server_key(server["id"], admin_id)
    assert new_key != server["key"] and len(new_key) == 64
    assert db.get_server_id_by_auth_key(server["key"]) is None

    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    assert db.delete_server(server["id"], admin_id) == []
    assert db.get_server_information_dict("testdomain") is None
    assert db._fetchvalue("SELECT count(*) FROM player_server_info") == 0


def test_concurrent_migrations_on_empty_database(db):
    """web workers and the socket server start at the same time and all try to create the schema."""
    import threading
    with db._cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    managers = [DatabaseManager(db_config=TEST_DB_CONFIG, minecraft=FakeMinecraft(), check_schema=False)
                for _ in range(4)]
    # Like the constructor does: this caches "schema_version does not exist" in the pooled
    # connection, which a later lookup in the same session must not reuse.
    for manager in managers:
        assert manager.get_schema_version() is None
    errors = []
    barrier = threading.Barrier(len(managers))

    def run(manager):
        barrier.wait()
        try:
            manager.migrate()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(m,)) for m in managers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for m in managers:
        m.close()
    assert errors == []
    assert db.get_schema_version() == SCHEMA_VERSION
    assert db._fetchvalue("SELECT count(*) FROM schema_version") == 1


# ------------------------------------------------------------------ signup corrections

def test_signup_replaces_unverified_account_with_same_username(db):
    old_id = db.add_server_admin("bob", "secret123", "typo@example.con")
    db.create_email_verification(old_id)
    new_id = db.add_server_admin("bob", "secret456", "bob@example.com", replace_unverified=True)
    assert new_id != old_id
    assert db._fetchvalue("SELECT email FROM server_admins WHERE username = 'bob'") == "bob@example.com"
    assert db._fetchvalue("SELECT count(*) FROM email_verification") == 0  # old token removed


def test_signup_replaces_unverified_account_with_same_email(db):
    db.add_server_admin("typoname", "secret123", "bob@example.com")
    db.add_server_admin("bob", "secret456", "Bob@Example.com", replace_unverified=True)
    assert db.get_admin_id_by_username("typoname") is None


def test_signup_never_replaces_verified_account(db, admin_id):
    import psycopg2.errors
    with pytest.raises(psycopg2.errors.UniqueViolation):
        db.add_server_admin("tobi", "hijack123", "evil@example.com", replace_unverified=True)
    assert db.verify_admin_login("tobi", "testPassword") is True


def test_signup_never_replaces_account_owning_servers(db):
    import psycopg2.errors
    owner = db.add_server_admin("owner", "secret123", "owner@example.com")
    db.add_server(owner, "owned", "owned.example.com", "Owned")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        db.add_server_admin("owner", "secret456", "other@example.com", replace_unverified=True)


def test_renew_email_verification(db, admin_id):
    bob = db.add_server_admin("bob", "secret123", "bob@example.com")
    old_token = db.create_email_verification(bob)
    assert db.renew_email_verification("bob@example.com") is None  # last mail too recent
    with db._cursor() as cur:
        cur.execute("UPDATE email_verification SET created_at = now() - interval '2 minutes'")
    username, token = db.renew_email_verification("BOB@example.com")
    assert username == "bob" and token != old_token
    assert db.verify_signupcode("bob", old_token) is False
    assert db.verify_signupcode("bob", token) is True
    assert db.renew_email_verification("bob@example.com") is None      # already verified
    assert db.renew_email_verification("tobi@example.com") is None     # verified account
    assert db.renew_email_verification("nobody@example.com") is None


# ------------------------------------------------------------------ password reset

def test_password_reset(db, admin_id):
    changed_before = db.get_admin_password_changed_at(admin_id)
    username, token = db.create_password_reset("TOBI@example.com")
    assert username == "tobi"
    assert db._fetchvalue("SELECT token_hash FROM password_reset") != token  # only the hash is stored
    assert db.get_password_reset_username(token) == "tobi"
    assert db.reset_password(token, "brandNew123") == "tobi"
    assert db.verify_admin_login("tobi", "brandNew123") is True
    assert db.verify_admin_login("tobi", "testPassword") is False
    assert db.get_admin_password_changed_at(admin_id) > changed_before
    # single use
    assert db.reset_password(token, "again12345") is None
    assert db.get_password_reset_username(token) is None


def test_password_reset_only_for_verified_accounts(db):
    db.add_server_admin("bob", "secret123", "bob@example.com")
    assert db.create_password_reset("bob@example.com") is None
    assert db.create_password_reset("nobody@example.com") is None


def test_password_reset_rate_limit_and_expiry(db, admin_id):
    _, token = db.create_password_reset("tobi@example.com")
    assert db.create_password_reset("tobi@example.com") is None  # too soon
    with db._cursor() as cur:
        cur.execute("UPDATE password_reset SET created_at = now() - interval '61 minutes'")
    assert db.get_password_reset_username(token) is None
    assert db.reset_password(token, "brandNew123") is None
    _, new_token = db.create_password_reset("tobi@example.com")  # allowed again
    assert db.reset_password(new_token, "brandNew123") == "tobi"


def test_wrong_token_does_not_reset(db, admin_id):
    db.create_password_reset("tobi@example.com")
    assert db.reset_password("wrong", "brandNew123") is None
    assert db.verify_admin_login("tobi", "testPassword") is True


def test_admin_login_with_username_or_email(db, admin_id):
    assert db.authenticate_admin("tobi", "testPassword") == (admin_id, "tobi")
    assert db.authenticate_admin("TOBI@Example.com", "testPassword") == (admin_id, "tobi")
    assert db.authenticate_admin("tobi@example.com", "wrong") is None
    assert db.authenticate_admin("Tobi", "testPassword") is None  # usernames are exact
    assert db.authenticate_admin("nobody@example.com", "testPassword") is None



def test_whitelist_server_without_address(db, admin_id):
    server_id = db.add_server(admin_id, "private", None, "Private", whitelist=True)
    info = db.get_server_information_dict("private")
    assert info["id"] == server_id and info["whitelist"] is True and info["mc_server_domain"] is None
    assert db.update_server(server_id, admin_id, whitelist=False, mc_server_domain="play.example.com") is True
    assert db.get_server_information_dict("private")["whitelist"] is False



# ------------------------------------------------------------------ server images

def test_server_images(db, admin_id, server):
    from database.databaseManagerV2 import MAX_GALLERY_IMAGES
    image_id, replaced = db.add_server_image(server["id"], admin_id, "banner", "a" * 32 + ".webp")
    assert replaced == []
    _, replaced = db.add_server_image(server["id"], admin_id, "banner", "b" * 32 + ".webp")
    assert replaced == ["a" * 32 + ".webp"]
    for i in range(MAX_GALLERY_IMAGES):
        assert db.add_server_image(server["id"], admin_id, "gallery", f"{i:032x}.webp") is not None
    assert db.add_server_image(server["id"], admin_id, "gallery", "f" * 32 + ".webp") is None  # full
    images = db.get_server_images(server["id"])
    assert images["banner"] == "b" * 32 + ".webp" and len(images["gallery"]) == MAX_GALLERY_IMAGES

    intruder = db.add_server_admin("eve", "secret123", "eve@example.com")
    first = images["gallery"][0]
    assert db.add_server_image(server["id"], intruder, "gallery", "e" * 32 + ".webp") is None
    assert db.delete_server_image(first["id"], intruder) is None
    assert db.delete_server_image(first["id"], admin_id) == first["filename"]

    filenames = db.delete_server(server["id"], admin_id)
    assert len(filenames) == MAX_GALLERY_IMAGES  # banner + 11 gallery images
    assert db._fetchvalue("SELECT count(*) FROM server_images") == 0



# ------------------------------------------------------------------ prefixes

def test_own_prefix_create_update_delete(db, server):
    owner = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    member = db.ensure_player_on_server(server["id"], OTHER_UUID)
    assert db.save_own_prefix(owner, "Bauteam", "gold") == [PLAYER_UUID]
    prefix = db.get_player_prefix(owner)
    assert (prefix["text"], prefix["color"], prefix["owner_name"], prefix["has_password"]) == ("Bauteam", "gold", "_Tobias4444", False)

    assert db.join_prefix(member, prefix["prefix_id"]) == "ok"
    assert sorted(db.save_own_prefix(owner, "Baumeister", "red")) == sorted([PLAYER_UUID, OTHER_UUID])
    assert db.get_player_prefix(member)["text"] == "Baumeister"
    assert db.list_prefixes(server["id"])[0]["members"] == 2
    assert db.get_all_worn_prefixes(server["id"]) == {PLAYER_UUID: ("Baumeister", "red"), OTHER_UUID: ("Baumeister", "red")}

    assert sorted(db.delete_own_prefix(owner)) == sorted([PLAYER_UUID, OTHER_UUID])
    assert db.get_player_prefix(member) is None and db.list_prefixes(server["id"]) == []


def test_prefix_password_is_hashed_and_checked(db, server):
    owner = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    member = db.ensure_player_on_server(server["id"], OTHER_UUID)
    db.save_own_prefix(owner, "Geheim", "dark_purple", password="clanpass")
    stored = db._fetchvalue("SELECT password_hash FROM prefixes")
    assert stored.startswith("$argon2")
    prefix_id = db.get_owned_prefix(owner)["prefix_id"]
    assert db.join_prefix(member, prefix_id, "falsch") == "wrong password"
    assert db.join_prefix(member, prefix_id, "clanpass") == "ok"
    # updating without a password keeps it, remove_password drops it
    db.save_own_prefix(owner, "Geheim", "blue")
    assert db.get_owned_prefix(owner)["has_password"] is True
    db.save_own_prefix(owner, "Geheim", "blue", remove_password=True)
    assert db.get_owned_prefix(owner)["has_password"] is False
    db.leave_prefix(member)
    assert db.get_player_prefix(member) is None


def test_prefix_text_unique_per_server_but_not_globally(db, server, other_server):
    import psycopg2.errors
    first = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    second = db.ensure_player_on_server(server["id"], OTHER_UUID)
    db.save_own_prefix(first, "Clan", "red")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        db.save_own_prefix(second, "clan", "blue")
    elsewhere = db.ensure_player_on_server(other_server["id"], OTHER_UUID)
    db.save_own_prefix(elsewhere, "Clan", "blue")
    # a prefix of another server cannot be joined
    assert db.join_prefix(elsewhere, db.get_owned_prefix(first)["prefix_id"]) == "not found"


# ------------------------------------------------------------------ moderation

def test_moderators_and_auto_op(db, admin_id, server):
    player = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    op = db.ensure_player_on_server(server["id"], OTHER_UUID)
    db.set_player_op(server["id"], OTHER_UUID, True)
    assert not db.is_moderator(player) and not db.is_moderator(op)

    assert db.set_moderator(server["id"], "_tobias4444", True) == "_Tobias4444"
    assert db.set_moderator(server["id"], "nobody", True) is None
    assert db.is_moderator(player)
    assert [m["name"] for m in db.list_moderators(server["id"])] == ["_Tobias4444"]

    db.update_server(server["id"], admin_id, auto_mod_ops=True)
    assert db.is_moderator(op)
    assert {m["name"]: m["explicit"] for m in db.list_moderators(server["id"])} == {"_Tobias4444": True, "Notch": False}

    db.set_moderator(server["id"], "_Tobias4444", False)
    assert not db.is_moderator(player)


# ------------------------------------------------------------------ bans

def test_web_ban_with_reason_default_duration(db, server):
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    reason_id = db._fetchvalue("SELECT id FROM ban_reasons WHERE reason = 'hacking'")
    ban = db.ban_player(server["id"], "_tobias4444", "Mod Max", reason_id=reason_id, comment="Killaura")
    assert ban["name"] == "_Tobias4444" and ban["reason"] == "hacking"
    assert timedelta(days=364) < ban["end"] - datetime.now(timezone.utc) < timedelta(days=366)
    [active] = db.list_active_bans(server["id"])
    assert (active["source"], active["banned_by"], active["comment"]) == ("web", "Mod Max", "Killaura")
    assert db.ban_player(server["id"], "nobody", "x") is None


def test_permanent_ban_and_unban(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    ban = db.ban_player(server["id"], "_Tobias4444", "Admin", days=0)
    assert ban["end"] is None and ban["reason"] == "Gebannt"
    assert db.get_ban_reason_from_player_id(player_id) == "Gebannt"
    assert db.get_ban_start_and_ban_end_by_player_id(player_id)[1] is None
    assert db.unban_player(server["id"], player_id) == {"uuid": PLAYER_UUID, "name": "_Tobias4444"}
    assert db.get_ban_reason_from_player_id(player_id) is None


def test_expired_bans_are_not_active(db, server):
    player_id = db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.add_banned_player(player_id, None, None, datetime.now(timezone.utc) - timedelta(days=1), reason_text="alt")
    assert db.get_ban_reason_from_player_id(player_id) is None
    assert db.list_active_bans(server["id"]) == []


def test_ingame_ban_sync_keeps_web_bans(db, server):
    db.ensure_player_on_server(server["id"], PLAYER_UUID)
    db.ensure_player_on_server(server["id"], OTHER_UUID)
    db.ban_player(server["id"], "_Tobias4444", "Admin", days=3)
    in_a_day = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp() * 1000)
    assert db.sync_ingame_bans(server["id"], [{"name": "Notch", "reason": "Spam", "source": "Steve", "expires": in_a_day}]) == 1
    sources = {b["name"]: (b["source"], b["banned_by"]) for b in db.list_active_bans(server["id"])}
    assert sources == {"_Tobias4444": ("web", "Admin"), "Notch": ("ingame", "Steve")}
    db.sync_ingame_bans(server["id"], [])
    assert [b["name"] for b in db.list_active_bans(server["id"])] == ["_Tobias4444"]
