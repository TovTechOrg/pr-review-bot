"""Where am I in setup, what is missing, and what do I run next.

Read-only and idempotent: doctor never writes a file, starts a process, or
mutates remote state. Writing belongs to scripts/init_env.py and
scripts/create_github_app.py (design spec 2026-08-18 section 4d).

It COMPOSES scripts/deploy.py's checks rather than reimplementing them --
two check implementations that could drift is the thing most worth avoiding
here -- and adds only the backwards-looking probes deploy.py has no reason to
own (is .env populated, does the PEM decode).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import NamedTuple

import github_app
from config import settings
from scripts import _prereqs, _probes, create_github_app, deploy

class State(NamedTuple):
    """Observable setup state. Every field is a plain bool: a step is either
    satisfied or it is not, and nothing here can carry a secret."""

    prereqs: bool
    app_credentials: bool
    app_installed: bool
    llm_ready: bool
    database: bool
    public_url: bool
    webhook: bool
    keepalive: bool


class Step(NamedTuple):
    number: int
    title: str
    field: str    # the State field that must be True for this step to be done
    command: str  # the exact next action, verbatim


_STEPS: tuple[Step, ...] = (
    Step(1, "Install prerequisites", "prereqs",
         "uv sync, then install anything the prereqs rows above name"),
    Step(2, "Create the GitHub App", "app_credentials",
         "create it by hand in GitHub's UI (Settings -> Developer settings -> GitHub Apps -> "
         "New GitHub App), then paste the App ID / webhook secret / base64-encoded private "
         "key into .env yourself"),
    Step(3, "Install the App on your repo(s)", "app_installed",
         "open https://github.com/settings/apps -> your app -> Install App"),
    Step(4, "Create the Supabase project", "database",
         "create it at https://supabase.com, then set DATABASE_URL to the "
         "Session-mode pooler string (port 5432, NOT 6543)"),
    Step(5, "Configure an LLM provider", "llm_ready",
         "set the provider's API key in .env, then run "
         "`uv run python -m scripts.set_override <provider> --model <name>` "
         "against your now-live DATABASE_URL"),
    Step(6, "Create the Render service", "public_url",
         "Render dashboard -> New + -> Blueprint -> point it at a repo with "
         "render.yaml (the upstream repo's URL works too) -- leave every var "
         "blank, then get a RENDER_API_KEY and run Step 7's --sync-env to push "
         "them all"),
    Step(7, "Sync config and verify", "webhook",
         "uv run python -m scripts.deploy --sync-env"),
    Step(8, "Add the keep-warm pinger", "keepalive",
         "create an UptimeRobot monitor on <your-service>/healthz at a 5-minute "
         "interval (the URL must match exactly); set UPTIMEROBOT_API_KEY too, so "
         "doctor can verify it"),
)


def steps_for() -> tuple[Step, ...]:
    return _STEPS


def current_step(state: State) -> Step | None:
    """The EARLIEST unsatisfied step, or None when setup is complete.

    Earliest, not most-severe: a later gap is usually a consequence of an
    earlier one, so reporting it first would send an operator down the wrong
    path.
    """
    for step in steps_for():
        if not getattr(state, step.field):
            return step
    return None


_APP_CREDENTIALS = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET")


def check_prereqs() -> deploy.CheckResult:
    """Python version and the tools that must be on PATH."""
    problems: list[str] = []
    if not _prereqs.python_version_ok():
        major, minor = _prereqs.MINIMUM_PYTHON
        problems.append(
            f"Python {major}.{minor}+ required, running "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
    problems.extend(
        _prereqs.install_hint(tool) for tool in _prereqs.REQUIRED_TOOLS
        if not _prereqs.is_available(tool)
    )
    if problems:
        return deploy.CheckResult("prereqs", "FAIL", "\n".join(problems))
    return deploy.CheckResult("prereqs", "PASS", "")


def check_test_database() -> deploy.CheckResult:
    """Whether `uv run pytest` can get a Postgres: Docker present, or a
    DATABASE_URL that looks like a local/CI Postgres already set. WARN, not
    FAIL -- it blocks the test suite, never the service itself, so it must
    not stop an operator from deploying."""
    if _prereqs.database_available():
        return deploy.CheckResult("test-db", "PASS", "")
    return deploy.CheckResult(
        "test-db", "WARN",
        "DB-touching tests need Docker running or a local DATABASE_URL set; "
        "a remote DATABASE_URL (e.g. Supabase) doesn't count -- tests refuse "
        "to run against it unless ALLOW_REMOTE_TEST_DB=1 is set; "
        f"{_prereqs.install_hint(_prereqs.DOCKER)}",
    )


def check_local_config() -> deploy.CheckResult:
    """The App credentials present locally.

    Reports NAMES and a decode boolean only -- never a value, never a length in
    the failure path. Pasting the PEM verbatim instead of its base64 form is the
    single most common setup mistake, so it gets its own line. The FAIL detail
    also names which credentials ARE present (via a `(have: ...)` clause) --
    never their values, just like the missing list.
    """
    present = _probes.present_secrets()
    missing = [name for name in _APP_CREDENTIALS if name not in present]
    have = [name for name in _APP_CREDENTIALS if name in present]
    problems = []
    if missing:
        line = f"missing: {', '.join(missing)}"
        if have:
            line += f" (have: {', '.join(have)})"
        problems.append(line)
    if "GITHUB_APP_PRIVATE_KEY" in present and not _probes.private_key_decodes():
        problems.append(
            "GITHUB_APP_PRIVATE_KEY is set but does not base64-decode to a PEM "
            "-- it must be the base64 form, not the file's contents verbatim: "
            "uv run python -m scripts.encode_credential github-app-private-key.pem"
        )
    if problems:
        return deploy.CheckResult("local-config", "FAIL", "\n".join(problems))
    return deploy.CheckResult("local-config", "PASS", "")


def check_app_permissions() -> deploy.CheckResult:
    """Whether the App's ACTUAL permissions and event subscriptions on
    GitHub match what this project's code needs (github_app.
    diff_app_permissions against scripts/create_github_app.MANIFEST_
    PERMISSIONS/MANIFEST_EVENTS -- the same constants the manifest flow
    itself requests, so there is exactly one definition of "what this App
    needs" for both creating it and verifying it). READ-ONLY: GET /app only.

    This is what makes creating the App entirely by hand
    (guide/setup/02-github-app.md) as safe as the automated manifest flow,
    and also catches drift the manifest flow alone can't: GitHub Apps can be
    edited by hand in the UI at any point after creation (add a permission,
    subscribe to an extra event), regardless of how the App was originally
    made. This check reads the App's CURRENT state, not its creation-time
    manifest, so it catches both.

    Missing/weaker-than-needed permissions or a missing event are FAIL: the
    bot cannot do something its own code depends on. Broader-than-needed
    permissions or an extra event are WARN, not FAIL: a least-privilege nit,
    not something that breaks the bot -- mirrors the "pricing is a warning
    only" precedent elsewhere in this file for a non-blocking finding.
    """
    if not all(name in _probes.present_secrets() for name in _APP_CREDENTIALS):
        return deploy.CheckResult(
            "app-permissions", "SKIPPED", "needs the App credentials (see local-config)"
        )
    try:
        actual_permissions, actual_events = github_app.get_app_permissions()
    except Exception as exc:  # noqa: BLE001 -- structural report, never the value
        return deploy.CheckResult(
            "app-permissions", "FAIL",
            f"could not read the App's permissions ({type(exc).__name__})",
        )
    under, over = github_app.diff_app_permissions(
        actual_permissions, actual_events,
        create_github_app.MANIFEST_PERMISSIONS, create_github_app.MANIFEST_EVENTS,
    )
    if under:
        detail = "missing/insufficient: " + "; ".join(under)
        if over:
            detail += " (also broader than needed: " + "; ".join(over) + ")"
        detail += " -- fix in the App's settings: https://github.com/settings/apps"
        return deploy.CheckResult("app-permissions", "FAIL", detail)
    if over:
        return deploy.CheckResult(
            "app-permissions", "WARN",
            "broader than needed (least-privilege nit, not blocking): " + "; ".join(over),
        )
    return deploy.CheckResult("app-permissions", "PASS", "")


def check_github_install() -> deploy.CheckResult:
    """Whether the App has exactly one installation. READ-ONLY."""
    if not all(name in _probes.present_secrets() for name in _APP_CREDENTIALS):
        return deploy.CheckResult(
            "github-install", "SKIPPED", "needs the App credentials (see local-config)"
        )
    try:
        installation_id = github_app.discover_installation_id_for_app()
    except Exception as exc:  # noqa: BLE001 -- structural report, never the value
        return deploy.CheckResult(
            "github-install", "FAIL",
            f"could not resolve an installation ({type(exc).__name__}); "
            "install the App at https://github.com/settings/apps",
        )
    return deploy.CheckResult("github-install", "PASS", f"installation={installation_id}")


def check_target_repo_covered() -> deploy.CheckResult:
    """Whether GITHUB_TARGET_REPO (if set) is covered by the App's actual
    installation. READ-ONLY -- reuses github_app.repos_not_covered, the exact
    comparison scripts/deploy.py's github-app check uses, so the two can
    never drift on what "covered" means; only that check's other half
    (writing the webhook) is deliberately not duplicated here.

    Step 2 (create the App) and step 3 (install it) both happen in a
    BROWSER, under whichever GitHub account that browser session happens to
    be logged into -- not necessarily the account GITHUB_TARGET_REPO's repo
    belongs to. Nothing before this check ever compares the two; this is
    that comparison, from the App-installation side. See check_gh_auth for
    the other side: whether the LOCAL `gh` CLI's authenticated account can
    actually reach the same repo.
    """
    if not settings.is_target_repo_configured():
        return deploy.CheckResult(
            "target-repo", "SKIPPED", "GITHUB_TARGET_REPO not set yet"
        )
    repos = settings.target_repos()
    if not repos:
        return deploy.CheckResult(
            "target-repo", "SKIPPED",
            "GITHUB_TARGET_REPO=* (track-all mode) -- no specific repo to verify",
        )
    if not all(name in _probes.present_secrets() for name in _APP_CREDENTIALS):
        return deploy.CheckResult(
            "target-repo", "SKIPPED", "needs the App credentials (see local-config)"
        )
    try:
        installation_id = github_app.discover_installation_id_for_app()
        covered = github_app.list_installation_repos(installation_id)
    except Exception as exc:  # noqa: BLE001 -- structural report, never the value
        return deploy.CheckResult(
            "target-repo", "FAIL", f"could not verify ({type(exc).__name__})"
        )
    missing = github_app.repos_not_covered(covered, repos)
    if missing:
        return deploy.CheckResult(
            "target-repo", "FAIL",
            "not covered by the App's installation: " + ", ".join(missing) +
            " -- either a typo in GITHUB_TARGET_REPO, or the App was created/installed "
            "in the browser under a different GitHub account than this repo belongs to; "
            "check the App's Installed repositories list on GitHub",
        )
    return deploy.CheckResult(
        "target-repo", "PASS", f"installation={installation_id}; covered ({len(repos)} repo(s))"
    )


def _run_gh(*args: str) -> subprocess.CompletedProcess[str]:
    """A single read-only `gh` invocation. Never raises on a nonzero exit --
    callers read .returncode -- and always bounded, so an interactive prompt
    gh might otherwise print (e.g. no terminal attached) can't hang doctor."""
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=15, check=False
    )


