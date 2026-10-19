"""Mobile layout and number consistency of the server pages.

The first tests need no browser: the numbers on the player page, in the comparison and in the rankings must be
the same values (and say what they add up), and the templates must carry what the mobile layout relies on.
The browser tests render the pages in Chrome at phone width (Playwright, `pip install playwright`, uses the
installed Chrome) and check that nothing runs out of the screen; they are skipped without Playwright or Chrome.
"""
import re
import threading
from html.parser import HTMLParser

import pytest

from database import metrics
from tests.conftest import PLAYER_UUID
from tests.test_metrics import STATS_A, STATS_B, two_players  # noqa: F401 (fixture)
from tests.test_web import app, client, login_player, on  # noqa: F401 (fixtures)

HOUR = 72000  # ticks
ORES = {"minecraft:iron_ore": 245, "minecraft:deepslate_iron_ore": 10, "minecraft:stone": 900}


@pytest.fixture
def ore_player(db, server, two_players):
    a, _ = two_players
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": ORES})})
    return a


# ------------------------------------------------------------------ the same numbers everywhere

def test_player_page_cards_and_table_show_the_metric_values(client, db, ore_player):
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    play_time = metrics.format_value(metrics.METRICS_BY_KEY["play_time"], db.get_player_metrics(ore_player)["play_time"])
    assert play_time == "1 Std."
    assert f'data-card="6">{play_time}<' in html  # card, rendered without waiting for the poll
    assert f'<td class="num" data-value="{HOUR}">{play_time}</td>' in html  # "Alle Werte": same value and unit
    assert 'data-card="2">5<' in html


def test_general_values_have_units_and_german_names(client, ore_player):
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Spielzeit" in html and "Strecke: Zu Fuß" in html
    assert ">1,5 km</td>" in html  # walk_one_cm 150000, not "150000"
    assert ">72000<" not in html and ">150000<" not in html


def test_detail_table_columns_are_german(client, ore_player):
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    for column in ("Abgebaut", "Platziert", "Hergestellt", "Getötet", "Hat dich getötet"):
        assert column in html
    for english in (">Mined", ">Placed", ">Killed By"):
        assert english not in html


def test_ore_metric_says_it_adds_deepslate(client, ore_player):
    """Eisenerz 255 in the comparison vs Iron Ore 245 in the block table: both are right, the page says why."""
    html = client.get("/vergleich?spieler=_Tobias4444,Notch", **on("testdomain")).get_data(as_text=True)
    assert "Eisenerz <span class=\"cmp-note\">inkl. Deepslate</span>" in html
    assert "245 normal + 10 Deepslate" in html
    assert re.search(r'<span class="cmp-value">255\b', html)
    assert "Erze stehen hier je Variante für sich" in html  # note of the detail tables
    page = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Iron Ore" in page and "Deepslate Iron Ore" in page and "zählen beide zusammen" in page


def test_ore_split_only_for_all_time(client, ore_player):
    html = client.get("/vergleich?spieler=_Tobias4444,Notch&zeitraum=7", **on("testdomain")).get_data(as_text=True)
    assert "normal + " not in html  # gains of 7 days have no split per block
    assert "nicht nur die\n    letzten 7 Tage" in html or "nicht nur die letzten 7 Tage" in re.sub(r"\s+", " ", html)


def test_deaths_by_mobs_are_shown_as_part_of_all_deaths(client, db, server, two_players):
    a, _ = two_players
    db.update_player_stats(a, {"stats": {"minecraft:killed_by": {"minecraft:creeper": 2},
                                         "minecraft:custom": {"minecraft:deaths": 5}}})
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "Am häufigsten getötet von" in html and "2 von 5 Toden" in html
    assert "Häufigste Todesursache" not in html
    assert "Tode insgesamt (auch durch Stürze, Lava, Ertrinken …): 5" in html
    compare = client.get("/vergleich?spieler=_Tobias4444,Notch", **on("testdomain")).get_data(as_text=True)
    assert "Getötet von Mobs" in compare and "Nur Tode durch Mobs" in compare


def test_first_join_is_the_same_on_player_page_and_player_list(client, db, server, two_players):
    a, _ = two_players
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET first_played = '2025-03-04 12:00+00' WHERE player_id = %s", (a,))
    page = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    player_list = client.get("/spieler", **on("testdomain")).get_data(as_text=True)
    assert 'data-card="3">04.03.2025<' in page
    assert "Dabei seit 04.03.2025" in player_list


