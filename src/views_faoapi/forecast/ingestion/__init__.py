"""Ingestion: turn the upstream forecast artifact into a clean, validated, dense grid.

One reason to change per module:
- `parquet_reader`  — how the artifact is decoded into array columns (the #100 frame seam)
- `dense_grid`      — how missing (time, entity) cells are recreated and filled
- `plausibility`    — what makes ingested values/metadata trustworthy (C-72)
"""
