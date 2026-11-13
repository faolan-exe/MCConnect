"""The addon settings form in a real browser (Chrome): the item picker fills recipe slots and item fields."""
from tests.test_addon_protected import recipes_addon  # noqa: F401 (fixture)
from tests.test_metrics import two_players  # noqa: F401 (fixture)
from tests.test_mobile import browser, live_site  # noqa: F401 (fixtures)
from tests.test_web import app  # noqa: F401 (fixture)


def test_the_item_picker_fills_slots_and_item_fields(db, server, admin_id, recipes_addon, browser, live_site):  # noqa: F811
    main = live_site.replace("testdomain.", "")
    page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(main + "/login", wait_until="domcontentloaded")
    assert page.evaluate("""async () => (await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: 'tobi', password: 'testPassword'})})).status""") == 200
    page.goto(main + f"/manage/{server['id']}#addons/easynewcraftingrecipes", wait_until="networkidle")
    page.get_by_text("Rahmen", exact=True).first.click()  # open the recipe

    page.locator(".ext-slot").nth(1).click()
    page.locator(".ext-picker input[type=search]").fill("diamond_block")
    page.locator(".ext-picker-item[title='diamond_block']").click()
    assert page.locator(".ext-slot").nth(1).get_attribute("aria-label") == "Feld 2: Diamond Block"
    assert page.locator(".ext-picker").get_attribute("hidden") is not None  # closed

    page.locator(".ext-material").first.click()  # the result
    page.locator(".ext-picker input[type=search]").fill("stick")
    page.locator(".ext-picker input[type=search]").press("Enter")
    assert "stick" in page.locator(".ext-material").first.inner_text()
    assert errors == []
