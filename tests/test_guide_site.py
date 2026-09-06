"""The site config is the one thing that makes the guide navigable, so its
shape is asserted rather than assumed. Everything here reads files -- no test
in Stage 3b touches the network (see the plan's Global Constraints)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_MKDOCS = _ROOT / "mkdocs.yml"


def _tracked_root_markdown_files() -> list[Path]:
    """Root-level ``*.md`` files that are actually tracked by git -- excludes
    local scratch/session files (e.g. a gitignored ``memory.md``) that may
    exist on a particular machine but are not part of the shipped guide/docs
    set, so a scan over them would be flaky depending on what happens to sit
    on disk."""
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [_ROOT / rel for rel in out if "/" not in rel]


def _config() -> dict:
    # mkdocs.yml uses !!python/name: tags for some Material extensions; the
    # base loader keeps them as plain strings instead of refusing to parse.
    return yaml.load(_MKDOCS.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_site_builds_from_the_guide_directory():
    config = _config()
    assert config["docs_dir"] == "guide", (
        "docs/ holds internal engineering notes and superpowers specs; pointing "
        "MkDocs at it would publish all of them"
    )
    assert config["site_name"]


def test_material_features_the_guide_relies_on_are_enabled():
    config = _config()
    assert config["theme"]["name"] == "material"
    extensions = config["markdown_extensions"]
    flat = [e if isinstance(e, str) else next(iter(e)) for e in extensions]
    # Admonitions carry the gotchas that must not read as body text; tabbed
    # content collapses the bash/PowerShell duplication README carries today.
    assert "admonition" in flat
    assert "pymdownx.tabbed" in flat
    assert "pymdownx.superfences" in flat


def test_every_nav_target_exists_on_disk():
    """A nav entry pointing at a missing file builds a broken site silently."""

    def targets(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for value in node.values():
                yield from targets(value)
        elif isinstance(node, list):
            for item in node:
                yield from targets(item)

    for target in targets(_config()["nav"]):
        assert (_ROOT / "guide" / target).is_file(), f"nav points at missing {target}"


def test_the_generated_reference_pages_are_in_the_nav():
    flat = list(_MKDOCS.read_text(encoding="utf-8").splitlines())
    joined = "\n".join(flat)
    for name in ("config.md", "pricing.md", "checks.md", "sync-env.md"):
        assert f"reference/{name}" in joined


def test_landing_page_states_the_prerequisites_up_front():
    text = (_ROOT / "guide" / "index.md").read_text(encoding="utf-8")
    assert "3.12" in text, "the Python floor is in .python-version and nowhere a reader looks"
    assert "uv" in text
    assert "Postgres" in text


_SETUP = _ROOT / "guide" / "setup"


def test_setup_pages_match_doctors_step_titles():
    """If the guide and doctor disagree about a step's name, an operator
    following one while running the other cannot tell where they are."""
    from scripts import doctor

    pages = {
        1: "01-prerequisites.md",
        2: "02-github-app.md",
        3: "03-install-app.md",
        4: "04-llm-provider.md",
        5: "05-supabase.md",
        6: "06-render.md",
        7: "07-sync.md",
        8: "08-pinger.md",
    }
    for step in doctor.steps_for():
        text = (_SETUP / pages[step.number]).read_text(encoding="utf-8")
        assert step.title in text, f"step {step.number} page must carry doctor's title"


def test_prerequisites_page_uses_portable_commands_only():
    """spec section 5: `base64 -w0` is GNU-only and `curl` on Windows
    PowerShell aliases Invoke-WebRequest, which takes different arguments."""
    text = (_SETUP / "01-prerequisites.md").read_text(encoding="utf-8")
    assert "base64 -w0" not in text
    assert "winget install Docker" not in text or "=== " in text, (
        "a Windows-only install line must sit inside tabbed content"
    )


def test_github_app_page_encodes_the_pem_with_the_project_script():
    text = (_SETUP / "02-github-app.md").read_text(encoding="utf-8")
    assert "scripts.encode_credential" in text
    assert "base64 -w0" not in text


def test_install_app_page_sets_up_the_demo_repo_step_8_relies_on():
    """Regression: Step 8 needs a repo with push access, the App installed
    on it, and GITHUB_TARGET_REPO set -- but nothing before Step 8 ever told
    the reader to arrange any of that, so a fresh-account reader hit
    `seed_demo_pr` failing with no repo configured. Step 3 is the step
    already about installing the App on repos in the first place, so the fix
    lives here: pick/create a repo before installing, then set
    GITHUB_TARGET_REPO to it right away -- instead of leaving both to be
    discovered at Step 8."""
    text = (_SETUP / "03-install-app.md").read_text(encoding="utf-8")
    assert "gh repo create" in text
    assert "GITHUB_TARGET_REPO=" in text


def test_install_app_page_creates_the_repo_with_a_default_branch():
    """Regression: `gh repo create ... --clone=false` with no initial commit
    leaves a repo with no default branch. seed_demo_pr (Step 8) then pushes a
    branch and asks `gh pr create` to open a PR against that nonexistent
    default branch, which fails with a cryptic `GraphQL: can't be blank
    (createPullRequest)` instead of a clear error. --add-readme gives the
    repo an initial commit, and thus a real default branch to PR against."""
    text = (_SETUP / "03-install-app.md").read_text(encoding="utf-8")
    assert "gh repo create" in text
    assert "--add-readme" in text


def test_step_8_page_points_back_to_step_3_instead_of_repeating_it():
    """The repo/App-install/GITHUB_TARGET_REPO prerequisite now lives once,
    in Step 3 -- Step 8 should link back to it rather than re-explain it
    end to end."""
    text = (_SETUP / "08-pinger.md").read_text(encoding="utf-8")
    assert "03-install-app.md" in text


def test_install_app_page_uses_doctor_not_bare_deploy_for_installation_id():
    """Regression: scripts.deploy's main() exits 2 immediately ("a public
    base URL ... is required") before it ever reaches the installation-id
    discovery check -- and Step 3 is before Step 6, so no PUBLIC_BASE_URL or
    RENDER_EXTERNAL_URL exists yet at this point in the guide. scripts.doctor
    needs no base URL and discovers the same installation id via its
    github-install check, so that's what this page must point at instead."""
    text = (_SETUP / "03-install-app.md").read_text(encoding="utf-8")
    assert "uv run python -m scripts.doctor" in text
    assert "```bash\nuv run python -m scripts.deploy\n```" not in text


