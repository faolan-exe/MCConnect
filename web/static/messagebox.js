/*
 * Message boxes (login, logout question): messagebox_info / messagebox_warning / messagebox_error.
 *
 * Same calls as the messageBoxLib used before, built on the browser's <dialog>: Escape and a click next to the box
 * cancel, the focus stays in the box and goes back afterwards, screen readers announce it. Looks and colors come from
 * css/messagebox.css (light and dark mode). Text only: header and detail are never parsed as HTML.
 *
 *   messagebox_info(header, detail, {confirmationButton: "Ja", cancelButton: "Nein"},
 *                   {confirmationCallback, confirmationParams, cancelCallback, cancelParams})
 *
 * Every call also returns a Promise: true (confirmed) or false (cancelled, closed).
 */
(function () {
  "use strict";

  const LABELS = { info: "Hinweis", warning: "Achtung", error: "Fehler" };
  let open = null;  // {dialog, resolve, before} of the box on screen: a new one replaces it

  // right away, not on its "close" event (that comes later): no second box meanwhile, and the old one must not
  // take the focus back from the new one
  function replace() {
    if (!open) return null;
    const old = open;
    open = null;
    old.dialog.dataset.replaced = "1";
    old.dialog.close();
    old.dialog.remove();
    old.resolve(false);
    return old.before;  // the focus goes back where it was before the first box
  }

  function call(callback, params) {
    if (typeof callback === "function") callback(...(Array.isArray(params) ? params : []));
  }

  function show(kind, header, detail, buttons, options) {
    buttons = buttons || {};
    options = options || {};
    const replacedFocus = replace();

    const dialog = document.createElement("dialog");
    dialog.className = "mbx mbx-" + kind;
    dialog.setAttribute("aria-labelledby", "mbx-title");
    dialog.setAttribute("aria-describedby", "mbx-text");
    if (kind === "error") dialog.setAttribute("role", "alertdialog");

    const head = document.createElement("div");
    head.className = "mbx-head";
    const label = document.createElement("span");
    label.className = "mbx-kind";
    label.textContent = LABELS[kind];
    const title = document.createElement("h2");
    title.id = "mbx-title";
    title.textContent = header == null ? "" : String(header);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "mbx-close";
    close.setAttribute("aria-label", "Schließen");
    close.textContent = "×";
    head.append(label, title, close);

    const text = document.createElement("p");
    text.id = "mbx-text";
    text.className = "mbx-text";
    text.textContent = detail == null ? "" : String(detail);

    const actions = document.createElement("div");
    actions.className = "mbx-actions";
    let confirm = null;
    if (buttons.cancelButton) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "mbx-btn mbx-btn-secondary";
      cancel.textContent = buttons.cancelButton;
      cancel.addEventListener("click", () => dialog.close("cancel"));
      actions.append(cancel);
    }
    if (buttons.confirmationButton) {
      confirm = document.createElement("button");
      confirm.type = "button";
      confirm.className = "mbx-btn";
      confirm.textContent = buttons.confirmationButton;
      confirm.addEventListener("click", () => dialog.close("confirm"));
      actions.append(confirm);
    }

    dialog.append(head, text);
    if (actions.childElementCount) dialog.append(actions);
    close.addEventListener("click", () => dialog.close("cancel"));
    // a click on the backdrop (outside the box) cancels
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) dialog.close("cancel");
    });

    const before = replacedFocus || document.activeElement;
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        if (dialog.dataset.replaced) return;  // done in replace()
        const result = dialog.returnValue;  // "confirm", "cancel", or "" after Escape
        dialog.remove();
        open = null;
        if (before && typeof before.focus === "function" && document.contains(before)) before.focus();
        if (result === "confirm") {
          call(options.confirmationCallback, options.confirmationParams);
          resolve(true);
        } else {
          call(options.cancelCallback, options.cancelParams);
          resolve(false);
        }
      });
      document.body.append(dialog);
      open = { dialog, resolve, before };
      dialog.showModal();
      (confirm || close).focus();
    });
  }

  window.messagebox_info = (header, detail, buttons, options) => show("info", header, detail, buttons, options);
  window.messagebox_warning = (header, detail, buttons, options) => show("warning", header, detail, buttons, options);
  window.messagebox_error = (header, detail, buttons, options) => show("error", header, detail, buttons, options);
})();
