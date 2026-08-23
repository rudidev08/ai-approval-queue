/* Iris top bar — one component shared by the dashboard page
   (app/dashboard.html) and the actions page (services/actions/page.html).
   Each page carries only <header class="bar"></header> and
   <body data-bar="dashboard"|"actions">;
   this script fills the header: the wordmark with its status text, the two
   page tiles and the chevron, the "updated" stamp and the theme key, and the
   fold-out row — the webui and actual keys and the text-size keys.

   Styles are injected here so no page carries its own copy; colors come from
   each page's --bar* / --s1..--s3 / --brand tokens, and --bar-max sets the
   bar's content width (1240px dashboard, 980px actions). --bar-h is
   published for anything that sticks under the bar (the actions page's area
   headers).

   The pages drive the status text with barStatus("ok"|"warn"|"off", title?);
   it stays empty until the first call. The fold-out state, the theme and the
   text size stick in localStorage — per device and per origin, so each page
   on each device is tuned on its own. Outbound keys open in a new tab: in
   the home-screen apps iOS would otherwise replace the app with the page. */
"use strict";

(() => {
  /* kept by hand — the same mappings as LINKS in app/server.py */
  const LINKS = {
    actions: "https://mac-mini.your-tailnet.ts.net/",
    dashboard: "https://mac-mini.your-tailnet.ts.net:30655/",
    webui: "https://mac-mini.your-tailnet.ts.net:35422/",
    actual: "https://mac-mini.your-tailnet.ts.net:52737/",
  };

  /* Lucide icons (https://lucide.dev, ISC): official names, inner markup
     verbatim from lucide-static. Static strings only — never user data. */
  const ICONS = {
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/> <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "activity": '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "message-square": '<path d="M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z"/>',
    "dollar-sign": '<line x1="12" x2="12" y1="2" y2="22"/> <path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    "sun": '<circle cx="12" cy="12" r="4"/> <path d="M12 2v2"/> <path d="M12 20v2"/> <path d="m4.93 4.93 1.41 1.41"/> <path d="m17.66 17.66 1.41 1.41"/> <path d="M2 12h2"/> <path d="M20 12h2"/> <path d="m6.34 17.66-1.41 1.41"/> <path d="m19.07 4.93-1.41 1.41"/>',
    "moon": '<path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/>',
  };
  const icon = (name) =>
    `<svg class="svg-icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

  const css = `
.bar { position: sticky; top: 0; z-index: 40;
  padding-top: env(safe-area-inset-top);
  background: var(--bar); border-bottom: 1px solid var(--bar-edge); }
/* page errors: a strip pinned under the bar — the bar's stamp slot is too
   small to carry a full error message */
#page-error { color: var(--amber); font-size: 12px;
  background: color-mix(in srgb, var(--amber) 10%, var(--bar));
  border-top: 1px solid var(--bar-edge);
  padding: 6px max(1rem, calc((100% - var(--bar-max)) / 2 + 1rem)); }
.bar-inner { max-width: var(--bar-max); margin: 0 auto;
  padding: 0.55rem max(1rem, env(safe-area-inset-right))
           0.55rem max(1rem, env(safe-area-inset-left));
  display: flex; align-items: center; flex-wrap: wrap; gap: 0.55rem 1.5rem; }
.brand { position: relative; display: flex; align-items: baseline; gap: 1px;
  font-size: 18px; font-weight: 700; letter-spacing: 0.02em;
  color: var(--bar-ink); }
.brand b { color: var(--brand); font-weight: 400; }
.brand .w { letter-spacing: 0.06em; }
/* overall status riding the wordmark's closing bracket — OK green, WARN
   amber when anything on the page is bad, OFFLINE gray when the server
   stops answering; empty until the page's first status call. In flow (not
   absolute) so a raised text size can never slide it under the page tiles */
.sstat { position: relative; top: -3px; margin-left: 6px;
  font-family: var(--mono); font-size: 13px; font-weight: 700;
  letter-spacing: 0.06em; line-height: 1; white-space: nowrap;
  color: var(--green); }
.sstat.warn { color: var(--amber); }
.sstat.off { color: var(--bar-sub); }
.apps { display: flex; gap: 12px; margin: 0 auto; }
.bar-right { display: flex; align-items: center; gap: 0.9rem; }
.app { font: inherit; background: none; border: 0; cursor: pointer;
  text-decoration: none; display: flex; align-items: center; gap: 8px;
  color: var(--bar-sub); }
.app .tile { display: grid; place-items: center; }
.app .svg-icon { width: 15px; height: 15px; }
.bar-right .app { min-height: 30px; padding: 2px 11px; border-radius: 6px;
  border: 2px solid var(--bar-rule); }
@media (hover: hover) {
  .bar-right .app:hover { color: var(--bar-ink); background: var(--bar-fill); }
}
/* the "updated" stamp rides the bar's right edge; the bar has no faint
   token, so dim bar-sub toward the bar background */
#stamp { color: color-mix(in srgb, var(--bar-sub) 65%, var(--bar));
  font-size: 11px; letter-spacing: 0.02em; white-space: nowrap; }
