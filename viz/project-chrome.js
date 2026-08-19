/* ─────────────────────────────────────────────────────────────────────────────
   project-chrome.js — back link, project name, light/dark toggle.
   CANONICAL SOURCE: portfolio/shared/project-chrome.js
   Vendored copies in watch-timegrapher/static/ and aic/viz/ are written by
   `npm run sync:projects` in the portfolio. Do not edit the copies.

   Load it synchronously in <head> so the theme lands before first paint, as a
   script tag with src="project-chrome.js". It needs no configuration: the bar
   carries a back link and a theme toggle, and nothing page-specific.

   NOTE: no literal closing script tag appears anywhere in this file, and none
   should be added. The AIC page inlines this source *into* a script element,
   and such a sequence -- even inside a comment -- would terminate that element
   early and dump the rest of the file onto the page as text.

   It applies the stored theme immediately, then injects the bar once the body
   exists. Projects that paint their own colours (canvases, Chart.js) listen for
   the `themechange` event on window, or read window.ProjectChrome.theme().
   ────────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  var STORAGE_KEY = "theme";
  var root = document.documentElement;

  function stored() {
    try {
      var t = localStorage.getItem(STORAGE_KEY);
      return t === "dark" || t === "light" ? t : null;
    } catch (e) {
      return null; // Privacy mode: fall through to the media query.
    }
  }

  function preferred() {
    return window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  /* The two mechanisms in play across the site: the Astro shell and its React
     islands toggle a `.dark` class; the AIC page keys off `data-theme`. Writing
     both is what lets a toggle in any one of them be understood by all three,
     and costs nothing. */
  function apply(theme) {
    root.setAttribute("data-theme", theme);
    root.classList.toggle("dark", theme === "dark");
  }

  var current = stored() || preferred();
  apply(current); // Before paint — this file is parsed in <head>.

  function setTheme(theme, persist) {
    if (theme !== "dark" && theme !== "light") return;
    current = theme;
    apply(theme);
    if (persist !== false) {
      try {
        localStorage.setItem(STORAGE_KEY, theme);
      } catch (e) {
        /* Nothing to do — the choice just won't survive the page. */
      }
    }
    window.dispatchEvent(
      new CustomEvent("themechange", { detail: { theme: theme } })
    );
  }

  /* Absolute by default so a standalone deploy of either project still has a
     route back to the site. When the page is already being served *from* the
     portfolio it lives under /projects/, and a same-site path is used instead —
     which keeps the link honest on localhost and on Firebase preview channels
     rather than bouncing a developer to production. */
  function backHref() {
    return location.protocol !== "file:" &&
      location.pathname.indexOf("/projects/") === 0
      ? "/projects"
      : "https://ernan.dev/projects";
  }

  var ARROW =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M19 12H5M12 19l-7-7 7-7"/></svg>';

  var MOON =
    '<svg class="pc-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>';

  var SUN =
    '<svg class="pc-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41' +
    'M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';

  function mount() {
    if (document.querySelector(".pc-bar")) return; // Idempotent.

    var bar = document.createElement("header");
    bar.className = "pc-bar";
    bar.innerHTML =
      '<div class="pc-left">' +
      '<a class="pc-back" href="' +
      backHref() +
      '">' +
      ARROW +
      '<span class="pc-back-label">Projects</span></a>' +
      "</div>" +
      '<div class="pc-right">' +
      '<button class="pc-toggle" type="button" aria-label="Toggle light and dark mode">' +
      MOON +
      SUN +
      "</button>" +
      "</div>";

    bar.querySelector(".pc-toggle").addEventListener("click", function () {
      setTheme(current === "dark" ? "light" : "dark");
    });

    document.body.insertBefore(bar, document.body.firstChild);
    root.classList.add("pc-has-bar"); // Drives the body's top offset.
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  // Another tab (or the site shell in another document) changed the choice.
  // Don't re-persist what we were just told.
  window.addEventListener("storage", function (e) {
    if (e.key !== STORAGE_KEY) return;
    setTheme(e.newValue === "dark" || e.newValue === "light"
      ? e.newValue
      : preferred(), false);
  });

  // Follow the OS only while the visitor has expressed no preference.
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onSystemChange = function (e) {
      if (!stored()) setTheme(e.matches ? "dark" : "light", false);
    };
    if (mq.addEventListener) mq.addEventListener("change", onSystemChange);
    else if (mq.addListener) mq.addListener(onSystemChange);
  }

  window.ProjectChrome = {
    theme: function () {
      return current;
    },
    setTheme: setTheme,
    /* Sugar for the common case: run the callback now with the current theme,
       then again on every change. Canvas-painting projects want exactly this,
       because they must draw once at startup too. */
    onChange: function (fn) {
      fn(current);
      window.addEventListener("themechange", function (e) {
        fn(e.detail.theme);
      });
    },
  };
})();
