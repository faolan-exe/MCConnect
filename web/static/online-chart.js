// Players online over time on /server-statistik. Plain SVG, no library.
// Data (#online-data): {day: [{t: label, v: count}], week: [...]}
(() => {
  const data = JSON.parse(document.getElementById("online-data").textContent);
  const root = document.getElementById("online-chart");
  const buttons = [...document.querySelectorAll("#online [data-range]")];
  const SVG = "http://www.w3.org/2000/svg";
  let range = "day";

  function el(name, attrs, parent) {
    const node = document.createElementNS(SVG, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    parent.append(node);
    return node;
  }

  function render() {
    root.replaceChildren();
    const points = data[range];
    const width = Math.max(root.clientWidth, 280);
    const height = width < 560 ? 200 : 240;
    const max = Math.max(...points.map((p) => p.v), 1);
    const step = max <= 5 ? 1 : max <= 10 ? 2 : max <= 25 ? 5 : 10;
    const top = Math.ceil(max / step) * step;
    const pad = { top: 12, right: 12, bottom: 26, left: 32 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const x = (i) => pad.left + (i / Math.max(1, points.length - 1)) * plotW;
    const y = (v) => pad.top + plotH - (v / top) * plotH;

    const svg = el("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": range === "day" ? "Spieler online in den letzten 24 Stunden" : "Spieler online in den letzten 7 Tagen" }, root);
    for (let v = 0; v <= top; v += step) {
      el("line", { x1: pad.left, x2: width - pad.right, y1: y(v), y2: y(v), class: v === 0 ? "cmp-axis" : "cmp-grid" }, svg);
      el("text", { x: pad.left - 8, y: y(v) + 4, "text-anchor": "end", class: "cmp-tick" }, svg).textContent = v;
    }
    // x labels: every 3 hours (6 on small screens) for 24 h, every day (every other day) for 7 days
    const every = (range === "day" ? 12 : 24) * (width < 560 ? 2 : 1);
    points.forEach((p, i) => {
      if ((points.length - 1 - i) % every) return;
      const text = range === "day" ? p.t.slice(-5) : p.t.slice(0, 2) + " " + p.t.slice(3, 9);
      el("text", { x: x(i), y: height - 8, "text-anchor": "middle", class: "cmp-tick" }, svg).textContent = text;
    });

    // line through the counts of the time buckets, with a light area below
    let line = "";
    points.forEach((p, i) => { line += `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`; });
    el("path", { d: `${line}L${x(points.length - 1)},${y(0)}L${x(0)},${y(0)}Z`, class: "ss-area" }, svg);
    el("path", { d: line, class: "cmp-line ss-line" }, svg);

    const cross = el("line", { y1: pad.top, y2: pad.top + plotH, class: "cmp-cross", visibility: "hidden" }, svg);
    const dot = el("circle", { r: 4, class: "cmp-dot ss-dot", visibility: "hidden" }, svg);
    const hit = el("rect", { x: pad.left, y: pad.top, width: plotW, height: plotH, fill: "transparent", tabindex: 0,
      class: "cmp-hit", "aria-label": "Werte ansehen (Pfeiltasten)" }, svg);
    const tip = document.createElement("div");
    tip.className = "cmp-tip";
    tip.hidden = true;
    root.append(tip);

    let current = null;
    function show(i) {
      current = Math.max(0, Math.min(points.length - 1, i));
      const p = points[current];
      cross.setAttribute("x1", x(current));
      cross.setAttribute("x2", x(current));
      cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(current));
      dot.setAttribute("cy", y(p.v));
      dot.setAttribute("visibility", "visible");
      tip.replaceChildren();
      const title = document.createElement("div");
      title.className = "cmp-tip-date";
      title.textContent = p.t + " Uhr";
      const value = document.createElement("strong");
      value.textContent = p.v === 1 ? "1 Spieler" : `${p.v} Spieler`;
      tip.append(title, value);
      tip.hidden = false;
      const left = x(current) + 14;
      tip.style.left = `${left + tip.offsetWidth > width ? x(current) - tip.offsetWidth - 14 : left}px`;
      tip.style.top = `${pad.top}px`;
    }
    function hide() {
      tip.hidden = true;
      cross.setAttribute("visibility", "hidden");
      dot.setAttribute("visibility", "hidden");
    }
    hit.addEventListener("pointermove", (event) => {
      const box = svg.getBoundingClientRect();
      show(Math.round(((event.clientX - box.left - pad.left) / plotW) * (points.length - 1)));
    });
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => show(points.length - 1));
    hit.addEventListener("blur", hide);
    hit.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      show((current ?? points.length - 1) + (event.key === "ArrowLeft" ? -1 : 1));
    });
  }

  buttons.forEach((button) => button.addEventListener("click", () => {
    range = button.dataset.range;
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    render();
  }));
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