def test_llm_provider_page_edits_the_files_directly_instead_of_running_init_env():
    """init_env.py stays in the repo but unwired from the guide: the two
    config files are already `cp`'d into existence by Step 2, so Step 4
    just has the operator edit LLM_PROVIDER/the credential by hand and
    verify with doctor -- one fewer script in the documented path, and one
    fewer place a malformed answer can reach config.py before doctor
    ever gets a chance to report it structurally."""
    step2 = (_SETUP / "02-github-app.md").read_text(encoding="utf-8")
    assert "cp .env.config.example .env.config" in step2

    step4 = (_SETUP / "04-llm-provider.md").read_text(encoding="utf-8")
    assert "scripts.init_env" not in step4
    assert "uv run python -m scripts.doctor" in step4


def test_setup_index_names_all_eight_steps():
    text = (_SETUP / "index.md").read_text(encoding="utf-8")
    for title in (
        "Install prerequisites", "Create the GitHub App",
        "Install the App on your repo(s)", "Configure an LLM provider",
        "Create the Supabase project", "Create the Render service",
        "Sync config and verify", "Add the keep-warm pinger",
    ):
        assert title in text


def test_supabase_page_pins_the_session_pooler_port():
    text = (_SETUP / "05-supabase.md").read_text(encoding="utf-8")
    assert "5432" in text and "6543" in text, "both ports named, so the wrong one is unmistakable"


def test_render_page_leaves_env_vars_blank_for_sync_env_to_push():
    """Regression: this page used to tell the reader to hand-type exactly
    four env vars into Render's dashboard to get the service booting -- but
    main.py's lifespan also requires GITHUB_APP_INSTALLATION_ID and a
    valid LLM_PROVIDER, so that first deploy always crash-looped. Since
    --sync-env (Step 7) doesn't actually need the service already booted,
    just already created, this page now has the reader leave every var
    blank and get RENDER_API_KEY here instead, deferring all of it to
    Step 7 in one push -- so it must no longer instruct hand-entering vars."""
    text = (_SETUP / "06-render.md").read_text(encoding="utf-8")
    assert "leave all of them blank" in text.lower() or "leave them blank" in text.lower()
    assert "RENDER_API_KEY" in text, "must say it is NOT a service env var"
    assert "Set exactly four env vars" not in text


def test_render_page_offers_the_point_at_upstream_option():
    """The upstream repo's own URL is a valid, lower-setup alternative to
    forking for Render's Blueprint flow -- confirmed by hand -- so Step 6
    should offer it alongside forking/pushing a new repo, not just the two
    options that need push access."""
    text = (_SETUP / "06-render.md").read_text(encoding="utf-8")
    assert "fork" in text.lower()
    assert "upstream" in text.lower()


def test_sync_page_is_where_the_boot_vars_actually_get_pushed():
    text = (_SETUP / "07-sync.md").read_text(encoding="utf-8")
    assert "--sync-env" in text
    assert "Application startup complete" in text


def test_pinger_page_warns_about_the_exact_url():
    text = (_SETUP / "08-pinger.md").read_text(encoding="utf-8")
    assert "healthz" in text
    assert "HEAD" in text, "UptimeRobot's free tier sends HEAD, not GET"


_OPS = _ROOT / "guide" / "operations"


def test_operations_pages_link_generated_tables_instead_of_restating_them():
    """A hand-copied check table is precisely the drift Stage 3a's generation
    and CI job exist to make impossible."""
    deploy_page = (_OPS / "deploy.md").read_text(encoding="utf-8")
    assert "reference/checks" in deploy_page
    assert "reference/sync-env" in deploy_page
    assert "| Check | Verifies |" not in deploy_page, "do not restate the generated table"


