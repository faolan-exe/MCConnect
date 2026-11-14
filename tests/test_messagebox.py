"""Message boxes (web/static/messagebox.js): login errors and the logout question, in Chrome (skipped without)."""
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_mobile import browser, live_site  # noqa: F401 (fixtures)
from tests.test_web import app, client, on  # noqa: F401 (fixtures)

OPEN = """(kind) => { window.result = null; window.calls = [];
  window["messagebox_" + kind]("Titel <b>fett</b>", "Text", {confirmationButton: "Ja", cancelButton: "Nein"},
    {confirmationCallback: (x) => calls.push("ok " + x), confirmationParams: [1],
     cancelCallback: (x) => calls.push("cancel " + x), cancelParams: [2]}).then((r) => { window.result = r; }); }"""


def login_page(browser, live_site, dark=False):  # noqa: F811
    page = browser.new_page(color_scheme="dark" if dark else "light")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(live_site + "/login?next=/", wait_until="load")
    return page, errors


def test_confirm_runs_the_callback(browser, live_site):  # noqa: F811
    page, errors = login_page(browser, live_site)
    page.evaluate(OPEN, "info")
    assert page.locator("dialog.mbx[open]").count() == 1
    assert page.evaluate("document.activeElement.textContent") == "Ja"  # the focus is on the main button
    assert page.locator("#mbx-title").inner_text() == "Titel <b>fett</b>"  # text, never HTML
    page.click("text=Ja")
    page.wait_for_function("() => window.result !== null", timeout=5000)
    assert page.evaluate("[result, calls]") == [True, ["ok 1"]]
    assert page.locator("dialog.mbx").count() == 0 and errors == []


def test_escape_close_button_and_backdrop_cancel(browser, live_site):  # noqa: F811
    page, errors = login_page(browser, live_site)
    for close in (lambda: page.keyboard.press("Escape"), lambda: page.click(".mbx-close"),
                  lambda: page.mouse.click(5, 5), lambda: page.click("text=Nein")):
        page.evaluate(OPEN, "warning")
        close()
        page.wait_for_function("() => window.result !== null", timeout=5000)
        assert page.evaluate("[result, calls]") == [False, ["cancel 2"]]
    assert errors == []


def test_a_new_box_replaces_the_open_one(browser, live_site):  # noqa: F811
    page, _ = login_page(browser, live_site)
    page.evaluate(OPEN, "info")
    page.evaluate("void messagebox_error('Zweite', 'Box', {confirmationButton: 'OK'}, {})")
    assert page.locator("dialog.mbx").count() == 1 and page.locator("#mbx-title").inner_text() == "Zweite"
    assert page.evaluate("[result, calls]") == [False, []]  # the first one is neither confirmed nor cancelled


def test_missing_text_and_options_do_not_break_it(browser, live_site):  # noqa: F811
    """The old library threw on an undefined text (a login answer without "info") and showed nothing."""
    page, errors = login_page(browser, live_site)
    page.evaluate("void messagebox_error(undefined, undefined, {confirmationButton: 'OK'})")
    page.click("text=OK")
    assert errors == []


def test_dark_mode_is_readable(browser, live_site):  # noqa: F811
    page, _ = login_page(browser, live_site, dark=True)
    page.evaluate("void messagebox_info('Logout', 'Wirklich?', {confirmationButton: 'Ja'}, {})")
    colors = page.evaluate("""() => { const s = (q) => getComputedStyle(document.querySelector(q));
      return [s('dialog.mbx').backgroundColor, s('.mbx-text').color]; }""")
    assert colors == ["rgb(22, 29, 26)", "rgb(163, 174, 168)"]  # dark box, light text (was white on light blue)
