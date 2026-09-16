// Demo-only chrome: the banner, readonly pre-filled credentials, and the
// bootstrap call that makes a review exist before the dashboard renders.
(function () {
  const BANNER_EN = "Demo — mock data. No real GitHub or LLM calls.";
  const BANNER_HE = "דמו — נתונים מדומים.";

  function banner() {
    const el = document.createElement("div");
    el.id = "demoBanner";
    el.textContent =
      document.documentElement.lang === "he" ? BANNER_HE : BANNER_EN;
    el.style.cssText =
      "padding:.5rem 1rem;text-align:center;background:#f5c518;color:#1a1a2e;font-weight:600";
    document.body.prepend(el);
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

  document.addEventListener("DOMContentLoaded", function () {
    banner();
    prefillLogin();
    const provider = new URLSearchParams(location.search).get("provider");
    fetch("/api/demo/bootstrap" + (provider ? "?provider=" + provider : ""));
  });
})();
