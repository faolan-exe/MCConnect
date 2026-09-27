// Selection of players for /vergleich. Buttons with data-compare="<name>" add or remove a
// player; a bar at the bottom of the page lists the selection and links to the comparison.
// The selection is kept in localStorage (per server subdomain) across pages.
(() => {
  const KEY = "mcc-compare";
  const MAX = 4;

  function load() {
    try {
      const value = JSON.parse(localStorage.getItem(KEY));
      return Array.isArray(value) ? value.filter((v) => typeof v === "string").slice(0, MAX) : [];
    } catch (e) {
      return [];
    }
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(selected)); } catch (e) {}
  }

  let selected = load();
  const has = (name) => selected.some((n) => n.toLowerCase() === name.toLowerCase());

  const bar = document.createElement("aside");
  bar.className = "cmp-bar";
  bar.setAttribute("aria-label", "Spielervergleich");
  bar.innerHTML = '<div class="cmp-bar-inner"><span class="cmp-bar-label">Vergleich</span>' +
    '<ul class="cmp-bar-list"></ul><span class="cmp-bar-note"></span>' +
    '<a class="cmp-bar-go">Vergleichen</a></div>';
  document.body.append(bar);
  const list = bar.querySelector(".cmp-bar-list");
  const note = bar.querySelector(".cmp-bar-note");
  const go = bar.querySelector(".cmp-bar-go");

  function render(message) {
    list.replaceChildren();
    for (const name of selected) {
      const item = document.createElement("li");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.compare = name;
      remove.setAttribute("aria-label", name + " entfernen");
      remove.textContent = "×";
      item.append(document.createTextNode(name), remove);
      list.append(item);
    }
    note.textContent = message || (selected.length < 2 ? "Wähle mindestens 2 Spieler" : "");
    go.href = "/vergleich?spieler=" + selected.map(encodeURIComponent).join(",");
    go.classList.toggle("disabled", selected.length < 2);
    bar.classList.toggle("open", selected.length > 0);
    document.body.classList.toggle("cmp-bar-open", selected.length > 0);

    document.querySelectorAll("[data-compare]").forEach((button) => {
      if (button.closest(".cmp-bar")) return;
      const active = has(button.dataset.compare);
      button.setAttribute("aria-pressed", String(active));
      button.textContent = active ? "✓" : "+";
      button.title = active ? "Aus dem Vergleich entfernen" : "Zum Vergleich hinzufügen";
    });
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-compare]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const name = button.dataset.compare;
    let message = "";
    if (has(name)) {
      selected = selected.filter((n) => n.toLowerCase() !== name.toLowerCase());
    } else if (selected.length >= MAX) {
      message = "Höchstens " + MAX + " Spieler";
    } else {
      selected.push(name);
    }
    save();
    render(message);
  });
  go.addEventListener("click", (event) => {
    if (selected.length < 2) event.preventDefault();
  });

  render();
})();
