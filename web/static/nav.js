// The header menu of the server pages (header.html) and the main domain (base.html): the phone panel and the
// dropdown groups (open on hover and by click; one at a time; closed by Escape or a click outside).
function toggleNavMenu(button) {
  const open = document.getElementById("nav-bar").classList.toggle("menueToggle");
  button.setAttribute("aria-expanded", String(open));
  document.documentElement.classList.toggle("mh-open", open);
}
// groups open on hover and by click (touch screens, keyboard); one at a time, closed by Escape or a click outside
function closeNavGroups(except) {
  for (const button of document.querySelectorAll(".mcc-nav .nav-group > button[aria-expanded='true']")) {
    if (button !== except) button.setAttribute("aria-expanded", "false");
  }
}
function toggleNavGroup(button) {
  closeNavGroups(button);
  button.setAttribute("aria-expanded", String(button.getAttribute("aria-expanded") !== "true"));
}
document.addEventListener("click", (event) => {
  if (!event.target.closest(".mcc-nav .nav-group")) closeNavGroups();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  closeNavGroups();
  const button = document.querySelector(".mh-toggle");
  if (button && button.getAttribute("aria-expanded") === "true") toggleNavMenu(button);
});
