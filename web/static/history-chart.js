// History line chart of the comparison page (/vergleich). Plain SVG, no library.
// Data (#history-data): {dates: [iso], players: [{name, color}], since: iso,
//   metrics: [{key, label, unit, group, values: [[per player: value per date or null]]}]}
// Values are the gain since the start of the loaded range; the chart rebases them
// to the start of the visible range.
(() => {
  const data = JSON.parse(document.getElementById("history-data").textContent);
  const root = document.getElementById("history-chart");
  const select = document.getElementById("history-metric");
  const sub = document.getElementById("history-sub");
  const table = document.getElementById("history-table");
  const rangeButtons = [...document.querySelectorAll("#history [data-days]")];
  const SVG = "http://www.w3.org/2000/svg";
  const de = (value, digits = 0) =>
    value.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });

  let metric = data.metrics[0];
  let days = 30;

  // fill the metric select, grouped
  let optgroup = null;
  for (const m of data.metrics) {
    if (!optgroup || optgroup.label !== m.group) {
      optgroup = document.createElement("optgroup");
      optgroup.label = m.group;
      select.append(optgroup);
    }
    optgroup.append(new Option(m.label, m.key));
  }

  function format(value, unit) {
    if (value == null) return "–";
    if (unit === "time") return value < 1 ? `${de(value * 60)} Min.` : `${de(value, value < 10 ? 1 : 0)} Std.`;
    if (unit === "distance") return value >= 1000 ? `${de(value / 1000, 1)} km` : `${de(value)} m`;
    if (unit === "damage") return `${de(value)} ♥`;
    return de(value);
  }
  function axisFormat(value, unit, max) {
    if (unit === "time") return `${de(value, max < 3 ? 1 : 0)} h`;
    if (unit === "distance") return max >= 2000 ? `${de(value / 1000, max < 10000 ? 1 : 0)} km` : `${de(value)} m`;
    return de(value);
  }
  const dateLabel = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.`;

  function niceTicks(max, count = 4) {
    if (max <= 0) return [0, 1];
    const raw = max / count;
    const power = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((f) => f * power).find((s) => s >= raw);
    const ticks = [];
    for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
    if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
    return ticks;
  }

  function el(name, attrs, parent) {
    const node = document.createElementNS(SVG, name);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    if (parent) parent.append(node);
    return node;
  }

  function visibleSeries() {
    const start = data.dates.length - 1 - days;
    const dates = data.dates.slice(start);
    const series = metric.values.map((values) => {
      const slice = values.slice(start);
      const base = slice.find((v) => v != null);
      return slice.map((v) => (v == null ? null : Math.max(0, v - base)));
    });
    return { dates, series };
  }

  function renderTable(dates, series) {
    table.replaceChildren();
    const head = table.createTHead().insertRow();
    const th = (text, row) => { const cell = document.createElement("th"); cell.textContent = text; row.append(cell); };
    th("Datum", head);
    data.players.forEach((p) => th(p.name, head));
    const body = table.createTBody();
    for (let i = dates.length - 1; i >= 0; i--) {
      if (series.every((s) => s[i] == null)) continue;
      const row = body.insertRow();
      row.insertCell().textContent = dateLabel(dates[i]) + dates[i].slice(0, 4);
      series.forEach((s) => { const cell = row.insertCell(); cell.className = "num"; cell.textContent = format(s[i], metric.unit); });
    }
  }

  function render() {
    const { dates, series } = visibleSeries();
    const unitText = { time: "Stunden", distance: "Blöcke", damage: "Herzen" }[metric.unit] || "Anzahl";
    sub.textContent = `Zuwachs ${metric.label} in den letzten ${days} Tagen (${unitText}). ` +
      `Aufgezeichnet seit ${dateLabel(data.since)}${data.since.slice(0, 4)}.`;
    renderTable(dates, series);
    root.replaceChildren();

    const hasData = series.some((s) => s.some((v) => v != null && v > 0));
    if (!hasData) {
      const empty = document.createElement("p");
      empty.className = "st-empty";
      empty.textContent = "In diesem Zeitraum gibt es dafür noch keinen Zuwachs.";
      root.append(empty);
      return;
    }

    const width = Math.max(root.clientWidth, 300);
    const height = width < 560 ? 240 : 300;
    const max = Math.max(...series.flat().filter((v) => v != null));
    const ticks = niceTicks(max);
    const top = ticks[ticks.length - 1];
    const labels = ticks.map((t) => axisFormat(t, metric.unit, top));
    const left = Math.max(...labels.map((l) => l.length)) * 7.5 + 12;
    const pad = { top: 14, right: 16, bottom: 28, left };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const x = (i) => pad.left + (dates.length === 1 ? plotW : (i / (dates.length - 1)) * plotW);
    const y = (v) => pad.top + plotH - (v / top) * plotH;

    const svg = el("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": `${metric.label}: Verlauf der letzten ${days} Tage` }, root);

    // grid and y axis
    ticks.forEach((t, i) => {
      el("line", { x1: pad.left, x2: width - pad.right, y1: y(t), y2: y(t),
        class: i === 0 ? "cmp-axis" : "cmp-grid" }, svg);
      el("text", { x: pad.left - 8, y: y(t) + 4, "text-anchor": "end", class: "cmp-tick" }, svg).textContent = labels[i];
    });
    // x axis: about 6 date labels
    const every = Math.max(1, Math.round(dates.length / 6));
    dates.forEach((d, i) => {
      if ((dates.length - 1 - i) % every !== 0) return;
      el("text", { x: x(i), y: height - 8, "text-anchor": i === 0 ? "start" : "middle", class: "cmp-tick" }, svg)
        .textContent = dateLabel(d);
    });

    // lines, end dots
    series.forEach((values, p) => {
      let path = "";
      let open = false;
      values.forEach((v, i) => {
        if (v == null) { open = false; return; }
        path += `${open ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
        open = true;
      });
      el("path", { d: path, class: "cmp-line", stroke: data.players[p].color }, svg);
      const last = values.length - 1;
      if (values[last] != null) {
        el("circle", { cx: x(last), cy: y(values[last]), r: 4, fill: data.players[p].color, class: "cmp-dot" }, svg);
      }
    });

    // crosshair + tooltip
    const cross = el("line", { y1: pad.top, y2: pad.top + plotH, class: "cmp-cross", visibility: "hidden" }, svg);
    const markers = series.map((_, p) => el("circle", { r: 4, fill: data.players[p].color, class: "cmp-dot",
      visibility: "hidden" }, svg));
    const hit = el("rect", { x: pad.left, y: pad.top, width: plotW, height: plotH, fill: "transparent",
      tabindex: 0, class: "cmp-hit", "aria-label": "Werte ansehen (Pfeiltasten)" }, svg);
    const tip = document.createElement("div");
    tip.className = "cmp-tip";
    tip.hidden = true;
    root.append(tip);

    let current = null;
    function show(i) {
      current = Math.max(0, Math.min(dates.length - 1, i));
      cross.setAttribute("x1", x(current));
      cross.setAttribute("x2", x(current));
      cross.setAttribute("visibility", "visible");
      tip.replaceChildren();
      const title = document.createElement("div");
      title.className = "cmp-tip-date";
      title.textContent = dateLabel(dates[current]) + dates[current].slice(0, 4);
      tip.append(title);
      series.forEach((values, p) => {
        const v = values[current];
        markers[p].setAttribute("visibility", v == null ? "hidden" : "visible");
        if (v != null) { markers[p].setAttribute("cx", x(current)); markers[p].setAttribute("cy", y(v)); }
        const row = document.createElement("div");
        row.className = "cmp-tip-row";
        row.style.setProperty("--c", data.players[p].color);
        const strong = document.createElement("strong");
        strong.textContent = format(v, metric.unit);
        const name = document.createElement("span");
        name.textContent = data.players[p].name;
        row.append(strong, name);
        tip.append(row);
      });
      tip.hidden = false;
      const tipX = x(current) + 14;
      tip.style.left = `${tipX + tip.offsetWidth > width ? x(current) - tip.offsetWidth - 14 : tipX}px`;
      tip.style.top = `${pad.top}px`;
    }
    function hide() {
      current = null;
      tip.hidden = true;
      cross.setAttribute("visibility", "hidden");
      markers.forEach((m) => m.setAttribute("visibility", "hidden"));
    }
    hit.addEventListener("pointermove", (event) => {
      const box = svg.getBoundingClientRect();
      const px = event.clientX - box.left;
      show(Math.round(((px - pad.left) / plotW) * (dates.length - 1)));
    });
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => show(dates.length - 1));
    hit.addEventListener("blur", hide);
    hit.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        show((current ?? dates.length - 1) + (event.key === "ArrowLeft" ? -1 : 1));
      }
    });
  }

  select.addEventListener("change", () => {
    metric = data.metrics.find((m) => m.key === select.value);
    render();
  });
  rangeButtons.forEach((button) => button.addEventListener("click", () => {
    days = Number(button.dataset.days);
    rangeButtons.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    render();
  }));
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 150);
  });
  render();
})();
