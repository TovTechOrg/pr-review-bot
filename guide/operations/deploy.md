# Deploying and verifying

`scripts/deploy.py` is the one tool for both verifying and performing a
deploy. It is a plain CLI — no editor, assistant, or Claude Code required —
and it always runs from **your own machine**, never inside the Render
container: `scripts/` is not copied into the Docker image, and
`RENDER_EXTERNAL_URL` only exists inside Render's own container, which is
why every invocation below passes `PUBLIC_BASE_URL` explicitly.

## Three modes, mutually exclusive

Passing more than one of these together is refused (`exit 2`) — they are
separate modes, not composable.

- **No flag** — run the full check suite and report. This is the default,
  credential-light way to answer "is everything OK?"
- **`--sync-env`** — push `.env.config` and the active provider's credential
  to the Render service, trigger a redeploy, wait for it to settle, then run
  the full check suite against the result. See
  [What `--sync-env` pushes](../reference/sync-env.md) for the exact,
  provider-derived push set — it is generated from the code, so it is linked
  here rather than restated.
- **`--sync-config-db`** — push only the cooldown and usage-cap settings
  straight into the database, skipping the Render/redeploy machinery
  entirely. Covered in full on [Tuning cooldowns and usage
  caps](tuning.md).
- **`--health-only`** — run only the `health` check: a narrower,
  credential-free "is the service up?" with nothing else. Needs just
  `PUBLIC_BASE_URL`/`RENDER_EXTERNAL_URL`.

=== "bash"

    ```bash
    PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m scripts.deploy --health-only
    ```

=== "PowerShell"

    ```powershell
    $env:PUBLIC_BASE_URL = "https://<your-service>.onrender.com"
    uv run python -m scripts.deploy --health-only
    ```

For a full deploy, swap `--health-only` for `--sync-env` (needs
`RENDER_API_KEY` too):

=== "bash"

    ```bash
    PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m scripts.deploy --sync-env
    ```

=== "PowerShell"

    ```powershell
    $env:PUBLIC_BASE_URL = "https://<your-service>.onrender.com"
    uv run python -m scripts.deploy --sync-env
    ```

Claude Code users can run `/deploy` instead, which wraps the same CLI.

!!! note "Budget your time"
    Before triggering anything, `--sync-env` waits for any deploy already in
    progress to settle (it never stacks a second deploy on top of one still
    building) — worst case that's up to 900s waiting for the in-flight one,
    plus up to 900s for the one it triggers itself, so budget up to ~30
    minutes in the rare worst case. A warm redeploy with nothing already in
    flight has taken well under a minute in practice.

## What gets checked

The full check suite always runs every check and prints one line each, so a
single run surfaces every problem rather than only the first. The check list,
what each one verifies, and which ones are skipped without an operator-local
key are generated from the check registry itself — see [Deployment
checks](../reference/checks.md) rather than a copy here that could drift
from it.

One operator-local key still unskips two checks, and is never set on the
Render service itself: `DATABASE_URL` — it enables `database` and
`provider`. Every other check now runs unconditionally; see [Deployment
checks](../reference/checks.md#unskipping-the-optional-checks).

## Exit codes

| Exit | Meaning |
| --- | --- |
| exit 0 | every check that ran passed (a skipped check never fails the run) — or, for `--sync-env`, env vars were already in sync and no deploy was even triggered |
| exit 1 | at least one check failed, **or** (`--sync-env` only) the sync/deploy itself hit a problem after starting — see the caveat below, these are not the same kind of exit 1 |
| exit 2 | the run never really started, before any change actually landed on Render: two modes passed together; a public base URL is unset; `--sync-env` without `RENDER_API_KEY`; `--sync-config-db` (or the `--sync-config-db` step `--sync-env` runs internally) without `DATABASE_URL`, or with a cooldown that would resolve to something invalid (`factor < 1.0`, `base > cap`, or a non-positive base/cap), or a database error while writing the config to `runtime_config`; or `--sync-env` refusing to push because `runtime_config.provider` is unset or names an unsupported provider (there is no `LLM_PROVIDER` env var to fall back to — set it with `scripts.set_override`), an empty required value, or a Render API error while resolving the service before any push began |

An unpriced model is explicitly **not** an exit-2 cause: it only ever prints
a warning to stderr and the push continues normally (see `pricing` in
[Deployment checks](../reference/checks.md) and
`tests/test_deploy_script.py::test_unpriced_model_warns_and_does_not_fail_the_run`)
— a missing cost estimate is a nice-to-have, not a blocker.

!!! warning "Two different exit 1s for `--sync-env`"
    For the default run and `--health-only`, exit 1 always means "read the
    printed report — one of its rows says `FAIL`." For `--sync-env`,
    exit 1 can also come from the sync/deploy step itself, **before any
    check report is ever printed**: no Render service found under
    `RENDER_SERVICE_NAME`, a push interrupted partway through by an
    exception (some vars already changed — re-run to finish, don't assume
    nothing happened), the wait for an already-in-flight deploy timing out,
    or the triggered deploy itself timing out, failing, or being
    canceled/superseded. In every one of these cases there is no report
    table to go read — the reason is on stderr instead. If `--sync-env`
    exits 1 and you see a check report with a `FAIL` row, that row is what
    to fix; if you see no report at all, read the stderr line instead.

In short: exit 0 means trust the report as-is; exit 1 during the default run
or `--health-only` means read the report for what to fix; exit 1 during
`--sync-env` means check whether a report even printed — if not, the reason
is on stderr; exit 2 always means the run never really started.

## Next

- [Switching providers and API keys](overrides.md)
- [Tuning cooldowns and usage caps](tuning.md)
- [Deploying an image](image-deploys.md)
