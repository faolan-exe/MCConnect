// Single series line chart (server health). Plain SVG, no library.
// <div data-line-chart="<id of a JSON script>" aria-label="...">
// JSON: {unit: " MB", max: number or null (top of the y axis), points: [{t: label, v: number or null}]}
// Gaps (null) break the line, e.g. while the server was offline.
function mccLineChart(root) {
  const data = JSON.parse(document.getElementById(root.dataset.lineChart).textContent);
  const SVG = "http://www.w3.org/2000/svg";
  const de = (v, digits = 0) => v.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });

  function el(name, attrs, parent) {
    const node = document.createElementNS(SVG, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    parent.append(node);
    return node;
  }

  function render() {
    root.replaceChildren();
    const points = data.points;
    const width = Math.max(root.clientWidth, 260);
    const height = 180;
    const values = points.map((p) => p.v).filter((v) => v != null);
    const top = data.max || Math.max(...values, 1);
    const step = top / 4;
    const digits = step % 1 ? 1 : 0;
    const labels = [0, 1, 2, 3, 4].map((i) => de(step * i, digits));
    const pad = { top: 10, right: 10, bottom: 24, left: Math.max(...labels.map((l) => l.length)) * 7.5 + 12 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const x = (i) => pad.left + (i / Math.max(1, points.length - 1)) * plotW;
    const y = (v) => pad.top + plotH - (Math.min(v, top) / top) * plotH;

    const svg = el("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": root.getAttribute("aria-label") || "" }, root);
    labels.forEach((label, i) => {
      el("line", { x1: pad.left, x2: width - pad.right, y1: y(step * i), y2: y(step * i), class: i ? "cmp-grid" : "cmp-axis" }, svg);
      el("text", { x: pad.left - 8, y: y(step * i) + 4, "text-anchor": "end", class: "cmp-tick" }, svg).textContent = label;
    });
    const every = Math.max(1, Math.round(points.length / 5));
    points.forEach((p, i) => {
      if ((points.length - 1 - i) % every) return;
      el("text", { x: x(i), y: height - 6, "text-anchor": "middle", class: "cmp-tick" }, svg).textContent = p.t.slice(-5);
    });

    // one path per run without gaps
    let runs = [], run = [];
    points.forEach((p, i) => {
      if (p.v == null) { if (run.length) runs.push(run); run = []; return; }
      run.push(i);
    });
    if (run.length) runs.push(run);
    for (const r of runs) {
      const line = r.map((i, n) => `${n ? "L" : "M"}${x(i).toFixed(1)},${y(points[i].v).toFixed(1)}`).join("");
      el("path", { d: `${line}L${x(r[r.length - 1])},${y(0)}L${x(r[0])},${y(0)}Z`, class: "ss-area" }, svg);
      el("path", { d: line, class: "cmp-line ss-line" }, svg);
    }

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
      dot.setAttribute("visibility", p.v == null ? "hidden" : "visible");
      if (p.v != null) { dot.setAttribute("cx", x(current)); dot.setAttribute("cy", y(p.v)); }
      tip.replaceChildren();
      const title = document.createElement("div");
      title.className = "cmp-tip-date";
      title.textContent = p.t + " Uhr";
      const value = document.createElement("strong");
      value.textContent = p.v == null ? "keine Daten (offline)" : de(p.v, p.v % 1 ? 1 : 0) + (data.unit || "");
      tip.append(title, value);
      tip.hidden = false;
      const left = x(current) + 14;
      tip.style.left = `${left + tip.offsetWidth > width ? x(current) - tip.offsetWidth - 14 : left}px`;
      tip.style.top = `${pad.top}px`;
    }
    const hide = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); };
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
}
document.querySelectorAll("[data-line-chart]").forEach(mccLineChart);
