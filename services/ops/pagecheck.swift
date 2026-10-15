// Renders a page in the Mac's own WebKit and reports what it drew — the
// engine behind Safari and every iOS browser. test_page.py compiles this
// with swiftc and points it at a test server in the page's demo mode.
//
//   pagecheck <url> <seconds> [demo]
//
// demo stores the bar's assistant-mode=demo before any page script runs, so the
// page sends every /api call to /demo/api (canned data, writes are no-ops).
// After <seconds> it prints one JSON object: errors (uncaught exceptions and
// unhandled rejections), page_error (the page's own render-error strip),
// text (the whole document's text) and tracks (every .track element's text).
// Storage is non-persistent: nothing of the real browser profile is read
// or written.
import Foundation
import WebKit

let args = CommandLine.arguments
let url = URL(string: args[1])!
let wait = Double(args[2]) ?? 4
let demo = args.count > 3 && args[3] == "demo"

let cfg = WKWebViewConfiguration()
cfg.websiteDataStore = WKWebsiteDataStore.nonPersistent()
cfg.userContentController.addUserScript(WKUserScript(source: """
  window.__errs = [];
  window.addEventListener('error', e => window.__errs.push(e.message + ' @' + e.lineno));
  window.addEventListener('unhandledrejection',
    e => window.__errs.push('rejection: ' + (e.reason && e.reason.message || e.reason)));
  \(demo ? "localStorage.setItem('assistant-mode', 'demo');" : "")
""", injectionTime: .atDocumentStart, forMainFrameOnly: true))

let web = WKWebView(frame: CGRect(x: 0, y: 0, width: 1200, height: 900), configuration: cfg)
web.load(URLRequest(url: url))
DispatchQueue.main.asyncAfter(deadline: .now() + wait) {
  web.evaluateJavaScript("""
    JSON.stringify({
      errors: window.__errs,
      page_error: (document.querySelector('#page-error') || {}).textContent || '',
      text: document.body.textContent,
      tracks: [...document.querySelectorAll('.track')].map(e => e.textContent)})
  """) { out, err in
    if let s = out as? String { print(s); exit(0) }
    FileHandle.standardError.write("evaluate failed: \(String(describing: err))\n".data(using: .utf8)!)
    exit(1)
  }
}
RunLoop.main.run()
