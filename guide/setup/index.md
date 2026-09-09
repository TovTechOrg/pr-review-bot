# Setup

Getting from a fresh clone to a first posted review comment is eight steps.

## The eight steps

1. **Install prerequisites** — Python, `uv`, git, and a way to reach Postgres.
2. **Create the GitHub App** — the identity the bot uses to read PRs and post
   comments.
3. **Install the App on your repo(s)** — a browser-only step; GitHub does not
   let an App install itself.
4. **Create the Supabase project** — the durable queue Postgres.
5. **Configure an LLM provider** — pick a provider, get a key, set it.
6. **Create the Render service** — where the bot actually runs.
7. **Sync config and verify** — push everything to Render in one command,
   deploy, and confirm it's live.
8. **Add the keep-warm pinger** — a free external monitor that keeps both
   Render and Supabase's free tiers from spinning down.

Run `uv run python -m scripts.doctor` at any point — it tells you which
of these are still outstanding.

## The one command to remember

```bash
uv run python -m scripts.doctor
```

Run it any time, from a fresh clone or mid-setup. It answers three
questions: where am I, what's missing, and what's next — without ever
mutating anything.

Continue to [Step 1: install prerequisites](01-prerequisites.md).
