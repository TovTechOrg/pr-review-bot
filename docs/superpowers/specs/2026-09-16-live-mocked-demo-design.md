# Live mocked demo: wizard + bot (2026-09-16)

A publicly linkable demo, reachable from a LinkedIn article, that lets a
reader click through the onboarding wizard and then watch a pull request get
reviewed -- running this project's real code the whole way, with every
external integration replaced by a mock and no real credential anywhere in
the deployment.

## What this is for

The article needs a link a stranger can open and understand in under a
minute. Both halves of this project are already deployed and live, but
neither is demonstrable to an anonymous reader: the wizard provisions real
infrastructure and asks for real credentials, and the bot only does anything
visible when a real pull request arrives in a repository its GitHub App is
installed on. A reader has no way to see either one work.

## Why live-and-mocked rather than recorded

A recording would be more reliable and was the obvious first answer. It was
rejected because clicking through software yourself is a categorically
different experience from watching someone else do it, and the whole
persuasive claim of this project -- that setup is genuinely self-service --
is one a reader can only evaluate by doing it.

The cost of "live" is normally reliability and exposure: free-tier cold
starts, abuse surface, per-visitor cost, and real credentials sitting on a
service strangers are invited to poke at. Mocking every integration removes
all four at once. There is nothing to spend, nothing to rate-limit, nothing
to leak, and nothing that can fail because a third party is down.

## Non-goals

- **Not a second implementation.** The demo reuses the real webhook handler,
  orchestrator, specialists, formatter, dispatcher, wizard router, and
  dashboard. Only the integration boundary is swapped.
- **Not full wizard fidelity.** The real wizard drives seven external
  integrations. The demo walks four steps and collapses the rest (see below).
- **Not a sandbox for reader-supplied input.** The reviewed diff is fixed.
  Readers do not submit code, repositories, or credentials.
- **Not persistent.** Nothing a reader does is stored beyond their own
  session, and sessions are evicted.

## Scope: two repositories, one story

The work spans `~/pr-review-bot` (this repo -- review engine plus dashboard)
and `~/onboarding-wizard` (the sibling repo -- the setup wizard). Each grows
its own mock adapters and its own `Dockerfile.demo`, deployed as its own
Render service:

| Repo | Real service | Demo service |
|---|---|---|
| `pr-review-bot` | `pr-review-engine` | `demo-pr-review-engine` |
| `onboarding-wizard` | `onboarding-wizard` | `demo-onboarding-wizard` |

Deliberately **not** a new repository. A forked demo codebase would duplicate
the webhook flow, orchestrator, dashboard UI, and wizard steps, then drift
from them silently -- and a demo that misrepresents the product is worse than
no demo. Keeping the demo in the same repositories means it is built from the
same source as the thing it depicts, and the only question is whether the
mock adapters keep pace (see *Testing and CI*).

Equally deliberately **not** a runtime `DEMO_MODE` flag on the existing
services. Demo wiring is selected by a separate build target, so there is no
env var that could be flipped in production and no code path where real
credentials and mock adapters coexist.

## Architecture: mock adapters at the integration boundary

Both repos already isolate their external calls behind narrow modules. The
demo swaps exactly those:

| Repo | Real | Demo |
|---|---|---|
| bot | `providers/{gemini,vertex,groq}.py` | `providers/mock.py` |
| bot | `github_app.py` | `github_app_mock.py` |
| bot | `render_client.py` | `render_client_mock.py` |
| bot | `review_queue/store.py` | in-memory `MockStore` |
| wizard | `render_client.py`, `github_client.py`, `llm_client.py` | mock equivalents |
| wizard | `session_store.py` | in-memory session store |

This is the same swap the provider layer was designed for -- `providers/mock.py`
is structurally just another adapter alongside the three real ones.

`supabase_client.py` and `uptimerobot_client.py` need no mock: the only steps
that call them are the ones the trimmed flow collapses, so the demo build
never invokes them.

## The reader's path, end to end

1. The article links to a **launcher page** on the existing GitHub Pages docs
   site. It shows an honest "starting the demo..." state, wakes the service,
   polls until healthy, and redirects.
2. **Wizard, four live steps**: connect Render -> pick an LLM provider ->
   GitHub App -> Finish & Deploy. A **"skip to the dashboard"** link is
   present throughout, so none of this is mandatory.
3. **Finish & Deploy** plays a single provisioning animation, then redirects
   to the demo bot's dashboard, passing the chosen provider as a query
   parameter.
