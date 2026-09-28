// Moderation pages (/users/...): everything is saved with fetch, the page is never reloaded. After a change
// only the parts marked data-live="<name>" (tables, lists, the tabs with their counters) are replaced with
// the current version of the page, so open sections, typed text and the scroll position stay as they are.
// Needs postJson and showMessage from the base template.
//
//   <button data-mod-post="/api/..." data-id="3" [data-body='{"accept": true}'] [data-confirm="Sure?"]>
//   <form data-mod-form="/api/..." [data-options]>   fields as JSON (data-options: "options" split by line)
//   <form data-settings-form>                         fields to /api/mod/settings (checkboxes as true/false)
//   <button data-setting="access_mode" data-value="code" aria-pressed="false">  one option of a setting

async function modRefresh() {
  try {
    const response = await fetch(location.pathname, { headers: { Accept: "text/html" } });
    if (!response.ok) return;
    const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
    document.querySelectorAll("[data-live]").forEach((part) => {
      const next = fresh.querySelector(`[data-live="${part.dataset.live}"]`);
      if (next && next.innerHTML !== part.innerHTML) part.innerHTML = next.innerHTML;
    });
    document.dispatchEvent(new Event("mod:refreshed"));
  } catch (e) {}
}

// keeps the live parts current while the tab is visible (e.g. new reports on the overview)
function modLive(intervalMs) {
  let timer = null;
  const start = () => { if (!timer) timer = setInterval(modRefresh, intervalMs); };
  const stop = () => { clearInterval(timer); timer = null; };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else { modRefresh(); start(); }
  });
  if (!document.hidden) start();
}

(() => {
  // a short note next to the element that was used ("Gespeichert.")
  function note(element, text, kind) {
    let box = element.closest("form")?.querySelector('[data-role="message"]') || element.nextElementSibling;
    if (!box || !box.classList.contains("mcc-message")) {
      box = document.createElement("div");
      element.after(box);
    }
    showMessage(box, text, kind);
    if (kind === "success") setTimeout(() => {
      if (box.textContent !== text) return;
      box.textContent = "";
      box.className = "mcc-message";
    }, 4000);
  }

  function formBody(form) {
    const body = Object.fromEntries(new FormData(form));
    form.querySelectorAll('input[type="checkbox"][name]').forEach((box) => (body[box.name] = box.checked));
    if ("options" in form.dataset) body.options = (body.options || "").split("\n").map((o) => o.trim()).filter(Boolean);
    return body;
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-mod-post], [data-setting]");
    if (!button || button.disabled) return;
    if (button.dataset.confirm && !confirm(button.dataset.confirm)) return;
    button.disabled = true;
    try {
      if (button.dataset.setting) {
        const result = await postJson("/api/mod/settings", { [button.dataset.setting]: button.dataset.value });
        if (!result.ok) return note(button.parentElement, result.data.error || "Speichern fehlgeschlagen.", "error");
        button.parentElement.querySelectorAll(`[data-setting="${button.dataset.setting}"]`)
          .forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
        note(button.parentElement, "Gespeichert.", "success");
      } else {
        const body = Object.assign({ id: Number(button.dataset.id) }, JSON.parse(button.dataset.body || "{}"));
        const result = await postJson(button.dataset.modPost, body);
        if (!result.ok) return alert(result.data.error || "Das hat nicht geklappt.");
        await modRefresh();
      }
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-mod-form], [data-settings-form]");
    if (!form) return;
    event.preventDefault();
    const url = form.dataset.modForm || "/api/mod/settings";
    const submit = form.querySelector('[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      const result = await postJson(url, formBody(form));
      if (!result.ok) return note(form, result.data.error || "Das hat nicht geklappt.", "error");
      if (form.dataset.modForm) {
        form.reset();
        form.querySelectorAll("select").forEach((select) => select.dispatchEvent(new Event("change")));
      }
      note(form, result.data.code ? `Code ${result.data.code} angelegt.` : "Gespeichert.", "success");
      await modRefresh();
    } finally {
      if (submit) submit.disabled = false;
    }
  });
})();
