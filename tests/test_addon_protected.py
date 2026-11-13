"""Protected addon fields ("owner_only", e.g. the item data of EasyNewCraftingRecipes): only the owner changes them,
unless the owner lets moderators (servers.addon_mods_protected)."""
import json
import os

import pytest

from database import addons
from tests.test_addons import connect, mod  # noqa: F401 (fixture)
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)
from tests.test_web import admin_client, app, client, on  # noqa: F401 (fixtures)

MANIFEST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "java plugin", "addons", "EasyNewCraftingRecipes",
                        "src", "main", "resources", "mcconnect-addon.json")
DATA = 'item_frame[entity_data={id:"minecraft:item_frame",Invisible:1b}]'


def manifest():
    with open(MANIFEST, encoding="utf-8") as file:
        return addons.check_manifest(json.load(file))


def recipe(name, data="", **fields):
    return dict({"name": name, "type": "shaped", "grid": ["stick"] + [""] * 8, "result": "item_frame", "result_data": data}, **fields)


def settings(*rows):
    result, errors = addons.normalize(manifest()["settings"], {"recipes": list(rows)})
    assert errors == []
    return result


# ------------------------------------------------------------------ the rule

def test_only_owner_only_fields_count():
    sections = manifest()["settings"]
    old = settings(recipe("Rahmen", DATA), recipe("Licht"))
    assert addons.owner_only_changes(sections, old, old) == []
    # other fields, moving and removing rows, new rows without data: allowed
    edited = settings(recipe("Licht", amount=4), recipe("Neu"), recipe("Rahmen", DATA, amount=8))
    assert addons.owner_only_changes(sections, old, edited) == []
    assert addons.owner_only_changes(sections, old, settings(recipe("Licht"))) == []
    # changing, adding or emptying item data: not allowed
    assert [c["path"] for c in addons.owner_only_changes(sections, old, settings(recipe("Rahmen", "stick"), recipe("Licht")))] \
        == ["recipes.0.result_data"]
    assert [c["path"] for c in addons.owner_only_changes(sections, old, settings(recipe("Rahmen", DATA), recipe("Licht", "stick")))] \
        == ["recipes.1.result_data"]
    assert addons.owner_only_changes(sections, old, settings(recipe("Rahmen"), recipe("Licht")))
    # a new row with data, and renaming a row with data (the data would move to another recipe)
    assert addons.owner_only_changes(sections, old, settings(recipe("Rahmen", DATA), recipe("Licht"), recipe("X", "stick")))
    assert addons.owner_only_changes(sections, old, settings(recipe("Anders", DATA), recipe("Licht")))
    # two rows with the same name: each old row counts once
    twice = settings(recipe("Rahmen", DATA), recipe("Rahmen"))
    assert addons.owner_only_changes(sections, twice, twice) == []
    assert addons.owner_only_changes(sections, twice, settings(recipe("Rahmen", DATA), recipe("Rahmen", DATA)))
    # no settings stored yet: the defaults
    assert addons.owner_only_changes(sections, None, settings(recipe("Licht"))) == []


def test_the_flag_survives_the_manifest_check():
    fields = manifest()["settings"][1]["fields"][0]["fields"]
    assert [f["key"] for f in fields if f.get("owner_only")] == ["result_data"]
    presets = next(f for f in fields if f["key"] == "preset")
    assert presets["default"] == "none" and {"invisible_item_frame", "knockback_stick", "fireball"} <= {o["value"] for o in presets["options"]}


# ------------------------------------------------------------------ website and game

@pytest.fixture
def recipes_addon(db, server, admin_id):
    db.record_addon_releases([("e" * 64, "easynewcraftingrecipes", "2.1")])
    db.sync_reported_addons(server["id"], [{"id": "easynewcraftingrecipes", "name": "EasyNewCraftingRecipes",
                                            "plugin": "EasyNewCraftingRecipes", "version": "2.1",
                                            "file": "EasyNewCraftingRecipes-2.1.jar", "sha256": "e" * 64, "manifest": manifest()}])
    db.set_addon_config(server["id"], "easynewcraftingrecipes", settings(recipe("Rahmen", DATA)))
    assert db.update_server(server["id"], admin_id, addon_mods=True)


def save(client, *rows):
    return client.post("/api/mod/addons/easynewcraftingrecipes/config", json={"config": {"recipes": list(rows)}}, **on("testdomain"))


def test_moderators_keep_their_hands_off_the_item_data(mod, db, server, admin_id, recipes_addon):  # noqa: F811
    view = mod.get("/api/mod/addons", **on("testdomain")).json
    assert view["rights"]["protected"] is False
    assert save(mod, recipe("Rahmen", DATA, amount=2)).status_code == 200  # everything else: fine
    refused = save(mod, recipe("Rahmen", "stick"))
    assert refused.status_code == 403 and refused.json["errors"][0]["path"] == "recipes.0.result_data"
    assert db.get_addon(server["id"], "easynewcraftingrecipes")["config"]["recipes"][0]["result_data"] == DATA
    assert db.update_server(server["id"], admin_id, addon_mods_protected=True)  # the owner allows it
    assert save(mod, recipe("Rahmen", "stick")).status_code == 200
    assert db.get_addon(server["id"], "easynewcraftingrecipes")["config"]["recipes"][0]["result_data"] == "stick"


def test_the_owner_changes_item_data_and_the_switch(admin_client, db, server, recipes_addon):
    response = admin_client.post(f"/api/servers/{server['id']}/addons/easynewcraftingrecipes/config",
                                 json={"config": {"recipes": [recipe("Rahmen", "stick")]}}, **on(None))
    assert response.status_code == 200
    rights = admin_client.post(f"/api/servers/{server['id']}/addons/rights", json={"protected": True}, **on(None)).json["rights"]
    assert rights["protected"] is True and db.may_mods_edit_protected(server["id"])


def test_the_game_cannot_change_protected_fields(db, server, plugin, recipes_addon, admin_id):  # noqa: F811
    connection = connect(plugin, server)
    changed = json.dumps({"recipes": [recipe("Rahmen", "stick")]})
    assert connection.request(f"!ADDONCONFIG~easynewcraftingrecipes|_Tobias4444|{changed}") == "error|005"
    assert db.get_addon(server["id"], "easynewcraftingrecipes")["config"]["recipes"][0]["result_data"] == DATA
