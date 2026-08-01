"""The forecast data domain for views-faoapi.

Decomposition of the former `data/handlers.py` god-class into single-responsibility
modules (epic #87), each depending toward the frozen views-frames leaf:

- `ingestion/`   — decode the parquet, fill the dense grid, validate plausibility
- `frames/`      — build views-frames PredictionFrame / TargetFrame value objects
- (later) `geography/`, `summarize/`, `aggregate/`, `serialize/`, `dataset.py`
"""
