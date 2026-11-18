"""The world map (BlueMap) on /karte: the address of the owner, the page, its CSP and the links (ADR 0015)."""
import pytest

from database import bluemap
from tests.conftest import OTHER_UUID
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_tickets import WHERE, addon, mod_client, request_id  # noqa: F401 (fixtures)
from tests.test_web import admin_client, app, client, on  # noqa: F401 (fixtures)

MAP = "https://karte.example.de/"


# ------------------------------------------------------------------ rules

@pytest.mark.parametrize("value, expected", [
    ("", None), ("   ", None), (None, None),
    ("https://karte.example.de", "https://karte.example.de/"),
    (" HTTPS://Karte.Example.DE:8443/map/ ", "https://karte.example.de:8443/map/"),
    ("https://example.de/map/?lang=de", "https://example.de/map/?lang=de"),
])
def test_valid_addresses_are_normalized(value, expected):
    assert bluemap.normalize_url(value) == (expected, None)


@pytest.mark.parametrize("value", [
    "http://karte.example.de/",          # a page on https may not frame http
    "javascript:alert(1)", "data:text/html,x", "//karte.example.de/", "karte.example.de",
    "https://", "https://user:pw@karte.example.de/", "https://a@karte.example.de/",
    "https://karte.example.de/#world:0:64:0", "https://karte.example.de/#",
    "https://karte.example.de/a b", "https://karte.example.de/a\nb", "https://karte.example.de:99999/",
    "https://karte.example.de/" + "x" * 300,
])
def test_invalid_addresses_are_refused(value):
    url, error = bluemap.normalize_url(value)
    assert url is None and error


def test_origin():
    assert bluemap.origin("https://karte.example.de:8443/map/?a=1") == "https://karte.example.de:8443"
    assert bluemap.origin(None) is None


@pytest.mark.parametrize("args, expected", [
    (("world", 10, 64, -20), "#world:10:64:-20:150:0:0:0:0:perspective"),
    (("world_nether", "-5", "70", "3"), "#world_nether:-5:70:3:150:0:0:0:0:perspective"),
    (("world", 30_000_000, -2048, -30_000_000), "#world:30000000:-2048:-30000000:150:0:0:0:0:perspective"),
    (("world", 30_000_001, 64, 0), ""), (("world", 0, 4097, 0), ""), (("world", 0, -2049, 0), ""),
    (("world", "1.5", 64, 0), ""), (("world", None, 64, 0), ""), ((None, 0, 64, 0), ""),
    (("a:b", 0, 64, 0), ""), (("x" * 65, 0, 64, 0), ""), (("welt mit leerzeichen", 0, 64, 0), ""),
])
def test_links_jump_only_to_valid_positions(args, expected):
    assert bluemap.view_url(MAP, *args) == MAP + expected
    assert bluemap.view_url(None, *args) is None


# ------------------------------------------------------------------ settings

def test_the_owner_sets_and_removes_the_address(admin_client, db, server):  # noqa: F811
    url = f"/api/servers/{server['id']}/update"
    assert admin_client.post(url, json={"map_url": "https://Karte.example.de"}, **on(None)).status_code == 200
    assert db.get_server_information_dict("testdomain")["map_url"] == MAP
    assert 'value="https://karte.example.de/"' in admin_client.get(f"/manage/{server['id']}", **on(None)).get_data(as_text=True)

    response = admin_client.post(url, json={"map_url": "http://karte.example.de/"}, **on(None))
    assert response.status_code == 400 and "https://" in response.json["error"]
    assert db.get_server_information_dict("testdomain")["map_url"] == MAP

    assert admin_client.post(url, json={"server_name": "Neu"}, **on(None)).status_code == 200  # not sent: kept
    assert db.get_server_information_dict("testdomain")["map_url"] == MAP
    assert admin_client.post(url, json={"map_url": ""}, **on(None)).status_code == 200
    assert db.get_server_information_dict("testdomain")["map_url"] is None


# ------------------------------------------------------------------ page

def test_no_map_no_page(client, server):  # noqa: F811
    assert client.get("/karte", **on("testdomain")).status_code == 404
    assert 'href="/karte"' not in client.get("/", **on("testdomain")).get_data(as_text=True)


def test_the_map_is_public_and_only_it_may_be_framed(client, db, server, admin_id):  # noqa: F811
    db.update_server(server["id"], admin_id, map_url=MAP)
    assert 'href="/karte"' in client.get("/", **on("testdomain")).get_data(as_text=True)  # not logged in

    response = client.get("/karte", **on("testdomain"))
    assert response.status_code == 200
    assert f'<iframe class="km-frame" src="{MAP}"' in response.get_data(as_text=True)
    assert "frame-src https://karte.example.de;" in response.headers["Content-Security-Policy"]
    assert "frame-src 'self'" in client.get("/", **on("testdomain")).headers["Content-Security-Policy"]

    page = client.get("/karte?welt=world&x=10&y=64&z=-20", **on("testdomain")).get_data(as_text=True)
    assert f'src="{MAP}#world:10:64:-20:150:0:0:0:0:perspective"' in page
    page = client.get("/karte?welt=world&x=10&y=64&z=%22%3E%3Cscript%3E", **on("testdomain")).get_data(as_text=True)
    assert f'src="{MAP}"' in page and "<script>" not in page.split("<iframe")[1].split(">")[0]


def test_other_servers_do_not_get_the_map(client, db, server, other_server, admin_id):  # noqa: F811
    db.update_server(server["id"], admin_id, map_url=MAP)
    assert client.get("/karte", **on(other_server["subdomain"])).status_code == 404


def test_tickets_link_to_their_position(mod_client, db, server, addon, two_players, admin_id):  # noqa: F811
    db.add_ticket(server["id"], request_id(1), OTHER_UUID, "grief", "Mein Haus ist weg", WHERE, 3)
    assert 'class="tk-map"' not in mod_client.get("/tickets", **on("testdomain")).get_data(as_text=True)
    db.update_server(server["id"], admin_id, map_url=MAP)
    page = mod_client.get("/tickets", **on("testdomain")).get_data(as_text=True)
    assert 'href="/karte?welt=world&amp;x=10&amp;y=64&amp;z=-20"' in page
