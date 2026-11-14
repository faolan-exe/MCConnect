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

// Chips that jump to sections of the page (<nav class="md-nav"> with "#..." links) mark the section in view: the last one
// whose top passed below the header, or the last one at the end of the page.
document.addEventListener("DOMContentLoaded", () => {
  for (const nav of document.querySelectorAll("nav.md-nav")) {
    const links = [...nav.querySelectorAll('a[href^="#"]')];
    const sections = links.map((a) => document.getElementById(a.getAttribute("href").slice(1))).filter(Boolean);
    if (sections.length < 2) continue;
    const mark = () => {
      const atEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
      let current = sections[0];
      for (const section of sections) if (section.getBoundingClientRect().top <= 140) current = section;
      if (atEnd) current = sections[sections.length - 1];
      for (const a of links) {
        if (a.getAttribute("href") === "#" + current.id) a.setAttribute("aria-current", "location");
        else a.removeAttribute("aria-current");
      }
    };
    let queued = false;
    window.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; mark(); });
    }, { passive: true });
    mark();
  }
});