# ------------------------------------------------------------------ what the mobile layout needs in the templates

class _Cells(HTMLParser):
    """Cells of the tables with class mcc-stack: [(table index, has data-label)]."""

    def __init__(self):
        super().__init__()
        self.depth, self.index, self.cells = 0, -1, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            if self.depth or "mcc-stack" in (attrs.get("class") or "").split():
                self.depth += 1
                self.index += self.depth == 1
        elif tag == "td" and self.depth and "mcc-empty" not in (attrs.get("class") or ""):  # "Lade …", "Niemand …"
            self.cells.append((self.index, "data-label" in attrs))

    def handle_endtag(self, tag):
        if tag == "table" and self.depth:
            self.depth -= 1


@pytest.fixture
def mod_client(client, db, server, two_players):
    db.set_plugin_connected(server["id"], True)
    db.set_moderator(server["id"], "_Tobias4444", True)
    assert login_player(client, db, two_players[0]).json["status"] == "success"
    db.create_report(server["id"], two_players[1], "_Tobias4444", "Hat am Spawn Lava verteilt")
    db.add_mod_log(server["id"], "_Tobias4444", "note_add", "Notch", "Klicktest")
    return client


@pytest.mark.parametrize("path", ["/users/spieler", "/users/inhalte", "/users/zugang", "/users/protokoll"])
def test_moderation_tables_name_their_columns(mod_client, path):
    """On phones every row is a card; each cell shows its column name from data-label."""
    response = mod_client.get(path, **on("testdomain"))
    assert response.status_code == 200
    parser = _Cells()
    parser.feed(response.get_data(as_text=True))
    assert all(labelled for _, labelled in parser.cells), path


@pytest.mark.parametrize("path", ["/", "/spieler", "/spieler?player=_Tobias4444", "/rangliste", "/vergleich?spieler=_Tobias4444,Notch",
                                  "/server-statistik", "/wettbewerbe", "/events", "/galerie"])
def test_server_pages_have_the_mobile_layout(client, two_players, path):
    html = client.get(path, **on("testdomain")).get_data(as_text=True)
    assert 'name="viewport"' in html and "width=device-width" in html
    assert "css/mobile.css" in html and "css/responsiveHeader.css" in html
    assert 'class="mh-toggle"' in html and 'aria-controls="nav-bar"' in html and 'aria-expanded="false"' in html
    assert not re.search(r"width:\s*1[0-9]{3}px", html), "fixed widths break small screens"


def test_mobile_menu_lists_account_links(mod_client):
    html = mod_client.get("/rangliste", **on("testdomain")).get_data(as_text=True)
    account = html[html.index('class="mh-account"'):html.index('class="theme-item"')]
    for link in ("/spieler?player=_Tobias4444", "/profil", "askLogout"):
        assert link in account
    assert 'href="/login"' not in account


def test_player_page_needs_no_jquery_from_google(client, two_players):
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "ajax.googleapis.com" not in html
    assert html.count("jquery.min.js") == 1  # once, for MineRender (3D skin)


# ------------------------------------------------------------------ in a browser at phone width

PHONE = {"width": 360, "height": 780}  # small Android phones; iPhones are 375-430


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            chrome = p.chromium.launch(channel="chrome", args=["--host-resolver-rules=MAP *.mc.test 127.0.0.1, MAP mc.test 127.0.0.1"])
        except Exception as e:  # noqa: BLE001 (no Chrome installed)
            pytest.skip(f"Chrome not available: {e}")
        yield chrome
        chrome.close()


