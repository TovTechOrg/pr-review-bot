# Step 8: Add the keep-warm pinger

Both Render (free tier) and Supabase (free tier) spin down after inactivity.
A free external pinger keeps both warm.

## Create the monitor

1. Go to <https://uptimerobot.com> (free) — cron-job.org also works, but
   UptimeRobot is what `scripts/deploy.py`'s `uptime-pinger` check verifies
   against.
2. Create a new monitor that pings your Render URL's `/healthz` endpoint:

   ```
   https://<your-service>.onrender.com/healthz
   ```

3. Set the interval to **5 minutes**; anything above 10 lets Render's
   ~15-minute spin-down win.

This keeps Render warm and also keeps Supabase un-paused — the dispatcher
polls the queue continuously, so pinging `/healthz` guarantees activity.

!!! warning "The URL must match exactly"
    A stray trailing character (a comma pasted from prose, for instance)
    404s on every check while the dashboard still shows the monitor firing
    on schedule and looking perfectly healthy. Double-check the URL you
    pasted against the one above, character for character.

## Why `/healthz` answers both GET and HEAD

UptimeRobot's free tier sends `HEAD` rather than `GET`, which is why
`/healthz` answers both verbs.

## Let doctor verify it

`UPTIMEROBOT_API_KEY` is required (not just recommended) — without it,
`doctor`/`deploy`'s `uptime-pinger` check reports `FAIL`, not `SKIPPED`. Set
it locally so `doctor`/`deploy` can verify the monitor's existence and
interval.

## Your first review

This uses the repo you picked, installed the App on, and set as
`GITHUB_TARGET_REPO` back in [Step 3](03-install-app.md) — that setting
only needs to exist on your own machine for this demo, since `seed_demo_pr`
runs entirely locally; it doesn't need to be pushed to Render (the deployed
service's own copy of `GITHUB_TARGET_REPO` is a separate, still-required
narrowing of which installed repos the bot itself acts on). If you skipped
setting it locally, do it now — `uv run python -m scripts.doctor`'s
`gh-auth` and `target-repo` rows will FAIL with the specific account/repo
mismatch if there is one, rather than you finding out from `seed_demo_pr`
failing below.

```bash
uv run python -m scripts.seed_demo_pr
```

This clones the configured test repo, plants known-bad code from
`fixtures/bad_code/`, and opens a real PR against it — which GitHub then
delivers to your deployed Render service as a webhook event.

### What a good result looks like

Within roughly 15 seconds of the PR opening, a single comment appears on it
naming real findings across all three sections (security, performance, code
quality — a section with no findings still renders, just as
"✅ no findings"), with a footer along the lines of:

```
Runtime 11.4s · 4,910 tok in / 780 tok out · est. $0.0021 · provider: groq
```

Runtime and token counts always appear; the cost estimate only appears if
the active model has a priced entry in this project's pricing table — an
unpriced model still runs and reviews normally, just without the `est.`
fragment.

That comment is the whole point of this project: a fresh PR from a fresh
clone, reviewed automatically, no manual step in between.

## Done

All eight steps are complete. `uv run python -m scripts.doctor` should now
report every row `PASS`.
