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
