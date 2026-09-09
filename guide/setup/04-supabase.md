# Step 4: Create the Supabase project

The durable queue lives in Supabase Postgres. `store.py` is psycopg3-only,
so a real reachable Postgres is a hard requirement.

## Create it

1. Create a project at <https://supabase.com>.
2. **Wait until the dashboard reports the project ready** (~2 minutes).

!!! warning "Wait for ready before continuing"
    A connection attempt against a still-provisioning project fails, and
    Render does **not** retry a failed deploy — see Step 7's troubleshooting
    note. Confirm the dashboard shows the project ready before moving on to
    Step 5, which needs a live `DATABASE_URL` to configure the LLM provider.

## Copy the connection string

Open **Connect** (or Project Settings → Database) and copy the
**Session-mode pooler** connection string — port **5432**, not 6543:

```
postgresql://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres
```

!!! warning "Copy it verbatim"
    Do not retype or reconstruct this string. Both the
    `postgres.<project-ref>` username and the region-varying subdomain are
    project-specific, and either one wrong yields
    `FATAL: Tenant or user not found`.

    If the password contains `@ # / ?`, percent-encode it — those characters
    terminate fields in a URI.

## Set it

Set the resulting connection string as `DATABASE_URL` — locally in `.env`
for now; Step 7's `--sync-env` pushes it (and everything else) to Render.

Optional hardening: libpq's default `sslmode=prefer` gets an encrypted
connection but performs no certificate verification. For MITM protection use
`sslmode=verify-full` together with Supabase's CA certificate. The app does
not enforce this.

## Next

Continue to [Step 5: configure an LLM provider](05-llm-provider.md).
