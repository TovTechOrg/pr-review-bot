# Step 5: Configure an LLM provider

Each specialist call goes through one of three providers: `gemini`, `groq`,
or `vertex`. The active provider and model live in `runtime_config`/
`slot_config` — a live database row, not a Render env var — with **no
default and no env fallback**: the service refuses to start without a
provider set and a matching `slot_config` row for it.

This is why this step comes after [Step 4](04-supabase.md), not before it:
setting it requires a live `DATABASE_URL`, which is now in `.env` from that
step.

## Pick a provider

**Groq is the recommended starting point**: it has a free tier, needs no
card, and is what every live rehearsal of this project has used.

- **Groq** — <https://console.groq.com/keys>. Free tier, no card.
- **Gemini** (AI Studio) — a free API key, but Google's free-tier keys have
  been known to trip an account-level Trust & Safety flag under heavy
  testing; see the guide's *Provider history* page if you hit a persistent
  `403`.
- **Vertex AI** — a GCP service-account identity rather than an API-key
  string; requires GCP billing to be enabled on the project.

## Set it

1. **`.env`** — the credential goes here, in the line the template already
   names for your provider (`GROQ_API_KEY`, `GEMINI_API_KEY`, or
   `VERTEX_GCP_SERVICE_ACCOUNT_KEY`). Paste the value in yourself; nothing
   writes it for you.
2. Activate the provider and its model together, against the `DATABASE_URL`
   you just set:

```bash
uv run python -m scripts.set_override groq --model llama-3.3-70b-versatile
```

This writes `runtime_config.provider` and slot 0's `slot_config` row in one
command, live-verifying the credential in `.env` against the real provider
API first (unless `--force`). Swap `groq`/`llama-3.3-70b-versatile` for
`gemini`/`gemini-flash-latest` or `vertex`/`gemini-2.5-flash` if you picked
a different provider above.

```bash
uv run python -m scripts.doctor
```

`doctor`'s `provider` row confirms the provider you set has a matching
credential in place, without ever printing the credential itself.

Switching providers later is the same command, run again with the new
provider/model — any time, with no redeploy needed (see [Switching
providers and API keys](../operations/overrides.md)).

## Model pricing is optional

A model with no entry in this project's pricing table still runs — the
posted PR comment simply appears without a cost estimate, and
`set_override`'s own check (like `scripts/deploy.py`'s `pricing` check)
reports it as a warning, not a blocker. See [Model
pricing](../reference/pricing.md) for the models this project has verified
rates for.

## Next

Steps 1–5 are done. Continue to
[Step 6: create the Render service](06-render.md).
