// Design früh setzen (vor dem ersten Zeichnen), damit nichts aufblitzt.
// data-theme: classic | dsm | dark | warm – „classic“ folgt hell/dunkel des Geräts (Klasse .dark).
(function () {
  "use strict";
  var KEY = "ze_theme", THEMES = ["classic", "dsm", "dark", "warm"];
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function apply(t) {
    if (THEMES.indexOf(t) < 0) t = "classic";
    var d = document.documentElement;
    d.setAttribute("data-theme", t);
    d.classList.toggle("dark", t === "dark" || (t === "classic" && !!(mq && mq.matches)));
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", getComputedStyle(d).getPropertyValue("--accent").trim() || "#0f766e");
    try { localStorage.setItem(KEY, t); } catch (e) { /* privat */ }
    return t;
  }
  var saved = "classic";
  try { saved = localStorage.getItem(KEY) || "classic"; } catch (e) { /* privat */ }
  apply(saved);
  if (mq && mq.addEventListener) mq.addEventListener("change", function () { apply(document.documentElement.getAttribute("data-theme")); });
  window.zeTheme = apply;
})();
