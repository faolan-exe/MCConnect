// Daily play time of the last 30 days on the player page. Plain SVG, no library.
// Data (#activity-data): [{date: iso, hours: number or null, text}] (null = no snapshot that day).
(() => {
  const days = JSON.parse(document.getElementById("activity-data").textContent);
  const root = document.getElementById("activity-chart");
  const SVG = "http://www.w3.org/2000/svg";
  const dateLabel = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.`;
  const de = (v, digits) => v.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });

  function el(name, attrs, parent) {
    const node = document.createElementNS(SVG, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    parent.append(node);
    return node;
  }

  function render() {
    root.replaceChildren();
    const width = Math.max(root.clientWidth, 280);
    const height = 200;
    const max = Math.max(...days.map((d) => d.hours || 0), 0.5);
    const step = max <= 2 ? 0.5 : max <= 5 ? 1 : max <= 10 ? 2 : 5;
    const top = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
    const pad = { top: 10, right: 4, bottom: 26, left: 40 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const band = plotW / days.length;
    const barW = Math.min(24, Math.max(3, band - 2));
    const y = (v) => pad.top + plotH - (v / top) * plotH;

    const svg = el("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": "Spielzeit pro Tag in den letzten 30 Tagen" }, root);
    ticks.forEach((t, i) => {
      el("line", { x1: pad.left, x2: width - pad.right, y1: y(t), y2: y(t), class: i === 0 ? "cmp-axis" : "cmp-grid" }, svg);
      el("text", { x: pad.left - 8, y: y(t) + 4, "text-anchor": "end", class: "cmp-tick" }, svg)
        .textContent = `${de(t, step < 1 ? 1 : 0)} h`;
    });

    const tip = document.createElement("div");
    tip.className = "cmp-tip";
    tip.hidden = true;
    root.append(tip);

    days.forEach((day, i) => {
      const x = pad.left + i * band + (band - barW) / 2;
      if ((days.length - 1 - i) % 5 === 0) {
        el("text", { x: x + barW / 2, y: height - 8, "text-anchor": "middle", class: "cmp-tick" }, svg)
          .textContent = dateLabel(day.date);
      }
      // hit area over the whole column, bigger than the bar
      const hit = el("rect", { x: pad.left + i * band, y: pad.top, width: band, height: plotH, class: "pi-bar-hit",
        tabindex: 0, "aria-label": `${dateLabel(day.date)}: ${day.text}` }, svg);
      if (day.hours) {
        const h = Math.max(2, plotH - (y(day.hours) - pad.top));
        const r = Math.min(4, barW / 2, h);
        // rounded top, square at the baseline
        const x0 = x, x1 = x + barW, yb = pad.top + plotH, yt = yb - h;
        el("path", { class: "pi-bar", d: `M${x0},${yb}V${yt + r}Q${x0},${yt} ${x0 + r},${yt}H${x1 - r}Q${x1},${yt} ${x1},${yt + r}V${yb}Z` }, svg);
      } else {
        el("rect", { x, y: pad.top + plotH - 2, width: barW, height: 2, class: day.hours === 0 ? "pi-bar" : "pi-nodata" }, svg);
      }
      const show = () => {
        tip.replaceChildren();
        const date = document.createElement("div");
        date.className = "cmp-tip-date";
        date.textContent = dateLabel(day.date) + day.date.slice(0, 4);
        const value = document.createElement("strong");
        value.textContent = day.hours === null ? "keine Daten" : day.hours === 0 ? "nicht online" : day.text;
        tip.append(date, value);
        tip.hidden = false;
        const left = pad.left + (i + 0.5) * band + 10;
        tip.style.left = `${left + tip.offsetWidth > width ? left - tip.offsetWidth - 20 : left}px`;
      };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", () => (tip.hidden = true));
      hit.addEventListener("blur", () => (tip.hidden = true));
    });
  }

  // only redraw when the width really changed (other scripts fire resize events too)
  let timer;
  let lastWidth = root.clientWidth;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (root.clientWidth === lastWidth) return;
      lastWidth = root.clientWidth;
      render();
    }, 150);
  });
  render();
})();
