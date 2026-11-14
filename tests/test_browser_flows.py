"""The pages in a real browser at desktop width: menu, profile tabs, prefix, events and polls, moderation tabs and the
logout. Every step saves without a page reload (the page keeps a marker set at the start) and no page logs a JavaScript
error. Skipped without Playwright or Chrome, like the phone tests."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import PLAYER_UUID
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_mobile import browser, live_site  # noqa: F401 (fixtures)
from tests.test_web import app, client  # noqa: F401 (fixtures)

DESKTOP = {"width": 1280, "height": 900}


@pytest.fixture
def desk(browser, app, live_site, server, two_players):
    context = browser.new_context(viewport=DESKTOP)
    host = live_site.split("//")[1].split(":")[0]
    a, _ = two_players
    cookie = app.session_interface.get_signing_serializer(app).dumps(
        {"player_id": str(a), "server_id": server["id"], "uuid": PLAYER_UUID})
    context.add_cookies([{"name": "session", "value": cookie, "domain": host, "path": "/"}])
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and page.errors.append(m.text))
    yield page
    assert page.errors == []
    context.close()


def open_page(page, url):
    page.goto(url, wait_until="networkidle")
    page.evaluate("window.__noReload = true")  # gone after a reload


def not_reloaded(page):
    assert page.evaluate("window.__noReload === true"), "the page was reloaded"


def test_menu_groups_open_by_hover_click_and_close_with_escape(desk, live_site):
    open_page(desk, live_site + "/rangliste")
    menu = desk.locator(".nav-group", has=desk.locator("button", has_text="Wettkämpfe")).locator(".nav-menu")
    assert not menu.is_visible()
    desk.hover("button:has-text('Wettkämpfe')")
    assert menu.is_visible() and "Duelle" in menu.inner_text()
    desk.mouse.move(640, 600)
    desk.click("button:has-text('Community')")
    community = desk.locator(".nav-group", has=desk.locator("button", has_text="Community")).locator(".nav-menu")
    assert community.is_visible() and "Events & Umfragen" in community.inner_text()
    desk.keyboard.press("Escape")
    assert desk.locator("button:has-text('Community')").get_attribute("aria-expanded") == "false"
    # the current page is marked in its group and in the dropdown
    assert "current" in desk.locator("button:has-text('Statistiken')").get_attribute("class")
    assert desk.locator(".nav-menu a[href='/rangliste']").get_attribute("aria-current") == "page"


def test_account_menu_and_logout(desk, live_site):
    open_page(desk, live_site + "/")
    desk.click(".nav-me")
    account = desk.locator(".mh-account .nav-menu")
    assert account.is_visible()
    for text in ("Meine Spielerseite", "Mein Profil", "Im Spiel", "Prefix", "Logout"):
        assert text in account.inner_text()
    desk.click(".mh-logout")
    with desk.expect_navigation():
        desk.click("dialog >> text=Ja")
    desk.wait_for_load_state("networkidle")
    assert desk.locator(".mh-login").is_visible() and desk.locator(".nav-me").count() == 0


def test_profile_tabs_save_without_reload(desk, live_site, db, two_players):
    a, _ = two_players
    open_page(desk, live_site + "/profil")
    desk.fill("#bio", "Baue am Hafen")
    desk.click("#profile-form button[type=submit]")
    desk.wait_for_selector("#profile-message.success")
    not_reloaded(desk)
    assert db.get_profile(a)["bio"] == "Baue am Hafen"

    desk.click(".md-tabs a:has-text('Im Spiel')")
    desk.wait_for_url(live_site + "/profil/spiel")
    desk.evaluate("window.__noReload = true")
    desk.click("[data-sidebar='playtime']")
    desk.wait_for_selector("[data-sidebar='playtime'][aria-pressed='true']")
    desk.click("[data-badges-preset='none']")
    desk.wait_for_selector("#badges-message.success")
    not_reloaded(desk)
    assert db.get_sidebar(a) == "playtime"


def test_prefix_tab_creates_joins_and_leaves(desk, live_site, db, two_players):
    a, _ = two_players
    open_page(desk, live_site + "/profil/prefix")
    assert desk.inner_text("[data-live='heading']") == "Prefix erstellen"
    desk.fill("#text", "Hafenbande")
    desk.click("#prefix-form button[type=submit]")
    desk.wait_for_selector("#prefix-message.success")
    desk.wait_for_selector("[data-live='heading']:has-text('Prefix bearbeiten')")
    assert "[Hafenbande]" in desk.inner_text("#beitreten")  # the list was updated as well
    desk.click("#leave-prefix")
    desk.wait_for_selector("#prefix-join-message.success")
    not_reloaded(desk)
    assert db.get_player_prefix(a) is None
    desk.click("#beitreten .srv-prefix-join button")
    desk.wait_for_selector("#leave-prefix")
    assert db.get_player_prefix(a)["text"] == "Hafenbande"


def test_events_and_polls_on_one_page(desk, live_site, db, server):
    starts = datetime.now(timezone.utc) + timedelta(days=2)
    db.create_event(server["id"], "Bootsrennen", starts, place="Hafen")
    db.create_poll(server["id"], "Was bauen wir?", ["Hafen", "Burg"], starts)
    open_page(desk, live_site + "/events")
    desk.click("[data-signup]")
    desk.wait_for_selector("[data-signup][aria-pressed='true']:has-text('Abmelden')")
    assert "1 Anmeldung" in desk.inner_text("[data-event]")
    desk.click("#umfragen [data-option='1']")
    desk.wait_for_selector("#umfragen .po-mine")
    assert "100 %" in desk.inner_text("#umfragen")
    not_reloaded(desk)
    # the old address of the polls lands on this page
    desk.goto(live_site + "/umfragen", wait_until="networkidle")
    assert desk.url.endswith("/events#umfragen")


def test_moderation_tabs_group_the_game_pages(desk, live_site):
    open_page(desk, live_site + "/users/icons")
    assert desk.inner_text(".md-tabs a[aria-current='page']").startswith("Im Spiel")
    sub = desk.locator(".md-subnav a")
    assert [sub.nth(i).inner_text() for i in range(sub.count())] == ["Join-Nachrichten & Belohnungen", "Eigene Icons"]
    assert desk.locator(".md-subnav a[aria-current='page']").inner_text() == "Eigene Icons"
    open_page(desk, live_site + "/users/spieler")
    desk.click(".md-nav a[href='#activity']")
    assert desk.evaluate("document.querySelector('#activity').getBoundingClientRect().top") >= 64  # below the header


@pytest.mark.parametrize("path", ["/", "/spieler", "/spieler?player=_Tobias4444", "/rangliste", "/vergleich?spieler=_Tobias4444,Notch",
                                  "/server-statistik", "/teams", "/wettbewerbe", "/ruhmeshalle", "/duelle", "/events", "/galerie",
                                  "/melden", "/profil", "/profil/spiel", "/profil/prefix", "/users", "/users/spieler",
                                  "/users/inhalte", "/users/zugang", "/users/belohnungen", "/users/icons", "/users/protokoll"])
def test_pages_run_without_script_errors_in_dark_mode(desk, live_site, path):
    desk.emulate_media(color_scheme="dark")
    desk.goto(live_site + path, wait_until="networkidle")
    assert desk.evaluate("document.documentElement.hasAttribute('data-dark')")
    # the header stays one row and nothing in it is cut off
    assert desk.evaluate("document.querySelector('.mcc-header').getBoundingClientRect().height") <= 66
