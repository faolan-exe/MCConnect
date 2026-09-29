"""Prints a session cookie of the moderator _Tobias4444 on testdomain (dev database) for ui-test.js.

    FLASK_SERVER_NAME=mc.t-auer.local:5070 .venv/bin/python tools/e2e/session.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
logging.disable(logging.CRITICAL)
from web.main import create_app  # noqa: E402

app = create_app()
db = app.extensions["mcconnect_db"]
server = db.get_server_information_dict("testdomain")
player_id = str(db.get_player_id_from_player_name_and_server_id("_Tobias4444", server["id"]))
db.set_moderator(server["id"], "_Tobias4444", True)
print(app.session_interface.get_signing_serializer(app).dumps(
    {"player_id": player_id, "server_id": server["id"], "uuid": str(db.get_mojang_uuid_from_player_id(player_id))}))
