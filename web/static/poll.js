// Polls a JSON endpoint while the tab is visible (replaces the server-sent events, which kept a
// worker thread busy for every open tab). onData gets the parsed answer.
function pollJson(url, intervalMs, onData) {
  let timer = null;
  async function tick() {
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      if (response.ok) onData(await response.json());
    } catch (e) {}  // offline for a moment: the next tick tries again
  }
  function start() {
    if (timer) return;
    tick();
    timer = setInterval(tick, intervalMs);
  }
  function stop() {
    clearInterval(timer);
    timer = null;
  }
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  if (!document.hidden) start();
}

// Keeps the parts of a page marked with data-live="<name>" up to date: loads the page itself every
// intervalMs (only while the tab is visible) and replaces those parts when they changed. Everything
// else (forms, opened <details>, scroll position) stays as it is; use event delegation for buttons
// inside the live parts.
function liveParts(intervalMs) {
  if (!document.querySelector("[data-live]")) return;
  let timer = null;
  async function tick() {
    try {
      const response = await fetch(location.href, { headers: { Accept: "text/html" } });
      if (!response.ok) return;
      const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
      document.querySelectorAll("[data-live]").forEach((part) => {
        const next = fresh.querySelector(`[data-live="${part.dataset.live}"]`);
        if (next && next.innerHTML !== part.innerHTML) part.innerHTML = next.innerHTML;
      });
    } catch (e) {}
  }
  function start() {
    if (!timer) timer = setInterval(tick, intervalMs);
  }
  function stop() {
    clearInterval(timer);
    timer = null;
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else { tick(); start(); }
  });
  if (!document.hidden) start();
}
