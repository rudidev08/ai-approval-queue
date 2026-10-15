/* Assistant top bar — one component for both pages (actions.html, overview.html).
   Each page carries only <header class="bar"></header> and
   <body data-bar="overview"|"actions">, and loads shared.js first;
   this script fills the header: the wordmark with its status text, the two
   page tiles, on the actions page the chevron, the "updated" stamp, the
   text-size key and the theme key, plus the error strip under the bar.
   The chevron publishes its open state as the class bar-open on the root
   element: the actions page shows its area cards only while it is open.

   Styles are injected here so no page carries its own copy; colors come from
   theme.css, and each page's --bar-max sets the bar's content width. The
   bar scrolls with the page.

   The pages report each poll with barUpdate({checkedAt, failed, pageError,
   status, title}): checkedAt is the last good fetch (ms), status
   "ok"|"warn"|"pause". The bar shows "updated <age>" — amber when the last
   fetch failed or the page has an error — the error text in the strip, and
   the status word: OFFLINE when the fetch failed, empty until the first
   good fetch. It repaints the age every 10 s on its own. PAUSE stands in
   for OK while the pause file holds the assistant's model runs (llm_pause).
   The chevron state, the theme and the text size stick in localStorage on
   this device, shared by every page. */
"use strict";

(() => {
  /* the two pages, both on this server */
  const LINKS = { actions: "/", overview: "/overview" };

  const svg = (name) => icon(name).outerHTML;

  const css = `
.bar { padding-top: env(safe-area-inset-top);
  background: var(--bar); border-bottom: 1px solid var(--bar-edge); }
/* page errors: a strip under the bar — the bar's stamp slot is too
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
/* the status word rides the wordmark's closing bracket; in flow (not
   absolute) so a raised text size can never slide it under the page tiles */
.sstat { position: relative; top: -3px; margin-left: 6px;
  font-family: var(--mono); font-size: 13px; font-weight: 700;
  letter-spacing: 0.06em; line-height: 1; white-space: nowrap;
  color: var(--green); }
.sstat.warn { color: var(--amber); }
.sstat.pause { color: var(--brand); }
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

/* page tiles: flat icon tiles; the nav shows the actions and overview
   pages, the one you are on is lit */
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

/* the chevron: shows and hides the actions page's area cards */
.more .tile { color: var(--bar-sub); transition: transform .15s; }
@media (hover: hover) { .more:hover .tile { color: var(--bar-ink); } }
/* the whole tile turns: an svg rotates around its top-left corner, so the
   icon would clip */
html.bar-open .more .tile { transform: rotate(180deg); background: var(--bar-rule); }
/* the text-size key: an A beside three stacked dots, lit from the bottom
   up to the current level; a tap moves to the next level, and from the
   largest back to the smallest. It shrinks on the press */
#text-size { gap: 6px; font-size: 13px; font-weight: 700; transition: transform .08s; }
#text-size:active { transform: scale(.92); }
.dots { display: flex; flex-direction: column-reverse; gap: 2px; }
.dots i { width: 4px; height: 4px; border-radius: 50%;
  background: var(--bar-rule); }
.dots i.on { background: var(--bar-ink); }

/* the m and l text levels */
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
html[data-text="m"] #text-size { font-size: 14.5px; }
html[data-text="l"] #text-size { font-size: 16px; }

@media (max-width: 760px) { .bar-inner { gap: 1rem; } }`;

  document.head.append(Object.assign(document.createElement("style"),
                                     { textContent: css }));

  const bar = document.querySelector(".bar");
  const page = document.body.dataset.bar;
  const tile = (id, name, tint, label) =>
    `<a class="keybtn ${tint}${page === id ? " active" : ""}" id="app-${id}"`
    + `${page === id ? ' aria-current="page"' : ""} href="${LINKS[id]}"`
    + ` aria-label="${label || id}"><span class="tile">${svg(name)}</span></a>`;

  bar.innerHTML = `
  <div class="bar-inner">
    <div class="brand"><b>[</b><span class="w">assistant</span><b>]</b><span class="sstat" id="sstat"></span></div>
    <nav class="apps" aria-label="Apps">
      ${tile("actions", "inbox", "c4")}
      ${tile("overview", "activity", "c3")}
      ${page === "actions" ? `<button class="keybtn more" id="more" aria-label="Area cards"
        aria-expanded="false"><span class="tile">${svg("chevron-down")}</span></button>` : ""}
    </nav>
    <span class="devrow" id="devrow"></span>
    <div class="bar-right">
      <span id="stamp"></span>
      <button class="app" id="text-size">A<span class="dots"
        aria-hidden="true"><i></i><i></i><i></i></span></button>
      <button class="app" id="theme" aria-label="Toggle theme">
        <span class="tile"></span></button>
    </div>
  </div>
  <div id="page-error" hidden></div>`;

  /* theme: OS by default, toggle wins both ways; the button shows the mode a
     tap switches to (moon in light mode, sun in dark) */
  const themeBtn = bar.querySelector("#theme");
  const forced = new URLSearchParams(location.search).get("theme");
  const saved = forced || localStorage.getItem("assistant-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  const current = () => document.documentElement.getAttribute("data-theme")
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const paintTheme = () => {
    themeBtn.querySelector(".tile").innerHTML =
      svg(current() === "dark" ? "sun" : "moon");
  };
  paintTheme();
  themeBtn.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("assistant-theme", next);
    paintTheme();
  });

  const more = bar.querySelector("#more");
  if (more) {
    const root = document.documentElement;
    if (localStorage.getItem("assistant-ext") === "1") {
      root.classList.add("bar-open");
      more.setAttribute("aria-expanded", "true");
    }
    more.addEventListener("click", () => {
      const open = root.classList.toggle("bar-open");
      more.setAttribute("aria-expanded", String(open));
      localStorage.setItem("assistant-ext", open ? "1" : "0");
    });
  }

  /* text size: three levels. s is the pages' own size; m and l are
     handcrafted bigger readings — the bar and each page carry per-level
     rules keyed off data-text on the root element. No zoom: iOS Safari
     magnifies a zoomed body without narrowing it, so zoomed content
     spilled past the screen edge and scrolled sideways. */
  const TEXT_KEY = "assistant-text", LEVELS = ["s", "m", "l"];
  const NAMES = { s: "small", m: "medium", l: "large" };
  const textBtn = bar.querySelector("#text-size");
  const textGet = () =>
    LEVELS.includes(localStorage.getItem(TEXT_KEY))
      ? localStorage.getItem(TEXT_KEY) : "s";
  const textApply = () => {
    const lv = textGet();
    document.documentElement.dataset.text = lv;
    textBtn.querySelectorAll(".dots i").forEach((dot, i) =>
      dot.classList.toggle("on", i <= LEVELS.indexOf(lv)));
    textBtn.title = "text size: " + NAMES[lv];
    textBtn.setAttribute("aria-label", "Text size, " + NAMES[lv]);
  };
  textBtn.addEventListener("click", () => {
    localStorage.setItem(TEXT_KEY,
      LEVELS[(LEVELS.indexOf(textGet()) + 1) % LEVELS.length]);
    textApply();
  });
  textApply();

  /* the stamp, the error strip and the wordmark's status text, from the
     page's last report */
  let last = null;
  const paint = () => {
    const stamp = bar.querySelector("#stamp");
    stamp.textContent = last && last.checkedAt
      ? "updated " + ago(Date.now() - last.checkedAt) : "connecting…";
    stamp.classList.toggle("bad", !!last && (last.failed || !!last.pageError));
    const strip = bar.querySelector("#page-error");
    strip.textContent = last ? last.pageError || "" : "";
    strip.hidden = !strip.textContent;
    const state = !last || !last.checkedAt ? ""
      : last.failed ? "off" : last.pageError ? "warn" : last.status;
    const s = bar.querySelector("#sstat");
    s.textContent = { ok: "OK", warn: "WARN", pause: "PAUSE", off: "OFFLINE" }[state] || "";
    s.className = "sstat" + (state && state !== "ok" ? " " + state : "");
    const title = state === "off" || !last ? "" : last.title;
    if (title) s.title = title;
    else s.removeAttribute("title");
  };
  window.barUpdate = (report) => { last = report; paint(); };
  paint();
  setInterval(paint, 10000);
})();
