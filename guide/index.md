# PR Review Engine

Open a pull request, and this bot reviews it automatically — checking for
security risks, performance issues, and code-quality problems — then posts
the results as a single comment on the PR itself. Three specialists run in
parallel, and later pushes edit that same comment in place rather than
piling up new ones.

!!! tip "Try it first"

    **[Open the live demo →](demo/?to=bot)** — the engine reviewing a pull
    request on mock data, or **[start from the setup wizard →](demo/)** to see
    how a deployment gets provisioned. Free hosting, so the first load takes
    about a minute.

**Needs:** Python 3.12, [uv](https://docs.astral.sh/uv/), and a free
[Supabase](https://supabase.com) Postgres project — budget about 30 minutes
for a first working review.

<style>
.flow-diagram {
  --fd-stage-bg: #e8eefc;
  --fd-stage-border: #5b7cd6;
  --fd-stage-fg: #1a1a2e;
  --fd-specialist-bg: #eaf7ef;
  --fd-specialist-border: #3fa66d;
  --fd-specialist-fg: #0f2f1c;
  --fd-detail-fg: #4a4a58;
  --fd-arrow: #9aa5b1;
  margin: 2em 0 2.5em;
}
[data-md-color-scheme="slate"] .flow-diagram {
  --fd-stage-bg: #22314f;
  --fd-stage-border: #7b93e0;
  --fd-stage-fg: #e7ecff;
  --fd-specialist-bg: #1e3a2c;
  --fd-specialist-border: #5fce8f;
  --fd-specialist-fg: #dff5e8;
  --fd-detail-fg: #b7bccb;
  --fd-arrow: #6b7686;
}
.flow-diagram .fd-row {
  display: flex;
  align-items: stretch;
  justify-content: center;
  flex-wrap: wrap;
  gap: 0.75em;
}
.flow-diagram .fd-branches {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  justify-items: center;
  gap: 1em;
}
.flow-diagram .fd-node {
  flex: 1 1 200px;
  max-width: 260px;
  border-radius: 12px;
  border: 1.5px solid var(--fd-stage-border);
  background: var(--fd-stage-bg);
  color: var(--fd-stage-fg);
  padding: 0.9em 1.1em;
}
.flow-diagram .fd-node.fd-specialist {
  border-color: var(--fd-specialist-border);
  background: var(--fd-specialist-bg);
  color: var(--fd-specialist-fg);
}
.flow-diagram .fd-title {
  font-weight: 600;
  font-size: 0.95em;
}
.flow-diagram .fd-detail {
  color: var(--fd-detail-fg);
  font-size: 0.85em;
  line-height: 1.35;
  margin-top: 0.5em;
}
.flow-diagram .fd-arrow {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--fd-arrow);
  font-size: 1.3em;
  flex: 0 0 auto;
}
.flow-diagram .fd-arrow-down {
  width: 100%;
  padding: 0.1em 0;
}
.flow-diagram .fd-junction {
  display: flex;
  justify-content: center;
}
.flow-diagram .fd-junction .fd-arrow-down {
  display: none;
}
.flow-diagram .fd-connector {
  display: block;
  width: 100%;
  height: 56px;
}
.flow-diagram .fd-connector path {
  fill: none;
  stroke: var(--fd-arrow);
  stroke-width: 2.5;
  stroke-linecap: round;
}
.flow-diagram .fd-connector marker path {
  fill: var(--fd-arrow);
  stroke: none;
}
@media (max-width: 600px) {
  .flow-diagram .fd-row { flex-direction: column; align-items: stretch; }
  .flow-diagram .fd-arrow:not(.fd-arrow-down) { transform: rotate(90deg); }
  .flow-diagram .fd-node { max-width: none; }
  .flow-diagram .fd-branches { grid-template-columns: 1fr; }
  .flow-diagram .fd-connector { display: none; }
  .flow-diagram .fd-junction .fd-arrow-down { display: flex; }
}
</style>

<div class="flow-diagram">
  <div class="fd-row">
    <div class="fd-node">
      <div class="fd-title">Pull request opened or updated</div>
      <div class="fd-detail">You push code or open a PR — that's the only step you take.</div>
    </div>
    <div class="fd-arrow">&rarr;</div>
    <div class="fd-node">
      <div class="fd-title">Webhook received</div>
      <div class="fd-detail">GitHub calls the bot automatically the moment your PR changes.</div>
    </div>
  </div>

  <div class="fd-arrow fd-arrow-down">&darr;</div>

  <div class="fd-row">
    <div class="fd-node">
      <div class="fd-title">Queued for review</div>
      <div class="fd-detail">The request is saved in line so nothing gets missed, even under heavy traffic.</div>
    </div>
  </div>

  <div class="fd-junction">
    <svg class="fd-connector" viewBox="0 0 300 70" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <marker id="fd-arrowhead" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" />
        </marker>
      </defs>
      <path d="M150,0 L150,24" />
      <path d="M150,24 C150,40 50,40 50,68" marker-end="url(#fd-arrowhead)" />
      <path d="M150,24 L150,68" marker-end="url(#fd-arrowhead)" />
      <path d="M150,24 C150,40 250,40 250,68" marker-end="url(#fd-arrowhead)" />
    </svg>
    <div class="fd-arrow fd-arrow-down">&darr;</div>
  </div>

  <div class="fd-row fd-branches">
    <div class="fd-node fd-specialist">
      <div class="fd-title">Security</div>
      <div class="fd-detail">Looks for risky code, like exposed secrets or unsafe input handling.</div>
    </div>
    <div class="fd-node fd-specialist">
      <div class="fd-title">Performance</div>
      <div class="fd-detail">Flags code that could run slowly or waste resources.</div>
    </div>
    <div class="fd-node fd-specialist">
      <div class="fd-title">Code Quality</div>
      <div class="fd-detail">Suggests cleaner, easier-to-maintain code.</div>
    </div>
  </div>

  <div class="fd-junction">
    <svg class="fd-connector" viewBox="0 0 300 70" preserveAspectRatio="none" aria-hidden="true">
      <path d="M50,2 C50,30 150,30 150,46" />
      <path d="M150,2 L150,46" />
      <path d="M250,2 C250,30 150,30 150,46" />
      <path d="M150,46 L150,70" marker-end="url(#fd-arrowhead)" />
    </svg>
    <div class="fd-arrow fd-arrow-down">&darr;</div>
  </div>

  <div class="fd-row">
    <div class="fd-node">
      <div class="fd-title">Findings combined</div>
      <div class="fd-detail">All three reports are merged into one clear summary.</div>
    </div>
  </div>

  <div class="fd-arrow fd-arrow-down">&darr;</div>

  <div class="fd-row">
    <div class="fd-node">
      <div class="fd-title">Posted as a PR comment</div>
      <div class="fd-detail">The summary appears directly on your pull request — no dashboard required.</div>
    </div>
  </div>
</div>

## What a review looks like

A real posted comment, condensed to one finding per specialist:

```markdown
## 🤖 Automated Code Review — PR #42
_3 specialists · llama-3.3-70b-versatile (groq) · 4.2s · ~$0.0021_

### 🔒 Security — 1 finding
| Severity | Line | Issue | Suggested fix |
| --- | --- | --- | --- |
| 🔴 critical | `app/auth.py:88` | API key is logged in plaintext when the request fails | Log only the key's length/hash, never the raw value |

### ⚡ Performance — 1 finding
| Impact | Line | Issue | Suggestion |
| --- | --- | --- | --- |
| 🟡 medium | `app/api/users.py:145` | N+1 | Batch these lookups into a single query |

### 🧹 Code Quality — 1 finding
| Category | Line | Issue | Refactoring suggestion |
| --- | --- | --- | --- |
| duplication | `app/utils/format.py:22` | Date-formatting logic is duplicated across three modules | Extract a shared helper |

---
<sub>Runtime 4.2s · 1,842 tok in / 612 tok out · est. $0.0021 · provider: groq</sub>
```

If a specialist's own check fails outright, its section says so plainly
instead of vanishing — partial failure is always visible, never silent.

## The one command to remember

```bash
uv run python -m scripts.doctor
```

Run it any time, from a fresh clone or mid-setup. It answers three
questions: where am I, what's missing, and what's next — without ever
mutating anything.

**[Get started →](setup/index.md)**
