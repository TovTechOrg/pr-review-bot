"""Set or clear the DB-backed provider override and/or a provider's
API-key-slot index or model override -- in one combined write+verification
pass when several are given.

    uv run python -m scripts.set_override groq
    uv run python -m scripts.set_override --clear
    uv run python -m scripts.set_override groq --index 1
    uv run python -m scripts.set_override groq --index 1 --no-activate
    uv run python -m scripts.set_override groq --clear-index
    uv run python -m scripts.set_override groq --clear-index --no-activate
    uv run python -m scripts.set_override vertex --model gemini-2.5-flash
    uv run python -m scripts.set_override vertex --model gemini-2.5-flash --no-activate
    uv run python -m scripts.set_override vertex --clear-model --no-activate
    uv run python -m scripts.set_override --list

--list prints names and booleans only -- which slots exist locally, which
exist on Render, the active index, and the active model -- and never a
credential value, so it is safe to run and to paste anywhere (including into
an agent's own transcript).

--model warns, rather than refuses, a value with no providers/pricing.py
rate-table entry for this provider -- naming the models that ARE known -- and
still sets the override: an unpriced model runs fine, it simply produces no
cost estimate on the review comment (providers/pricing.py::
estimate_cost_usd returns None; design spec 2026-08-18 section 6b).

The model override is per-provider, not global: setting one only changes the
model used when that specific provider is active, so flipping the active
provider carries each one's own correct model along with it. This is what
makes `set_override.py vertex` safe even though gemini and vertex draw from
different model catalogs -- vertex's own override travels with it, rather
than leaving whatever model the previously-active provider had configured.

Full replacement for scripts/set_provider.py and scripts/set_api_key.py --
see docs/superpowers/specs/2026-08-12-override-cli-unification-design.md
section 5 for the complete mapping table. Both older scripts are temporary
and are NOT modified by this script's existence; they are deleted
separately, after the presentation this was built for.

Verifies against the EFFECTIVE index -- whatever will actually be active
for this provider after the write, not always index 0 -- via
scripts._override.verify_render_slot. This fixes a latent gap in
scripts/set_provider.py, which always verified index 0 regardless of any
existing key-index override for that provider.

Verification is intentionally asymmetric around --clear-index: paired with
--no-activate (clearing the index override without touching which provider
is active), it never verifies at all -- matching old set_api_key.py's
--clear, which also never checked a credential before clearing one.
Reverting to the default slot is exactly the operation an operator reaches
for during a key rotation, precisely when a Render/local mismatch is most
likely, so a clear must not be blockable by a verification failure.
--clear-index WITHOUT --no-activate still verifies (against index 0, the
slot about to become active) because that combination puts a provider into
production and checking its target credential first is a genuine, worthwhile
check.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

import render_client as _render
from config import settings
from providers import active_model, pricing, registry
from review_queue import store
from scripts import _override
from scripts._prereqs import _looks_like_local_test_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_override",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/deploy.py,
        # scripts/set_provider.py, and scripts/set_api_key.py all carry the
        # same guard after an identical abbreviation match fired a live
        # production sync.
        allow_abbrev=False,
        description=(
            "Set or clear the DB-backed provider override and/or a provider's "
            "API-key-slot index or model override."
        ),
    )
    parser.add_argument(
        "provider",
        nargs="?",
        help=f"one of: {', '.join(sorted(registry.PROVIDERS))}",
    )
    parser.add_argument(
        "--index", type=int, help="set this provider's key-index override to N"
    )
    parser.add_argument(
        "--clear-index", action="store_true", help="clear this provider's key-index override"
    )
    parser.add_argument(
        "--model", help="set this provider's model override to NAME"
    )
    parser.add_argument(
        "--clear-model", action="store_true", help="clear this provider's model override"
    )
    parser.add_argument(
        "--no-activate",
        action="store_true",
        help="only touch the key-index override; leave the active-provider override untouched",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="clear runtime_config.provider (must be used alone) -- there is no "
        "env fallback, so this leaves the service unable to boot until a new "
        "provider is set",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "write despite a failed live-verification refusal, or despite "
            "--model naming a model with no pricing-table entry"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="show each provider's slots, active index, and active model (must be used alone)",
    )
    return parser


def _refuse_local_test_db() -> bool:
    """True (having already printed a refusal) if DATABASE_URL points at a
    local/test Postgres. Same risk deploy.py's --sync-config-db guards
    against (ISSUES.md Parked Issues): a shell that ran
    `eval "$(uv run python -m scripts.test_db)"` earlier has a throwaway
    localhost:5433 URL sitting in os.environ, which Settings reads ahead of
    any .env file -- this would silently write the override into that local
    container while an operator believes production was updated."""
    if not settings.database_url or not _looks_like_local_test_db(settings.database_url):
        return False
    host = urlsplit(settings.database_url).hostname or "?"
    print(
        f"refusing to write: DATABASE_URL points at {host}, a local/test Postgres -- "
        "this would write the override into a database on this machine, not production. "
        "This is almost certainly a shell where "
        '`eval "$(uv run python -m scripts.test_db)"` was run; `unset DATABASE_URL` '
        "(or use a fresh shell) and re-run.",
        file=sys.stderr,
    )
    return True


def _print_inventory() -> int:
    """Per provider: which key slots exist locally, which exist on Render, the
    active index, and the active model.

    NAMES AND BOOLEANS ONLY. Every value this touches -- a local slot value, a
    Render env-var value -- is reduced to presence before anything is printed,
    per scripts/_render.py::env_vars()'s contract. This is what lets an agent
    answer "is --index 2 valid?" without ever opening .env, which it may not do.

    Partial picture for vertex specifically: "local slots" here only reflects
    VERTEX_GCP_SERVICE_ACCOUNT_KEY (and its numbered siblings) being set -- it says
    nothing about implicit ADC (`gcloud auth application-default login`),
    which providers/vertex_credentials.py also accepts as a valid way to
    authenticate. So vertex can print "local slots -" here even when a
    working ADC setup exists. This is deliberately not fixed by adding
    ADC-detection logic -- see the printed caveat on vertex's own line below.
    """
    render_keys: set[str] = set()
    render_note = ""
    if settings.render_api_key:
        try:
            service_id = _render.find_service_id()
            if service_id is None:
                render_note = f"(no Render service named {settings.render_service_name})"
            else:
                render_keys = {
                    key for key, value in _render.env_vars(service_id).items() if value
                }
        # deliberate: inability to reach Render degrades to a note, never a failure
        except Exception as exc:  # noqa: BLE001
            render_note = f"(could not reach Render: {type(exc).__name__})"
    else:
        render_note = "(no RENDER_API_KEY; local slots only)"

    index_overrides: dict[str, int] = {}
    slot_configs: dict[tuple[str, int], dict] = {}
    if settings.database_url:
        try:
            store.init_pool()
            index_overrides = store.get_all_key_index_overrides()
            slot_configs = store.get_all_slot_configs()
            active_model.set_override_cache(
                {key: config["model"] for key, config in slot_configs.items() if config["model"]}
            )
        # deliberate: the DB being unreachable degrades to "env values", never a failure
        except Exception as exc:  # noqa: BLE001
            render_note = f"{render_note} (DB unreachable: {type(exc).__name__})".strip()

    if render_note:
        print(render_note)
    for provider in sorted(registry.PROVIDERS):
        base, _ = registry.PROVIDERS[provider]
        local = ((0,) if getattr(settings, base.lower(), "") else ()) + (
            _override.local_slot_indices(base)
        )
        # Derived from the names Render actually reports, not a scanned index
        # range: a range would silently stop reporting slots past its bound,
        # and "no slot 12" reads identically to "slot 12 not checked".
        slot_pattern = re.compile(rf"^{re.escape(base)}(?:_(\d+))?$")
        hosted = sorted(
            int(match.group(1) or 0)
            for key in render_keys
            if (match := slot_pattern.match(key))
        )
        active_index = index_overrides.get(provider, 0)
        index_source = "override" if provider in index_overrides else "default"
        model_source = (
            "override" if (provider, active_index) in slot_configs else "unconfigured"
        )
        # vertex-only caveat: "local slots" above only reflects
        # VERTEX_GCP_SERVICE_ACCOUNT_KEY (and numbered siblings), never implicit ADC
        # -- also valid per vertex_credentials.py -- so "local slots -" here
        # does not mean vertex is unusable.
        vertex_note = (
            " (vertex: reflects VERTEX_GCP_SERVICE_ACCOUNT_KEY only, not implicit ADC)"
            if provider == "vertex"
            else ""
        )
        print(
            f"{provider}: local slots {list(local) or '-'}, "
            f"render slots {hosted or '-'}, "
            f"active index {active_index} ({index_source}), "
            f"model {active_model.active_model(provider, active_index)} ({model_source})"
            f"{vertex_note}"
        )
        for index in sorted(set(local) | set(hosted)):
            name = registry.slot_env_name(provider, index)
            print(
                f"    {name}: local {'yes' if index in local else 'no'}, "
                f"render {'yes' if index in hosted else 'no'}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    if args.list:
        if (
            args.provider
            or args.index is not None
            or args.clear_index
            or args.no_activate
            or args.clear
            or args.model is not None
            or args.clear_model
        ):
            print("--list must be used alone", file=sys.stderr)
            return 2
        return _print_inventory()

    if args.clear:
        if (
            args.provider
            or args.index is not None
            or args.clear_index
            or args.no_activate
            or args.model is not None
            or args.clear_model
        ):
            print("--clear must be used alone", file=sys.stderr)
            return 2
        if _refuse_local_test_db():
            return 2
        store.init_pool()
        store.set_provider_override(None, datetime.now(timezone.utc).isoformat())
        print(
            "provider cleared -- there is no env fallback, so the service will "
            "refuse to boot until a new provider is set"
        )
        return 0

    if not args.provider:
        print("a provider is required (or --clear)", file=sys.stderr)
        return 2
    if args.provider not in registry.PROVIDERS:
        accepted = ", ".join(sorted(registry.PROVIDERS))
        print(
            f"unsupported provider {args.provider!r} (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    if args.index is not None and args.clear_index:
        print("--index and --clear-index are mutually exclusive", file=sys.stderr)
        return 2
    if args.model is not None and args.clear_model:
        print("--model and --clear-model are mutually exclusive", file=sys.stderr)
        return 2
    if args.model is not None and not args.model.strip():
        print("--model must not be empty", file=sys.stderr)
        return 2
    if args.model is not None:
        stripped_model = args.model.strip()
        # Warn on an unpriced model rather than refuse it (design spec
        # 2026-08-18 section 6b): an unpriced model runs fine, it simply
        # produces no cost estimate on the review comment
        # (providers/pricing.py::estimate_cost_usd returns None) -- there
        # is nothing here worth blocking an operator's explicit, one-shot
        # command over. --force still governs the separate live-credential
        # check below, unrelated to pricing.
        if not pricing.is_known(args.provider, stripped_model):
            known = pricing.models_for(args.provider)
            known_str = ", ".join(known) if known else "(none known for this provider)"
            print(
                f"warning: {args.provider} model {stripped_model!r} has no "
                f"pricing-table entry (known {args.provider} models: {known_str}); "
                "the override is set anyway -- reviews will run without a cost estimate",
                file=sys.stderr,
            )
    if (
        args.no_activate
        and args.index is None
        and not args.clear_index
        and args.model is None
        and not args.clear_model
    ):
        print(
            "--no-activate requires --index, --clear-index, --model, or --clear-model",
            file=sys.stderr,
        )
        return 2
    if args.index is not None and args.index < 0:
        print(f"index must be >= 0, got {args.index}", file=sys.stderr)
        return 2

    if _refuse_local_test_db():
        return 2
    store.init_pool()

    # A pure "clear the key-index override, leave the active provider
    # alone" never verifies -- same as old set_api_key.py's --clear, which
    # never checked a credential before clearing one either. Clearing is
    # exactly what an operator reaches for during a key rotation, precisely
    # when a Render/local mismatch is most likely, so it must not be
    # refusable. --clear-index WITHOUT --no-activate is NOT covered by this
    # skip -- it also activates the provider at index 0, so verifying that
    # index actually has a credential first is worthwhile and still runs
    # below.
    # Credential verification exists to catch "activating a provider whose key
    # slot has no credential". A call that only touches the model or only
    # clears the index -- and does not activate anything -- puts no credential
    # into production, so there is nothing to verify and a verification failure
    # must not be able to block it.
    _credential_untouched = args.no_activate and args.index is None
    if not _credential_untouched:
        if args.index is not None:
            effective_index = args.index
        elif args.clear_index:
            effective_index = 0
        else:
            effective_index = store.get_key_index_override(args.provider) or 0

        ok, message = _override.verify_render_slot(args.provider, effective_index)
        if ok:
            print(message)
        elif args.force:
            print(f"{message} -- proceeding anyway (--force)", file=sys.stderr)
        else:
            print(f"refusing to set the override: {message}", file=sys.stderr)
            return 2

    # Index write before provider-activation write (not the reverse): these
    # are two separate statements, not one transaction (see the design doc's
    # non-atomic-by-choice decision), so this order picks the safer partial-
    # failure mode -- if the second write never happens, the index changed
    # but the provider isn't active yet, so nothing behavior-visible changes
    # until both succeed. The reverse order would leave a provider active
    # against a stale index if only the first write landed.
    now = datetime.now(timezone.utc).isoformat()
    if args.index is not None:
        store.set_key_index_override(args.provider, args.index, now)
        print(f"{args.provider} key-index override set to {args.index}")
    elif args.clear_index:
        store.set_key_index_override(args.provider, None, now)
        print(f"{args.provider} key-index override cleared")
    # Model write sits between the index write and provider activation, for the
    # same partial-failure reasoning as the index write above: if a later write
    # never happens, the model changed but the provider is not active yet, so
    # nothing behavior-visible has changed. Activating first would leave a
    # provider live against a stale model -- exactly the gemini/vertex breakage
    # this override exists to prevent.
    #
    # Targets the SAME slot the credential verification above just checked
    # (--index when given, else this provider's currently-active slot) -- so
    # this can never write a model to a slot whose credential presence was
    # never verified. Writes slot_config, not the retired flat
    # runtime_config.{provider}_model column: providers/active_model.py is
    # fed from slot_config only, and set_slot_config's contract is all three
    # fields together, so the other two (vertex project/location) are read
    # back and re-written unchanged rather than being silently nulled.
    if args.model is not None or args.clear_model:
        if args.index is not None:
            target_index = args.index
        elif args.clear_index:
            target_index = 0
        else:
            target_index = store.get_key_index_override(args.provider) or 0
        existing = store.get_slot_config(args.provider, target_index) or {}
        model = args.model.strip() if args.model is not None else None
        store.set_slot_config(
            args.provider,
            target_index,
            model=model,
            vertex_gcp_project=existing.get("vertex_gcp_project"),
            vertex_gcp_location=existing.get("vertex_gcp_location"),
            now=now,
        )
        if model is not None:
            print(f"{args.provider} slot {target_index} model set to {model}")
        else:
            print(f"{args.provider} slot {target_index} model cleared")
    if not args.no_activate:
        store.set_provider_override(args.provider, now)
        print(f"provider override set to {args.provider}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
