# Per-push review comments, collapsible by default, reaction-as-webhook-signal

## Problem

Today the bot maintains exactly one PR comment for the life of a PR
(`upsert_comment`/`_find_bot_comment` in `github_app.py`), re-editing it in
place on every push. A `Ticket` row is one-per-PR (`UNIQUE (repo_full_name,
pr_number)` in `review_queue/store.py`), re-armed on each push via
`enqueue_or_update`. This loses per-push history: once a new push's review
overwrites the comment, there is no PR-visible trace of what the previous
push's review said. There is also no way for a rapid run of pushes to show
its own history — every push before the last one to actually get reviewed is
invisibly discarded by the existing cooldown re-arm logic.

## Goals

- One PR comment per qualifying push, so review history for a PR is visible
  as a sequence of timeline comments rather than one comment
  overwritten in place.
- Comments default to collapsed (`<details>`) to avoid visually spamming the
  PR timeline as comments accumulate.
- A push that arrives while an earlier push for the same PR is still
  cooling down/pending discards (supersedes) that earlier push's ticket —
  it never gets reviewed — and this is now visible (the earlier comment is
  edited to say so) instead of silently absorbed into a re-armed row.
- The eyes reaction becomes a signal that the webhook fired, decoupled from
  whether a ticket was durably registered, and reworked to react to
  something that actually exists to react to (see below).

## Non-goals

- No true GitHub reply-threading. Issue comments (what this bot posts) have
  no `in_reply_to`; only PR review (diff-line) comments do. "Reply to the
  push" means a new top-level comment per push, not a threaded reply.
- No live-updating cooldown countdown. The placeholder comment is now
  static text ("Review queued") for its whole time in cooldown; today's
  periodic `append_schedule_notice`/`clear_schedule_notice` mechanism is
  retired rather than adapted.
- No attempt to correlate a `synchronize` push with a human-authored PR
  comment (GitHub gives no such link — see the "Reaction target" section).

## Ticket schema

`tickets` becomes keyed by `(repo_full_name, pr_number, head_sha)` instead of
`(repo_full_name, pr_number)`. One row per qualifying push (`opened`,
`reopened` with a new `head_sha`, `synchronize`, `ready_for_review`).

New nullable column: `discard_reason TEXT` (`'superseded'` or `'cancelled'`),
replacing the single-purpose "is this superseded" concept with a general one
shared by both the supersede path and the PR-closed path (see below).
`notice_not_before` is dropped (no more live schedule notice).

Per this project's existing schema convention ("Declared, not migrated" —
`store.py`'s comment above `_SCHEMA`), this is a declared shape change, not
an in-place migration: `CREATE TABLE IF NOT EXISTS` cannot retrofit a new
unique key or a new column onto an already-provisioned table. **Deploying
this feature requires dropping and recreating the live `tickets` table**,
consistent with existing project convention for schema changes. This
discards any tickets currently `pending`/`deferred`/`running` at deploy
time. The separate `reviews` table (append-only review history, no FK to
`tickets`) is completely unaffected — dashboard review-log history survives
this deploy untouched.

## Webhook flow

### Action filtering (unchanged)

Same trigger set: `opened`, `reopened`, `synchronize`, `ready_for_review`,
plus base-branch retargets. Same repo allow-list check. Same `closed` →
cancel-path split.

### Reaction target

GitHub gives no object representing "a push" that supports reactions — only
the PR/issue itself, issue comments, review comments, or (via a separate
endpoint) commit comments. Every PR always has a body, reachable through the
Issues reactions API, regardless of whether the body text is empty — so:

- **`opened` / `reopened` / `ready_for_review`**: react 👀 to the PR's own
  body via the Issues reactions API (`POST
  /repos/{owner}/{repo}/issues/{pr_number}/reactions`) — this is exactly
  today's `react_eyes_to_pr` behavior, unchanged.
- **`synchronize`**: no reaction. There is no reliable way to tell whether
  "the push was accompanied by a comment" — a `synchronize` delivery and an
  `issue_comment` delivery share no correlating field, so any attempt to
  guess (e.g. timestamp proximity) is a fragile heuristic for a purely
  decorative feature. The placeholder comment posted for the push (below)
  is the visible "webhook fired" signal for this case instead.

Ordering: react (when applicable) happens before the ticket row is
persisted, same as today — a signal decoupled from durable registration,
wrapped in try/except so a reaction failure never blocks the ticket. Because
the reaction target here is always the PR body (never the new placeholder
comment), it does not need the new comment's id to exist first.

### Supersede (discard) on a new push

Before creating the new ticket, look up every non-terminal ticket
(`pending`/`deferred`/`retrying`/`running`) for this `(repo_full_name,
pr_number)`, across any `head_sha`. For each:

```sql
UPDATE tickets
SET discard_reason = 'superseded'
WHERE id = %s AND status NOT IN ('done', 'failed', 'cancelled')
RETURNING discard_reason
```

If this affects a row, edit that ticket's comment to the shared collapsed
template (see "Comment templates" below). If it affects zero rows (the
ticket already reached a terminal status), it's a no-op — that ticket's
comment already shows its real result and is left alone.

This is a **discard of not-yet-completed work**, not an abort of anything
in flight: a `running` ticket keeps executing normally. Setting
`discard_reason` on a running ticket only changes what its *own* completion
step does when it finishes (see "Finalize" below) — it does not stop or
interrupt the orchestrator call already in progress.

### New ticket creation

1. Insert the new ticket row (status `pending`), with `cooldown_level` /
   backoff state seeded from the most recent prior ticket for this PR (see
   "Cooldown carry-over" below).