4. **Dashboard login**, pre-filled, against the real auth check -- but only
   for readers whose browser will store a session cookie. The dashboard
   session *is* a cookie, so in a cookie-blocked or cookie-partitioned
   context (LinkedIn's in-app browser, private modes) this step does not
   happen at all: the dashboard renders without the auth round trip rather
   than trapping the reader against a form that cannot succeed. See
   *Cookie-hostile environments degrade, never break* below. When it does
   happen, the provider query parameter must survive the round trip -- the
   reader arrives with it before authenticating, and the review that reports
   it is not generated until after.
5. **The dashboard is already populated**: on the first *authenticated* load
   for a session, the service fires its own signed webhook delivery
   internally, so the review is waiting rather than requiring the reader to
   ask for it.
6. A closing **"deploy your own for real"** call to action links to the real
   wizard.

Steps 2 and 3 are skippable and the dashboard is reachable directly. A reader
arriving at the demo bot's URL with no wizard session and no provider
parameter gets a working dashboard on a default provider, plus a link back to
the wizard for the setup story they skipped. The sequence above is the
intended narrative, not a prerequisite -- someone who only wants to see a
review should never have to click through provisioning to reach one.

## The wizard side (trimmed)

Four steps stay live, with mocked validation that always succeeds: Render
connect, LLM provider pick (mocked model listings for Gemini/Groq/Vertex),
GitHub App, and Finish & Deploy.

Everything else -- Supabase key validation and project creation, the
UptimeRobot monitor, dashboard-auth confirmation, the bulk env-var push, and
the deploy trigger/status poll -- collapses into the single provisioning
animation. Those steps matter enormously when provisioning real
infrastructure and mean nothing to a reader watching fake infrastructure get
provisioned; building seven mock integrations to animate them would be build
and maintenance cost spent on detail nobody will notice.

The provider the reader picks is carried to the dashboard in the redirect
URL. The two demo services sit on different `.onrender.com` subdomains and
cannot share a cookie, and a query parameter avoids coupling the wizard to
the bot's internals. It is the one choice the reader actually makes, and the
review they land on reports it.

A visible **"start over"** control resets the session (the wizard already
exposes `/api/session/reset`), and a **"skip to the dashboard"** link lets an
impatient reader jump straight to the payoff.

### The GitHub App step, which cannot be faked in place

`github_client.py` validates an App the visitor created **by hand on
github.com** -- App creation and installation are manual by design, following
the removal of the manifest flow. There is no API that can simulate that page,
and there is no real App or testbed repository behind this demo to validate
against.

The step therefore keeps its real instructional UI -- the permission list and
the explanation of what the App needs -- and adds a prominent **"use demo
credentials"** control that fills in fake values and reports them validated
without leaving the page. The instructions are worth keeping because they are
the honest answer to "how much work is this really?", which is exactly what a
reader evaluating the project wants to know.

## The bot side

### Boot without a database or a real GitHub

`main.py`'s lifespan currently requires a set of populated settings values, a
reachable Postgres, and a live GitHub API call
(`github_app.discover_and_verify_installation_id`).

Those settings are not a `.env` requirement. `config.py` declares
`env_file=(".env", ".env.config")`, but real environment variables take
precedence and the deployed image contains no `.env` at all -- production
values are env vars set on the Render service. The distinction matters here
because it decides where the demo's fake values live.

The demo build satisfies all three locally: fake-but-present settings values,
a mocked installation-id check, and `MockStore` in place of
`store.init_pool()`, preloaded with static valid runtime config so every
boot-time validity gate passes on its own merits rather than being bypassed.

Those fake values ship **in the image** rather than being configured on the
Render service, which is worth doing deliberately: a demo deployment then has
no env-var configuration to get wrong, and "no real credentials anywhere"
becomes a property of the build rather than a promise about how someone set
the service up.

The webhook path stays real. The dashboard's first load for a session
constructs a webhook delivery and POSTs it to the service's **own `/webhook`
route**, correctly HMAC-signed with the demo's own webhook secret. It runs
the real signature verification, the real delivery dedup, the real 202, the
real ticket enqueue, and the real dispatcher -- because the claim the demo is
making is "this is the engine", and a bypass route would quietly make that
claim false.

Repeat loads within a session are absorbed by the **existing** delivery dedup:
the synthesized delivery id is derived from the session, so a refresh is a
`200` no-op returning the same review rather than a duplicate ticket. No new
idempotency logic is written.

Running Postgres inside the demo container was considered and rejected: it
fits in the free tier's 512 MB, but free managed Render Postgres self-deletes
after 30 days, and an in-memory store is simpler than a second process for
data that is meant to evaporate anyway.

### MockStore: what it must cover, and what it deliberately does not

`review_queue/store.py` exposes 45 public functions. `MockStore` implements
the subset the demo's own code paths reach -- boot and config getters, enqueue
and claim, finalize and record, the four dashboard query functions -- and
stubs the rest.

The deferral and cooldown paths (`defer_rate_limited`, `defer_usage_capped`,
`defer_failed`) are reachable in principle but never exercised, because the
mock provider never returns 429 and never fails. They are stubbed rather than
reimplemented; faithfully simulating Postgres deferral semantics for a code
path a demo cannot reach is not worth the maintenance.

That "implements what is reached today" boundary is precisely what rots when
the real store grows. See *Testing and CI*.

### Per-session isolation without per-session storage

Several readers may arrive at once, and each should see their own review --
reporting their own chosen provider -- not a shared list accumulating
everyone's runs.

Each ticket is tagged with the visitor's session id, and the dashboard's
review list is **filtered at read time** to the current session. The store
and dispatcher stay single and shared, needing no partitioning and no
awareness that sessions exist. Idle sessions are evicted on a periodic sweep
so a public link cannot grow the process's memory without bound.

## The dashboard

The login screen stays, with the demo credentials pre-filled and the **real**
`dashboard/auth.py` check running against them. The fields are `readonly` --
not `disabled`, which would exclude them from submission and render greyed
out. Nobody in a demo wants to type credentials, so editability buys nothing
and adds a way to get stuck; and a reader who tries to type their own is
usefully reminded what this is.

The Environment panel stays visible, populated by `render_client_mock.py`
with plausible fake entries, masked exactly as the real dashboard masks them.
No real Render API key exists on the demo service, so there is nothing real to
display. **Save** controls render their normal success state and mutate
nothing; a reload returns the canned defaults.

## Canned content

A fixed, synthetic diff against **`bot-demo/example-app`** with exactly one
deliberate finding per specialist -- security, performance, code quality --
mirroring the example already in `README.md`. One finding each is enough to
show what each specialist is for and scans in seconds.

The findings are fixed rather than randomized: a demo that produces the same
polished result every time cannot embarrass itself, and reproducibility makes
the screenshots in the article match what readers see.

## Hosting and instance-hours

### Why nothing is kept warm

Render grants **750 free instance-hours per workspace per calendar month**,
shared across all free services, and exceeding it suspends every free web
service in the workspace until the next month. A calendar month is roughly
730 hours, so the free tier accommodates exactly one continuously-warm
service. Spun-down services consume nothing.

The consequence is that **no demo service may be pinned warm**, and the
existing pingers on the real services should be removed. Instance-hours are
consumed while a service is awake, not per visitor -- concurrent readers share
one instance -- so usage-based consumption for a bursty article launch is on
the order of tens of hours per month, against a budget that a single warm
service would exhaust entirely.

> **Open verification.** The account currently runs two free services pinned
> warm, which should have tripped this cap and apparently has not. One of the
> premises is wrong -- workspace scoping, enforcement, or the instance plans
> themselves. Confirm against `dashboard.render.com/billing#included-usage`
> before relying on the arithmetic above.

### The launcher

Because nothing is warm, a reader's first request pays a ~1 minute cold
start, and Render's router serves that wait before the application can say
anything. The article therefore links to a **launcher page on the existing
GitHub Pages docs site** -- free, always on -- which shows a branded
"starting the demo..." state, wakes the service, polls, and redirects. If the
service does not come up within a sensible timeout, it says so honestly and
links to the repository rather than spinning forever.

The launcher is an isolated component that the demo's internals do not depend
on. If always-on hosting becomes available, the launcher is deleted and
nothing else changes.

### What choosing this costs elsewhere

Unpinning the real bot service stops its dispatcher polling, which is what
currently keeps the production Supabase project from auto-pausing after 7
days of inactivity. That pause is accepted: the project is restorable, and
the service is only used for occasional testing. It is recorded here because
the dependency is not obvious from either codebase -- database liveness is
presently an accidental side effect of a web service being kept awake.

## Cross-cutting decisions

**Mobile is the primary viewport.** The article's readers arrive
overwhelmingly from a phone, much of it through LinkedIn's in-app browser,
while the wizard and especially the dashboard's data tables were built for
desktop operator use. Mobile rendering is an acceptance criterion for every
demo screen, verified with the `ui-visual-review` skill, and layout fixes are
in scope. This is the largest single quality risk in the project: a review
table that overflows horizontally is the entire impression a reader leaves
with.

**Cookie-hostile environments degrade, never break.** Every isolation
guarantee above assumes a session cookie, and the audience arrives from the
one application most likely to partition or block them. When no cookie can be
set, the demo falls back to a stateless shared view -- same canned review,
default provider -- rather than erroring.

This explicitly overrides the login gate, and the interaction is worth
stating because it is otherwise a contradiction an implementer would hit
head-on: the dashboard session *is* a cookie, so a reader who cannot store one
can never get past a login screen, no matter how correct the credentials. In
cookie-less mode the demo dashboard renders without the auth round trip
rather than looping a reader forever against a form that cannot succeed. This
is safe only because the demo guards nothing -- every value behind that gate
is synthetic -- and it must not be mistaken for a pattern the real dashboard
could adopt.

**Language.** Both products are fully bilingual EN/HE with RTL and a
persisted toggle. The demo keeps the toggle live and translates its own new
strings (banner, login hint, restart control, launcher) -- five to eight of
them. The default is English, matching the article. The **findings stay
English** in both languages, which is authentic rather than a gap: review
output is LLM-generated English regardless of UI language.

**Labelling.** Both demo services carry a persistent banner stating that this
is a demo running on mock data with no real GitHub or LLM calls. A stranger
following a link should never have to wonder whether they are being asked for
a real credential.

**Analytics** are structured log lines recording the furthest step reached,
read from Render's logs during the launch window. No database, no
third-party script, no stored identifiers. The only decision the numbers
inform is whether the article landed, which is a launch-window question;
durable year-scale counts are not a decision input, and are not worth putting
a real credential back onto a deliberately credential-free service.

## Safety: a service with no credentials

The demo's strongest property is that it holds nothing worth stealing. No
real API keys, no database URL, no GitHub App private key, no Render API key.
Its `Settings` values are fake-but-present strings; its webhook secret is real
only in the sense that the service signs and verifies against itself.

`Dockerfile.demo` and `.dockerignore` must exclude every secret-bearing file
that exists in the working tree -- the GitHub App PEM, the testbed PEM, and
the Vertex service-account JSON. This holds regardless of the demo being
mocked: the rule is that those files do not enter an image, not that this
particular image would not use them.

## Testing and CI

**Signature parity.** A test asserts `MockStore` covers the real `store`
module's public surface. This is the guard against the specific way this
design rots: the real store grows a function, the mock silently lacks it, and
the failure surfaces months later to a stranger following a link from an
article. A full behavioural conformance suite parameterized over both
backends is the gold-standard alternative and is disproportionate here.

**End-to-end demo smoke test** in CI, driving webhook -> dispatcher ->
dashboard-renders-review, catching flow breakage that signature parity cannot
see.

**Image boot check.** Both `Dockerfile.demo` builds get their own
`deploy-verify`-style boot smoke test in CI, mirroring the existing check on
the real images -- the same build/packaging gap a green `pytest` run cannot
catch.

**Weekly health check.** A scheduled workflow runs a short Playwright script
that wakes the demo, logs into the dashboard, and asserts the canned finding
text renders. Nothing else in this design notices a *deployed* demo breaking;
CI only sees build time. Scope is deliberately the dashboard payoff screen
rather than all four wizard steps -- a cron coupled to every step's markup
breaks constantly and gets ignored, which is worse than no check. Cost is
roughly 1.5 instance-hours per month.

## Follow-ups outside this design

- ~~Remove the two UptimeRobot pingers on the real services.~~ **Done
  2026-09-16** -- both paused in UptimeRobot.
- Correct `cost.md`, which now carries two suspect claims: the "$0 on free
  tiers + external pinger" model, which the 750-hour cap contradicts for more
  than one warm service, and the note that Supabase pausing is "mitigated by
  dispatcher polling", which holds only while the bot is running.
- Revisit hosting if TovTech-hosted infrastructure becomes available. That
  would swap the hosting layer only: the launcher becomes unnecessary and
  nothing else in this design changes.
