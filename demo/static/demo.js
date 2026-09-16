// Demo-only chrome: the banner, readonly pre-filled credentials, the
// cookie-capability probe, and the bootstrap call that makes a review exist
// before the dashboard renders.
(function () {
  const BANNER_EN = "Demo — mock data. No real GitHub or LLM calls.";
  const BANNER_HE = "דמו — נתונים מדומים.";
  const PROBE_COOKIE = "demo_cookie_probe";

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
    if (params.get("cookieless") === "1") return false;
    if (document.cookie.indexOf(PROBE_COOKIE) !== -1) return false;
    params.set("cookieless", "1");
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
