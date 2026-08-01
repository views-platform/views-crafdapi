"""Aggregate: roll posterior distributions up to a higher admin level, joint-sampling.

The contract is `HDI(Σ) ≠ Σ HDI` (register C-70): samples are summed element-wise across the
constituent cells *before* any collapse, preserving cross-cell correlation. The summation
itself is a views-frames responsibility (`aggregate_distributions_arrays`); faoapi injects the
geography (the mapping). One reason to change — the aggregation mechanics.
"""