def test_deploy_page_documents_the_exit_codes():
    text = (_OPS / "deploy.md").read_text(encoding="utf-8")
    for code in ("exit 0", "exit 1", "exit 2"):
        assert code in text


def test_config_files_page_states_the_two_file_split():
    text = (_OPS / "config-files.md").read_text(encoding="utf-8")
    assert ".env.config" in text and ".env" in text
    assert "OPERATIONAL_KEYS" in text


def test_tuning_page_says_the_db_only_settings_need_no_redeploy():
    text = (_OPS / "tuning.md").read_text(encoding="utf-8")
    assert "--sync-config-db" in text
    assert "runtime_config" in text


def test_no_operations_page_mentions_the_removed_cost_cap():
    """KEY_USAGE_COST_CAP_USD was removed in Stage 1; documenting it would
    invite someone to set a variable nothing reads."""
    for page in _OPS.glob("*.md"):
        assert "KEY_USAGE_COST_CAP_USD" not in page.read_text(encoding="utf-8")


_BG = _ROOT / "guide" / "background"


def test_provider_history_survives_the_migration():
    """This is the record of what was actually tried and what it cost --
    the thing most easily lost when a 750-line journal is restructured."""
    text = (_BG / "providers.md").read_text(encoding="utf-8")
    assert "PERMISSION_DENIED" in text, "the Gemini Trust & Safety block"
    assert "github_models" in text or "GitHub Models" in text
    assert "gemini-2.5-flash" in text, "the Vertex catalog finding"


def test_background_is_not_in_the_setup_reading_path():
    setup_pages = list((_ROOT / "guide" / "setup").rglob("*.md"))
    assert setup_pages
    for page in setup_pages:
        assert "background/" not in page.read_text(encoding="utf-8"), (
            "background is optional context; a setup step must not depend on it"
        )


def test_setup_md_is_gone_and_the_guide_replaced_it():
    assert not (_ROOT / "SETUP.md").exists()
    assert (_ROOT / "guide" / "setup" / "index.md").is_file()


def test_readme_is_a_landing_page_not_a_manual():
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 180, "README should be a landing page, not an ops manual"
    assert "Deploy your own" in text, "the guide link must be prominent"
    for heading in ("Architecture", "Tech stack", "Testing", "Known limitations", "Cost"):
        assert heading in text, f"{heading} belongs in README, not the guide"


def test_readme_no_longer_carries_the_operations_manual():
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    for moved in ("--sync-config-db", "set_override", "uptime-pinger"):
        assert moved not in text, f"{moved} moved to guide/operations/"


# Hardcoded rather than derived from `git config remote.origin.url`, so the
# assertion still means something in a checkout with no remote (a worktree, a
# tarball, CI on a fork). A rename of the repo or its owner is expected to
# fail this test -- that is the point: _GUIDE_URL is printed to operators, and
# a stale host sends them to someone else's site or a 404.
_EXPECTED_GUIDE_BASE = "https://tovtechorg.github.io/pr-review-bot/"


def test_deploy_points_at_a_guide_page_that_exists():
    """spec section 3d: the CLI prints this to a terminal user, so it must
    resolve to a real page -- on the right host, under the right repo."""
    from scripts import deploy

    assert deploy._GUIDE_URL.startswith(_EXPECTED_GUIDE_BASE), (
        f"_GUIDE_URL must point at this repo's Pages site "
        f"({_EXPECTED_GUIDE_BASE}), got {deploy._GUIDE_URL}"
    )
    path = deploy._GUIDE_URL[len(_EXPECTED_GUIDE_BASE) :].rstrip("/")
    assert path, "_GUIDE_URL must name a page, not just the site root"
    assert (_ROOT / "guide" / f"{path}.md").is_file(), f"no guide page for {path}"


def test_no_script_still_points_at_setup_md():
    """Scans scripts/*.py, root-level tracked *.md files (except ISSUES.md
    and SPEC.md, which legitimately reference SETUP.md as historical/
    design-record content), .env*.example files, and .claude/commands/*.md.

    tests/ is deliberately excluded throughout: this very file contains the
    literal "SETUP.md" in test_setup_md_is_gone_and_the_guide_replaced_it, so
    a scan including tests/ would fail on itself. That self-reference trap has
    now bitten this project three times -- see ISSUES.md (Stage 2 Task 4's
    docstring, and Stage 3a's ast-vs-grep note). Any test that scans source
    for a forbidden string must exclude the file asserting it.
    """
    scanned: list[Path] = list(_ROOT.glob("scripts/*.py"))
    scanned += [
        p for p in _tracked_root_markdown_files() if p.name not in {"ISSUES.md", "SPEC.md"}
    ]
    scanned += list(_ROOT.glob(".env*.example"))
    scanned += list(_ROOT.glob(".claude/commands/*.md"))
    assert scanned
    for path in scanned:
        assert "SETUP.md" not in path.read_text(encoding="utf-8"), f"{path} is stale"


def test_claude_md_points_at_the_guide():
    text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "SETUP.md" not in text