2. Post its placeholder comment: collapsed, static "⏳ Review queued" (no
   live countdown). Best-effort — wrapped in try/except; a failure here
   leaves the ticket with `comment_id = null`, which `finalize_review` must
   treat as "post fresh" rather than "edit" when it eventually completes.
3. Return 202.

### `reopened` special case

`reopened` carries the PR's *current* `head.sha`. Two situations:

- **`head_sha` differs from the most recent ticket's** — commits landed
  while the PR was closed, then it was reopened. This is a genuinely new
  push; the normal insert-new-ticket-row flow applies, nothing special.
- **`head_sha` matches the most recent ticket's, and that ticket's status is
  `cancelled`** — the unique key `(repo, pr_number, head_sha)` means we
  cannot insert a second row for the same commit. Instead, reset that exact
  row back to `pending` (cooldown re-seeded as level 0 / no backoff — a
  manual reopen is a deliberate action, not push churn) and edit its comment
  from the "cancelled" note back to "⏳ Review queued".
- **`head_sha` matches but status is `done`/`failed`** — that commit was
  already reviewed before closure; no action needed, existing comment
  stands as-is.

### PR closed (cancel path)

Loop over every non-terminal ticket for the PR (any `head_sha`); apply the
same conditional atomic update as supersede, but `discard_reason =
'cancelled'`, and the same collapsed-comment treatment with cancel-specific
wording. One shared code path for supersede and cancel, differing only in
the `discard_reason` value and the message/footnote text it selects.

## Dispatcher: finalize

`finalize_review`'s completion write reorders so the DB decision happens
*before* the network call, not around it:

1. Atomic, fast, no network call inside the transaction:
   ```sql
   UPDATE tickets
   SET status = 'done', ...
   WHERE id = %s AND status = 'running'
   RETURNING discard_reason
   ```
2. Build the comment body from the real review results. If
   `discard_reason` came back non-null, append one trailing `<sub>` footnote
   line, wording keyed by the reason:
   - `superseded` → "A newer push arrived before this finished."
   - `cancelled` → "This PR was closed before this finished."
   If null, no footnote.
3. Make the one GitHub call to post/edit the comment with that already-decided
   content (create if `comment_id` is null from a failed placeholder post,
   edit otherwise).

This correctly handles both orderings of the race between a push's
supersede/cancel write and this ticket's own completion write, without ever
holding a DB lock across the GitHub network call:

- Supersede/cancel commits first → this `UPDATE ... RETURNING` sees the
  reason already set → footnote included.
- This ticket's completion commits first → the later supersede/cancel
  attempt's `WHERE status NOT IN (...)` matches zero rows (already
  terminal) → no-op; this ticket's comment was already posted without a
  footnote, correctly, since nothing had actually superseded/cancelled it
  yet at the moment it finished.

## Comment templates

One shared collapsed-block helper in `formatting.py`, used for every bot
comment (placeholder, discard note, and final results):

```
<details>
<summary>{icon} {short summary}</summary>

{body}
</details>
```

- Placeholder: `⏳ Review queued` / "A review for this push will run
  shortly."
- Superseded: `⏭️ Superseded by a newer push` / "A newer push arrived
  before this finished."
- Cancelled: `⏹️ Review cancelled — PR closed` / "This PR was closed before
  this finished."
- Final results: existing `format_comment` output, now wrapped in the same
  collapsed block, summary line built from existing result data (e.g.
  specialist count / pass-fail), plus the trailing discard-reason footnote
  when present.

Consistent short wording across all three discard-adjacent messages, per
explicit request — no per-status variation beyond the icon/summary line.

## Comment lookup

`upsert_comment`'s PR-wide `_find_bot_comment` marker-scan (the mechanism
that finds "the" single shared bot comment when `comment_id` isn't already
known) is retired. Each ticket owns exactly one `comment_id`, recorded at
creation; every subsequent edit (discard note, final results) targets that
id directly. No more scanning the PR's comment list for the `COMMENT_MARKER`.

## Cooldown carry-over

`_due_after_cooldown`'s escalation currently re-arms `cooldown_level` in
place on the single re-used row. With separate rows per push, a new ticket
seeds its `cooldown_level` (and the backoff math that feeds `not_before`)
from the most recent prior ticket for the same `(repo, pr_number)`,
regardless of that ticket's `head_sha`:

```sql
SELECT * FROM tickets
WHERE repo_full_name = %s AND pr_number = %s
ORDER BY created_at DESC LIMIT 1
```

Same escalate-on-churn / reset-on-cooled-down math as today, just applied
onto the new row instead of in place. This preserves today's anti-spam
backoff behavior across the new per-push ticket boundary.

## Retired

- `append_schedule_notice`, `clear_schedule_notice`, `notice_not_before`
  (no live countdown; static placeholder text instead).
- `_find_bot_comment`'s marker-scan-across-PR-comments path (superseded by
  per-ticket `comment_id`).
- The re-arm branch of `enqueue_or_update` (a new push now always creates a
  new row; there is no existing row to re-arm since the key includes
  `head_sha`).

## Testing considerations

- Race ordering for the atomic `discard_reason` update vs. `finalize`'s
  completion update — both orderings, asserting the correct footnote
  presence/absence in each.
- `reopened` with matching `head_sha` against a `cancelled` row (re-enqueue
  in place) vs. against `done`/`failed` (no-op) vs. a differing `head_sha`
  (normal new-ticket path).
- Cooldown carry-over across two ticket rows (rapid churn still escalates
  backoff; a cooled-down gap still resets to level 0).
- Reaction: present for `opened`/`reopened`/`ready_for_review`, absent for
  `synchronize`.
- Comment-post failure at placeholder time (`comment_id` stays null) →
  `finalize_review` creates rather than edits.
