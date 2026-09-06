/* Armed-button: the shared two-tap confirm behavior for keys on the
   dashboard page and the actions page. Styles: tap-feedback.css (armed ring,
   in-flight blue pulse).

   armedButton(btn, id, fire) wires one button:
   - first tap arms the key: the ring pulse only — label and colors stay
     as they are; 5 s without the confirming tap disarms it
   - the confirming tap runs fire() and holds the key disabled with the
     blue in-flight pulse until fire()'s promise settles

   State lives in a registry under `id`, not on the node: the pages rebuild
   their DOM on every render, and the fresh node resumes its armed or
   in-flight state when the render wires it again under the same id. On
   settle the key returns to the disabled state the render gave the current
   node, so a render-disabled key stays disabled.

   Keys that fire the same call share one id (the audit's doctor-fix key,
   one per doctor finding): every key on the page wired under the id arms,
   flies and settles together, and the confirming tap can land on any of
   them. The group is found on the page by the id each wire stamps on its
   node, so a node a render dropped is simply not there any more.

   armedButtonBusy(id) reports an in-flight id, for page code that sets a
   key's disabled state itself while the request runs. */
"use strict";

const AB_ARM_MS = 5000;
const AB_STATE = new Map();     // id -> {mode, timer?}
const AB_TITLE = new WeakMap(); // node -> its own title, while armed
const AB_WAS = new WeakMap();   // node -> its disabled state, while in flight

function armedButtonBusy(id) {
  const st = AB_STATE.get(id);
  return !!st && st.mode === "inflight";
}

function abGroup(id) {
  return document.querySelectorAll(`[data-armed-id="${CSS.escape(id)}"]`);
}

function abArm(node) {
  AB_TITLE.set(node, node.getAttribute("title"));
  node.classList.add("armed");
  node.title = "tap again to confirm";
}

function abFly(node) {
  AB_WAS.set(node, node.disabled);
  node.disabled = true;
  node.classList.add("inflight");
}

function abDisarm(id) {
  const st = AB_STATE.get(id);
  if (!st || st.mode !== "armed") return;
  clearTimeout(st.timer);
  AB_STATE.delete(id);
  for (const node of abGroup(id)) {
    node.classList.remove("armed");
    const title = AB_TITLE.get(node);
    if (title == null) node.removeAttribute("title");
    else node.title = title;
  }
}

function armedButton(btn, id, fire) {
  btn.dataset.armedId = id;
  const st = AB_STATE.get(id);
  if (st) {                        // a rebuilt node resumes its state
    if (st.mode === "inflight") abFly(btn);
    else abArm(btn);
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();           // a key tap never toggles its row
    const cur = AB_STATE.get(id);
    if (cur && cur.mode === "inflight") return;
    if (cur) {                     // armed — this is the confirming tap
      abDisarm(id);
      AB_STATE.set(id, { mode: "inflight" });
      for (const node of abGroup(id)) abFly(node);
      Promise.resolve(fire()).finally(() => {
        AB_STATE.delete(id);
        for (const node of abGroup(id)) {
          node.classList.remove("inflight");
          node.disabled = AB_WAS.get(node);
        }
      });
      return;
    }
    AB_STATE.set(id, { mode: "armed",
                       timer: setTimeout(() => abDisarm(id), AB_ARM_MS) });
    for (const node of abGroup(id)) abArm(node);
  });
}
