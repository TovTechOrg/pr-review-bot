# Step 4: Configure an LLM provider

Each specialist call goes through one of three providers: `gemini`, `groq`,
or `vertex`. `LLM_PROVIDER` has **no default** — the service refuses to
start without it set to one of those three values.

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

Two edits, by hand, in the two files Step 2 already had you `cp` from their
templates:

1. **`.env.config`** — operational, not a secret, safe to open directly.
   `LLM_PROVIDER` already defaults to `groq`; leave it as-is, or change the
   line to `gemini`/`vertex` if you picked a different provider above.
2. **`.env`** — the matching credential goes here, in the line the template
   already names for your provider (`GROQ_API_KEY`, `GEMINI_API_KEY`, or
   `VERTEX_GCP_SERVICE_ACCOUNT_KEY`). Paste the value in yourself; nothing writes it
   for you, and nothing needs to read it back to confirm it — the next
   command does that.

```bash
uv run python -m scripts.doctor
```

`doctor`'s `llm-provider` row confirms the provider you set has a matching
credential in place, without ever printing the credential itself.

Switching providers later is the same two edits — change `LLM_PROVIDER` in
`.env.config` and make sure the new provider's credential is set in `.env` —
any time, with no script to re-run.

## Model pricing is optional

A model with no entry in this project's pricing table still runs — the
posted PR comment simply appears without a cost estimate, and
`scripts/deploy.py`'s `pricing` check reports it as a warning, not a
blocker. See [Model pricing](../reference/pricing.md) for the models this
project has verified rates for.

## Next

Steps 1–4 are done. Continue to
[Step 5: create the Supabase project](05-supabase.md).