def check_gh_auth() -> deploy.CheckResult:
    """Whether the LOCAL `gh` CLI (needed by scripts.seed_demo_pr, step 8) is
    authenticated, and -- once GITHUB_TARGET_REPO is set -- whether that
    authenticated account can actually push to it. READ-ONLY: every gh
    subcommand used here only queries state (`auth status`, `api user`,
    `repo view`), never mutates anything.

    This is the local-CLI side of the same mismatch check_target_repo_covered
    covers from the App-installation side: Steps 2-3 authenticate through a
    BROWSER, `gh auth login` authenticates this MACHINE, and nothing ties the
    two to the same GitHub account. A user who approved the App as one
    account but ran `gh auth login` as another discovers it, today, only when
    `scripts.seed_demo_pr` fails outright at step 8 -- this surfaces it as
    soon as GITHUB_TARGET_REPO is set instead, and names the likely cause
    rather than just relaying gh's own error text.
    """
    if not _prereqs.is_available(_prereqs.GH):
        return deploy.CheckResult("gh-auth", "SKIPPED", "install gh first (see prereqs)")

    try:
        status = _run_gh("auth", "status")
    except subprocess.TimeoutExpired:
        return deploy.CheckResult("gh-auth", "FAIL", "gh auth status timed out")
    if status.returncode != 0:
        return deploy.CheckResult(
            "gh-auth", "FAIL", "gh is installed but not authenticated -- run `gh auth login`"
        )

    login = _run_gh("api", "user", "--jq", ".login")
    who = login.stdout.strip() if login.returncode == 0 and login.stdout.strip() else "(unknown)"

    if not settings.is_target_repo_configured():
        return deploy.CheckResult(
            "gh-auth", "PASS",
            f"authenticated as {who}; set GITHUB_TARGET_REPO later to verify repo access",
        )
    repos = settings.target_repos()
    if not repos:
        return deploy.CheckResult(
            "gh-auth", "PASS",
            f"authenticated as {who}; GITHUB_TARGET_REPO=* (track-all mode) -- "
            "no specific repo to verify",
        )

    cant_push = []
    for repo in sorted(repos):
        view = _run_gh(
            "repo", "view", repo, "--json", "viewerPermission", "--jq", ".viewerPermission"
        )
        permission = view.stdout.strip() if view.returncode == 0 else ""
        if permission not in {"WRITE", "MAINTAIN", "ADMIN"}:
            cant_push.append(f"{repo} ({permission or 'not visible to this account'})")

    if cant_push:
        return deploy.CheckResult(
            "gh-auth", "FAIL",
            f"authenticated as {who}, which cannot push to: " + ", ".join(cant_push) +
            " -- if the App was created/installed under a different GitHub account in "
            "your browser, `gh auth login` (or `gh auth switch`) to that same account; "
            "otherwise GITHUB_TARGET_REPO may be pointed at the wrong repo",
        )
    return deploy.CheckResult(
        "gh-auth", "PASS", f"authenticated as {who}; can push to all of GITHUB_TARGET_REPO"
    )


