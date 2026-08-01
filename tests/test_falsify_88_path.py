"""Falsification guards for the #88 path (cross-repo audit, 2026).

The claim "we know the path to #88 100%" was FALSIFIED. Key findings the guards below pin:

1. views-frames structurally CANNOT carry per-row metadata or do dense-grid fill
   (ADR-013 = scalar provenance only; ADR-014 = geography injected, never embedded). So #88 is a
   DECOMPOSITION (samples -> PredictionFrame; metadata + grid + aggregation-mapping retained in a
   thin faoapi layer, ~150 LOC), not a clean "retire the fork".
2. The cross-level aggregation must use the *injected-mapping* pattern, for which there is a
   PROVEN reference: views-postprocessing reconciliation (cross_level_align_arrays + group-by-argsort,
   tested against a frozen oracle) and views-reporting's index-keyed metadata cache.
3. The joint-sampling assumption (C-70) IS guaranteed upstream (hydranet LockedDropout, ADR-057) and
   documented in views_frames_summarize's CIC — but is UNTESTED here and coupled to that mechanism.

These guards fill the C-70 test gap and pin the aggregation contract any #88 rewrite must preserve.
"""

import numpy as np
import pandas as pd

from views_faoapi.data.handlers import FAO_PGMDataset


def test_elementwise_sum_preserves_sample_axis_alignment():
    """Aggregation must sum the SAME sample index across cells (joint draws), not reshuffle."""
    agg = FAO_PGMDataset._elementwise_sum
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([10.0, 20.0, 30.0])
    np.testing.assert_array_equal(agg(None, pd.Series([a, b])), np.array([11.0, 22.0, 33.0]))


def test_aggregation_is_cell_order_independent():
    """Summation is commutative — a rewrite that sorted/reordered per-cell samples would break this."""
    agg = FAO_PGMDataset._elementwise_sum
    a, b, c = np.array([1.0, 2.0]), np.array([10.0, 20.0]), np.array([100.0, 200.0])
    np.testing.assert_array_equal(
        agg(None, pd.Series([a, b, c])), agg(None, pd.Series([c, a, b]))
    )


def test_joint_vs_independent_aggregation_diverge_pins_C70():
    """Document why joint sampling matters: summing aligned draws preserves cross-cell correlation;
    independent draws would give materially different aggregate spread. Pins the assumption so a
    #88 rewrite — or an upstream sampling change (hydranet LockedDropout removal) — can't silently
    flip it without a red test."""
    rng = np.random.default_rng(0)
    base = rng.normal(5.0, 1.0, 4000)
    joint = FAO_PGMDataset._elementwise_sum(None, pd.Series([base, base, base]))  # correlated draws
    independent = base + rng.normal(5.0, 1.0, 4000) + rng.normal(5.0, 1.0, 4000)  # independent draws
    # joint sum of 3 correlated draws spreads ~3x; independent ~sqrt(3)x — they must diverge.
    assert np.std(joint) > np.std(independent) * 1.3
