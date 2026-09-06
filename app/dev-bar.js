/* Dev design-proposal tiles — one component shared by the dashboard page and the
   actions page (and every devN.html generated from them), so the bar behaves
   the same on every page and mock page.

   Tapping the wordmark cycles the mode: [iris] -> [dev] -> [iris], and on a
   page with data-demo on its body [iris] -> [dev] -> [demo] -> [iris].

   dev: tiles 1-5 replace the app icons in the middle, linking to
   dev1.html..dev5.html next to the current page. A tile lights up when its
   file exists on the server (HEAD probe on each toggle), stays blank when it
   does not. A dev page opens in this mode instead of iris and fills its own
   tile — so moving between proposals keeps the 1-5 bar instead of resetting
   to the app icons; on a dev page the wordmark cycles [dev] -> [demo] -> [dev].

   demo: the page shows canned data from its own service (the actions page
   sends its /api calls to /demo/api). The bar only names the mode on the
   root element (data-mode) and reloads the page on the way in and out, so
   the page's script reads the mode once at start.

   The mode sticks in localStorage per app. The page provides: .bar >
   .bar-inner with .brand (holding .w), .apps and an empty <span
   class="devrow" id="devrow">, plus the --bar* / --s1..--s3 / --brand /
   --tool tokens. Styling below is injected so no page carries its own
   copy. */

(() => {
  const css = `
.brand { cursor: pointer; user-select: none; }
.devrow { display: none; gap: 6px; margin: 0 auto; }
.bar.dev .devrow { display: flex; }
.bar.dev .apps { display: none; }
/* the tiles can still outgrow a very narrow bar; the row then wraps below
   instead of clipping */
.bar.dev .bar-inner { flex-wrap: wrap; }
.devtile { display: grid; place-items: center; width: 26px; height: 26px;
  border-radius: 6px; border: 1px solid var(--bar-rule);
  font-size: 12px; font-weight: 700; color: transparent;
  text-decoration: none; pointer-events: none; }
.devtile.on { pointer-events: auto; }
.devtile.d1.on { color: var(--s1); border-color: var(--s1); }
.devtile.d2.on { color: var(--s2); border-color: var(--s2); }
.devtile.d3.on { color: var(--s3); border-color: var(--s3); }
.devtile.d4.on { color: var(--brand); border-color: var(--brand); }
.devtile.d5.on { color: var(--tool); border-color: var(--tool); }
/* the proposal you are on: filled, not a link */
.devtile.here { pointer-events: none; }
.devtile.on.here { color: var(--bar); }
.devtile.d1.here { background: var(--s1); }
.devtile.d2.here { background: var(--s2); }
.devtile.d3.here { background: var(--s3); }
.devtile.d4.here { background: var(--brand); }
.devtile.d5.here { background: var(--tool); }
@media (max-width: 760px) {
  .devrow { gap: 4px; }
  .devtile { width: 24px; height: 24px; }
}`;
  document.head.append(Object.assign(document.createElement("style"),
                                     { textContent: css }));

  const bar = document.querySelector(".bar");
  const row = document.getElementById("devrow");
  const word = document.querySelector(".brand .w");
  const here = Number((location.pathname.match(/\/dev([1-5])\.html$/) || [])[1]);

  for (let n = 1; n <= 5; n++) {
    const a = document.createElement("a");
    a.className = "devtile d" + n + (n === here ? " on here" : "");
    a.textContent = n;
    a.href = "dev" + n + ".html";
    row.append(a);
  }

  function probe() {
    for (const a of row.children) {
      if (a.classList.contains("here")) continue;
      fetch(a.getAttribute("href"), { method: "HEAD", cache: "no-store" })
        .then((r) => a.classList.toggle("on", r.ok))
        .catch(() => a.classList.remove("on"));
    }
  }

  const MODES = "demo" in document.body.dataset
    ? ["iris", "dev", "demo"] : ["iris", "dev"];
  const stored = localStorage.getItem("iris-mode");
  let mode = MODES.includes(stored) ? stored : "iris";
  /* a dev page opens in dev instead of iris; demo stays demo */
  if (here && mode === "iris") {
    mode = "dev";
    localStorage.setItem("iris-mode", "dev");
  }

  function paint() {
    document.documentElement.dataset.mode = mode;
    bar.classList.toggle("dev", mode === "dev");
    word.textContent = mode;
    if (mode === "dev") probe();
  }
  paint();

  document.querySelector(".brand").addEventListener("click", () => {
    const next = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
    localStorage.setItem("iris-mode", next);
    /* demo swaps the page's data source, and the page reads its mode once
       at start — so entering or leaving it is a reload */
    if (next === "demo" || mode === "demo") { location.reload(); return; }
    mode = next;
    paint();
  });
})();
