"""Generate contracts/provisioning.json -- what a provisioner must supply.

    uv run python -m scripts.gen_contract

This publishes a partition this repository already maintains internally: which
env vars go to Render and which are database-only, which provider maps to which
credential/model/slot-index name, and which runtime_config / slot_config values
the provisioner must write versus which this service fills in for itself at
boot (review_queue/store.py's widen + backfill). Nothing here is new policy --
it is the existing policy, written down where another repository can read it
without importing any of this one's code.

THE ONE RULE, the same one scripts/gen_docs.py and
review_queue/runtime_config_defaults.py state: every derivation reads MODULE
CONSTANTS and the Settings CLASS's model_fields[...].default, and NEVER
config.settings. model_fields carries each field's DECLARED default; the
settings instance carries this machine's real DATABASE_URL, API keys, and
service-account material. This file is committed here AND vendored verbatim
into the onboarding-wizard repository, so reading the instance would not just
publish those values, it would copy them into a second repository. Import the
Settings CLASS only (transitively, via runtime_config_defaults); the
module-level instance must never be imported here.
tests/test_provisioning_contract.py pins this both behaviourally and by
parsing this module's own import statements.

The contract carries env-var NAMES and non-secret operational DEFAULTS only.
env_vars() entries deliberately carry a placement and nothing else, so there is
no shape in which a credential's default could appear; runtime_config's
bot_backfilled block is the only one carrying values, and every one of them
comes from an OPERATIONAL_KEYS member.

Output must be DETERMINISTIC -- no timestamps, no unordered iteration, no
absolute paths. CI's docs job compares byte-for-byte, so any run-to-run
variation is a permanently red build rather than a useful signal. Every
set-derived list below is sorted(); every schema-derived list follows its
declared tuple's own order.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import OPERATIONAL_KEYS
from providers import registry
from review_queue import runtime_config_defaults, store
from scripts import deploy

# The only file this generator may write. Fixed, not configurable, for the
# same reason gen_docs.py fixes REFERENCE_DIR: a generator that can be
# pointed anywhere is one wrong argument away from replacing hand-written
# content.
CONTRACT_PATH = "contracts/provisioning.json"

# Bumped only when the SHAPE changes (a new block, a renamed key, a changed
# entry shape) -- never when an entry's content changes, which happens on
# any ordinary schema or env-var edit and is what the byte-compare already
# catches. The consumer reads this to know whether it understands the file
# at all.
CONTRACT_VERSION = 2

GENERATED_BY = "scripts.gen_contract -- do not edit by hand"

# slot_config columns a provisioner may legitimately leave NULL: the two
# Vertex-only overrides. Both are meaningless for gemini/groq (deploy.py's
# _seed_slot_zero_config_if_missing writes NULL for them unless the provider
# is vertex), and vertex_gcp_project is optional even for vertex ("unset
# means use the project_id embedded in the resolved service-account key" --
# config.py's own field comment).
#
# `model` is deliberately NOT here even though its column is nullable.
# Nullability in SLOT_CONFIG_COLUMNS is a widening constraint (ADD COLUMN
# NOT NULL fails against a populated table -- see
# store._widening_safe_sql_type), not a statement about ownership:
# providers/active_model.py calls a slot with no configured model "a real
# missing-config state" that factory._build turns into a visible failure.
_SLOT_CONFIG_OPTIONAL: tuple[str, ...] = ("vertex_gcp_project", "vertex_gcp_location")


def _slot_zero_seeded() -> frozenset[str]:
    """The model vars that reach the database as slot 0's seed.

    slot_zero_seed is the one sync destination with no constant of its own
    in deploy.py -- the three model vars are written exactly once, by
    _seed_slot_zero_config_if_missing(), rather than being pushed on every
    sync like every other group. Derived from registry.PROVIDERS, read at
    CALL time (see _placements), so a fourth provider is picked up
    automatically.
    """
    return frozenset(model_var for _credential, model_var in registry.PROVIDERS.values())


def _placements() -> tuple[tuple[str, frozenset[str]], ...]:
    """The five sync destinations scripts/deploy.py partitions OPERATIONAL_KEYS
    across, in the same order tests/test_deploy_script.py's
    test_operational_keys_partition_cleanly_across_every_sync_destination
    checks them.

    Derived from deploy's own constants, never re-typed: a key moved from
    one group to another there changes this file's output on the next run,
    which is the whole point of the freshness gate.

    A FUNCTION rather than a module-level constant so that every derivation
    in this module reads deploy/registry at CALL time -- the same freshness
    env_vars() already has from reading deploy._ALWAYS_SYNCED inline. Frozen
    at import time these groups would disagree with that live read the
    moment anything patched one of deploy's constants: the patched name
    would be in env_vars()'s name set but in none of the placement groups,
    surfacing as a confusing "lands in 0 sync destination(s)" from
    _placement() that has nothing to do with what was actually being
    exercised. Rebuilding five small frozensets per lookup costs nothing at
    this scale, and the generator runs once.
    """
    return (
        ("always_synced", frozenset(deploy._ALWAYS_SYNCED)),
        ("generic_operational", frozenset(deploy._GENERIC_OPERATIONAL_ENV_ATTRS)),
        ("slot_zero_seed", _slot_zero_seeded()),
        ("db_only", frozenset(deploy._DB_SYNCED_OPERATIONAL_KEYS)),
        ("never_synced", frozenset(deploy._NEVER_SYNCED_OPERATIONAL_KEYS)),
    )


def _placement(name: str) -> str:
    """Which of the five sync destinations `name` belongs to.

    Raises rather than guessing. A key in no destination is one nobody
    syncs anywhere -- ISSUES.md's 2026-08-17 "--sync-env silently never
    pushes 12 of the documented operational env vars" entry is what that
    looks like in production -- and a key in two is a contradiction. Either
    way, emitting a contract for it would publish a claim this repository
    cannot honour.
    """
    found = [label for label, members in _placements() if name in members]
    if len(found) != 1:
        raise ValueError(
            f"{name} lands in {len(found)} sync destination(s) "
            f"({', '.join(found) or 'none'}) -- exactly one is required. Fix the "
            "partition in scripts/deploy.py before regenerating the contract."
        )
    return found[0]


def env_vars() -> dict[str, dict[str, str]]:
    """Every env-var name a provisioner may have to handle, and where it goes.

    NAMES AND PLACEMENTS ONLY -- no value, no default, ever (see the module
    docstring). The name set is OPERATIONAL_KEYS (the placement surface
    proper) plus _ALWAYS_SYNCED (the credential and identity vars pushed on
    every sync); GITHUB_TARGET_REPO is in both and appears once.
    """
    names = set(OPERATIONAL_KEYS) | set(deploy._ALWAYS_SYNCED)
    return {name: {"placement": _placement(name)} for name in sorted(names)}


def providers() -> dict[str, dict[str, str]]:
    """Each provider's credential var, model var, and key-slot column.

    KEY_INDEX_COLUMNS is indexed rather than .get()'d on purpose: a provider
    added to PROVIDERS without a key-index column should fail here, loudly,
    rather than publish a provider entry a consumer cannot act on.
    """
    return {
        provider: {
            "credential_var": credential_var,
            "model_var": model_var,
            "key_index_column": registry.KEY_INDEX_COLUMNS[provider],
        }
        for provider, (credential_var, model_var) in sorted(registry.PROVIDERS.items())
    }


def runtime_config() -> dict[str, object]:
    """runtime_config's columns, split by who is responsible for each.

    provisioner_required is derived by SUBTRACTION, not hand-listed: a
    column this repository can fill for itself is one of the backfilled
    ones (a declared non-None default), the one deliberately left NULL
    (NO_DEFAULT_BY_DESIGN), or a key-index column. Whatever is left is, by
    construction, something only the provisioner knows -- so adding a
    column with a default moves it into bot_backfilled automatically, and
    adding one without a default surfaces it as a new provisioner
    obligation that tests/test_provisioning_contract.py then makes someone
    justify against main.py's boot gate.

    provisioner_required_one_of is separate because exactly ONE of the
    three *_key_index columns is written -- whichever provider the visitor
    chose. The other two are legitimately NULL, so a flat required-list
    would be wrong in both directions (spec section 6.1).

    `id` and `updated_at` land in provisioner_required by this subtraction
    even though store._backfill_runtime_config's own INSERT ... ON CONFLICT
    DO NOTHING-turned-DO-UPDATE statement technically supplies both when the
    row is entirely absent (id=1 literal, updated_at=now()). That fallback
    exists only for a database with no runtime_config row at all; the
    documented, expected chronology is still "the provisioner creates the
    row -- writing id, provider, the chosen *_key_index, and updated_at --
    before the bot ever boots against it" (spec section 2's two arrows), and
    a row the bot itself originates this way still has provider=NULL, so
    main.py's boot gate refuses to start regardless. Deliberately kept as
    provisioner_required (spec section 3.1: "with updated_at and id
    documented as the provisioner's responsibility instead"), not derived
    away by teaching this function about that fallback insert.
    """
    by_name = dict(store.RUNTIME_CONFIG_COLUMNS)
    defaults = runtime_config_defaults.declared_defaults()
    one_of = sorted(registry.KEY_INDEX_COLUMNS.values())
    no_default = sorted(runtime_config_defaults.NO_DEFAULT_BY_DESIGN)
    bot_owned = set(defaults) | set(no_default) | set(one_of)
    return {
        "provisioner_required": [
            name for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS if name not in bot_owned
        ],
        "provisioner_required_one_of": one_of,
        "bot_backfilled": [
            {"column": name, "sql_type": by_name[name], "default": defaults[name]}
            for name, _sql_type in store.RUNTIME_CONFIG_COLUMNS
            if name in defaults
        ],
        "no_default_by_design": no_default,
    }


def slot_config() -> dict[str, list[str]]:
    """slot_config's columns, split the same way -- see _SLOT_CONFIG_OPTIONAL."""
    declared = [name for name, _sql_type in store.SLOT_CONFIG_COLUMNS]
    unknown = set(_SLOT_CONFIG_OPTIONAL) - set(declared)
    if unknown:
        raise ValueError(
            f"_SLOT_CONFIG_OPTIONAL names no such slot_config column: {sorted(unknown)}"
        )
    return {
        "provisioner_required": [n for n in declared if n not in _SLOT_CONFIG_OPTIONAL],
        "optional": [n for n in declared if n in _SLOT_CONFIG_OPTIONAL],
    }


def model_validation() -> dict[str, object]:
    """What makes a `model` value valid, not just present.

    slot_config's shape says a provisioner must write `model`; it has never
    said what a writable model IS. For Vertex that gap was load-bearing:
    client.models.list() returns the global Model Garden rather than a
    per-project entitlement list, so a provisioner filling a dropdown from it
    can write a model that 404s every review while reporting the choice as
    validated. That is not hypothetical -- it reached production, and it
    reached it through two independent implementations that had each inferred
    the same wrong thing from the same listing call.

    Published here rather than described in each repository's own docstrings
    because docstrings are exactly what was in place while that happened. The
    consumer vendors this file and asserts its own conformance against it.

    Read from registry.MODEL_PROBE_POLICY -- a module constant, like every
    other derivation in this file (see the module docstring's one rule).
    """
    return {
        "error_codes": list(registry.MODEL_PROBE_ERROR_CODES),
        "providers": {
            provider: dict(registry.MODEL_PROBE_POLICY[provider])
            for provider in sorted(registry.PROVIDERS)
        },
    }


def build_contract() -> dict[str, object]:
    """The whole contract, in the key order it is serialized in."""
    return {
        "generated_by": GENERATED_BY,
        "contract_version": CONTRACT_VERSION,
        "env_vars": env_vars(),
        "providers": providers(),
        "model_validation": model_validation(),
        "runtime_config": runtime_config(),
        "slot_config": slot_config(),
    }


def render() -> str:
    """The contract's exact committed text.

    indent=2 for a readable git diff (the whole point of committing a
    generated file is that a reviewer can see what changed). sort_keys is
    deliberately OFF: every dict here is already built in a fixed order --
    sorted() where derived from a set, declared-tuple order where derived
    from the schema -- and sorting again would scramble
    runtime_config's blocks out of their meaningful order. The trailing
    newline keeps the file POSIX-clean so `git diff` has nothing to say
    about its last line.
    """
    return json.dumps(build_contract(), indent=2, sort_keys=False) + "\n"


def write_contract(root: Path) -> Path:
    """Write the contract under `root` and return the path.

    A REPLACING writer, confined to CONTRACT_PATH: the file has no
    hand-written content to preserve, carries a do-not-edit marker, and CI
    fails on drift. `root` itself is caller-supplied (main()'s --root) so
    the tests can point this at a scratch directory -- that's a guarantee
    about the file NAME, not a sandbox around `root`, exactly as
    gen_docs.write_all() documents.
    """
    path = root / CONTRACT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(), encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate contracts/provisioning.json from the code"
    )
    parser.add_argument("--root", default=".", help="repository root to write under")
    args = parser.parse_args(argv)
    # as_posix(), matching gen_docs.main(): the printed path follows whatever
    # --root was given (relative for the default "."), so a normal run prints
    # exactly "wrote contracts/provisioning.json". Do NOT relative_to(root)
    # here -- Path("contracts/x").relative_to(".") raises ValueError.
    print(f"wrote {write_contract(Path(args.root)).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
