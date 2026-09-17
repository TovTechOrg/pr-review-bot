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

  // Where a convinced reader goes next. The real wizard is unpinned too, so
  // it cold-starts -- warmed below when the CTA renders, which is the first
  // moment a click on it is plausible. Warming it from the launcher instead
  // would spend the instance-hours minutes too early, and a free service
  // spins back down after ~15 idle minutes.
  // Substituted server-side by demo/app.py at import time from `settings`
  // (config.py) via json.dumps -- these two tokens are deliberately
  // unquoted here (the substitution supplies the quotes) and must never be
  // edited directly.
  var REAL_WIZARD_URL = __REAL_WIZARD_URL__;
  var GUIDE_URL = __GUIDE_URL__;

  var CTA = {
    en: {
      heading: "Like what you see?",
      body: "That review came from mock data. Point the real engine at your own repository — it takes about 10 minutes.",
      primary: "Deploy your own →",
      secondary: "Read the setup guide"
    },
    he: {
      heading: "אהבתם?",
      body: "הסקירה הזו הופקה מנתונים מדומים. אפשר לחבר את המנוע האמיתי למאגר שלכם — זה לוקח כ-10 דקות.",
      primary: "התקינו אצלכם",
      secondary: "מדריך ההתקנה"
    }
  };

  function ctaStrings() {
    return CTA[document.documentElement.lang === "he" ? "he" : "en"];
  }

  function addClosingCta() {
    // The dashboard only, never the login page: the CTA is the payoff at the
    // end of the reader's path, not a distraction in front of the gate.
    var reviews = document.getElementById("reviews");
    if (!reviews || document.getElementById("demoCta")) return;

    var strings = ctaStrings();
    var section = document.createElement("section");
    section.id = "demoCta";
    // var(--card) does not exist in this dashboard's CSS (verified against
    // dashboard/static/dashboard.html) -- var(--surface) is its actual
    // elevated-surface token (--paper is the page background, --surface is
    // the card-like fill on top of it).
    section.style.cssText =
      "margin:1.5rem auto;max-width:48rem;padding:1.25rem;border-radius:12px;" +
      "border:1px solid var(--border);background:var(--surface);text-align:center";

    var heading = document.createElement("h2");
    heading.textContent = strings.heading;
    heading.style.cssText = "margin:0 0 .5rem;font-size:1.15rem";

    var body = document.createElement("p");
    body.textContent = strings.body;
    body.style.cssText = "margin:0 0 1rem;color:var(--text-muted);font-size:.925rem";

    var primary = document.createElement("a");
    primary.id = "demoCtaPrimary";
    primary.href = REAL_WIZARD_URL;
    primary.textContent = strings.primary;
    primary.rel = "noopener";
    // NOT var(--accent): measured against this dashboard's own theme values
    // (light --signal #a3550a, dark --signal #d98a34), white text on
    // --accent gives 5.44:1 in light mode but only 2.75:1 in dark mode --
    // well under the 4.5:1 AA threshold. #7a4008 is a darker shade in the
    // same brown/orange family that clears AA (8.19:1) in both themes,
    // since a solid button fill's contrast depends only on its own text,
    // not which theme is active.
    primary.style.cssText =
      "display:inline-block;padding:.7rem 1.15rem;border-radius:9px;background:#7a4008;" +
      "color:#fff;text-decoration:none;font-weight:600;min-height:44px;line-height:1.7";

    var secondary = document.createElement("a");
    secondary.href = GUIDE_URL;
    secondary.textContent = strings.secondary;
    secondary.rel = "noopener";
    // color:var(--text) explicitly, matching dashboard.html's own
    // a.comment-link convention -- this dashboard has no anchor-tag color
    // reset anywhere, so an unstyled link renders the browser's default
    // link blue, which measured 1.76:1 against the dark theme's --surface
    // (well under the 4.5:1 AA floor).
    secondary.style.cssText =
      "display:inline-block;margin-top:.85rem;font-size:.875rem;" +
      "color:var(--text);text-decoration:underline";

    primary.addEventListener("click", function () {
      // Fire-and-forget: navigation must never wait on analytics.
      try { fetch("/api/demo/step/cta_clicked", { method: "POST" }); } catch (err) {}
    });

    section.appendChild(heading);
    section.appendChild(body);
    section.appendChild(primary);
    section.appendChild(document.createElement("br"));
    section.appendChild(secondary);
    reviews.parentNode.insertBefore(section, reviews.nextSibling);

    // Warm the real wizard now that a click is plausible. no-cors because we
    // never read the answer -- this is a wake-up, not a health check.
    try {
      fetch(REAL_WIZARD_URL + "/healthz", { mode: "no-cors", cache: "no-store" });
    } catch (err) {}
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
    addClosingCta();
  });
})();
