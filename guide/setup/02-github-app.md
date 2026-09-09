# Step 2: Create the GitHub App

The GitHub App is the bot's identity: it's what lets the service read a
pull request's diff and post (and later edit) a review comment. This is
done entirely by hand in GitHub's UI — the same skill you'll need later
anyway to install the App (Step 3, also always a browser step) or to change
its permissions, rotate its webhook secret, or remove it from a repo.

## Create the App

If you don't already have them, start from the committed templates for both
files this project reads config from — `.env` (credentials, this step) and
`.env.config` (operational settings like `GITHUB_TARGET_REPO`, needed from
Step 3 on) — so there's one `cp` to remember instead of two separate prompts
later:

```bash
cp .env.example .env
cp .env.config.example .env.config
```

Go to **Settings → Developer settings → GitHub Apps → New GitHub App** and
fill in the form:

- **GitHub App name** — must be globally unique across all of GitHub, so
  the obvious choice (e.g. `pr-review-engine`) will likely already be
  taken. Pick your own.
- **Homepage URL** — required by the form, but there's no real URL yet:
  the Render URL isn't created until Step 6. Enter the obviously-fake
  `https://example.invalid` rather than a real-looking placeholder that
  could be mistaken for a working link.
- **Webhook → Active** — check it.
- **Webhook → Webhook URL** — same placeholder, with the path this
  project's webhook handler expects: `https://example.invalid/webhook`.
  Step 7 (`scripts/deploy.py`) corrects this to your real URL once it
  exists — that's normal, not something to fix here.
- **Webhook → Webhook secret** — GitHub does not generate this for you;
  type in your own value (any random string long enough to not guess —
  `openssl rand -hex 32` if you have it, or just type something long and
  random by hand). Keep this value visible for a moment; you'll paste it
  into `.env` in the next section. It's easy to miss since nothing about
  the App's settings *page* prompts for it afterward, but the service
  refuses to start without it — it's one of seven required boot
  credentials (see below).
- **Repository permissions** — set exactly:

    | Permission | Access |
    | --- | --- |
    | Contents | Read-only |
    | Issues | Read and write |
    | Pull requests | Read and write |
    | Metadata | Read-only (mandatory; GitHub sets this automatically) |

    Nothing else. `pull_requests`+`issues` write because a PR review
    comment is posted as an issue comment on GitHub's API; `contents` read
    to fetch the diff.
- **Subscribe to events** — check **Pull request** only.
- **Where can this GitHub App be installed?** — **Only on this account.**
  See the warning below for why this matters.

Then click **Create GitHub App**.

Checking boxes by hand is exactly the kind of step a typo survives —
`uv run python -m scripts.doctor`'s `app-permissions` row reads the App's
*actual* permissions and event subscriptions back from GitHub and compares
them against what this project's code needs, so a missed or extra checkbox
doesn't go unnoticed. Run it once you've collected the credentials below.

!!! warning "Keep the App private"
    "Only on this account" above is not a default to leave alone — publishing
    the App to the GitHub Marketplace ("Any account") is a real, clickable
    option on the same form. Setting `GITHUB_TARGET_REPO=*` (step 3) makes
    the bot act on *every* repo the installation covers, and that's only a
    safe choice because only accounts *you* choose can install a private
    App in the first place. A public App would let any third party
    self-install and have their events accepted in that same track-all mode.

!!! warning "Unlike permissions, this one isn't automatically verified"
    `doctor`'s `app-permissions` check (mentioned above) reads the App's
    actual permissions and event subscriptions back from GitHub and catches
    drift — but it does not check whether the App ended up public or
    private. Nothing in this project currently does. If you pick "Any
    account" by mistake here, or someone changes it later in the App's own
    settings, no `doctor` row will ever flag it. The automated script this
    guide used to document made this mistake structurally impossible — it
    hardcoded `public: False` in the manifest it submitted. Creating the App
    by hand trades that guarantee away: getting this one checkbox right, and
    noticing if it's ever wrong later, is on you.

## Collect the credentials into `.env`

Three of this project's seven required boot credentials come from this step
(`DATABASE_URL` comes later, in the Postgres step; the three `DASHBOARD_*`
vars are yours to generate any time — see below):

- **Webhook secret** — the value you just typed into the form → paste it
  into `GITHUB_WEBHOOK_SECRET` in `.env`.
- **App ID** → `GITHUB_APP_ID`. A short integer, near the top of the App's
  **General** settings page (the page you land on right after creating it).
- **Private key** — click **Generate a private key** on that same page to
  download a `.pem` file, then base64-encode it with this project's own
  script — never a raw file path:

    ```bash
    uv run python -m scripts.encode_credential github-app-private-key.pem
    ```

    Paste the output into `GITHUB_APP_PRIVATE_KEY` in `.env` (verbatim, the
    whole base64 string — never a file path).

The same settings page also shows a **Client ID** — easy to grab by
mistake, but this project **does not use it at all**.

One more ID exists — **Installation ID** → `GITHUB_APP_INSTALLATION_ID` —
but it only exists once the App is installed on an account, which is the
next step. **Required** no matter how the App was created — never
auto-discovered or guessed on your behalf.

## Set the dashboard credentials

Unlike the vars above, the three `DASHBOARD_*` vars don't come from GitHub
at all — they're a single shared operator credential you make up yourself,
gating this project's own ops/demo dashboard (`GET /`). All three are
required; the service refuses to boot if any is empty. Set them in `.env`
now, while you're already there:

- `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` — pick any values. See
  `.env.example`'s comment for why a generated, high-entropy
  `DASHBOARD_PASSWORD` is worth using rather than something memorable.
- `DASHBOARD_SESSION_SECRET` — signs the session cookie; not meant to be
  memorable, and must be at least 32 characters (the service refuses to
  boot with a shorter one). Generate one with:

    ```bash
    python -c "import secrets; print(secrets.token_urlsafe(32))"
    ```

## An automated alternative exists, but isn't the documented path here

`scripts/create_github_app.py` drives GitHub's **App Manifest flow**
instead: a browser form POSTs a manifest to `github.com/settings/apps/new`,
you approve it, and the script exchanges GitHub's one-time redirect code for
the App ID, private key, and webhook secret in one round trip, writing them
to `.env` itself. It still works (`uv run python -m scripts.create_github_app
--name your-app-name --help` for its options) and remains fully tested, but
it's no longer the path this guide walks through: automating a one-time
setup step didn't carry its weight against the manual process above, which
needs no coordination between your terminal and your browser (no
localhost/SSH/remote considerations at all — you can do it from any device,
any time), doesn't require trusting an unfamiliar script with real
credentials, and is the same skill you already need for Step 3 and for
maintaining the App afterward. If you use it anyway: run it yourself, never
through an agent — it writes real credentials to `.env`.

## Next

Continue to [Step 3: install the App](03-install-app.md) — GitHub does not
let an App install itself, so that part is always a browser step.
