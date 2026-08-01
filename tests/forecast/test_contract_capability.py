"""S5 (#250, ADR-033 §7, C-171): the deploy/serve capability rule — which wire-contract dialects
this build will render, and which it refuses rather than serve degraded."""

import pytest

from views_crafdapi.forecast import contract

pytestmark = pytest.mark.layer2_data


def test_served_version_is_rendered():
    assert contract.can_render_contract(contract.SERVED_CONTRACT_VERSION) is True


def test_unstamped_is_transition_safe_pass():
    # The producer does not yet stamp contract_version everywhere (cross-repo #133) — surfaced,
    # not gated. An absent/empty version must not refuse a run.
    assert contract.can_render_contract("") is True
    assert contract.can_render_contract("   ") is True


def test_older_minor_is_rendered():
    # SERVED is 1.5; a run written in an older minor of the same major is renderable.
    assert contract.can_render_contract("1.0") is True
    assert contract.can_render_contract("1.4") is True
    assert contract.can_render_contract("1") is True  # bare major → (1, 0)


def test_newer_minor_is_refused():
    # A newer minor may carry fields this build does not understand → would render degraded.
    assert contract.can_render_contract("1.6") is False
    assert contract.can_render_contract("1.99") is False


def test_different_major_is_refused():
    assert contract.can_render_contract("2.0") is False
    assert contract.can_render_contract("0.9") is False


def test_stamped_but_unparseable_is_refused():
    # A stamped-but-garbage version cannot establish compatibility → refuse, never degrade-serve.
    assert contract.can_render_contract("banana") is False
    assert contract.can_render_contract("1.x") is False
