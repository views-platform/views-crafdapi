"""Summarize: reduce a forecast's draws to the served summaries (`draws.summarize_draws`).

One reason to change: how the published summaries are computed. Numpy only; no geography.
`estimator.tower_collapse` is the older most-likely-value and interval estimator, still used by
`_GridDataset.calculate_map` / `calculate_hdi`, which no route serves.
"""
