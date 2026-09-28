// Declarative event handlers instead of inline on...= attributes, which the content security policy blocks.
//
//   <button data-on-click="copyConfig" data-args='[3]'>   calls window.copyConfig(3, button, event)
//   <form data-on-submit="updateServer" data-args='[3]'>  calls window.updateServer(3, form, event)
//   <a data-on-click="go" data-args='["/users"]'>          go(url) is defined here
//
// Supported events: click, change, submit, input. The functions have to be global (function declarations
// of classic scripts are).
function go(url) {
  window.location.href = url;
}

(() => {
  for (const type of ["click", "change", "submit", "input"]) {
    document.addEventListener(type, (event) => {
      const element = event.target.closest(`[data-on-${type}]`);
      if (!element) return;
      const handler = window[element.getAttribute(`data-on-${type}`)];
      if (typeof handler !== "function") return;
      handler(...JSON.parse(element.dataset.args || "[]"), element, event);
    });
  }
})();

// <select data-on-change="submitForm"> sends its form (e.g. the metric of /teams)
function submitForm(element) {
  element.form.submit();
}