@pytest.fixture
def live_site(app, db, server, two_players):
    """The app on a real port (Chrome needs one). Returns the base URL of the test server's pages."""
    from werkzeug.serving import make_server
    db.set_plugin_connected(server["id"], True)
    db.set_moderator(server["id"], "_Tobias4444", True)
    a, b = two_players
    db.update_player_stats(a, {"stats": dict(STATS_A["stats"], **{"minecraft:mined": ORES})})
    for name, uuid in (("jeb_", "853c80ef-3c37-49fd-aa49-938b674adae6"), ("AlexMines_Longname", "61699b2e-d327-4a01-9f1e-0ea8c3f06bc6")):
        player = db.register_player_join(server["id"], uuid, name)
        db.update_player_stats(player, STATS_B)
    db.add_player_note(b, "_Tobias4444", "Hat sich entschuldigt.")
    db.create_report(server["id"], b, "_Tobias4444", "Hat am Spawn Lava verteilt", ("world", 120, 64, -340))
    db.add_mod_log(server["id"], "_Tobias4444", "note_add", "Notch", "Klicktest-Notiz mit etwas längerem Text")
    http = make_server("127.0.0.1", 0, app, threaded=True)
    app.config["SERVER_NAME"] = f"mc.test:{http.server_port}"
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://testdomain.mc.test:{http.server_port}"
    http.shutdown()


@pytest.fixture
def phone(browser, app, live_site, server, two_players):
    context = browser.new_context(viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True)
    host = live_site.split("//")[1].split(":")[0]
    a, _ = two_players
    cookie = app.session_interface.get_signing_serializer(app).dumps(
        {"player_id": str(a), "server_id": server["id"], "uuid": PLAYER_UUID})
    context.add_cookies([{"name": "session", "value": cookie, "domain": host, "path": "/"}])
    page = context.new_page()
    yield page
    context.close()


OVERFLOW = """() => {
  const width = document.documentElement.clientWidth;
  const out = [];
  for (const el of document.querySelectorAll("body *")) {
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height || getComputedStyle(el).position === "fixed") continue;
    let scroller = el.parentElement, inside = false;  // allowed inside a container that scrolls sideways
    for (; scroller && scroller !== document.body; scroller = scroller.parentElement) {
      if (/(auto|scroll|hidden)/.test(getComputedStyle(scroller).overflowX)) { inside = true; break; }
    }
    if (!inside && (box.right > width + 1 || box.left < -1)) out.push(el.tagName + "." + el.className);
  }
  // text that does not wrap (e.g. white-space: nowrap) runs out of its box without making the box wider
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node; (node = walker.nextNode());) {
    if (!node.textContent.trim() || getComputedStyle(node.parentElement).position === "fixed") continue;
    let scroller = node.parentElement, inside = false;
    for (; scroller && scroller !== document.body; scroller = scroller.parentElement) {
      if (/(auto|scroll|hidden)/.test(getComputedStyle(scroller).overflowX)) { inside = true; break; }
    }
    const range = document.createRange();
    range.selectNodeContents(node);
    if (!inside && [...range.getClientRects()].some((box) => box.right > width + 1)) out.push("text: " + node.textContent.trim().slice(0, 40));
  }
  return {body: document.body.scrollWidth, width, out: out.slice(0, 5)};
}"""

PAGES = ["/", "/spieler", "/spieler?player=_Tobias4444", "/rangliste", "/vergleich?spieler=_Tobias4444,Notch,jeb_,AlexMines_Longname",
         "/server-statistik", "/teams", "/wettbewerbe", "/ruhmeshalle", "/events", "/umfragen", "/galerie", "/profil",
         "/users", "/users/spieler", "/users/inhalte", "/users/zugang", "/users/protokoll", "/users/belohnungen"]


@pytest.mark.parametrize("path", PAGES)
def test_nothing_runs_out_of_a_phone_screen(phone, live_site, path):
    phone.goto(live_site + path, wait_until="domcontentloaded")
    phone.wait_for_timeout(300)
    result = phone.evaluate(OVERFLOW)
    assert result["body"] <= result["width"] and not result["out"], result


def test_mobile_menu_opens_with_big_targets(phone, live_site):
    phone.goto(live_site + "/rangliste", wait_until="domcontentloaded")
    toggle = phone.locator(".mh-toggle")
    box = toggle.bounding_box()
    assert box["height"] >= 44 and box["width"] >= 44
    assert not phone.locator("#nav-bar").is_visible()
    toggle.tap()
    assert phone.locator("#nav-bar").is_visible()
    assert toggle.get_attribute("aria-expanded") == "true"
    links = phone.locator("#nav-bar .dropdown-content a, .mh-account a")
    heights = [links.nth(i).bounding_box()["height"] for i in range(links.count()) if links.nth(i).is_visible()]
    assert heights and min(heights) >= 44
    assert phone.locator('#nav-bar a[href="/rangliste"]').get_attribute("aria-current") == "page"
    phone.keyboard.press("Escape")
    assert not phone.locator("#nav-bar").is_visible()


