"""Manual live verification for the vertex provider (providers/*).

Not part of the pytest suite (CI never runs this) -- it depends on a real,
live call to Vertex AI using whatever credential
providers/vertex_credentials.py resolves: VERTEX_GCP_SERVICE_ACCOUNT_KEY, then
implicit ADC (`gcloud auth application-default login`).

Run it directly:

    uv run python -m scripts.manual_verify_vertex

It proves, against real Vertex AI, through the real validate-repair layer:
  1. A structured-output call succeeds and returns a validated instance of a
     tiny test schema (not a bare string).
  2. Real, non-zero token usage (tokens_in/tokens_out) comes back.

ONE deliberate call, run once -- not looped, not repeated across models or
keys (see CLAUDE.md's "LLM API testing hygiene"). If it returns 403 or 429,
stop and investigate via docs rather than retrying.

Resolves key-index slot 0 only: the DB key-index override is a dispatcher-
runtime concern (it is refreshed into a process-local cache per claimed
ticket), and a one-shot CLI has no such cache to read. To verify a different
service account locally, set VERTEX_GCP_SERVICE_ACCOUNT_KEY to its base64 form
(scripts/encode_credential.py).

Never prints the credential. The GCP project id IS printed -- an operator
needs to know which project was billed, and it is not a secret -- but no
private-key material ever is.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import BaseModel

from config import settings
from providers import vertex_credentials
from providers.google_genai import VertexProvider
from providers.pricing import estimate_cost_usd
from providers.validate import validate_and_repair


class Greeting(BaseModel):
    message: str


def main() -> int:
    info = vertex_credentials.resolve_service_account_info(0)
    project = settings.vertex_gcp_project or (info or {}).get("project_id", "")
    source = "service-account key" if info is not None else "implicit ADC (gcloud)"

    print(f"Provider: vertex   Model: {settings.vertex_model}")
    print(f"Credential source: {source}")
    print(f"Project: {project or '(none resolved)'}   Location: {settings.vertex_gcp_location}")
    print("(never printing the credential)")

    if not project:
        print(
            "\nno project to call with: set VERTEX_GCP_PROJECT, or provide a service-account "
            "key via VERTEX_GCP_SERVICE_ACCOUNT_KEY",
            file=sys.stderr,
        )
        return 2

    provider = VertexProvider(
        project=project,
        location=settings.vertex_gcp_location,
        service_account_info=info,
        model=settings.vertex_model,
    )

    system = "Respond in the given JSON schema."
    user = "Say hello in one short sentence."

    print("\nMaking a real, live call through validate_and_repair() ...")
    result = asyncio.run(validate_and_repair(provider, system, user, Greeting))

    print(f"\nok: {result.ok}")
    assert result.ok, f"live call failed: {result.error}"
    assert result.parsed is not None
    assert isinstance(result.parsed, Greeting)

    print(f"parsed: {result.parsed!r}")
    print(f"tokens_in: {result.tokens_in}")
    print(f"tokens_out: {result.tokens_out}")

    assert result.tokens_in > 0, "expected non-zero real prompt token usage"
    assert result.tokens_out > 0, "expected non-zero real completion token usage"

    cost = estimate_cost_usd("vertex", settings.vertex_model, result.tokens_in, result.tokens_out)
    print(f"est. cost: ${cost:.6f}" if cost is not None else "est. cost: n/a (unpriced model)")

    print(
        "\nSUCCESS: live Vertex AI structured-output call verified "
        "end-to-end through validate_and_repair()."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
