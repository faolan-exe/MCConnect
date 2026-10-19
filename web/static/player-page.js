// Player page (spieler-info.html): live cards, favourite, sharing, moderation notes and warnings, guestbook,
// live achievements and the detail tables (tabs, search, sorting). Needs poll.js.

function forceDownload(url, fileName) {
  fetch(url).then((response) => response.blob()).then((blob) => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });
}

function mccPlayerPage({ name }) {
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(async (r) => ({ ok: r.ok, data: await r.json().catch(() => ({})) }));

  // ------------------------------------------------------------ cards (same values as /api/player_info)
  pollJson("/api/player_info/" + encodeURIComponent(name), 5000, (values) => {
    for (const index of [2, 3, 5, 6]) {
      const card = document.querySelector(`[data-card="${index}"]`);
      if (card) card.textContent = index === 3 ? String(values[index]).split(", ")[0] : values[index];  // date only
    }
    const status = document.querySelector('[data-card="status"]');
    if (status) {
      const online = values[1] === "online";
      status.replaceChildren();
      const dot = document.createElement("span");
      dot.className = "pi-dot" + (online ? " pi-dot-on" : "");
      status.append(dot, online ? " Gerade online" : ` Offline · zuletzt online ${values[4]}`);
    }
  });

  // ------------------------------------------------------------ favourite and sharing
  const fav = document.querySelector("[data-favorite]");
  if (fav) fav.addEventListener("click", async () => {
    const result = await post("/api/favorites/toggle", { name: fav.dataset.favorite });
    if (!result.ok) return;
    fav.setAttribute("aria-pressed", String(result.data.favorite));
    fav.textContent = result.data.favorite ? "★ Favorit" : "☆ Als Favorit merken";
  });
  const share = document.getElementById("share-link");
  if (share) share.addEventListener("click", async () => {
    const url = location.origin + "/spieler?player=" + encodeURIComponent(name);
    if (navigator.share) {
      try { await navigator.share({ title: name, url }); } catch (e) {}
      return;
    }
    await navigator.clipboard.writeText(url);
    share.textContent = "Link kopiert";
    setTimeout(() => (share.textContent = "Link teilen"), 2000);
  });

  // ------------------------------------------------------------ moderation (delegated: the lists are replaced after changes)
  const notes = document.getElementById("notes");
  if (notes) {
    notes.querySelector(".nt-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if ((await post("/api/mod/notes", { name: notes.dataset.player, text: document.getElementById("note-text").value })).ok) {
        event.target.reset();
        refreshLiveParts();
      }
    });
    notes.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-note]");
      if (button && confirm("Notiz löschen?") && (await post("/api/mod/notes/delete", { id: Number(button.dataset.note) })).ok) refreshLiveParts();
    });
  }
  const discipline = document.getElementById("discipline");
  if (discipline) {
    discipline.querySelectorAll("[data-discipline]").forEach((form) => form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const result = await post(form.dataset.discipline, Object.assign({ name: discipline.dataset.player }, Object.fromEntries(new FormData(form))));
      const message = discipline.querySelector('[data-role="message"]');
      message.textContent = result.ok ? (result.data.message || "Gespeichert.") : (result.data.error || "Das hat nicht geklappt.");
      message.className = "mcc-message " + (result.ok ? "success" : "error");
      if (result.ok) { form.reset(); refreshLiveParts(); }
    }));
    discipline.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-warning]");
      if (!button || !confirm("Verwarnung löschen?")) return;
      if ((await post("/api/mod/warnings/delete", { id: Number(button.dataset.warning) })).ok) refreshLiveParts();
    });
  }

  // ------------------------------------------------------------ guestbook
  const guestbook = document.getElementById("guestbook");
  const gbForm = guestbook.querySelector(".gb-form");
  if (gbForm) gbForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = await post("/api/guestbook", { name: guestbook.dataset.player, text: gbForm.elements.text.value });
    const message = guestbook.querySelector('[data-role="message"]');
    message.textContent = result.ok ? "" : (result.data.error || "Das hat nicht geklappt.");
    message.className = "mcc-message" + (result.ok ? "" : " error");
    if (result.ok) { gbForm.reset(); refreshLiveParts(); }
  });
  guestbook.addEventListener("click", async (event) => {
    const remove = event.target.closest("[data-gb-delete]");
    const report = event.target.closest("[data-gb-report]");
    if (remove && confirm("Eintrag wirklich löschen?")) {
      if ((await post("/api/guestbook/delete", { id: Number(remove.dataset.gbDelete) })).ok) refreshLiveParts();
    } else if (report && confirm("Diesen Eintrag den Moderatoren melden?")) {
      if ((await post("/api/guestbook/report", { id: Number(report.dataset.gbReport) })).ok) {
        report.textContent = "Gemeldet";
        report.disabled = true;
      }
    }
  });

  // ------------------------------------------------------------ achievements update live
  const achievements = document.getElementById("achievements");
  if (achievements) setInterval(async () => {
    if (document.hidden) return;
    const response = await fetch("/api/spieler/" + encodeURIComponent(achievements.dataset.player) + "/erfolge");
    if (!response.ok) return;
    const data = await response.json();
    if (data.count === Number(achievements.dataset.count)) return;
    achievements.dataset.count = data.count;
    achievements.innerHTML = data.html;
    achievements.classList.add("ac-updated");
  }, 15000);

  // ------------------------------------------------------------ detail tables
  const details = document.getElementById("details");
  const tabs = [...details.querySelectorAll('[role="tab"]')];
  const search = document.getElementById("detail-search");

  function select(tab, focus) {
    for (const other of tabs) {
      const selected = other === tab;
      other.setAttribute("aria-selected", String(selected));
      other.tabIndex = selected ? 0 : -1;
      document.getElementById(other.getAttribute("aria-controls")).hidden = !selected;
    }
    if (focus) tab.focus();
    tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    filter();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (step) select(tabs[(index + step + tabs.length) % tabs.length], true);
    });
  });

  function filter() {
    const query = search.value.trim().toLowerCase();
    const panel = details.querySelector('[role="tabpanel"]:not([hidden])');
    if (!panel) return;
    let shown = 0;
    panel.querySelectorAll("tbody tr").forEach((row) => {
      row.hidden = query !== "" && !row.dataset.name.includes(query);
      if (!row.hidden) shown += 1;
    });
    const none = panel.querySelector('[data-role="no-match"]');
    if (none) none.hidden = shown > 0;
    // a hint when the table has more columns than fit on the screen
    const wrap = panel.querySelector(".pi-table-wrap");
    details.querySelector(".pi-swipe").hidden = !wrap || wrap.scrollWidth <= wrap.clientWidth + 1;
  }
  filter();
  window.addEventListener("resize", filter);
  search.addEventListener("input", filter);

  details.addEventListener("click", (event) => {
    const button = event.target.closest("[data-sort]");
    if (!button) return;
    const table = button.closest("table");
    const column = Number(button.dataset.sort);
    const descending = column > 0 && button.getAttribute("aria-sort") !== "descending";
    const ascendingName = column === 0 && button.getAttribute("aria-sort") !== "ascending";
    table.querySelectorAll("[data-sort]").forEach((b) => b.removeAttribute("aria-sort"));
    button.setAttribute("aria-sort", column === 0 ? (ascendingName ? "ascending" : "descending") : (descending ? "descending" : "ascending"));
    const body = table.tBodies[0];
    const key = (row) => column === 0 ? row.cells[0].textContent.trim().toLowerCase() : Number(row.cells[column].dataset.value);
    const rows = [...body.rows].sort((a, b) => {
      const x = key(a), y = key(b);
      const order = column === 0 ? x.localeCompare(y, "de") : x - y;
      return (column === 0 ? ascendingName : !descending) ? order : -order;
    });
    body.append(...rows);
  });
}