def check_webhook(base: str) -> deploy.CheckResult:
    """Whether the App's webhook points at `base`. READ-ONLY -- deliberately
    does not call deploy.py's equivalent check, which PATCHes the URL when it
    is wrong. Fixing it is `uv run python -m scripts.deploy`; reporting it is
    here."""
    if not base:
        return deploy.CheckResult("webhook", "SKIPPED", "no public base URL yet")
    if not all(name in _probes.present_secrets() for name in _APP_CREDENTIALS):
        return deploy.CheckResult("webhook", "SKIPPED", "needs the App credentials")
    wanted = base.rstrip("/") + "/webhook"
    try:
        current = github_app.get_webhook_url()
    except Exception as exc:  # noqa: BLE001
        return deploy.CheckResult("webhook", "FAIL", f"could not read it ({type(exc).__name__})")
    if current == wanted:
        return deploy.CheckResult("webhook", "PASS", "")
    return deploy.CheckResult(
        "webhook", "FAIL",
        f"points at {current or '(unset)'}, wanted {wanted} "
        "-- fix with: uv run python -m scripts.deploy",
    )


def build_state(base: str) -> tuple[State, list[deploy.CheckResult]]:
    """Probe, staged: local first, then remote only for resources that exist.

    Ordering matters. Render not existing at step 1 is the normal state, so a
    remote probe is SKIPPED rather than failed until its precondition holds.
    """
    results = [
        deploy._safe("prereqs", check_prereqs),
        deploy._safe("test-db", check_test_database),
        deploy._safe("local-config", check_local_config),
        deploy._safe("config", deploy.check_config),
        deploy._safe("pricing", deploy.check_pricing),
        deploy._safe("app-permissions", check_app_permissions),
        deploy._safe("github-install", check_github_install),
        deploy._safe("target-repo", check_target_repo_covered),
        deploy._safe("gh-auth", check_gh_auth),
        deploy._safe("database", deploy.check_database),
        deploy._safe("provider", deploy.check_provider),
    ]
    results.append(deploy._safe("health", deploy.check_health_endpoint, base) if base
                   else deploy.CheckResult("health", "SKIPPED", "no public base URL yet"))
    results.append(deploy._safe("webhook", check_webhook, base))
    results.extend([
        deploy._safe("boot-creds-live", deploy.check_boot_credentials_live),
        deploy._safe("provider-live", deploy.check_provider_live),
        deploy._safe("api-key-live", deploy.check_api_key_live),
        deploy._safe("render-service", deploy.check_render_service),
        deploy._safe("uptime-pinger", deploy.check_uptime_pinger, base),
    ])

    by_name = {r.name: r for r in results}

    def ok(name: str) -> bool:
        return by_name.get(name, deploy.CheckResult(name, "SKIPPED")).status in ("PASS", "WARN")

    state = State(
        prereqs=ok("prereqs"),
        app_credentials=ok("local-config"),
        app_installed=ok("github-install"),
        llm_ready=ok("provider"),
        database=ok("database"),
        public_url=ok("health"),
        webhook=ok("webhook"),
        keepalive=ok("uptime-pinger"),
    )
    return state, results


