# Step 7: Sync config and verify

Step 6 created the service with every var left blank — deliberately, since
hand-typing ~30 of them into a web form (several as long base64 blobs) is
exactly how a typo slips in unnoticed. This is what actually pushes all of
them, correctly, and turns that expected-failing first deploy into a real,
running one, with `RENDER_API_KEY` (Step 6) already set locally:

=== "bash"

    ```bash
    PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m scripts.deploy --sync-env
    ```

=== "PowerShell"

    ```powershell
    $env:PUBLIC_BASE_URL = "https://<your-service>.onrender.com"
    uv run python -m scripts.deploy --sync-env
    ```

See [What `--sync-env` pushes](../reference/sync-env.md) for the exact
push set — it's provider-derived, not a fixed list, so it's documented
there rather than restated here.

## Budget your time

Before triggering anything, `--sync-env` waits for any deploy already in
progress to settle — worst case that's up to 900s waiting for the in-flight
one, plus up to 900s for the one it triggers itself, so **budget up to ~30
minutes** in the rare worst case. In practice, a warm redeploy with nothing
already in flight has taken well under a minute.

## Claude Code shortcut

Claude Code users can run `/deploy` instead, which wraps the same CLI.

## Verify

Before considering this step done:

- The deploy's logs end with uvicorn's `Application startup complete.`
- `https://<your-service>.onrender.com/healthz` returns `{"status":"ok"}`.

## Troubleshooting

If it fails with `error connecting in 'pool-1'` or a `RuntimeError` about the
connection not opening, the usual cause is a Supabase project that was not
ready yet, or a mistyped pooler string (Step 4). Fix the value locally (in
`.env`) and re-run `--sync-env` — it pushes the corrected value and triggers
a fresh deploy the same way.

## Next

Continue to [Step 8: add the keep-warm pinger](08-pinger.md).
