/* Iris dashboard page — shared helpers. All data comes live from /api/snapshot,
   served with this page by server.py. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const sum = (a) => a.reduce((t, n) => t + n, 0);

/* the five macOS servers read as one "mac:" group in the tool catalogue */
const SERVER_FAMILY = {
  macos_calendar: "mac", macos_notes: "mac", macos_contacts: "mac", macos_messages: "mac",
  reminders: "mac",
};

/* Lucide icons (https://lucide.dev, ISC): official names, inner markup
   verbatim from lucide-static. Static strings only — never user data.
   Only the icons the page body uses; the bar's icons live in bar.js. */
function icon(name) {
  const inner = {
    "play": '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
    "terminal": '<path d="M12 19h8"/> <path d="m4 17 6-6-6-6"/>',
    "power": '<path d="M12 2v10"/> <path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
    "layout-grid": '<rect width="7" height="7" x="3" y="3" rx="1"/> <rect width="7" height="7" x="14" y="3" rx="1"/> <rect width="7" height="7" x="14" y="14" rx="1"/> <rect width="7" height="7" x="3" y="14" rx="1"/>',
    "activity": '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    "clock": '<circle cx="12" cy="12" r="10"/> <path d="M12 6v6l4 2"/>',
    "plug": '<path d="M12 22v-5"/> <path d="M15 8V2"/> <path d="M17 8a1 1 0 0 1 1 1v4a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1z"/> <path d="M9 8V2"/>',
    "cpu": '<path d="M12 20v2"/> <path d="M12 2v2"/> <path d="M17 20v2"/> <path d="M17 2v2"/> <path d="M2 12h2"/> <path d="M2 17h2"/> <path d="M2 7h2"/> <path d="M20 12h2"/> <path d="M20 17h2"/> <path d="M20 7h2"/> <path d="M7 20v2"/> <path d="M7 2v2"/> <rect x="4" y="4" width="16" height="16" rx="2"/> <rect x="8" y="8" width="8" height="8" rx="1"/>',
    "wrench": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/>',
    "message-square": '<path d="M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/> <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  }[name];
  return `<svg class="svg-icon" viewBox="0 0 24 24" aria-hidden="true">${inner}</svg>`;
}