def render(step: Step | None, results: list[deploy.CheckResult]) -> str:
    """deploy.py's table, plus the one line doctor exists to print."""
    report = deploy.render_report(results)
    if step is None:
        failing = [r.name for r in results if r.status == "FAIL"]
        note = (
            f" ({len(failing)} check(s) still reporting FAIL -- see the table "
            f"above: {', '.join(failing)})" if failing else ""
        )
        return f"{report}\n\nsetup complete, every step satisfied.{note}"
    return (
        f"{report}\n\nyou are at step {step.number} of 8: "
        f"{step.title}\nnext: {step.command}"
    )


def as_json(step: Step | None, results: list[deploy.CheckResult]) -> str:
    return json.dumps(
        {
            "step": None if step is None
            else {"number": step.number, "title": step.title, "command": step.command},
            "checks": [
                {"name": r.name, "status": r.status, "detail": r.detail} for r in results
            ],
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report where you are in setup and what to run next (read-only)"
    )
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit machine-readable output")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    base = deploy.resolve_base_url()
    state, results = build_state(base)
    step = current_step(state)
    print(as_json(step, results) if args.as_json else render(step, results))
    # Exit 0 always: "you are mid-setup" is information, not failure. Only a
    # bad invocation is an error (argparse itself exits 2 for that).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
