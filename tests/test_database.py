import json
from datetime import datetime, timedelta, timezone

import pytest

from database import stats
from database.databaseManagerV2 import (DatabaseManager, DatabaseNotInitializedError, MAX_LOGIN_ATTEMPTS)
from tests.conftest import OTHER_UUID, PLAYER_UUID, TEST_DB_CONFIG, FakeMinecraft


def test_schema_version_mismatch_is_rejected(db):
    with db._cursor() as cur:
        cur.execute("UPDATE schema_version SET version = 999")
    try:
        with pytest.raises(DatabaseNotInitializedError):
            DatabaseManager(db_config=TEST_DB_CONFIG, minecraft=FakeMinecraft()).close()
    finally:
        with db._cursor() as cur:
            cur.execute("UPDATE schema_version SET version = 1")


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

def test_only_active_bans_count(db, player_id):
    reason_id = db._fetchvalue("SELECT id FROM ban_reasons WHERE reason = 'hacking'")
    db.add_banned_player(player_id, None, reason_id, datetime.now(timezone.utc) - timedelta(days=1))
    assert db.get_ban_reason_from_player_id(player_id) is None
    db.add_banned_player(player_id, None, reason_id, datetime.now(timezone.utc) + timedelta(days=1))
    assert db.get_ban_reason_from_player_id(player_id) == "hacking"
    start, end = db.get_ban_start_and_ban_end_by_player_id(player_id)
    assert start < end
    db.delete_banned_player(player_id)
    assert db.get_ban_reason_from_player_id(player_id) is None


def test_prefixes(db, player_id):
    prefix_id = db.add_prefix(player_id, "TEST")
    db.update_player_prefix_by_player_id(player_id, prefix_id)
    assert db.get_prefix_id_by_player_id(player_id) == prefix_id
    db.update_prefix_text_by_prefix_id(prefix_id, "NEW")
    assert db.get_prefix_text_by_prefix_id(prefix_id) == "NEW"
    assert db.get_members_from_prefix_id(prefix_id) == [player_id]
