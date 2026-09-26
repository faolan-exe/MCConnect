import ast
from datetime import datetime, timedelta
import html
import json
import re
import secrets
import string
import threading
import time
import traceback
import uuid
import argon2
import os
import psycopg2
import functools

from colorlogx import get_logger
import logging
from .minecraft import Minecraft
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

RESET_DATABASE = False
PREFILL_DATABASE = True

TABLE_COUNT = 12
LOWEST_WEB_ACCESS_LEVEL = 0
DEFAULT_WEB_ACCESS_LEVEL = 3
BAN_REASONS = [
    ["breaking the rules", 30],
    ["hacking", 365],
    ["spamming", 1],
    ["other", 7],
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)

ph = argon2.PasswordHasher()
logger = get_logger("databaseManager",logging.DEBUG)
minecraft = Minecraft()

def read_sql_file(filepath):
    with open(filepath, "r") as file:
        return file.read()

def generate_secure_token(length=64):
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))

def db_error_handler(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as e:
            logger.error(f"Database error in {method.__name__}: {e}")
            if self.conn:
                self.conn.rollback()
            raise
    return wrapper

def decorate_all_db_methods(cls):
    blacklist = [""]
    for attr_name, attr_value in cls.__dict__.items():
        if callable(attr_value) and not attr_name.startswith("__") and attr_name not in blacklist:
            setattr(cls, attr_name, db_error_handler(attr_value))
    return cls

@decorate_all_db_methods
class DatabaseManager:
    def __init__(self):
        self.CURRENT_DOMAIN = open(os.path.join(PROJECT_ROOT, "DOMAIN.txt"), "r").readline().strip()
        logger.debug("Initializing database manager")
        self.conn = psycopg2.connect(
            database="mcConnect-TestDB-1",
            host="localhost",
            user="admin",
            password="admin",
            port="5432",
        )
        logger.info("Established connection to the database")
        self.cursor = self.conn.cursor()

        if (not self._check_database_integrity()) or RESET_DATABASE:
            print("RESET DATABASE")
            print(self._check_database_integrity())
            self._reset_database()
            self._prefill_database()

    ################################ DB INIT FUNCTIONS ###################################
    
    def _check_database_integrity(self):
        """
        Checks the integrity of the database by comparing the number of tables with the expected count.

        Parameters:
        self (DatabaseManager): The instance of the DatabaseManager class.

        Returns:
        bool: True if the database integrity check passes, False otherwise.
        """
        logger.debug("check_database_integrity is called")
        logger.debug(f"Expected table count: {TABLE_COUNT}")
        actual_table_count = len(self._get_all_tables())
        if actual_table_count < TABLE_COUNT:
            logger.critical("Database integrity check failed.")
            logger.debug(f"Actual table count: {actual_table_count}")
            return False
        else:
            logger.info("Database integrity check passed.")
            return True
    
    def _get_all_tables(self):
        """
        Retrieves all table names from the connected database.

        Parameters:
        None

        Returns:
        tables (list): A list of tuples, where each tuple contains one table name.
        """
        logger.debug("get_all_tables is called")
        query = "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');"
        logger.debug(f"executing SQL query: {query}")
        self.cursor.execute(query)
        tables = self.cursor.fetchall()
        logger.debug(f"Table names retrieved: {tables}")
        return tables

    def _reset_database(self):
        """
        WARNING: This function wipes the whole database.

        This function drops the existing database schema and recreates it by executing the SQL query from the initDB.sql file.
        It then commits the changes and prints a success message.

        Returns:
        bool: True if the tables are successfully initiated, False otherwise.

        Raises:
        Exception: If an error occurs while executing the SQL query or committing the changes.
        """
        logger.debug("drop_db is called")
        logger.warning("\nDropping database in 5 Seconds!!!\n\n!!!! To cancel press CTRL+C !!!!\n")
        for i in range(5, 0, -1):
            print(f"Reset in {i}...")
            time.sleep(0.5)
        query = "DROP SCHEMA public CASCADE;CREATE SCHEMA public;"
        logger.debug(f"executing SQL query: {query}")
        self.cursor.execute(query)
        logger.warning("Database dropped")
        self.conn.commit()
        self._init_tables()

    def _init_tables(self):
        logger.debug("init_tables is called")
        query = read_sql_file(os.path.join(BASE_DIR, "queries", "initDBv2.sql"))
        logger.debug(f"executing SQL query: {query}")
        self.cursor.execute(query)
        self.conn.commit()
        logger.info(f"Tables initiated successfully")

    ################################ PREFILL FUNCTIONS ###################################
    
    def _prefill_database(self):
        logger.debug("prefill_database is called")
        self.fill_item_blocks_lookup_table(os.path.join(BASE_DIR, "blocks.json"))
        self.fill_item_items_lookup_table(os.path.join(BASE_DIR, "itemlist.json"))
        for ban_reason in BAN_REASONS:
            self.add_ban_reason(*ban_reason)


        if PREFILL_DATABASE:
            logger.info("Prefilling database")
            self.add_player("4ebe5f6f-c231-4315-9d60-097c48cc6d30")
            server_id = self.add_server_admin(
                "tobi",
                "testPassword",
                "tobi@t-auer.com",
                True
            )

            self.add_server(
                server_id,
                "testDomain",
                "mc.t-auer.com",
                "Test Server",
                generate_secure_token(64)
            )

            self.add_player_server_info(
                1,
                "4ebe5f6f-c231-4315-9d60-097c48cc6d30"
            )
            player_id = self.get_player_id_from_mojang_uuid_and_server_id("4ebe5f6f-c231-4315-9d60-097c48cc6d30", 1)
            self.add_prefix(player_id, "TEST PREFIX")



        logger.info("Prefill completed")

    def fill_item_blocks_lookup_table(self, block_list_file_path):
        with open(block_list_file_path, 'r') as block_list_file:
            json_data = json.load(block_list_file)
        
        names = [block["name"] for block in json_data]
            
        query = "INSERT INTO block_lookup (blocks) VALUES (%s)"
        self.cursor.executemany(query, [(element,) for element in names])
        logger.info(f"Inserted {self.cursor.rowcount} rows successfully.")
        self.conn.commit()
        return True

    def fill_item_items_lookup_table(self, item_list_file_path):
        with open(item_list_file_path, 'r') as item_list_file:
            json_data = json.load(item_list_file)
        
        names = [item["id"] for item in json_data]
            
        query = """ INSERT INTO item_lookup (items)
                    SELECT %s
                    WHERE NOT EXISTS (
                    SELECT 1 FROM block_lookup WHERE "blocks" = %s
                    );
                """
        tools_substrings = ["axe", "shovel", "hoe", "sword", "pickaxe", "shield", "flint_and_steel"]
        armor_substrings = ["boots", "leggings", "chestplate", "helmet"]
        self.cursor.executemany(query, [(element, element) for element in names if not any(substring in element for substring in tools_substrings) and not any(substring in element for substring in armor_substrings)])
        logger.info(f"Inserted {self.cursor.rowcount} rows successfully.")
        self.conn.commit()


    ################################ ADD FUNCTIONS ####################################
    
    def add_player_server_info(self, server_id, mojang_uuid, web_access_permissions=DEFAULT_WEB_ACCESS_LEVEL):
        """
        Adds the player's server information to the database.
    
        Parameters:
        server_id (int): The ID of the server.
        mojang_uuid (str): The UUID of the player.
        web_access_permissions (int, optional): The web access permissions of the player. Defaults to DEFAULT_WEB_ACCESS_LEVEL.

        Returns:
        String: The ID of the added player server info.
        """
        query = "INSERT INTO player_server_info (mojang_uuid, server_id, web_access_permissions) VALUES (%s, %s, %s)"
        data = (mojang_uuid, server_id, web_access_permissions)
        self.cursor.execute(query, data)
        self.conn.commit()
    
    def add_player(self, mojang_uuid):
        """
        Adds mojang_uuid and minecraft_username to the database.
        
        Parameters:
        mojang_uuid (str): The UUID of the player.

        Returns:
        None
        """
        logger.debug("add_player is called")
        player_name = minecraft.get_player_name_from_mojang_uuid_online(mojang_uuid)
        data = (mojang_uuid, player_name)
        query = """INSERT INTO player (uuid, name)
                    VALUES (%s, %s)
                    ON CONFLICT (uuid) DO NOTHING;
                """
        logger.debug(f"with following data: {data}")
        logger.debug(f'Found playername: "{player_name}" for uuid: {mojang_uuid}')
        self.cursor.execute(query, data)
        self.conn.commit()
        
    def add_server(self, owner_id, subdomain, mc_server_domain, server_name, server_key, server_description_short="SHORT DESCR", server_description_long="LONG DESCR",  discord_url=None):
        """
        Adds the specified server to the database.
        
        Parameters:
        owner_username (str): The username of the server owner.
        subdomain (str): The subdomain of the server.
        mc_server_domain (str): The domain of the server.
        server_name (str): The name of the server.
        server_key (str): The key of the server.
        server_description_short (str, optional): The short description of the server. Defaults to "SHORT DESCR".
        server_description_long (str, optional): The long description of the server. Defaults to "LONG DESCR".
        discord_url (str, optional): The discord url of the server. Defaults to None.

        Returns:
        int: The ID of the added server.
        """
        query = "INSERT INTO servers (owner_id, subdomain, mc_server_domain, server_name, server_key, server_description_short, server_description_long, discord_url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        data = (owner_id, subdomain, mc_server_domain, server_name, server_key, server_description_short, server_description_long, discord_url)
        self.cursor.execute(query, data)
        self.conn.commit() 
        return self.cursor.lastrowid
    
    def add_prefix(self, player_id, prefix_text, password=None):
        """
        adds the specified prefix to the database
        
        Parameters:
        player_id (int): The ID of the player.
        prefix_text (str): The prefix to add.
        password (str, optional): The password for the prefix. Defaults to None.

        Returns:
        int: The ID of the added prefix.
        """
        query = "INSERT INTO prefixes (prefix_owner_id, prefix_text, password) VALUES (%s, %s, %s)"
        data = (player_id, prefix_text, password)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        self.conn.commit()
        return self.cursor.lastrowid
    
    def add_server_admin(self, username, password, email, email_verified=False):
        """
        Adds the specified server admin to the database.
        
        Parameters:
        username (str): The username of the server admin.
        password (str): The password of the server admin.
        email (str): The email of the server admin. 
        email_verified (bool, optional): Whether the email has been verified. Defaults to False.
    
        Returns:
        int: The ID of the added server admin.
        """
        query = "INSERT INTO server_admins (username, password, email, email_verified) VALUES (%s, %s, %s, %s) RETURNING id"
        data = (username, password, email, email_verified)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        self.conn.commit()
        id = self.cursor.fetchone()[0]
        return id

    def add_ban_reason(self, reason, durarion_in_days):
        """
        Adds a ban reason to the database.
        
        Parameters:
        reason (str): The reason for the ban.
        durarion_in_days (int): The duration of the ban in days.

        Returns:
        int: The ID of the added ban reason.
        """
        query = "INSERT INTO ban_reasons (reason, ban_duration_in_days) VALUES (%s, %s)"
        data = (reason, durarion_in_days)
        self.cursor.execute(query, data)
        self.conn.commit()
        return self.cursor.lastrowid

    def add_banned_player(self, banned_player_id, moderator_id, ban_reason_id, ban_end, comment=None):
        logger.debug("add_banned_player is called")
        query = """ INSERT INTO banned_players (banned_player_id, moderator_id, ban_reason_id, ban_start, ban_end, comment)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s, %s);"""
        data = (banned_player_id, moderator_id, ban_reason_id, ban_end, comment)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        self.conn.commit()
        logger.info(f'Added banned player: "{banned_player_id}" with admin: "{moderator_id}" and ban reason: "{ban_reason_id}" until: "{ban_end}"')
    
    # not used
    def add_login_entry(self, player_id):
        logger.debug("add_login_entry is called")
        self.delete_login_entry(player_id)
        pin = os.random.randint(104371, 999763)
        query = """INSERT INTO login (player_id, pin, timestamp) VALUES (%s, %s, CURRENT_TIMESTAMP)"""
        data = (player_id, pin)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        self.conn.commit()
        logger.info(f'Added login entry for player: "{player_id}" with pin: "{pin}"')

    def delete_login_entry(self, player_id):
        logger.debug("delete_login_entry is called")
        query = "DELETE FROM login WHERE player_id = %s"
        data = (player_id,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        self.conn.commit()
        logger.info(f'Deleted login entry for player: "{player_id}"')

    def add_login_entry_from_player_id(self, player_id, pin):
        logger.debug("add_login_entry is called")
        self.delete_login_entry(player_id)
        query = """INSERT INTO login (player_id, pin, timestamp) VALUES (%s, %s, CURRENT_TIMESTAMP)"""
        data = (player_id, pin)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        try:
            self.cursor.execute(query, data)
            self.conn.commit()
            logger.info(f'Added login entry for player: "{player_id}" with pin: "{pin}"')
        except Exception as e:
            logger.error(f'Failed to add login entry for player: "{player_id}". Error: {e}')
            self.conn.rollback()
            return False
        return True

    ###----------------------------- Update Functions ------------------------------------###
    def update_email_verification(self, username, code):
        logger.debug("update_email_verification is called")
        query = """
            UPDATE server_admins
            SET email_verified = TRUE
            FROM email_verification
            WHERE server_admins.verification_process_id = email_verification.id
            AND server_admins.username = %s
            AND email_verification.verification_code = %s
            AND email_verification.verification_timestamp > NOW() - INTERVAL '5 minutes';
        """
        data = (username, code)
        self.cursor.execute(query, data)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.conn.commit()

        updated_rows = self.cursor.rowcount
        logger.debug(f"rows updated: {updated_rows}")

        return updated_rows > 0

    def update_player_stats(self, player_id, stats):
        logger.info("update_player_stats is called")
        items = self.split_items_from_json(stats)
        query = """ INSERT INTO actions (player_id, object, category, value)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (player_id, object, category)
                    DO UPDATE SET
                    "value" = EXCLUDED.value
                    WHERE actions.value IS DISTINCT FROM EXCLUDED.value;"""
        data = [(player_id, item[0], item[1], item[2]) for item in items if item[2] != 0]

        logger.debug(f"Executing SQL query: {query}")
        logger.debug(f"With following data: {data}")
        
        #start_time = time.time()
        self.cursor.executemany(query, data)
        self.conn.commit()
        #end_time = time.time()
    
        #elapsed_time = end_time - start_time
        logger.info(f'Updated player: "{player_id}" stats with {self.cursor.rowcount} items.')
        self.conn.commit()
        #logger.info(f'Updated player: "{player_id}" stats with {self.cursor.rowcount} items in {elapsed_time} seconds.')

        return True

    def update_player_status_from_mojang_uuid_and_server_id(self, mojang_uuid, server_id, status):
        logger.info("update_player_status_from_mojang_uuid_and_server_id is called")
        query = """
                UPDATE player_server_info
                SET online = %s
                WHERE mojang_uuid = %s
                AND server_id = %s
                """
        data = (True if status == "online"  else False, mojang_uuid, server_id)

        logger.debug(f"Executing SQL query: {query}")
        logger.debug(f"With following data: {data}")
        self.cursor.execute(query, data)
        self.conn.commit()
        

    ################################ GET FUNCTIONS ####################################

    ###----------------------------- PLAYER IDs ------------------------------------###
    def get_player_id_from_mojang_uuid_and_server_id(self, mojang_uuid, server_id):
        logger.debug("get_player_id_from_mojang_uuid_and_server_id is called")
        query = "SELECT player_id FROM player_server_info WHERE mojang_uuid = %s AND server_id = %s"
        logger.debug(f"executing SQL query: {query}")
        data = (mojang_uuid, server_id)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.warning(f'No player found for uuid: "{mojang_uuid}" and server: "{server_id}"')
            return None
        player_id = result[0]
        logger.info(f'Found player id: "{player_id}" for uuid: "{mojang_uuid}" and server: "{server_id}"')
        return player_id

    def get_player_id_from_mojang_uuid_and_subdomain(self, mojang_uuid, subdomain):
        logger.debug("get_player_id_from_mojang_uuid_and_subdomain is called")
        query = """SELECT psi.id
                FROM player_server_info psi
                JOIN servers s ON psi.server_id = s.id
                WHERE psi.mojang_uuid = %s AND s.subdomain = %s;"""
        data = (mojang_uuid, subdomain,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.warning(f'No player found for uuid: "{mojang_uuid}" and subdomain: "{subdomain}"')
            return None
        player_id = result[0]
        logger.info(f'Found player id: "{player_id}" for uuid: "{mojang_uuid}" and subdomain: "{subdomain}"')
        return player_id

    def get_mojang_uuid_from_player_id(self, player_id):
        logger.debug("get_mojang_uuid_from_player_id is called")
        query = """SELECT mojang_uuid FROM player_server_info WHERE player_id = %s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.warning(f'No player uuid found for player id: "{player_id}"')
            return None
        mojang_uuid = result[0]
        logger.info(f'Found player uuid: "{mojang_uuid}" for player id: "{player_id}"')
        return mojang_uuid

    def get_server_id_from_player_id(self, player_id):
        logger.info("get_server_id_from_player_id is called")
        query = """
                SELECT server_id 
                FROM player_server_info
                WHERE player_id = %s
        """
        data = (player_id, )
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.warning(f'No server id found for player id: "{player_id}"')
            return None
        return result[0]


    def get_mojang_uuid_from_player_name(self, player_name):
        logger.debug("get_mojang_uuid_from_player_name is called")
        query = """SELECT mojang_uuid FROM player WHERE name = %s"""
        data = (player_name,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.warning(f'No player uuid found for player name: "{player_name}"')
            return None
        mojang_uuid = result[0]
        logger.info(f'Found player uuid: "{mojang_uuid}" for player name: "{player_name}"')
        return mojang_uuid

    def get_player_name_from_mojang_uuid(self, mojang_uuid):
        logger.debug("get_player_name_from_mojang_uuid is called")
        query = """SELECT name FROM player WHERE mojang_uuid = %s"""
        data = (mojang_uuid,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.warning(f'No player name found for player uuid: "{mojang_uuid}"')
            return None
        player_name = result[0]
        logger.info(f'Found player name: "{player_name}" for player uuid: "{mojang_uuid}"')
        return player_name

    def get_player_name_from_player_id(self, player_id):
        logger.debug("get_player_name_from_player_id is called")
        query = """SELECT p.name
                FROM player p
                JOIN player_server_info psi ON p.uuid = psi.mojang_uuid
                WHERE psi.player_id = %s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.warning(f'No player name found for player id: "{player_id}"')
            return None
        player_name = result[0]
        logger.info(f'Found player name: "{player_name}" for player id: "{player_id}"')
        return player_name

    def get_prefix_id_by_player_id(self, player_id):
        logger.debug("get_prefix_id_by_player_id is called")
        query = """ SELECT prefix FROM player_server_info WHERE player_id=%s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No prefix found for player id: "{player_id}"')
            return None
        prefix_id = result[0]
        logger.info(f'Found prefix id: "{prefix_id}" for player id: "{player_id}"')
        return prefix_id

    def get_prefix_text_by_prefix_id(self, prefix_id):
        logger.debug("get_text_by_prefix_id is called")
        query = """ SELECT prefix_text FROM prefixes WHERE id=%s"""
        data = (prefix_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No text found for prefix id: "{prefix_id}"')
            return None
    
    def get_ban_reason_from_player_id(self, player_id):
        logger.debug("get_ban_reason_from_player_id is called")
        query = """SELECT br.reason FROM ban_reasons br
                    JOIN banned_players bp ON br.id = bp.ban_reason_id
                    WHERE bp.banned_player_id = %s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No ban reason found for player id: "{player_id}"')
            return None
        ban_reason = result[0]
        logger.info(f'Found ban reason: "{ban_reason}" for player id: "{player_id}"')
        return ban_reason
    
    def get_server_id_from_subdomain(self, subdomain):
        logger.debug("get_server_id_from_subdomain is called")
        query = "SELECT id FROM servers WHERE subdomain = %s"
        data = (subdomain,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.warning(f'No server found for subdomain: "{subdomain}"')
            return None
        server_id = result[0]
        logger.info(f'Found server id: "{server_id}" for subdomain: "{subdomain}"')
        return server_id
    
    def get_members_from_prefix_id(self, prefix_id):
        logger.debug(f"getting members_from_prefix_id is called")
        query = """ SELECT psi.player_id
                    FROM player_server_info psi
                    JOIN prefixes p ON psi.prefix_id = p.prefix_id
                    WHERE p.prefix_id = %s;"""
        data = (prefix_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchall()
        logger.info(f'Found players for prefix id: "{prefix_id}"')
        return result

    def get_all_player_ids_from_subdomain(self, subdomain):
        logger.debug("get_all_player_ids_from_subdomain is called")
        query = """SELECT player_id FROM player_server_info WHERE server_id IN (SELECT id FROM servers WHERE subdomain = %s)"""
        data = (subdomain,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchall()
        logger.info(f'Found player ids for subdomain: "{subdomain}"')
        return result
   
    def get_online_player_count_from_subdomain(self, subdomain):
        logger.debug("get_online_player_count_from_subdomain is called")
        query = """SELECT COUNT(*) FROM player_server_info WHERE server_id IN (SELECT id FROM servers WHERE subdomain = %s) AND online = true"""
        data = (subdomain,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        logger.info(f'Found online player count: "{result[0]}" for subdomain: "{subdomain}"')        
        return result[0]
   
    def get_first_seen_by_player_id(self, player_id):
        logger.debug("get_first_seen_by_player_id is called")
        query = """SELECT first_seen FROM player_server_info WHERE player_id = %s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        logger.info(f'Found first seen timestamp: "{result[0]}" for player id: "{player_id}"')
        return result[0]
   
    def get_last_seen_by_player_id(self, player_id):
        logger.debug("get_last_seen_by_player_id is called")
        query = """SELECT last_seen FROM player_server_info WHERE player_id = %s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        logger.info(f'Found last seen timestamp: "{result[0]}" for player id: "{player_id}"')
        return result[0]
   
    def get_server_id_by_auth_key(self, auth_key):
        logger.debug("get_server_id_by_auth_key is called")
        query = """ SELECT id FROM servers WHERE server_key = %s; """
        data = (auth_key,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        result = result[0] if result else None
        logger.info(f'Found server id: "{result}" for auth_key: "{auth_key}"')
        return result
    

    def get_all_mojang_uuids_from_subdomain(self, subdomain):
        logger.debug("get_all_mojang_uuids_from_subdomain is called")
        query = """SELECT mojang_uuid FROM player_server_info WHERE server_id IN (SELECT id FROM servers WHERE subdomain = %s)"""
        data = (subdomain,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchall()
        uuids = [uuid[0] for uuid in result]
        logger.debug(f"Found uuids: {uuids} for subdomain: {subdomain}")
        return uuids
    ###----------------------------- Player Statuses ------------------------------------###
    
    def get_online_status_by_player_uuid_and_subdomain(self, uuid, subdomain):
        logger.debug("get_online_status_by_player_uuid_and_subdomain is called")
        query = """
                SELECT psi.online
                FROM player_server_info psi
                JOIN servers s ON psi.server_id = s.id
                WHERE psi.player_uuid = %s AND s.subdomain = %s;
                """
        data = (uuid, subdomain)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No online status found for player uuid: "{uuid}" and subdomain: "{subdomain}"')
            return None
        online_status = result[0]
        logger.debug(f'Found online status: "{online_status}" for player uuid: "{uuid}" and subdomain: "{subdomain}"')
        return online_status
    
    def get_online_status_by_player_id(self, player_id):
        logger.debug("get_online_status_by_player_id is called")
        query = """ SELECT online FROM player_server_info WHERE player_id=%s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No online status found for player id: "{player_id}"')
            return None
        online_status = result[0]
        logger.info(f'Found online status: "{online_status}" for player id: "{player_id}"')
        return online_status

    def get_ban_start_and_ban_end_by_player_id(self, player_id):
        logger.debug("get_ban_time_by_player_id is called")
        query = """ SELECT ban_start, ban_end
                    FROM banned_players
                    WHERE banned_player_id = %s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No ban time found for player id: "{player_id}"')
            return None
        ban_start, ban_end = result
        logger.info(f'Found ban time: "{ban_start}" for player id: "{player_id}"')
        return ban_start, ban_end


    def get_web_access_permission_from_player_id(self, player_id):
        logger.debug("get_web_access_permission_from_player_id is called")
        query = """ SELECT web_access_permissions
                    FROM player_server_info
                    WHERE player_id = %s"""
        data = (player_id,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No web access permission found for player id: "{player_id}"')
            return None
        web_access_permissions = result[0]
        logger.info(f'Found web access permission: "{web_access_permissions}" for player id: "{player_id}"')
        return web_access_permissions

    def get_server_information_dict(self, subdomain):
        logger.debug("getting_server_information_dict is called")
        query = "SELECT * FROM servers WHERE LOWER(subdomain) = %s;"
        data = (subdomain.lower(),)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")

        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if not result:
            logger.warning(f'No server found for subdomain: "{subdomain}"')
            return None

        # Convert result to dict
        columns = [desc[0] for desc in self.cursor.description]
        result_dict = dict(zip(columns, result))

        logger.info(f'Found server information for subdomain: "{subdomain}"')
        return result_dict


        ################################ UPDATE FUNCTIONS ####################################

        def update_online_status_by_player_id(self, player_id, online_status):
            logger.debug("update_online_status_by_player_id is called")
            query = """ UPDATE player_server_info
                        SET online = %s
                        WHERE player_id = %s;"""
            data = (online_status, player_id)
            logger.debug(f"with following data: {data}")
            self.cursor.execute(query, data)
            self.conn.commit()
            logger.info(f'Updated online status for player id: "{player_id}" to: "{online_status}"')

        def update_player_prefix_by_player_id(self, player_id, prefix_id):
            logger.debug("update_prefix_by_player_id is called")
            query = """ UPDATE player_server_info
                        SET prefix = %s
                        WHERE player_id = %s;"""
            data = (prefix_id, player_id)
            logger.debug(f"with following data: {data}")
            self.cursor.execute(query, data)
            self.conn.commit()
            logger.info(f'Updated prefix for player id: "{player_id}" to: "{prefix_id}"')

        def update_prefix_text_by_prefix_id(self, prefix_id, prefix_text):
            logger.debug("update_prefix_text_by_prefix_id is called")
            query = """ UPDATE prefixes
                        SET prefix_text = %s
                        WHERE id = %s;"""
            data = (prefix_text, prefix_id)
            logger.debug(f"with following data: {data}")
            self.cursor.execute(query, data)
            self.conn.commit()
            logger.info(f'Updated prefix text for prefix id: "{prefix_id}" to: "{prefix_text}"')
        
        def update_prefix_password_by_prefix_id(self, prefix_id, prefix_password):
            logger.debug("update_prefix_password_by_prefix_id is called")
            query = """ UPDATE prefixes
                        SET password = %s
                        WHERE id = %s;"""
            data = (prefix_password, prefix_id)
            logger.debug(f"with following data: {data}")
            self.cursor.execute(query, data)
            self.conn.commit()
            logger.info(f'Updated prefix password for prefix id: "{prefix_id}" to: "{prefix_password}"')
        
        ################################ DELETE FUNCTIONS #################################
        
        def delete_banned_player(self, banned_player_id):
            logger.debug("delete_banned_player is called")
            query = """ DELETE FROM banned_players WHERE banned_player_id = %s;"""
            data = (banned_player_id,)
            logger.debug(f"with following data: {data}")
            self.cursor.execute(query, data)
            self.conn.commit()
            logger.info(f'Deleted banned player: "{banned_player_id}"')


    def get_subdomain_from_license(self, license):
        logger.debug("get_subdomain_from_license is called")
        query = """ SELECT subdomain FROM licenses WHERE license = %s"""
        data = (license,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No subdomain found for license: "{license}"')
            return None
        subdomain = result[0]
        logger.info(f'Found subdomain: "{subdomain}" for license: "{license}"')
        return subdomain

    def get_license_from_subdomain(self, subdomain):
        logger.debug("get_license_from_subdomain is called")
        query = """ SELECT license FROM licenses WHERE subdomain = %s"""
        data = (subdomain,)
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        if result is None:
            logger.debug(f'No license found for subdomain: "{subdomain}"')
            return None
        license = result[0]
        logger.info(f'Found license: "{license}" for subdomain: "{subdomain}"')
        return license
    

    ###----------------------------- Minecraft Items ------------------------------------###
    def get_value_from_unique_object_from_action_table_with_player_id(self, object, player_id):
        """
        This function retrieves the value associated with a unique object from the 'actions' table.

        Parameters:
        object (str): The unique object for which the value needs to be retrieved.

        Returns:
        value (str): The value associated with the unique object. If the object is not found, it returns None.
        """
        logger.debug(f"get_value_from_unique_object_from_action_table_with_player_id is called with object: {object}")
        query = """SELECT value FROM actions WHERE object = %s and player_id = %s"""
        data = (object,player_id)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        result = result[0] if result else None
        logger.debug(f"Found value: {result} for object: {object}")
        return result

    
    def get_all_armor_stats(self, player_id):
        '''
        ----> Layout.txt
        '''
        logger.debug("getting_all_armor_stats is called")
        query = """ SELECT jsonb_agg(category_objects) AS grouped_objects
                    FROM (
                        SELECT category, jsonb_agg(jsonb_build_object('object', object, 'value', value)) AS category_objects
                        FROM actions
                        WHERE category IN (19, 16, 9, 5, 1) AND player_id = %s
                        GROUP BY category
                        ORDER BY category DESC
                    ) subquery;
                """     
        data = (player_id,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")

        try:
            self.cursor.execute(query, data)
            result = self.cursor.fetchone()
            logger.debug(f"Found grouped_objects: {result[0]} for player_id: {player_id}")
            return result[0]
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error in getting_all_armor_stats: {e}")
            return None
        
    def get_all_tools_stats(self, player_id):
        '''
        ----> Layout.txt
        '''
        logger.debug("getting_all_tools_stats is called")
        query = """ SELECT jsonb_agg(category_objects) AS grouped_objects
                    FROM (
                        SELECT category, jsonb_agg(jsonb_build_object('object', object, 'value', value)) AS category_objects
                        FROM actions
                        WHERE category IN (20, 15, 10, 6, 0) AND player_id = %s
                        GROUP BY category
                        ORDER BY category DESC
                    ) subquery;
                """     
        data = (player_id,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")

        try:
            self.cursor.execute(query, data)
            result = self.cursor.fetchone()
            logger.debug(f"Found grouped_objects: {result[0]} for player_id: {player_id}")
            return result[0]
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error in getting_all_tools_stats: {e}")
            return None
        
    def get_all_items_stats(self, player_id):
        '''        
        ----> Layout.txt
        '''
        logger.debug("getting_all_items_stats is called")
        query = """ SELECT jsonb_agg(category_objects) AS grouped_objects
                    FROM (
                        SELECT category, jsonb_agg(jsonb_build_object('object', object, 'value', value)) AS category_objects
                        FROM actions
                        WHERE category IN (18, 14, 8, 4) AND player_id = %s
                        GROUP BY category
                        ORDER BY category DESC
                    ) subquery;
                """     
        data = (player_id,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")

        try:
            self.cursor.execute(query, data)
            result = self.cursor.fetchone()
            logger.debug(f"Found grouped_objects: {result[0]} for player_id: {player_id}")
            return result[0]
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error in getting_all_items_stats: {e}")
            return None
    
    def get_all_blocks_stats(self, player_id):
        '''
                ----> Layout.txt
        '''
        logger.debug("getting_all_blocks_stats is called")
        query = """ SELECT jsonb_agg(category_objects) AS grouped_objects
                    FROM (
                        SELECT category, jsonb_agg(jsonb_build_object('object', object, 'value', value)) AS category_objects
                        FROM actions
                        WHERE category IN (17, 13, 7, 3, 2) AND player_id = %s
                        GROUP BY category
                        ORDER BY category DESC
                    ) subquery;
                """     
        data = (player_id,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")

        try:
            self.cursor.execute(query, data)
            result = self.cursor.fetchone()
            logger.debug(f"Found grouped_objects: {result[0]} for player_id: {player_id}")
            return result[0]
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error in getting_all_blocks_stats: {e}")
            return None
        
    def get_all_mobs_stats(self, player_id):
        '''
        ----> Layout.txt

        '''
        logger.debug("getting_all_mobs_stats is called")
        query = """ SELECT jsonb_agg(category_objects) AS grouped_objects
                    FROM (
                        SELECT category, jsonb_agg(jsonb_build_object('object', object, 'value', value)) AS category_objects
                        FROM actions
                        WHERE category IN (12, 11) AND player_id = %s
                        GROUP BY category
                        ORDER BY category DESC
                    ) subquery;
                """     
        data = (player_id,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")

        try:
            self.cursor.execute(query, data)
            result = self.cursor.fetchone()
            logger.debug(f"Found grouped_objects: {result[0]} for player_id: {player_id}")
            return result[0]
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error in getting_all_mobs_stats: {e}")
            return None
        
    def get_all_custom_stats(self, player_id):
        '''
        Custom:
            custom: 21
        '''
        logger.debug("getting_all_custom_stats is called")
        query = """ SELECT jsonb_agg(category_objects) AS grouped_objects
                    FROM (
                        SELECT category, jsonb_agg(jsonb_build_object('object', object, 'value', value)) AS category_objects
                        FROM actions
                        WHERE category = 21 AND player_id = %s
                        GROUP BY category
                        ORDER BY category DESC
                    ) subquery;
                """     
        data = (player_id,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")

        try:
            self.cursor.execute(query, data)
            result = self.cursor.fetchone()
            logger.debug(f"Found grouped_objects: {result[0]} for player_id: {player_id}")
            return result[0]
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error in getting_all_custom_stats: {e}")
            return None
    
    ################################# Verify Functions #######################################

    def verify_player_login(self, player_id, pin):
        logger.debug("verify_player_login is called")
        query = "SELECT pin, timestamp FROM login WHERE player_id = %s;"
        data = (player_id,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        result = self.cursor.fetchone()
        
        if not result:
            logger.debug(f"No entry found for player_id: {player_id}")

            return [False, "no entry found in the database"]
        
        stored_pin, saved_timestamp = result
        
        # Verify PIN
        if pin != stored_pin:
            logger.debug(f"PIN is wrong for player_id: {player_id}")
            return [False, "wrong pin provided"]
        
        # Verify timestamp validity
        current_timestamp = datetime.now()
        time_difference = current_timestamp - saved_timestamp
        
        # Check if saved timestamp is older than 5 minutes from current timestamp
        if time_difference > timedelta(minutes=5):
            logger.debug(f"Saved timestamp is older than 5 minutes for player_id: {player_id}")
            self.delete_login_entry(player_id)
            return [False, "timeout reached"]
        
        logger.debug(f"Login attempt for player_id: {player_id} is valid")
        self.delete_login_entry(player_id)

        return [True,]

    def verify_admin_login(self, username, password):
        logger.debug("verify_admin_login is called")
        query = "SELECT password FROM unsetuser WHERE username = %s and email_verified = TRUE"
        data = (username,)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.cursor.execute(query, data)
        server_entry = self.cursor.fetchone()
        if server_entry:
            logger.info(f'Activated server credentials found for username: "{username}"')
            
            if ph.verify(password, server_entry[0]):
                logger.debug("Server credentials verified.")
                return True
        return False

    def verify_signupcode(self, username, code):
        logger.debug("verify_signupcode is called")
        query = """
                SELECT sa.created_at
                FROM server_admins sa
                JOIN email_verification ev ON sa.verification_process_id = ev.id
                WHERE sa.username = %s
                AND ev.verification_code = %s
                AND ev.created_at > NOW() - INTERVAL '5 minutes';

        """
        query = "SELECT created_at FROM server_admins WHERE username = %s AND verification_code = %s"
        data = (username, code)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")

        self.cursor.execute(query, data)
        timestamp = self.cursor.fetchone()
        if timestamp is None:
            logger.info(f'No signup code found for username: "{username}". Or time up...')
            query = "DELETE FROM server_admins WHERE username = %s AND verification_code = %s"
            data = (username, code)
            try: 
                self.cursor.execute(query, data)
                self.conn.commit()
            except Exception as e:
                ...
            return False

        logger.info(f'Verified signup code for username: "{username}"')
        query = """
                update sa.email_verified = TRUE
                FROM server_admins sa
                JOIN email_verification ev ON sa.verification_process_id = ev.id
                WHERE sa.username = %s
                AND ev.verification_code = %s;

        """
        data = (username, code)
        self.cursor.execute(query, data)
        logger.debug(f"executing SQL query: {query}")
        logger.debug(f"with following data: {data}")
        self.conn.commit()
        return True

    ################################# helper functions #######################################
    def split_items_from_json(self, items):
        '''
        Return: [[object/item, category, value],]
        '''
        return_list = []
        stats_categorys = ["minecraft:broken", "minecraft:mined","minecraft:dropped","minecraft:used","minecraft:killed","minecraft:crafted","minecraft:killed_by","minecraft:custom","minecraft:picked_up"]
        all_data = json.loads(items)["stats"]
        for category in stats_categorys:
            try:
                data = all_data[category]
                # if category == "minecraft:broken":
                for item_name, item_data in data.items():
                    db_category = self.get_db_category_from_item_and_json_category(item_name, category)
                    return_list.append([item_name, db_category, item_data])
            except KeyError:
                continue

        return return_list
    
    def get_db_category_from_item_and_json_category(self, item, category):
        '''
        categorys = ["minecraft:broken", "minecraft:mined","minecraft:dropped","minecraft:used","minecraft:killed","minecraft:killed_by","minecraft:crafted","minecraft:picked_up","minecraft:custom"]

        ----> Layout.txt
        Return: database category name
        '''
        tools_substrings = ["axe", "shovel", "hoe", "sword", "pickaxe", "shield", "flint_and_steel", "bow", "crossbow", "brush", "trident", "shears", "fishing_rod", "mace"]
        armor_substrings = ["boots", "leggings", "chestplate", "helmet"]
        recognized_item_group = ""
        if any(substring in item for substring in tools_substrings):
            recognized_item_group = "tool"
        elif any(substring in item for substring in armor_substrings):
            recognized_item_group = "armor"
        elif self.check_item_for_block(item):
            recognized_item_group = "block"
        elif self.check_item_for_item(item):
            recognized_item_group = "item"
        else:
            recognized_item_group = "unknown"

        if recognized_item_group == "tool":
            if category == "minecraft:broken":
                return 15
            elif category == "minecraft:dropped":
                return 10
            elif category == "minecraft:used":
                return 20
            elif category == "minecraft:crafted":
                return 0
            elif category == "minecraft:picked_up":
                return 6

        elif recognized_item_group == "armor":
            if category == "minecraft:broken":
                return 16
            elif category == "minecraft:dropped":
                return 9
            elif category == "minecraft:used":
                return 19
            elif category == "minecraft:crafted":
                return 1
            elif category == "minecraft:picked_up":
                return 5

        elif recognized_item_group == "block":
            if category == "minecraft:mined":
                return 17
            elif category == "minecraft:dropped":
                return 7
            elif category == "minecraft:used":
                return 13
            elif category == "minecraft:crafted":
                return 2
            elif category == "minecraft:picked_up":
                return 3

        elif recognized_item_group == "item":
            if category == "minecraft:dropped":
                return 14
            elif category == "minecraft:used":
                return 18
            elif category == "minecraft:crafted":
                return 4
            elif category == "minecraft:picked_up":
                return 8

        if category == "minecraft:killed":
            return 12
        elif category == "minecraft:killed_by":
            return 11
        elif category == "minecraft:custom":
            return 21
        else:
            return -1  # Return an invalid value if no match is found

    def check_item_for_block(self, item):
        item = item.replace("minecraft:", "")
        query = f"SELECT EXISTS (SELECT 1 FROM block_lookup WHERE blocks = %s)"
        self.cursor.execute(query, (item,))
        result = self.cursor.fetchone()[0]
        return result
        
    def check_item_for_item(self, item):
        item = item.replace("minecraft:", "")

        query = f"SELECT EXISTS (SELECT 1 FROM item_lookup WHERE items = %s)"
        self.cursor.execute(query, (item,))
        result = self.cursor.fetchone()[0]
        return result

       
    def format_time(self, seconds):
        """
        Formats a given number of seconds into a human-readable time string.

        Args:
            seconds (int): The number of seconds to be formatted.

        Returns:
            str: A formatted time string in the format "X Tage, Y Stunden, Z Minuten, W Sekunden",
                    where the parts are included only if they are non-zero.
        """
        days = int(seconds // 86400)
        seconds %= 86400

        hours = int(seconds // 3600)
        seconds %= 3600

        minutes = int(seconds // 60)
        seconds = int(seconds % 60)

        time_parts = []
        setMinutes, setSeconds = True, True
        if days > 0:
            time_parts.append(f"{days} Tag{'e' if days > 1 else ''}")
            setSeconds, setMinutes = False, False
        if hours > 0:
            time_parts.append(f"{hours} Std.")
            setMinutes = False
        if minutes > 0 and setMinutes:
            time_parts.append(f"{minutes} Min.")
        if seconds > 0 and setSeconds:
            time_parts.append(f"{seconds} Sec.")

        return ' '.join(time_parts)      
    
    def get_all_logins(self):
        logger.warn("DEPRECATED: get_all_logins is deprecated and will be removed in the future.")
        logger.debug("get_all_logins is called")
        query = "SELECT pin, player_id FROM login"
        logger.debug(f"executing SQL query: {query}")
        self.cursor.execute(query)
        logins = self.cursor.fetchall()
        logger.debug(f"Logins retrieved: {logins}")
        return logins

if __name__ == "__main__":
    db = DatabaseManager()


# TODO: Insert player kills from 'other' to 'mobs' -> player