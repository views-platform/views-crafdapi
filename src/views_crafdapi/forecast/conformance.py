"""Run the published views-frames conformance contracts on real crafdapi frames (#91).

crafdapi is a cross-repo consumer of the frozen views-frames leaf. Asserting the leaf's own
published contracts — the frame contract, the summarizer contract, and the cross-level
alignment law — on **real crafdapi-built frames and the injected GAUL mapping** proves the full
leaf + summarize + cross-level surface end-to-end, as the leaf's ADR-016 intends ("one
contract, N consumers, each running the suite in its own CI"). Pinned to the governed floor.
"""

from views_frames.conformance import (
    CONFORMANCE_FLOOR,
    assert_cross_level_alignment_law,
    assert_frame_contract,
)
from views_frames_summarize.conformance import assert_summarizer_contract

__all__ = ["CONFORMANCE_FLOOR", "assert_frame", "assert_cross_level_law"]


def assert_frame(frame) -> None:
    """Assert the leaf frame contract + the summarizer contract on a crafdapi-built frame."""
    assert_frame_contract(frame)
    assert_summarizer_contract(frame)


def assert_cross_level_law(index, mapping, target_level) -> None:
    """Assert the cross-level alignment law for crafdapi's injected `(time, unit) -> target`
    mapping (`mapping` is a `{(time, unit): target_unit}` dict)."""
    assert_cross_level_alignment_law(index, mapping, target_level)
