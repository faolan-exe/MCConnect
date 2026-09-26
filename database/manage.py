"""Database management commands (run from the project root).

    python -m database.manage status
    python -m database.manage init            # create the schema in an empty database
    python -m database.manage reset --yes     # DROP everything and recreate (dev only)
    python -m database.manage seed            # add a dev admin + server, prints the server key
    python -m database.manage create-admin USERNAME EMAIL   # asks for the password
    python -m database.manage add-server OWNER SUBDOMAIN MC_DOMAIN NAME
"""
import argparse
import getpass
import sys

from .databaseManagerV2 import DatabaseManager, SCHEMA_VERSION


def _server_key(db, server_id):
    return db._fetchvalue("SELECT server_key FROM servers WHERE id = %s", (server_id,))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m database.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("init")
    reset = sub.add_parser("reset")
    reset.add_argument("--yes", action="store_true", help="really drop all data")
    seed = sub.add_parser("seed")
    seed.add_argument("--password", default="testPassword")
    create_admin = sub.add_parser("create-admin", help="create a server admin with verified email")
    create_admin.add_argument("username")
    create_admin.add_argument("email")
    add_server = sub.add_parser("add-server")
    add_server.add_argument("owner", help="username of an existing server admin")
    add_server.add_argument("subdomain")
    add_server.add_argument("mc_domain")
    add_server.add_argument("name")
    args = parser.parse_args(argv)

    if args.command == "status":
        db = DatabaseManager(check_schema=False)
        print(f"schema version: {db.get_schema_version()} (expected {SCHEMA_VERSION})")
        return 0

    if args.command == "init":
        DatabaseManager(auto_init=True)
        print(f"Database ready (schema version {SCHEMA_VERSION})")
        return 0

    if args.command == "reset":
        if not args.yes:
            print("Refusing to reset without --yes", file=sys.stderr)
            return 1
        DatabaseManager(check_schema=False).reset_database()
        print(f"Database reset (schema version {SCHEMA_VERSION})")
        return 0

    db = DatabaseManager(auto_init=False)

    if args.command == "seed":
        admin_id = db.get_admin_id_by_username("tobi") or db.add_server_admin(
            "tobi", args.password, "tobi@t-auer.com", email_verified=True)
        server_id = db.get_server_id_from_subdomain("testdomain") or db.add_server(
            admin_id, "testDomain", "mc.t-auer.com", "Test Server")
        print(f"admin 'tobi' (id {admin_id}), server 'testdomain' (id {server_id})")
        print(f"server key: {_server_key(db, server_id)}")
        return 0

    if args.command == "create-admin":
        password = getpass.getpass("Password: ")
        if len(password) < 8 or password != getpass.getpass("Repeat password: "):
            print("Passwords differ or are shorter than 8 characters", file=sys.stderr)
            return 1
        admin_id = db.add_server_admin(args.username, password, args.email, email_verified=True)
        print(f"admin '{args.username}' created (id {admin_id})")
        return 0

    if args.command == "add-server":
        owner_id = db.get_admin_id_by_username(args.owner)
        if owner_id is None:
            print(f"Unknown admin '{args.owner}'", file=sys.stderr)
            return 1
        server_id = db.add_server(owner_id, args.subdomain, args.mc_domain, args.name)
        print(f"server id {server_id}, key: {_server_key(db, server_id)}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
