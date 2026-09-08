# Switching providers and API keys

`scripts/set_override.py` swaps which already-deployed provider, model, and
credential slot are active — live, with no restart and no redeploy.

```bash
uv run python -m scripts.set_override groq --index 1        # activate groq AND its index-1 slot, together
uv run python -m scripts.set_override groq --index 1 --no-activate   # index only, leave the active provider alone
uv run python -m scripts.set_override groq --clear-index --no-activate  # clear index only, same
uv run python -m scripts.set_override groq                  # activate only, keep the existing index override
uv run python -m scripts.set_override --clear                # clear the provider override
uv run python -m scripts.set_override groq --index 1 --force  # write despite a failed live check
uv run python -m scripts.set_override vertex --model gemini-2.5-flash        # override this provider's model too
uv run python -m scripts.set_override vertex --clear-model --no-activate     # clear the model override only
uv run python -m scripts.set_override --list                 # slot inventory, active index, active model
```

## What this actually writes

This writes a provider override and/or a provider's key-index override to
the `runtime_config` table and takes effect on the **next ticket the
dispatcher claims** — no restart, no redeploy. It writes to whatever
`DATABASE_URL` currently resolves to, so running it against a local `.env`
sets a **local** override only; nothing reaches production unless your
local `DATABASE_URL` happens to be the production one.

Each provider's credential env var can have numbered siblings —
`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ... — provisioned ahead
of time exactly like any other env var (one redeploy, via `--sync-env` or
the Render dashboard, to add a new slot). `vertex` rides the identical
mechanism with a differently-shaped (but still verbatim, base64-encoded, no
file path) credential: `VERTEX_GCP_SERVICE_ACCOUNT_KEY`, `_1`, `_2`, ... — so
`uv run python -m scripts.set_override vertex --index 1` swaps service
accounts with no redeploy and no CLI change.

**Each provider tracks its own key-index independently**, so switching
providers never disturbs the slot chosen for the other two, and **no secret
value is ever written to, read from, or logged by the database — only the
slot's integer index is.**

`--list` prints slot inventory, the active index, and the active model as
names and booleans only — never a credential value — so it is **safe to
run and paste anywhere**, including a chat transcript or a bug report.

## Live verification before writing

It verifies against the **effective** index — whichever index will
actually be active for that provider after the write, not always index 0 —
against the live Render service (when `RENDER_API_KEY` is set), and refuses
by default (`exit 2`; pass `--force` to override) if the target credential
is missing or, for index 0, differs from your local `.env`.

- Clearing the index override with `--no-activate` never verifies at all —
  an operator reaching for this during a key rotation must not be blockable
  by a Render/local mismatch.
- Clearing it while also activating still verifies, against index 0, the
  slot about to become active.
- If your local `DATABASE_URL` isn't the one Render's service actually
  reads (e.g. you're testing against a local database), verification
  against a local `.env` value is skipped automatically, since the write
  cannot affect production either way.

## The read-only counterparts

`scripts/deploy.py`'s `provider`/`provider-live` checks are the read-only
counterparts of the provider override: they confirm the resolved
provider's credential is set locally, and genuinely present on Render,
respectively. `api-key-live` is the read-only counterpart of the key-index
override: it confirms the actively-resolved slot is genuinely present on
Render, catching the exact gap a redeploy-free index flip can introduce —
the DB says index 2, but nobody ever pushed `GROQ_API_KEY_2` to Render. See
[Deployment checks](../reference/checks.md) for the full check list.

## Next

- [Tuning cooldowns and usage caps](tuning.md)
- [Deploying and verifying](deploy.md)
