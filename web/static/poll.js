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

// Replaces the parts of the page marked with data-live="<name>" with their current version (the page is
// loaded again in the background). Everything else (forms, opened <details>, scroll position) stays as it
// is, so saving something never needs a page reload. Use event delegation for buttons inside live parts.
async function refreshLiveParts() {
  try {
    const response = await fetch(location.pathname + location.search, { headers: { Accept: "text/html" } });
    if (!response.ok) return;
    const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
    document.querySelectorAll("[data-live]").forEach((part) => {
      const next = fresh.querySelector(`[data-live="${part.dataset.live}"]`);
      if (next && next.innerHTML !== part.innerHTML) part.innerHTML = next.innerHTML;
    });
    document.dispatchEvent(new Event("live:refreshed"));
  } catch (e) {}
}

// refreshLiveParts every intervalMs while the tab is visible
function liveParts(intervalMs) {
  if (!document.querySelector("[data-live]")) return;
  let timer = null;
  const tick = refreshLiveParts;
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
