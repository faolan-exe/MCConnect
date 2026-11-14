"""Dark mode (web/static/css/dark.css): the spots that broke before."""
import os
import re

from tests.test_web import app, client, on  # noqa: F401 (fixtures)

CSS = open(os.path.join(os.path.dirname(__file__), "..", "web", "static", "css", "dark.css")).read()


def rules(selector_part):
    """The bodies of the rules whose selector contains selector_part."""
    return [body for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS) if selector_part in selector]


def test_stats_table_keeps_its_light_mode_colors():
    """The table is dark in light mode already: dark mode must not repaint it (it did, with other row colors)."""
    assert not rules(".stats_table") and not rules(".stats_div")


def test_tabs_keep_the_orange_frame():
    """Gray borders on the tabs broke the orange frame of the table."""
    for body in rules(".tabBlock-tab"):
        assert "border-color" not in body.replace("border-bottom-color", "") and "!important" not in body


def test_active_tab_opens_into_the_table():
    active = " ".join(rules(".tabBlock-tab.is-active"))
    assert "background-color: rgb(36, 45, 46)" in active and "border-bottom-color: rgb(36, 45, 46)" in active


def test_login_page_has_its_own_dark_look(client, server):
    """The general page background made the login page black with dark blue shapes and light inputs."""
    html = client.get("/login?next=/", **on("testdomain")).get_data(as_text=True)
    assert '<body class="login-page">' in html and "dark.css" in html
    assert rules("body.login-page") and rules(".login-page .username-input")


def test_cards_are_lifted_from_the_page():
    """Cards have a shadow in dark mode too (it was switched off, everything looked flat)."""
    cards = [body for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS) if ".srv-card" in selector and ".pl-row" in selector]
    assert cards and "var(--dk-shadow)" in cards[0]
    assert "box-shadow: none" not in CSS


def test_cards_keep_their_accent_borders():
    """The card rule set every border gray: the green edge of hints and banners and the tier color of achievements
    were gone. Only the shadow is set; the gray comes from --srv-border."""
    cards = [body for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS) if ".srv-card" in selector and ".st-hint" in selector]
    assert cards and "border" not in cards[0].split("/*")[0].replace("box-shadow", "")
    rings = " ".join(rules(".ev-running"))
    assert "0 0 0 1px var(--srv-green)" in rings
