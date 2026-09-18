"""The example models each provider family offers in the demo.

Single source of truth for three different demo surfaces that must never
disagree: `demo/providers_mock.py` (the Environment tab's mocked credential
validate/model-catalog calls), `demo/provider.py` (which model the mocked PR
review actually reports/prices once a reader picks one), and -- across the
repo boundary -- the sibling onboarding-wizard project's own
`demo/content.py::GEMINI_MODELS`/`GROQ_MODELS`/`VERTEX_MODELS`, which is
what actually populates the wizard's LLM-frame model <select>. Neither repo
can import the other's Python, so this list has to be kept a byte-for-byte
match with that one by hand -- confirmed missing "gemini-2.5-pro" during a
2026-09-18 end-to-end verification (the wizard offered it, but this catalog
didn't recognize it, so demo_provider_and_model() silently substituted the
priced default instead of what the reader actually picked). If either
project's list changes, update the other in the same sitting.

Order matters: index 0 of each list is the fallback used whenever no model
was requested, or the requested one doesn't belong to this provider -- kept
as the entry already priced in providers/pricing.py so its cost never
renders blank. Every other entry has no pricing.py entry, so picking one
renders a blank cost in the review comment -- a demo would rather show a
real "unpriced" state than a fabricated rate.
"""

from __future__ import annotations

MODELS_BY_PROVIDER: dict[str, list[str]] = {
    "gemini": ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "vertex": ["gemini-flash-latest", "gemini-2.5-flash"],
}
