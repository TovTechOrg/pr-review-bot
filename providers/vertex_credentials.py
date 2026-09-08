"""Resolves the Vertex service-account credential: VERTEX_GCP_SERVICE_ACCOUNT_KEY
(env, index-aware) -> None, meaning "let google-auth discover implicit ADC".

Separate from credentials.py on purpose: that module knows exactly one
credential shape ("one env var, one string") and stays that way for the two
providers that need nothing more. The JSON parsing lives here instead of
complicating it for them.

One index, one meaning everywhere: it selects among the numbered
VERTEX_GCP_SERVICE_ACCOUNT_KEY_{n} env-var slots -- provisioned on Render, exported
locally when a developer wants to test against several different service
accounts (a quota-exhausted one vs. a healthy one) without touching Render
or Supabase at all.

A malformed value raises rather than falling through to implicit ADC: a
corrupt env var must surface as a failure, not silently run the review
against a different account than the operator selected.
"""

from __future__ import annotations

import base64
import json

from providers import credentials


def resolve_service_account_info(index: int) -> dict | None:
    """The parsed service-account key for `index`, or None for implicit ADC.

    None is NOT an error here (unlike an empty gemini/groq credential): it is
    the signal to pass no explicit credentials to genai.Client, which is what
    makes google-auth discover `gcloud auth application-default login`'s local
    ADC file on its own.
    """
    _, b64 = credentials.resolve("vertex", index)
    if not b64:
        return None
    data = json.loads(base64.b64decode(b64, validate=True).decode())
    if not isinstance(data, dict):
        raise ValueError(
            f"GCP service-account credential at index {index} is not a JSON object"
        )
    return data
