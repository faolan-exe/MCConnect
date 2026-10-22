// The addon store on /manage/<id>#addons: tiles of all addons, the page of one addon (#addons/<id>) with its actions
// and a settings form built from the addon's own settings description (database/addons.py). Nothing reloads the
// page; the state is polled every few seconds while the tab is open.
(() => {
  const root = document.querySelector(".ad-root");
  if (!root) return;
  const serverId = root.dataset.serverId;
  const api = (path) => `/api/servers/${serverId}/addons${path || ""}`;
  const POLL_MS = 5000;

  let data = null;        // the last answer of the API
  let openId = null;      // the addon whose page is open
  let draft = null;       // the settings being edited (a copy of addon.config)
  let saved = null;       // JSON of the settings as last loaded, to tell changes
  let remoteChanged = false;

  // Minecraft chat colors for the preview
  const COLORS = {
    0: "#000000", 1: "#0000AA", 2: "#00AA00", 3: "#00AAAA", 4: "#AA0000", 5: "#AA00AA", 6: "#FFAA00", 7: "#AAAAAA",
    8: "#555555", 9: "#5555FF", a: "#55FF55", b: "#55FFFF", c: "#FF5555", d: "#FF55FF", e: "#FFFF55", f: "#FFFFFF",
  };
  const COLOR_NAMES = {
    0: "Schwarz", 1: "Dunkelblau", 2: "Dunkelgrün", 3: "Türkis", 4: "Dunkelrot", 5: "Lila", 6: "Gold", 7: "Grau",
    8: "Dunkelgrau", 9: "Blau", a: "Grün", b: "Hellblau", c: "Rot", d: "Pink", e: "Gelb", f: "Weiß",
  };
  const FORMATS = { l: "Fett", o: "Kursiv", n: "Unterstrichen", m: "Durchgestrichen", r: "Zurücksetzen" };

  // ------------------------------------------------------------------ helpers

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  // append() turns null into the text "null": leave missing parts out
  const put = (node, ...children) => node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const prettyItem = (id) => id.split("_").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");

  // sprite icons of css/minecraft.css; ids without a sprite get a plain square
  let spriteIds = null;
  function hasSprite(id) {
    if (spriteIds === null) {
      spriteIds = new Set();
      for (const sheet of document.styleSheets) {
        if (!sheet.href || !sheet.href.includes("minecraft.css")) continue;
        try {
          for (const rule of sheet.cssRules) {
            const match = /^\.icon-minecraft-([a-z0-9-]+)$/.exec(rule.selectorText || "");
            if (match) spriteIds.add(match[1]);
          }
        } catch (e) { /* not readable */ }
      }
    }
    return spriteIds.has(id.replace(/_/g, "-"));
  }

  function itemIcon(id, small) {
    if (!id) return el("span", { class: "ad-item-empty", "aria-hidden": "true" });
    if (hasSprite(id)) {
      return el("i", { class: `icon-minecraft${small ? "-sm" : ""} icon-minecraft-${id.replace(/_/g, "-")}`, title: prettyItem(id) });
    }
    return el("span", { class: "ad-item-letter", title: prettyItem(id), text: id.charAt(0).toUpperCase() });
  }

  // "&a" colors and formats as HTML (text nodes only, nothing is parsed as HTML)
  function chatPreview(text) {
    const line = el("span", { class: "ad-chat-line" });
    let style = { color: COLORS.f };
    const parts = String(text).split(/(&[0-9a-fk-or])/i);
    for (const part of parts) {
      const code = /^&([0-9a-fk-or])$/i.exec(part);
      if (code) {
        const c = code[1].toLowerCase();
        if (COLORS[c]) style = { color: COLORS[c] };
        else if (c === "r") style = { color: COLORS.f };
        else style = Object.assign({}, style, { [c]: true });
        continue;
      }
      if (!part) continue;
      const span = el("span", { text: part });
      span.style.color = style.color;
      if (style.l) span.style.fontWeight = "700";
      if (style.o) span.style.fontStyle = "italic";
      if (style.n || style.m) span.style.textDecoration = [style.n && "underline", style.m && "line-through"].filter(Boolean).join(" ");
      line.append(span);
    }
    return line;
  }

  function withExamples(text, field) {
    let result = String(text);
    for (const p of field.placeholders || []) result = result.split(p.name).join(p.example || p.label || p.name);
    return result;
  }

  async function getJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }

  // ------------------------------------------------------------------ item picker (one popover for all fields)

  let items = null;
  async function loadItems() {
    if (items === null) items = getJson("/api/minecraft/items").catch(() => []);
    return items;
  }

  const picker = el("div", { class: "ad-picker", role: "dialog", "aria-label": "Item wählen", hidden: true });
  document.body.append(picker);
  let pickerDone = null;

  async function openPicker(anchor, current, done) {
    pickerDone = done;
    const list = await loadItems();
    const search = el("input", { type: "search", placeholder: "Item suchen, z. B. glass", value: current || "", "aria-label": "Item suchen" });
    const results = el("div", { class: "ad-picker-results" });
    const render = () => {
      const query = search.value.trim().toLowerCase().replace(/\s+/g, "_");
      const matches = list.filter((id) => !query || id.includes(query))
        .sort((a, b) => (a.startsWith(query) ? 0 : 1) - (b.startsWith(query) ? 0 : 1) || a.length - b.length).slice(0, 60);
      results.replaceChildren(...matches.map((id) => el("button", { type: "button", class: "ad-picker-item", title: id,
        onclick: () => choose(id) }, itemIcon(id), el("span", { text: prettyItem(id) }))));
      if (!matches.length) results.append(el("p", { class: "hint", text: "Nichts gefunden. Du kannst die ID auch direkt eintippen." }));
    };
    const choose = (id) => { closePicker(); if (pickerDone) pickerDone(id); };
    search.addEventListener("input", render);
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        const first = results.querySelector(".ad-picker-item");
        choose(first ? first.title : search.value.trim().toLowerCase());
      } else if (event.key === "Escape") {
        closePicker();
      }
    });
    picker.replaceChildren(
      el("div", { class: "ad-picker-head" }, search,
        el("button", { type: "button", class: "btn btn-secondary", onclick: () => choose(""), text: "Leeren" })),
      results);
    render();
    picker.hidden = false;
    const box = anchor.getBoundingClientRect();
    const width = Math.min(360, window.innerWidth - 24);
    picker.style.width = width + "px";
    picker.style.left = Math.max(12, Math.min(window.scrollX + box.left, window.scrollX + window.innerWidth - width - 12)) + "px";
    picker.style.top = window.scrollY + box.bottom + 6 + "px";
    search.focus();
    search.select();
  }

  function closePicker() {
    picker.hidden = true;
    pickerDone = null;
  }

  document.addEventListener("mousedown", (event) => {
    if (!picker.hidden && !picker.contains(event.target) && !event.target.closest("[data-picker]")) closePicker();
  });

  // ------------------------------------------------------------------ settings form

  // show_if of a field, against the values of its own object (the settings, or one row of a list)
  function visible(field, scope) {
    const condition = field.show_if;
    if (!condition) return true;
    const value = scope[condition.key];
    if ("is" in condition) return value === condition.is;
    if ("in" in condition) return condition.in.includes(value);
    return true;
  }

  // Calls back with the new value; rerender() rebuilds the object (for show_if and list changes).
  function fieldNode(field, scope, path, rerender) {
    const id = "ad-f-" + path.replace(/[^a-z0-9]/gi, "-");
    const set = (value, rebuild) => {
      scope[field.key] = value;
      changed();
      if (rebuild) rerender();
    };
    const value = scope[field.key];
    const label = el("label", { for: id, text: field.label });
    const help = field.help ? el("p", { class: "hint ad-help", text: field.help }) : null;
    const wrap = el("div", { class: `ad-field ad-field-${field.type}`, dataset: { path } });
    const error = el("p", { class: "ad-error", hidden: true });

    switch (field.type) {
      case "toggle": {
        const input = el("input", { type: "checkbox", id, class: "ad-switch-input", checked: value === true,
          onchange: (e) => set(e.target.checked, true) });
        put(wrap, el("label", { class: "ad-switch", for: id }, input, el("span", { class: "ad-switch-track", "aria-hidden": "true" }),
          el("span", { class: "ad-switch-label", text: field.label })), help);
        break;
      }
      case "number": {
        const step = field.step || 1;
        const number = el("input", { type: "number", id, min: field.min, max: field.max, step, value,
          oninput: (e) => { if (e.target.value !== "") { set(Number(e.target.value)); if (range) range.value = e.target.value; } } });
        const span = field.max - field.min;
        const range = span > 0 && span <= 1000 && span / step <= 2000
          ? el("input", { type: "range", min: field.min, max: field.max, step, value, "aria-label": field.label,
            oninput: (e) => { number.value = e.target.value; set(Number(e.target.value)); } })
          : null;
        put(wrap, label, el("div", { class: "ad-number" }, range,
          el("div", { class: "input-suffix" }, number, field.unit ? el("span", { text: field.unit }) : null)), help);
        break;
      }
      case "text":
      case "lines": {
        put(wrap, label, help);
        const lines = field.type === "lines" ? value.slice() : [value];
        const preview = field.colors ? el("div", { class: "ad-chat", "aria-label": "Vorschau im Chat" }) : null;
        const updatePreview = () => {
          if (!preview) return;
          preview.replaceChildren(...lines.filter((l) => l !== "")
            .map((l) => el("div", {}, chatPreview(withExamples(l, field)))));
          if (!preview.childNodes.length) preview.append(el("span", { class: "ad-chat-empty", text: "(leer)" }));
        };
        let lastInput = null;
        const inputs = el("div", { class: "ad-lines" });
        const renderLines = () => {
          inputs.replaceChildren(...lines.map((line, index) => {
            const input = el("input", { type: "text", maxlength: field.max, value: line, id: index === 0 ? id : null,
              list: field.suggest ? `ad-suggest-${field.suggest}` : null,
              placeholder: field.type === "lines" ? `Zeile ${index + 1}` : null,
              onfocus: (e) => (lastInput = e.target),
              oninput: (e) => {
                lines[index] = e.target.value;
                set(field.type === "lines" ? lines.slice() : lines[0]);
                updatePreview();
              } });
            if (field.type === "text") return input;
            return el("div", { class: "ad-line" }, input,
              el("button", { type: "button", class: "ad-icon-btn", title: "Zeile entfernen", "aria-label": "Zeile entfernen", text: "×",
                onclick: () => { lines.splice(index, 1); set(lines.slice()); renderLines(); updatePreview(); } }));
          }));
          if (field.type === "lines" && lines.length < field.max_items) {
            inputs.append(el("button", { type: "button", class: "btn btn-secondary ad-small", text: "+ Zeile",
              onclick: () => { lines.push(""); set(lines.slice()); renderLines(); updatePreview(); inputs.querySelectorAll("input")[lines.length - 1].focus(); } }));
          }
        };
        renderLines();
        // insert a color code or placeholder where the cursor is
        const insert = (text) => {
          const input = lastInput && inputs.contains(lastInput) ? lastInput : inputs.querySelector("input");
          if (!input) return;
          const start = input.selectionStart ?? input.value.length, end = input.selectionEnd ?? input.value.length;
          input.value = (input.value.slice(0, start) + text + input.value.slice(end)).slice(0, field.max);
          input.dispatchEvent(new Event("input"));
          input.focus();
          input.setSelectionRange(start + text.length, start + text.length);
        };
        const tools = el("div", { class: "ad-tools" });
        if (field.colors) {
          tools.append(...Object.entries(COLORS).map(([code, color]) => {
            const swatch = el("button", { type: "button", class: "ad-swatch", title: `${COLOR_NAMES[code]} (&${code})`,
              "aria-label": COLOR_NAMES[code], onmousedown: (e) => e.preventDefault(), onclick: () => insert("&" + code) });
            swatch.style.background = color;
            return swatch;
          }), ...Object.entries(FORMATS).map(([code, name]) => el("button", { type: "button", class: "ad-format", title: `${name} (&${code})`,
            onmousedown: (e) => e.preventDefault(), onclick: () => insert("&" + code), text: code.toUpperCase() })));
        }
        for (const p of field.placeholders || []) {
          tools.append(el("button", { type: "button", class: "ad-chip", title: p.label, onmousedown: (e) => e.preventDefault(),
            onclick: () => insert(p.name), text: p.name }));
        }
        put(wrap, inputs, tools.childNodes.length ? tools : null, preview);
        updatePreview();
        break;
      }
      case "select": {
        if (field.options.length <= 4 && field.options.every((o) => o.label.length <= 34)) {
          const group = el("div", { class: "ad-segmented", role: "radiogroup", "aria-label": field.label });
          for (const option of field.options) {
            group.append(el("button", { type: "button", role: "radio", "aria-checked": String(option.value === value), text: option.label,
              onclick: () => set(option.value, true) }));
          }
          put(wrap, el("span", { class: "ad-label", text: field.label }), group, help);
        } else {
          const select = el("select", { id, onchange: (e) => set(e.target.value, true) },
            ...field.options.map((o) => el("option", { value: o.value, selected: o.value === value, text: o.label })));
          put(wrap, label, select, help);
        }
        break;
      }
      case "multiselect": {
        const box = el("div", { class: "ad-checks", role: "group", "aria-label": field.label });
        for (const option of field.options) {
          const input = el("input", { type: "checkbox", checked: value.includes(option.value),
            onchange: (e) => {
              const chosen = new Set(scope[field.key]);
              if (e.target.checked) chosen.add(option.value); else chosen.delete(option.value);
              set(field.options.map((o) => o.value).filter((v) => chosen.has(v)));
            } });
          box.append(el("label", { class: "ad-check" }, input, el("span", { text: option.label })));
        }
        const all = el("button", { type: "button", class: "ad-link", text: "alle",
          onclick: () => set(field.options.map((o) => o.value), true) });
        const none = el("button", { type: "button", class: "ad-link", text: "keine", onclick: () => set([], true) });
        put(wrap, el("div", { class: "ad-label-row" }, el("span", { class: "ad-label", text: field.label }), all, none), box, help);
        break;
      }
      case "material": {
        const button = el("button", { type: "button", class: "ad-material", id, dataset: { picker: "1" },
          onclick: (e) => openPicker(e.currentTarget, scope[field.key], (picked) => set(picked, true)) },
          itemIcon(value), el("span", { text: value ? prettyItem(value) : "Item wählen …" }),
          value ? el("code", { text: value }) : null);
        put(wrap, label, button, help);
        break;
      }
      case "recipe": {
        const grid = el("div", { class: "ad-recipe", role: "group", "aria-label": field.label });
        value.forEach((slot, index) => {
          grid.append(el("button", { type: "button", class: "ad-slot", dataset: { picker: "1" },
            title: slot ? prettyItem(slot) : "Leeres Feld – klicken zum Füllen",
            "aria-label": `Feld ${index + 1}: ${slot ? prettyItem(slot) : "leer"}`,
            onclick: (e) => openPicker(e.currentTarget, slot, (picked) => {
              const slots = scope[field.key].slice();
              slots[index] = picked;
              set(slots, true);
            }),
            oncontextmenu: (e) => {
              e.preventDefault();
              const slots = scope[field.key].slice();
              slots[index] = "";
              set(slots, true);
            } }, itemIcon(slot)));
        });
        const clear = el("button", { type: "button", class: "ad-link", text: "Raster leeren", onclick: () => set(Array(9).fill(""), true) });
        put(wrap, el("div", { class: "ad-label-row" }, el("span", { class: "ad-label", text: field.label }), clear),
          grid, el("p", { class: "hint ad-help", text: "Feld anklicken zum Füllen, Rechtsklick leert es." }), help);
        break;
      }
      case "position": {
        const axes = el("div", { class: "ad-position" });
        for (const axis of ["x", "y", "z"]) {
          axes.append(el("label", { class: "ad-axis" }, el("span", { text: axis.toUpperCase() }),
            el("input", { type: "number", step: 1, value: value[axis], id: axis === "x" ? id : null, "aria-label": `${field.label} ${axis}`,
              oninput: (e) => { if (e.target.value !== "") set(Object.assign({}, scope[field.key], { [axis]: Math.trunc(Number(e.target.value)) })); } })));
        }
        put(wrap, label, axes, help);
        break;
      }
      case "list":
        put(wrap, listNode(field, value, path, (rows) => set(rows, true)));
        break;
      default:
        put(wrap, label, el("p", { class: "hint", text: "Diese Einstellung kann die Seite nicht anzeigen." }));
    }
    put(wrap, error);
    return wrap;
  }

  function fieldsNode(fields, scope, path) {
    const box = el("div", { class: "ad-fields" });
    const render = () => {
      box.replaceChildren(...fields.filter((f) => visible(f, scope)).map((f) => fieldNode(f, scope, path + f.key, render)));
    };
    render();
    return box;
  }

  const openRows = new Set();  // "path.index" of list rows that are unfolded

  function listNode(field, rows, path, update) {
    const box = el("div", { class: "ad-list" });
    const head = el("div", { class: "ad-label-row" }, el("span", { class: "ad-label", text: field.label }),
      el("span", { class: "hint", text: `${rows.length} / ${field.max_items}` }));
    box.append(head);
    if (!rows.length) box.append(el("p", { class: "hint ad-empty", text: `Noch kein ${field.item_label}.` }));
    rows.forEach((row, index) => {
      const key = `${path}.${index}`;
      const title = (field.title_key && row[field.title_key]) || `${field.item_label} ${index + 1}`;
      const details = el("details", { class: "ad-row", open: openRows.has(key) });
      details.addEventListener("toggle", () => (details.open ? openRows.add(key) : openRows.delete(key)));
      const move = (delta) => {
        const copy = rows.slice();
        const [moved] = copy.splice(index, 1);
        copy.splice(index + delta, 0, moved);
        openRows.clear();
        update(copy);
      };
      const off = "enabled" in row && row.enabled === false;
      const summary = el("summary", {}, el("span", { class: "ad-row-title", text: title }),
        off ? el("span", { class: "ad-pill ad-pill-muted", text: "aus" }) : null,
        el("span", { class: "ad-row-actions" },
          el("button", { type: "button", class: "ad-icon-btn", title: "Nach oben", "aria-label": "Nach oben", text: "↑", disabled: index === 0,
            onclick: (e) => { e.preventDefault(); move(-1); } }),
          el("button", { type: "button", class: "ad-icon-btn", title: "Nach unten", "aria-label": "Nach unten", text: "↓",
            disabled: index === rows.length - 1, onclick: (e) => { e.preventDefault(); move(1); } }),
          el("button", { type: "button", class: "ad-icon-btn ad-danger", title: "Entfernen", "aria-label": `${title} entfernen`, text: "×",
            onclick: (e) => {
              e.preventDefault();
              if (!confirm(`${field.item_label} »${title}« entfernen?`)) return;
              const copy = rows.slice();
              copy.splice(index, 1);
              openRows.clear();
              update(copy);
            } })));
      details.append(summary, fieldsNode(field.fields, row, key + "."));
      box.append(details);
    });
    if (rows.length < field.max_items) {
      box.append(el("button", { type: "button", class: "btn btn-secondary ad-small", text: `+ ${field.item_label}`,
        onclick: () => {
          const fresh = {};
          for (const f of field.fields) fresh[f.key] = clone(f.default);
          openRows.add(`${path}.${rows.length}`);
          update(rows.concat([fresh]));
        } }));
    }
    return box;
  }

  // ------------------------------------------------------------------ the page of one addon

  const isDirty = () => draft !== null && JSON.stringify(draft) !== saved;

  function changed() {
    const bar = root.querySelector(".ad-savebar");
    if (bar) bar.hidden = !isDirty();
  }

  async function act(addon, action, body, confirmText) {
    if (confirmText && !confirm(confirmText)) return;
    const message = root.querySelector(".ad-detail .message");
    const result = await postJson(api(`/${addon.id}/${action}`), body || {});
    if (!result.ok) {
      showMessage(message, result.data.error || "Das hat nicht geklappt.", "error");
      return result;
    }
    data = result.data;
    render(false);
    return result;
  }

  async function save(addon) {
    const result = await postJson(api(`/${addon.id}/config`), { config: draft });
    const message = root.querySelector(".ad-savebar .message") || root.querySelector(".ad-detail .message");
    root.querySelectorAll(".ad-field.ad-invalid").forEach((f) => f.classList.remove("ad-invalid"));
    root.querySelectorAll(".ad-error").forEach((e) => (e.hidden = true));
    if (!result.ok) {
      for (const error of result.data.errors || []) {
        // open the list rows on the way to the field, then mark it
        const parts = error.path.split(".");
        for (let i = 2; i <= parts.length; i += 2) openRows.add(parts.slice(0, i).join("."));
      }
      renderForm(addon, false);
      for (const error of result.data.errors || []) {
        const field = root.querySelector(`.ad-field[data-path="${CSS.escape(error.path)}"]`);
        if (!field) continue;
        field.classList.add("ad-invalid");
        const text = field.querySelector(":scope > .ad-error");
        text.textContent = error.message;
        text.hidden = false;
      }
      const first = root.querySelector(".ad-invalid");
      if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
      showMessage(root.querySelector(".ad-savebar .message"), result.data.error || "Speichern fehlgeschlagen.", "error");
      root.querySelector(".ad-savebar").hidden = false;
      return;
    }
    data = result.data;
    const current = data.addons.find((a) => a.id === addon.id);
    draft = clone(current.config);
    saved = JSON.stringify(draft);
    remoteChanged = false;
    render(false);
    const note = root.querySelector(".ad-detail .message");
    showMessage(note, current.installed ? "Gespeichert – gilt sofort auf dem Server." : "Gespeichert – gilt, sobald die Erweiterung läuft.", "success");
  }

  function stateChip(addon) {
    return el("span", { class: `ad-state ad-state-${addon.state}`, text: addon.state_label });
  }

  function actions(addon) {
    const row = el("div", { class: "btn-row ad-actions" });
    const busy = ["install", "remove"].includes(addon.state);
    if (addon.official && (addon.state === "available" || addon.state === "failed")) {
      row.append(el("button", { type: "button", class: "btn", text: "Installieren", disabled: !data.plugin.supports && data.plugin.known,
        onclick: () => act(addon, "install") }));
    }
    if (addon.update) {
      row.append(el("button", { type: "button", class: "btn", text: `Aktualisieren auf ${addon.store_version}`,
        onclick: () => act(addon, "install") }));
    }
    if (addon.installed && (addon.state === "pending" || addon.state === "changed")) {
      row.append(el("button", { type: "button", class: "btn", text: "Freigeben",
        onclick: () => act(addon, "approve", { approved: true },
          addon.verified ? null : `${addon.name} kommt nicht aus dem MCConnect-Store. Freigeben heißt: Die Erweiterung darf mit ` +
            "MCConnect sprechen und auf deinem Server ihre Funktionen ausführen. Nur freigeben, wenn du ihr vertraust.") }));
    }
    if (addon.state === "active") {
      row.append(el("button", { type: "button", class: "btn btn-secondary", text: "Pausieren", onclick: () => act(addon, "enabled", { enabled: false }) }));
    }
    if (addon.state === "paused") {
      row.append(el("button", { type: "button", class: "btn", text: "Fortsetzen", onclick: () => act(addon, "enabled", { enabled: true }) }));
    }
    if (addon.installed && addon.approved && !addon.verified && !busy) {
      row.append(el("button", { type: "button", class: "btn btn-secondary", text: "Freigabe zurückziehen",
        onclick: () => act(addon, "approve", { approved: false }) }));
    }
    if ((addon.installed || addon.state === "install" || addon.state === "restart" || addon.state === "failed") && addon.state !== "remove") {
      row.append(el("button", { type: "button", class: "btn btn-danger", text: "Entfernen",
        onclick: () => act(addon, "remove", {}, `${addon.name} vom Server entfernen? Die Einstellungen bleiben hier gespeichert.`) }));
    }
    if (addon.official) {
      row.append(el("a", { class: "btn btn-secondary", href: `/download/addons/${addon.id}.jar`, text: "Jar herunterladen",
        title: "Für die Installation von Hand: in den plugins-Ordner legen und den Server neu starten" }));
    }
    return row;
  }

  function notices(addon) {
    const box = el("div", { class: "ad-notices" });
    const note = (kind, ...content) => box.append(el("div", { class: `ad-note ad-note-${kind}` }, ...content));
    if (!data.plugin.supports && data.plugin.known) {
      note("warn", "Dein MCDataLink kennt noch keine Erweiterungen. Im Reiter „Plugin“ gibt es Version 3.22+ (mit auto-update kommt sie beim nächsten Neustart von selbst).");
    }
    if (addon.state === "install") {
      note("info", data.plugin.online ? "MCDataLink lädt die Erweiterung gerade herunter …"
        : "Der Server ist gerade nicht verbunden. Die Installation startet, sobald er wieder online ist.");
    }
    if (addon.state === "remove") note("info", data.plugin.online ? "Wird entfernt …" : "Wird entfernt, sobald der Server wieder online ist.");
    if (addon.state === "restart") note("info", `Version ${addon.status_text || ""} liegt bereit und ist nach dem nächsten Server-Neustart aktiv.`);
    if (addon.state === "failed") note("error", el("b", { text: "Hat nicht geklappt: " }), addon.status_text || "unbekannter Fehler");
    if (addon.state === "pending") {
      note(addon.verified ? "info" : "warn", addon.verified
        ? "Diese Erweiterung ist eine offizielle Version aus dem Store. Gib sie frei, damit sie loslegt."
        : "Diese Erweiterung kommt nicht aus dem MCConnect-Store. Sie tut nichts und bekommt nichts von MCConnect, bis du sie freigibst.");
    }
    if (addon.state === "changed") {
      note("warn", "Die Datei der Erweiterung hat sich seit deiner Freigabe geändert (andere Prüfsumme). Sie ist deshalb gesperrt, bis du sie neu freigibst.");
    }
    if (addon.problems && addon.problems.length) {
      note("warn", el("b", { text: "Meldungen vom Server:" }), el("ul", {}, ...addon.problems.map((p) => el("li", { text: p }))));
    }
    if (remoteChanged) {
      note("info", "Die Einstellungen wurden gerade geändert (z. B. im Spiel). ",
        el("button", { type: "button", class: "ad-link", text: "Neu laden und meine Änderungen verwerfen",
          onclick: () => { draft = null; remoteChanged = false; render(true); } }));
    }
    return box;
  }

  function renderForm(addon, resetOpen) {
    const holder = root.querySelector(".ad-form");
    if (!holder) return;
    if (resetOpen) openRows.clear();
    if (!addon.settings.length) {
      holder.replaceChildren(el("p", { class: "hint", text: "Diese Erweiterung hat keine Einstellungen." }));
      return;
    }
    const sections = addon.settings.map((section, index) => {
      const node = el("section", { class: "ad-section" },
        el("h3", { text: section.title }), section.description ? el("p", { class: "hint", text: section.description }) : null,
        fieldsNode(section.fields, draft, ""));
      node.style.setProperty("--i", index);
      return node;
    });
    const bar = el("div", { class: "ad-savebar", hidden: !isDirty() },
      el("span", { class: "ad-savebar-text", text: "Ungespeicherte Änderungen" }),
      el("button", { type: "button", class: "btn btn-secondary", text: "Verwerfen",
        onclick: () => { draft = clone(addon.config); saved = JSON.stringify(draft); renderForm(addon, false); changed(); } }),
      el("button", { type: "button", class: "btn", text: "Speichern", onclick: () => save(addon) }),
      el("div", { class: "message" }));
    const reset = el("button", { type: "button", class: "ad-link ad-reset", text: "Alles auf die Standardwerte setzen",
      onclick: () => { draft = clone(addon.defaults); openRows.clear(); renderForm(addon, false); changed(); } });
    holder.replaceChildren(...sections, reset, bar);
  }

  function detail(addon) {
    if (draft === null) {
      draft = clone(addon.config);
      saved = JSON.stringify(draft);
    }
    const head = el("div", { class: "ad-detail-head" },
      el("div", { class: "ad-icon ad-icon-lg" }, itemIcon(addon.icon)),
      el("div", { class: "ad-detail-title" }, el("h3", { text: addon.name }), metaNode(addon)));
    const about = el("div", { class: "ad-about" },
      addon.description ? el("p", { text: addon.description }) : null,
      addon.features.length ? el("ul", { class: "ad-features" }, ...addon.features.map((f) => el("li", { text: f }))) : null,
      addon.commands.length ? el("div", { class: "ad-commands" }, el("b", { text: "Im Spiel" }),
        ...addon.commands.map((c) => el("div", {}, el("code", { text: c.usage || "/" + c.name }),
          c.description ? el("span", { class: "hint", text: " – " + c.description }) : null))) : null);
    const form = el("div", { class: "ad-form" });
    const editable = addon.state !== "available";
    const page = el("div", { class: "ad-detail" },
      el("button", { type: "button", class: "ad-back", text: "← Alle Erweiterungen", onclick: () => navigate(null) }),
      head, actions(addon), el("div", { class: "message" }), notices(addon), about,
      el("h3", { class: "ad-settings-title", text: "Einstellungen" }),
      editable ? form : el("p", { class: "hint", text: "Nach der Installation stellst du hier alles ein." }));
    root.replaceChildren(page, suggestions());
    if (editable) renderForm(addon, false);
  }

  function metaNode(addon) {
    const meta = [addon.category, addon.installed ? `Version ${addon.version}` : addon.store_version ? `Version ${addon.store_version}` : null,
      addon.size_kb ? `${addon.size_kb} KB` : null].filter(Boolean).join(" · ");
    return el("p", { class: "meta" }, meta, " ", stateChip(addon),
      addon.installed ? el("span", { class: `ad-pill ${addon.verified ? "ad-pill-ok" : "ad-pill-warn"}`,
        text: addon.verified ? "✓ offiziell, Prüfsumme stimmt" : "nicht aus dem Store",
        title: addon.verified ? "Die Datei auf dem Server ist genau eine Version aus dem MCConnect-Store." : addon.plugin || "" }) : null);
  }

  function suggestions() {
    return el("div", { hidden: true },
      el("datalist", { id: "ad-suggest-players" }, ...(data.players || []).map((name) => el("option", { value: name }))),
      el("datalist", { id: "ad-suggest-worlds" }, ...["world", "world_nether", "world_the_end"].map((w) => el("option", { value: w }))));
  }

  // ------------------------------------------------------------------ the store

  function tile(addon) {
    const primary = addon.state === "available" && addon.official ? "Installieren"
      : addon.state === "pending" || addon.state === "changed" ? "Freigeben" : "Einstellen";
    return el("article", { class: `ad-tile${addon.installed ? " ad-installed" : ""}` },
      el("button", { type: "button", class: "ad-tile-open", "aria-label": `${addon.name} öffnen`, onclick: () => navigate(addon.id) }),
      el("div", { class: "ad-tile-head" }, el("div", { class: "ad-icon" }, itemIcon(addon.icon)),
        el("div", {}, el("b", { text: addon.name }), el("span", { class: "ad-category", text: addon.category }))),
      el("p", { class: "ad-summary", text: addon.summary || addon.description }),
      el("div", { class: "ad-tile-foot" }, stateChip(addon), addon.update ? el("span", { class: "ad-pill ad-pill-ok", text: "Update" }) : null,
        el("span", { class: "ad-tile-go", text: primary + " →" })));
  }

  function store() {
    const installed = data.addons.filter((a) => a.state !== "available");
    const available = data.addons.filter((a) => a.state === "available");
    const parts = [];
    if (!data.plugin.supports && data.plugin.known) {
      parts.push(el("div", { class: "ad-note ad-note-warn" }, "Dein MCDataLink ist älter als 3.22 und kennt noch keine Erweiterungen. ",
        "Lade im Reiter „Plugin“ die neue Version – mit auto-update kommt sie beim nächsten Neustart von selbst."));
    } else if (!data.plugin.online) {
      parts.push(el("div", { class: "ad-note ad-note-info" }, "Der Server ist gerade nicht verbunden. Du kannst trotzdem installieren und einstellen – ",
        "es wird übertragen, sobald er online ist."));
    }
    if (installed.length) parts.push(el("h3", { class: "ad-group", text: "Auf deinem Server" }), el("div", { class: "ad-grid" }, ...installed.map(tile)));
    if (available.length) parts.push(el("h3", { class: "ad-group", text: installed.length ? "Mehr Erweiterungen" : "Store" }),
      el("div", { class: "ad-grid" }, ...available.map(tile)));
    if (!data.addons.length) parts.push(el("p", { class: "hint", text: "Im Store gibt es gerade keine Erweiterungen." }));
    parts.push(el("details", { class: "ad-dev" }, el("summary", { text: "Eigene Erweiterung entwickeln" }),
      el("p", { class: "hint" }, "Jedes Bukkit-Plugin kann eine MCConnect-Erweiterung sein: eine ", el("code", { text: "mcconnect-addon.json" }),
        " mit Id, Name und Einstellungen, ", el("code", { text: "depend: [MCDataLink]" }), " und in ", el("code", { text: "onEnable" }), " ",
        el("code", { text: "MCConnect.register(this, addon)" }), ". Sie erscheint dann hier, wartet auf deine Freigabe und bekommt ihre ",
        "Einstellungsseite automatisch.")));
    root.replaceChildren(...parts);
  }

  // ------------------------------------------------------------------ navigation, polling

  function render(resetOpen) {
    if (!data) return;
    const addon = openId && data.addons.find((a) => a.id === openId);
    if (openId && !addon) openId = null;
    if (addon) {
      const scrollY = window.scrollY;
      detail(addon);
      if (resetOpen) openRows.clear();
      window.scrollTo(0, scrollY);
    } else {
      store();
    }
  }

  function navigate(id) {
    if (isDirty() && !confirm("Ungespeicherte Änderungen verwerfen?")) return;
    openId = id;
    draft = null;
    remoteChanged = false;
    openRows.clear();
    history.replaceState(null, "", id ? `#addons/${id}` : "#addons");
    render(true);
    root.closest("[data-panel]").scrollIntoView({ block: "start" });
  }

  function fromHash() {
    const match = /^#addons(?:\/([a-z0-9_]+))?$/.exec(location.hash);
    if (!match) return false;
    if ((match[1] || null) !== openId) {
      openId = match[1] || null;
      draft = null;
      remoteChanged = false;
      render(true);
    }
    return true;
  }

  const panelVisible = () => !root.closest("[data-panel]").hidden;

  async function refresh() {
    let fresh;
    try {
      fresh = await getJson(api());
    } catch (e) {
      return;
    }
    const before = data;
    data = fresh;
    const addon = openId && data.addons.find((a) => a.id === openId);
    if (addon && draft !== null) {
      const old = before && before.addons.find((a) => a.id === openId);
      const remote = JSON.stringify(addon.config);
      if (old && JSON.stringify(old.config) !== remote) {
        if (isDirty()) {
          remoteChanged = true;
        } else {
          draft = clone(addon.config);
          saved = remote;
        }
      }
      // keep the form (open rows, focus, unsaved input) and only update what is around it
      const focused = document.activeElement && root.contains(document.activeElement);
      if (focused || isDirty()) {
        const page = root.querySelector(".ad-detail");
        page.querySelector(".ad-actions").replaceWith(actions(addon));
        page.querySelector(".ad-notices").replaceWith(notices(addon));
        page.querySelector(".ad-detail-head .meta").replaceWith(metaNode(addon));
        return;
      }
    }
    render(false);
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest('[data-tab="addons"]') && openId) navigate(null);
  });
  window.addEventListener("hashchange", fromHash);
  window.addEventListener("beforeunload", (event) => {
    if (isDirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  (async () => {
    await refresh();
    fromHash();
    setInterval(() => { if (!document.hidden && panelVisible()) refresh(); }, POLL_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && panelVisible()) refresh(); });
  })();
})();
