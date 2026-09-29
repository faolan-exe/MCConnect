// Clicks through the pages that save without reload and checks that nothing reloads or collapses.
const puppeteer = require("puppeteer");
const BASE = process.env.BASE || "http://testdomain.mc.t-auer.local:5070";
const results = [];
const check = (name, ok, detail = "") => { results.push(ok); console.log(`${ok ? "OK  " : "FAIL"} ${name}${detail ? " – " + detail : ""}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  await page.setCookie({ name: "session", value: process.env.SESSION, domain: "testdomain.mc.t-auer.local", path: "/" });
  const problems = [];
  page.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
  page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
  page.on("dialog", (d) => d.accept());
  const mark = () => page.evaluate(() => (window.__noReload = 42));
  const stillSame = () => page.evaluate(() => window.__noReload === 42);

  // 1. moderation: create an event, it shows up, the form stays open, no reload; then delete it
  await page.goto(BASE + "/users/inhalte", { waitUntil: "networkidle0" });
  await mark();
  await page.click("#events details.md-create summary");
  await page.type("#ev-title", "Klicktest-Event");
  await page.$eval("#ev-start", (el) => {
    const d = new Date(Date.now() + 3 * 86400000);
    el.value = d.toISOString().slice(0, 11) + "20:00";
  });
  await page.click('#events form [type="submit"]');
  await sleep(1500);
  check("event created without reload", await stillSame());
  check("event is in the table", (await page.$eval('[data-live="events"]', (el) => el.textContent)).includes("Klicktest-Event"));
  check("create form stays open", await page.$eval("#events details.md-create", (el) => el.open));
  check("form was emptied", (await page.$eval("#ev-title", (el) => el.value)) === "");
  const rowButton = await page.evaluateHandle(() => [...document.querySelectorAll('[data-live="events"] tr')]
    .find((tr) => tr.textContent.includes("Klicktest-Event")).querySelector("[data-mod-post]"));
  await rowButton.click();
  await sleep(1500);
  check("event deleted without reload", await stillSame());
  check("event is gone", !(await page.$eval('[data-live="events"]', (el) => el.textContent)).includes("Klicktest-Event"));

  // 2. access mode buttons: saved in place
  await page.goto(BASE + "/users/zugang", { waitUntil: "networkidle0" });
  await mark();
  const before = await page.$eval('[data-setting="access_mode"][aria-pressed="true"]', (el) => el.dataset.value);
  const other = before === "code" ? "both" : "code";
  await page.click(`[data-setting="access_mode"][data-value="${other}"]`);
  await sleep(1000);
  check("access mode switched without reload", await stillSame() &&
    (await page.$eval('[data-setting="access_mode"][aria-pressed="true"]', (el) => el.dataset.value)) === other);
  await page.click(`[data-setting="access_mode"][data-value="${before}"]`);
  await sleep(800);

  // 3. level editor: adding a condition keeps the level open (the reported bug)
  await page.goto(BASE + "/users/belohnungen", { waitUntil: "networkidle0" });
  await mark();
  await page.evaluate(() => document.querySelectorAll("details.rw-level")[2].querySelector("summary").click());
  const count = () => page.evaluate(() => document.querySelectorAll("details.rw-level")[2].querySelectorAll(".rw-condition").length);
  const groups = () => page.evaluate(() => document.querySelectorAll("details.rw-level")[2].querySelectorAll(".rw-group").length);
  const n = await count(), g = await groups();
  const clickIn = (label, last) => page.evaluate((label, last) => {
    const buttons = [...document.querySelectorAll("details.rw-level")[2].querySelectorAll("button")].filter((b) => b.textContent === label);
    (last ? buttons[buttons.length - 1] : buttons[0]).click();
  }, label, last);
  await clickIn("+ oder");
  await sleep(300);
  check("level stays open after + oder", await page.evaluate(() => document.querySelectorAll("details.rw-level")[2].open));
  check("alternative added", (await groups()) === g + 1, g + " -> " + (await groups()));
  await clickIn("+ und", true);
  await sleep(300);
  check("level stays open after + und", await page.evaluate(() => document.querySelectorAll("details.rw-level")[2].open));
  check("condition added to the new alternative", (await count()) === n + 2, n + " -> " + (await count()));
  check("'oder' shown between alternatives", (await page.evaluate(() => document.querySelectorAll("details.rw-level")[2].querySelectorAll(".rw-or").length)) === g);
  await page.click('[data-role="save"]');
  await sleep(1200);
  check("levels saved", (await page.$eval('[data-role="levels-message"]', (el) => el.textContent)).includes("Gespeichert"));
  check("level still open after saving", await page.evaluate(() => document.querySelectorAll("details.rw-level")[2].open));
  await page.click('[data-role="reset"]');
  await sleep(1200);
  // own text: added and listed, the level editor keeps its unsaved condition
  await page.type('[data-add-text="join"] input[name="text"]', "{name} testet die Knöpfe.");
  await page.click('[data-add-text="join"] button');
  await sleep(1200);
  check("own text added without reload", await stillSame() &&
    (await page.$eval('[data-texts="join"]', (el) => el.textContent)).includes("testet die Knöpfe"));
  check("editor still there", (await count()) === n);
  check("level still open", await page.evaluate(() => document.querySelectorAll("details.rw-level")[2].open));
  await page.evaluate(() => [...document.querySelectorAll('[data-texts="join"] li')]
    .find((li) => li.textContent.includes("testet die Knöpfe")).querySelector("button").click());
  await sleep(1200);
  check("own text removed", !(await page.$eval('[data-texts="join"]', (el) => el.textContent)).includes("testet die Knöpfe"));

  // 4. profile: choosing a color updates the preview in place
  await page.goto(BASE + "/profil", { waitUntil: "networkidle0" });
  await mark();
  await page.click('[data-field="color"] button[data-value="gray"]');
  await sleep(1000);
  check("color saved without reload", await stillSame() &&
    (await page.$eval('[data-role="join-preview"]', (el) => el.innerHTML)).includes("#AAAAAA"));
  await page.click('[data-field="color"] button[data-value="white"]');
  await sleep(800);

  // 5. player page: moderator note added and removed in place
  await page.goto(BASE + "/spieler?player=Notch", { waitUntil: "domcontentloaded" });
  await sleep(2000);
  await mark();
  await page.type("#note-text", "Klicktest-Notiz");
  await page.click("#notes .nt-form button");
  await sleep(1500);
  check("note added without reload", await stillSame() &&
    (await page.$eval('[data-live="notes"]', (el) => el.textContent)).includes("Klicktest-Notiz"));
  await page.evaluate(() => [...document.querySelectorAll('[data-live="notes"] .nt-note')]
    .find((n) => n.textContent.includes("Klicktest-Notiz")).querySelector("[data-note]").click());
  await sleep(1500);
  check("note removed", !(await page.$eval('[data-live="notes"]', (el) => el.textContent)).includes("Klicktest-Notiz"));

  const relevant = problems.filter((p) => !p.includes("minerender.org") && !p.includes("favicon"));
  check("no console errors (incl. CSP)", relevant.length === 0, relevant.slice(0, 5).join(" | "));
  await browser.close();
  console.log(`${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(results.every(Boolean) ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(2); });
