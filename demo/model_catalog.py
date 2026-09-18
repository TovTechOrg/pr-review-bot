"""The couple of example models each provider family offers in the demo.

Single source of truth for two different demo surfaces that must never
disagree: `demo/providers_mock.py` (the Environment tab's mocked credential
validate/model-catalog calls) and `demo/provider.py` (which model the
mocked PR review actually reports/prices once a reader picks one in the
wizard's LLM frame). Keeping both keyed off this one dict is what makes a
model picked in the Guided-setup dialog or the wizard the same model the
mocked review claims to have used.

Order matters: index 0 of each list is the fallback used whenever no model
was requested, or the requested one doesn't belong to this provider -- kept
as the pair already priced in providers/pricing.py so its cost never renders
blank. The second entry has no pricing.py entry, so picking it renders a
blank cost in the review comment -- a demo would rather show a real
"unpriced" state than a fabricated rate.
"""

from __future__ import annotations

MODELS_BY_PROVIDER: dict[str, list[str]] = {
    "gemini": ["gemini-flash-latest", "gemini-2.5-flash"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "vertex": ["gemini-flash-latest", "gemini-2.5-flash"],
}