#stamp.bad { color: #cf9967; }

/* page tiles: flat icon tiles; the nav shows both pages, the one you are on
   is lit */
.keybtn { display: flex; align-items: center; gap: 8px; min-width: 0;
  padding: 0; background: none; border: 0; cursor: pointer;
  font: inherit; text-decoration: none; }
.keybtn .tile { display: grid; place-items: center;
  width: 30px; height: 30px; border-radius: 8px;
  background: var(--bar-fill); border: 2px solid var(--bar-rule); }
.keybtn .tile .svg-icon { width: 17px; height: 17px; stroke-width: 2.2; }
.keybtn.c3 .svg-icon { color: var(--s3); }
.keybtn.c4 .svg-icon { color: var(--brand); }
.keybtn.active { cursor: default; }
.keybtn.active .tile { background: var(--bar-rule); border-color: var(--bar-sub); }

/* the chevron and the fold-out row it opens */
.more .tile { color: var(--bar-sub); transition: transform .15s; }
@media (hover: hover) { .more:hover .tile { color: var(--bar-ink); } }
/* the whole tile turns: an svg rotates around its top-left corner, so the
   icon would clip */
.bar.open .more .tile { transform: rotate(180deg); background: var(--bar-rule); }
.ext { display: none; border-top: 1px solid var(--bar-rule); }
.bar.open .ext { display: block; }
.ext-inner { max-width: var(--bar-max); margin: 0 auto;
  padding: 8px max(1rem, env(safe-area-inset-right))
           8px max(1rem, env(safe-area-inset-left));
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
/* outbound keys: the bar's bordered-button shape, one row height with the
   page tiles above them (30px, 34 and 38 at the bigger text sizes) */
.xbtn { display: flex; align-items: center; gap: 8px;
  min-height: 30px; padding: 2px 11px; border-radius: 6px;
  border: 2px solid var(--bar-rule); background: none;
  font: inherit; font-size: 13px; color: var(--bar-sub);
  text-decoration: none; cursor: pointer; white-space: nowrap;
  transition: transform .08s, color .15s,
    background-color .15s, border-color .15s; }
@media (hover: hover) {
  .xbtn:hover { color: var(--bar-ink); background: var(--bar-fill); }
}
.pi { display: grid; place-items: center; }
.pi .svg-icon { width: 14px; height: 14px; stroke-width: 2.2; }
.pi.cw .svg-icon { color: var(--s3); }
.pi.ca .svg-icon { color: var(--s1); }
/* the press dip the key buttons in this row give back on a tap */
button.xbtn:enabled:active { transform: scale(.92); }
/* the text-size keys ride the row's right edge: three A's, one per level;
   the chosen level is lit. The A's get line-height 1 in a fixed-height box
   so all three keys match the row's other keys instead of each A's line
   box setting its own height */
.fontctl { margin-left: auto; display: flex; gap: 8px; }
.fontctl .pi { font-weight: 700; line-height: 1; height: 18px; }
.fontctl .ta-s { font-size: 11px; }
.fontctl .ta-m { font-size: 14px; }
.fontctl .ta-l { font-size: 17px; }
.fontctl .xbtn.on { background: var(--bar-rule); color: var(--bar-ink); }
.fontctl .xbtn.on .pi { color: var(--bar-ink); }
html[data-text="m"] .fontctl .pi { height: 20px; }
html[data-text="l"] .fontctl .pi { height: 23px; }

/* the m and l text levels: the handcrafted bigger readings of the bar (the
   pages carry their own per-level rules) */
html[data-text="m"] .brand { font-size: 20px; }
html[data-text="l"] .brand { font-size: 22px; }
html[data-text="m"] .sstat { font-size: 15px; }
html[data-text="l"] .sstat { font-size: 17px; }
html[data-text="m"] #stamp { font-size: 12.5px; }
html[data-text="l"] #stamp { font-size: 14px; }
html[data-text="m"] #page-error { font-size: 13.5px; }
html[data-text="l"] #page-error { font-size: 15px; }
html[data-text="m"] .keybtn .tile { width: 34px; height: 34px; }
html[data-text="l"] .keybtn .tile { width: 38px; height: 38px; }
html[data-text="m"] .keybtn .tile .svg-icon { width: 19px; height: 19px; }
html[data-text="l"] .keybtn .tile .svg-icon { width: 21px; height: 21px; }
html[data-text="m"] .app .svg-icon { width: 17px; height: 17px; }
html[data-text="l"] .app .svg-icon { width: 19px; height: 19px; }
html[data-text="m"] .bar-right .app { min-height: 34px; }
html[data-text="l"] .bar-right .app { min-height: 38px; }
html[data-text="m"] .xbtn { font-size: 14.5px; min-height: 34px; }
html[data-text="l"] .xbtn { font-size: 16px; min-height: 38px; }
html[data-text="m"] .pi .svg-icon { width: 16px; height: 16px; }
html[data-text="l"] .pi .svg-icon { width: 18px; height: 18px; }

@media (max-width: 760px) { .bar-inner { gap: 1rem; } }`;

  document.head.append(Object.assign(document.createElement("style"),
                                     { textContent: css }));

  const bar = document.querySelector(".bar");
  const page = document.body.dataset.bar;
  const tile = (id, name, tint, label) =>
    `<a class="keybtn ${tint}${page === id ? " active" : ""}" id="app-${id}"`
    + `${page === id ? ' aria-current="page"' : ""} href="${LINKS[id]}"`
    + ` aria-label="${label || id}"><span class="tile">${icon(name)}</span></a>`;

  bar.innerHTML = `
  <div class="bar-inner">
    <div class="brand"><b>[</b><span class="w">iris</span><b>]</b><span class="sstat" id="sstat"></span></div>
    <nav class="apps" aria-label="Apps">
      ${tile("actions", "inbox", "c4")}
      ${tile("dashboard", "activity", "c3")}
      <button class="keybtn more" id="more" aria-label="More apps and text size"
        aria-expanded="false"><span class="tile">${icon("chevron-down")}</span></button>
    </nav>
    <span class="devrow" id="devrow"></span>
    <div class="bar-right">
      <span id="stamp"></span>
      <button class="app" id="theme" aria-label="Toggle theme">
        <span class="tile"></span></button>
    </div>
  </div>
  <div class="ext" id="ext">
    <div class="ext-inner">
      <a class="xbtn" id="app-webui" href="${LINKS.webui}" target="_blank"
        rel="noopener"><span class="pi cw">${icon("message-square")}</span>webui</a>
      <a class="xbtn" id="app-actual" href="${LINKS.actual}" target="_blank"
        rel="noopener"><span class="pi ca">${icon("dollar-sign")}</span>actual</a>
      <span class="fontctl" role="group" aria-label="Text size">
        <button class="xbtn" id="text-s" aria-label="Small text"
          title="small text"><span class="pi ta-s">A</span></button>
        <button class="xbtn" id="text-m" aria-label="Medium text"
          title="medium text"><span class="pi ta-m">A</span></button>
        <button class="xbtn" id="text-l" aria-label="Large text"
          title="large text"><span class="pi ta-l">A</span></button>
      </span>
    </div>
  </div>
  <div id="page-error" hidden></div>`;

  /* the bar's height, for things that stick right under it (the actions
     page's area headers use top: var(--bar-h, 48px)) — it grows with the
     safe-area inset, the text size and the open fold-out row. The
     ResizeObserver covers resizes; the fold-out and text-size handlers
     below also call this directly because their changes don't always
     trigger it. The exact fractional height, never offsetHeight: that one
     rounds up, and the sub-pixel rest shows as a seam of scrolled content
     between the bar and a pinned header */
  const setBarH = () => document.documentElement.style.setProperty(
    "--bar-h", bar.getBoundingClientRect().height + "px");
  new ResizeObserver(setBarH).observe(bar);
  setBarH();

  /* theme: OS by default, toggle wins both ways; the button shows the mode a
     tap switches to (moon in light mode, sun in dark) */
  const themeBtn = bar.querySelector("#theme");
  const forced = new URLSearchParams(location.search).get("theme");
  const saved = forced || localStorage.getItem("iris-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  const current = () => document.documentElement.getAttribute("data-theme")
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const paintTheme = () => {
    themeBtn.querySelector(".tile").innerHTML =
      icon(current() === "dark" ? "sun" : "moon");
  };
  paintTheme();
  themeBtn.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("iris-theme", next);
    paintTheme();
  });

  /* the fold-out row; the choice sticks like the panel folds */
  const more = bar.querySelector("#more");
  if (localStorage.getItem("iris-ext") === "1") {
    bar.classList.add("open");
    more.setAttribute("aria-expanded", "true");
  }
  more.addEventListener("click", () => {
    const open = bar.classList.toggle("open");
    more.setAttribute("aria-expanded", String(open));
    localStorage.setItem("iris-ext", open ? "1" : "0");
    setBarH();
  });

  /* text size: three levels. s is the pages' own size; m and l are
     handcrafted bigger readings — the bar and each page carry per-level
     rules keyed off data-text on the root element. No zoom: iOS Safari
     magnifies a zoomed body without narrowing it, so zoomed content
     spilled past the screen edge and scrolled sideways. The choice sticks
     in localStorage, per device and per origin */
  const TEXT_KEY = "iris-text", LEVELS = ["s", "m", "l"];
  localStorage.removeItem("iris-font");   /* the old zoom keys' store */
  const textGet = () =>
    LEVELS.includes(localStorage.getItem(TEXT_KEY))
      ? localStorage.getItem(TEXT_KEY) : "s";
  const textApply = () => {
    document.documentElement.dataset.text = textGet();
    LEVELS.forEach((lv) => bar.querySelector("#text-" + lv)
      .classList.toggle("on", lv === textGet()));
    setBarH();
    /* pages listen for this to re-check size-dependent layouts */
    document.dispatchEvent(new CustomEvent("iris-text-size"));
  };
  LEVELS.forEach((lv) => bar.querySelector("#text-" + lv)
    .addEventListener("click", () => {
      localStorage.setItem(TEXT_KEY, lv);
      textApply();
    }));
  textApply();

  /* the wordmark's status text: empty until the page's first call */
  window.barStatus = (state, title) => {
    const s = bar.querySelector("#sstat");
    s.textContent = { ok: "OK", warn: "WARN", off: "OFFLINE" }[state] || "";
    s.className = "sstat" + (state === "off" ? " off"
                           : state === "warn" ? " warn" : "");
    if (title) s.title = title;
    else s.removeAttribute("title");
  };
})();
