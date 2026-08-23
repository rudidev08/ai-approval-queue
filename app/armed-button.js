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

   armedButtonBusy(id) reports an in-flight id, for page code that sets a
   key's disabled state itself while the request runs. */
"use strict";

const AB_ARM_MS = 5000;
const AB_STATE = new Map();   // id -> {mode, node, timer?, title?, wasDisabled?}

function armedButtonBusy(id) {
  const st = AB_STATE.get(id);
  return !!st && st.mode === "inflight";
}

function abDisarm(id) {
  const st = AB_STATE.get(id);
  if (!st || st.mode !== "armed") return;
  clearTimeout(st.timer);
  AB_STATE.delete(id);
  st.node.classList.remove("armed");
  if (st.title == null) st.node.removeAttribute("title");
  else st.node.title = st.title;
}

function armedButton(btn, id, fire) {
  const st = AB_STATE.get(id);
  if (st) {                        // a rebuilt node resumes its state
    st.node = btn;
    if (st.mode === "inflight") {
      st.wasDisabled = btn.disabled;
      btn.disabled = true;
      btn.classList.add("inflight");
    } else {
      st.title = btn.getAttribute("title");
      btn.classList.add("armed");
      btn.title = "tap again to confirm";
    }
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();           // a key tap never toggles its row
    const cur = AB_STATE.get(id);
    if (cur && cur.mode === "inflight") return;
    if (cur) {                     // armed — this is the confirming tap
      abDisarm(id);
      const run = { mode: "inflight", node: btn, wasDisabled: false };
      AB_STATE.set(id, run);
      btn.disabled = true;
      btn.classList.add("inflight");
      Promise.resolve(fire()).finally(() => {
        AB_STATE.delete(id);
        run.node.classList.remove("inflight");
        run.node.disabled = run.wasDisabled;
      });
      return;
    }
    const arm = { mode: "armed", node: btn,
                  title: btn.getAttribute("title"),
                  timer: setTimeout(() => abDisarm(id), AB_ARM_MS) };
    AB_STATE.set(id, arm);
    btn.classList.add("armed");
    btn.title = "tap again to confirm";
  });
}
