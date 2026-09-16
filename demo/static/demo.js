// Demo-only chrome: the banner, readonly pre-filled credentials, the
// cookie-capability probe, and the bootstrap call that makes a review exist
// before the dashboard renders.
(function () {
  const BANNER_EN = "Demo — mock data. No real GitHub or LLM calls.";
  const BANNER_HE = "דמו — נתונים מדומים.";
  const PROBE_COOKIE = "demo_cookie_probe";
  const COOKIELESS_PARAM = "cookieless";

  // ---------------------------------------------------------------------
  // Cookie-hostile fetch patch. THIS RUNS AT SCRIPT-PARSE TIME ON PURPOSE.
  //
  // Do not move any of this into the DOMContentLoaded listener at the
  // bottom of this file. This script tag is the LAST one in
  // dashboard.html, so by the time it is parsed, dashboard.html's own
  // inline <script> has already registered its DOMContentLoaded listener.
  // Listeners fire in registration order, so dashboard.html's
  // refreshDashboard() -- and its `fetch("/api/dashboard")` -- would run
  // BEFORE a listener registered here. Patching at top level runs before
  // any DOMContentLoaded listener fires at all, which is the only ordering
  // that gets the patch in front of that first call.
  //
  // Why it is needed: `?cookieless=1` (see demo/app.py::_cookie_hostile)
  // only ever rides on the top-level navigation. Every dashboard XHR is a
  // bare relative path, so for a browser that discards cookies those calls
  // arrive with neither a session cookie nor the marker -- a real 401,
  // which dashboard.html answers with `location.href = "/login"`, which
  // sends demo.js's redirectIfCookieHostile() straight back to
  // `/?cookieless=1`: an infinite loop. Propagating the marker onto every
  // same-origin fetch is what makes the bypass survive past the first
  // navigation.
  // ---------------------------------------------------------------------

  function cookielessMode() {
    return new URLSearchParams(location.search).get(COOKIELESS_PARAM) === "1";
  }

  // Returns a rewritten URL string, or null meaning "leave this call
  // exactly as it was". Null is the safe answer for every shape this
  // cannot confidently handle: a Request object, an absolute or
  // protocol-relative URL, anything non-string, or a URL that fails to
  // parse. Deliberately never throws -- a demo-chrome helper must not be
  // able to break a fetch call it does not understand.
  function withCookieless(input) {
    if (typeof input !== "string") return null;
    // Root-relative only. "//host/path" is protocol-relative, i.e. a
    // different origin wearing a leading slash, so it is excluded here.
    if (input.charAt(0) !== "/" || input.charAt(1) === "/") return null;
    try {
      const url = new URL(input, location.origin);
      if (url.origin !== location.origin) return null;
      if (url.searchParams.get(COOKIELESS_PARAM) === "1") return null;
      // .set() on URLSearchParams, never string concatenation: it keeps
      // every parameter the caller already put on the URL (e.g.
      // /api/environment/credential/x/models?foo=bar) instead of
      // clobbering the query string.
      url.searchParams.set(COOKIELESS_PARAM, "1");
      return url.pathname + url.search + url.hash;
    } catch (err) {
      return null;
    }
  }

  const realFetch = window.fetch;
  if (typeof realFetch === "function") {
    window.fetch = function (input, init) {
      if (cookielessMode()) {
        const rewritten = withCookieless(input);
        if (rewritten !== null) return realFetch.call(this, rewritten, init);
      }
      return realFetch.call(this, input, init);
    };
  }

  function banner() {
    const el = document.createElement("div");
    el.id = "demoBanner";
    el.textContent =
      document.documentElement.lang === "he" ? BANNER_HE : BANNER_EN;
    el.style.cssText =
      "padding:.5rem 1rem;text-align:center;background:#f5c518;color:#1a1a2e;font-weight:600";
    document.body.prepend(el);
  }

  function isLoginPage() {
    return document.getElementById("passwordInput") !== null;
  }

  function prefillLogin() {
    const user = document.getElementById("usernameInput");
    const pass = document.getElementById("passwordInput");
    if (!user || !pass) return;
    user.value = "demo";
    pass.value = "demo";
    // readonly, never disabled: disabled inputs are excluded from form
    // submission and render greyed out.
    user.readOnly = true;
    pass.readOnly = true;
  }

  // The server hands every response a plain, JS-readable probe cookie. If it
  // is missing by the time the login page has rendered, this browser cannot
  // store cookies at all (LinkedIn's in-app browser, some private modes) --
  // and the dashboard session IS a cookie, so no password could ever get the
  // reader past this form. Only then do we ask the server to skip the gate,
  // carrying ?provider= along. A browser that CAN store cookies never takes
  // this branch and sees the real login screen, which is the point.
  function redirectIfCookieHostile() {
    if (!isLoginPage()) return false;
    const params = new URLSearchParams(location.search);
    if (params.get(COOKIELESS_PARAM) === "1") return false;
    if (document.cookie.indexOf(PROBE_COOKIE) !== -1) return false;
    params.set(COOKIELESS_PARAM, "1");
    location.replace("/?" + params.toString());
    return true;
  }

  document.addEventListener("DOMContentLoaded", function () {
    banner();
    prefillLogin();
    if (redirectIfCookieHostile()) return;
    // Bootstrap fires on the dashboard only, never on the login page: the
    // design's ordering is that the reader arrives with ?provider= BEFORE
    // authenticating and the review reporting it is not generated until
    // after. login.html carries the parameter through its own redirect.
    if (isLoginPage()) return;
    const provider = new URLSearchParams(location.search).get("provider");
    fetch("/api/demo/bootstrap" + (provider ? "?provider=" + provider : ""));
  });
})();
