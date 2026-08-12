"""The cross-repo seam-contract binding for this consumer API — one concept, one home.

This module is the **single source of truth** for the store-document ``name`` this API owns
and filters every prediction query on, plus the facts needed to verify that name against the
platform's public coordinate registry. views-postprocessing ADR-013 §4.1a: the name is
"Config-owned by [the consumer]; changing it is a **contract amendment**, not a deploy detail."

Why this matters: the producer (views-postprocessing) uploads each delivery under this exact
name; if our name and its upload name ever drift apart the upload *succeeds*, the file is
stored and paid for, the endpoint returns **empty**, and nothing anywhere raises — the
delivery is invisible (ADR-017 §1). The binding is enforced in CI by
``tests/test_seam_contract_binding.py``, which reads the registry declaration
``[contract.UNCRAFD_CONSUMER_DOCUMENT_NAME]`` at an immutable git tag and asserts it equals
:data:`CONSUMER_DOCUMENT_NAME`.

Design: the git/tag/CI plumbing that *fetches* the registry text lives in that test (an
environment concern); this module keeps only the identity facts and a **pure** parser
(:func:`declared_value`), so it carries no I/O and has one reason to change — the registry's
contract-table shape. Per the Appwrite Seam Contract §5.8 (þing-01 D8), each consumer API
ships its **own** copy of this module; there is deliberately no shared cross-repo importable
code (WET-before-DRY).
"""
import tomllib

#: The store-document name this API serves/filters on — declared here and nowhere else.
#: Changing it is a contract amendment: bump the registry edition and re-pin both sides.
CONSUMER_DOCUMENT_NAME = "un_crafd"

#: The registry ``[contract]`` row this name is the authority-checked mirror of.
REGISTRY_CONTRACT_KEY = "UNCRAFD_CONSUMER_DOCUMENT_NAME"

#: The immutable registry edition we bind to. The UNCRAFD row exists only from v1.5.2, so this
#: must be ≥ that; re-pinning to a later edition is a deliberate edit = the contract amendment.
REGISTRY_PIN_TAG = "appwrite-seam-v1.5.2"

#: Path of the coordinate registry within the views-appwrite repo.
REGISTRY_RELPATH = "docs/ADRs/platform/coordinate_registry.toml"


def declared_value(registry_toml_text: str, contract_key: str = REGISTRY_CONTRACT_KEY) -> str:
    """Return ``[contract.<contract_key>].value`` parsed from the coordinate-registry TOML.

    Pure (text in, value out): the caller fetches the text at the pinned tag. Raises a clear
    ``ValueError`` naming the missing row if the ``[contract]`` table/key/value is absent — the
    likely cause is a pin that predates the row (UNCRAFD exists only from
    ``appwrite-seam-v1.5.2``). Loud and legible, never a silent wrong answer.
    """
    registry = tomllib.loads(registry_toml_text)
    try:
        return registry["contract"][contract_key]["value"]
    except KeyError as exc:
        raise ValueError(
            f"registry TOML has no [contract.{contract_key}].value — the pinned edition may "
            "predate this contract row (UNCRAFD exists only from appwrite-seam-v1.5.2)."
        ) from exc