def test_comparison_shows_all_players_side_by_side(phone, live_site):
    phone.goto(live_site + "/vergleich?spieler=_Tobias4444,Notch,jeb_,AlexMines_Longname", wait_until="domcontentloaded")
    cells = phone.locator(".cmp-table tbody tr:not(.cmp-group)").first.locator("td")
    assert cells.count() == 4
    for i in range(4):
        box = cells.nth(i).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"], "a player column is off screen"


def test_player_page_tables_tabs_search_and_sort(phone, live_site):
    phone.goto(live_site + "/spieler?player=_Tobias4444", wait_until="domcontentloaded")
    rows = phone.locator("#panel-blocks tbody tr")
    assert rows.first.locator("th").inner_text().strip() == "Stone"  # most mined first
    phone.fill("#detail-search", "iron")
    visible = [rows.nth(i).locator("th").inner_text().strip() for i in range(rows.count()) if rows.nth(i).is_visible()]
    assert visible == ["Iron Ore", "Deepslate Iron Ore"]
    phone.fill("#detail-search", "")
    phone.locator("#tab-custom").tap()
    assert phone.locator("#panel-custom").is_visible() and not phone.locator("#panel-blocks").is_visible()
    assert "1 Std." in phone.locator("#panel-custom").inner_text()
    phone.locator('#panel-blocks [data-sort="0"]').evaluate("b => b.click()")  # sort by name (panel hidden: no tap)
    assert phone.locator("#panel-blocks tbody tr").first.locator("th").inner_text().strip() == "Deepslate Diamond Ore"


def test_moderation_rows_become_cards(phone, live_site):
    phone.goto(live_site + "/users/protokoll", wait_until="domcontentloaded")
    table = phone.locator("table.mcc-stack").first
    if table.count():
        assert table.locator("thead").bounding_box()["height"] <= 1  # column names move into the cells
        cell = table.locator("tbody td").first
        assert phone.evaluate("c => getComputedStyle(c, '::before').content", cell.element_handle()) != "none"


def test_every_metric_is_the_same_number_on_every_page(client, db, server, ore_player):
    """One source for all pages: rankings, comparison, player list, player page and records show the same values."""
    per_player = db.get_player_metrics(ore_player)
    server_wide = {p["player_id"]: p["values"] for p in db.get_server_metrics(server["id"])}[str(ore_player)]
    assert per_player == server_wide
    ranking = client.get("/rangliste", **on("testdomain")).get_data(as_text=True)
    compare = client.get("/vergleich?spieler=_Tobias4444,Notch", **on("testdomain")).get_data(as_text=True)
    for metric in metrics.METRICS:
        text = metrics.format_value(metric, per_player[metric.key])
        if per_player[metric.key]:
            assert f'<span class="st-value">{text}</span>' in ranking, metric.key
        assert f'<span class="cmp-value">{text}' in compare, metric.key


def test_records_show_the_current_value_not_a_stale_one(client, db, server, two_players):
    a, b = two_players
    db.update_records(a)  # stores the records of the first stats
    with db._cursor() as cur:  # the stored value lags behind (e.g. the stats file is older than the live stats)
        cur.execute("UPDATE record_history SET value = value - 100 WHERE metric = 'blocks_mined'")
    current = metrics.format_value(metrics.METRICS_BY_KEY["blocks_mined"], db.get_player_metrics(a)["blocks_mined"])
    for path in ("/", "/ruhmeshalle", "/spieler?player=_Tobias4444"):
        html = client.get(path, **on("testdomain")).get_data(as_text=True)
        assert current in html, path
    # the holder hides the stats: the best visible player is shown, like on the start page
    with db._cursor() as cur:
        cur.execute("UPDATE player_server_info SET hide_stats = true WHERE player_id = %s", (a,))
    notch = metrics.format_value(metrics.METRICS_BY_KEY["blocks_mined"], db.get_player_metrics(b)["blocks_mined"])
    for path in ("/", "/ruhmeshalle"):
        html = client.get(path, **on("testdomain")).get_data(as_text=True)
        assert current not in html and notch in html and "Notch" in html, path
