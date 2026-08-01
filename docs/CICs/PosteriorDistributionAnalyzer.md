# Class Intent Contract: PosteriorDistributionAnalyzer

**Status:** Superseded — class removed 2026-07-24 (ADR-025 output-schema epic, #222 / S1)  
**Owner:** Project maintainers  
**Last reviewed:** 2026-06-03  
**Related ADRs:** ADR-003 (declared parameters, not inferred), ADR-006 (this contract)  

> **Superseded.** `PosteriorDistributionAnalyzer` (the v1 hand-rolled histogram MAP + HDI
> estimator, `src/views_crafdapi/data/statistics.py`) was **removed** in favour of the
> views-frames tower estimator (`forecast/summarize/estimator.tower_collapse`), which had long
> been the sole serving path. This contract is retained for historical reference only and is no
> longer enforced. See ADR-030 (representation migration) and epic #222.

---

## 1. Purpose

> **What is this class for?**

`PosteriorDistributionAnalyzer` computes empirical MAP (Maximum A Posteriori) estimates and HDI (Highest Density Interval) credibility intervals from posterior distribution samples. It provides a lightweight, histogram-based alternative to full Bayesian density estimation, designed for fast summarization of prediction ensemble outputs.

**Location:** `src/views_crafdapi/data/statistics.py`

---

## 2. Non-Goals (Explicit Exclusions)

- This class does **not** cache results between calls. Each `analyze()` invocation computes fresh results.
- This class does **not** filter NaN/Inf values from caller-supplied data proactively. While `_validate_samples` does filter NaN/Inf internally, the caller should prefilter when possible to avoid silent data loss.
- This class does **not** own data semantics. It computes statistics on whatever numeric samples it receives; it has no knowledge of what the samples represent (fatalities, probabilities, etc.).
- This class does **not** perform parametric density estimation (e.g., KDE, Gaussian mixture). The MAP is histogram-based only.
- This class does **not** persist results. `self.summary` is transient instance state, not a store.

---

## 3. Responsibilities and Guarantees

- **MAP estimation:** Computes a histogram-based MAP estimate from `np.histogram(samples, bins, density=True)`. The MAP is the bin center with maximum density. If the fraction of samples at or near zero (`mass_at_zero`) meets or exceeds `zero_mass_threshold` (default 0.3, i.e., 30%), the MAP is forced to 0.0.
- **HDI computation:** Computes empirical HDIs using the sorted-sample shortest-interval algorithm. For each credible mass alpha in `credible_masses`: sorts samples, computes `k = floor(alpha * n)`, finds the interval `[sorted[min_idx], sorted[min_idx + k]]` with minimum width across all valid starting indices.
- **Nesting enforcement:** Guarantees that narrower HDIs are nested within wider ones via `_enforce_hdi_structure()`. The narrowest HDI is shifted (preserving width) so the MAP falls within it. Each subsequent wider HDI is expanded to fully contain the previous one.
- **Input validation:** Guarantees that all inputs are validated before computation. Invalid inputs cause immediate `ValueError` exceptions, never silent fallback.
- **Fresh-instance contract:** Each call to `analyze()` is intended to be made on a fresh `PosteriorDistributionAnalyzer()` instance. No shared state between calls. This was previously a concurrency risk (C-01) and has been fixed at all call sites.
- **Return structure:** `analyze()` returns a dict with exactly these keys: `map` (float), `min` (float), `max` (float), `mass_at_zero` (float), `hdis` (list of (low, high) tuples, one per credible mass, in ascending order of mass).

---

## 4. Inputs and Assumptions

- **`samples`** (`np.ndarray` or `List[float]`): A 1-D array of numeric posterior samples. Must contain at least one finite value after NaN/Inf filtering. Caller is responsible for ensuring samples are meaningful; the class does not validate semantic correctness.
- **`credible_masses`** (`Tuple[float, ...]`, default `(0.5, 0.95, 0.99)`): Each value must be strictly between 0 and 1. Values are sorted internally in ascending order before HDI computation.
- **`zero_mass_threshold`** (`float`, default `0.3`): Must be in `[0, 1]`. Controls when the MAP is forced to 0.0 instead of using the histogram peak.
- **`bins`** (`int`, default `100`): Must be a positive integer. Number of histogram bins for MAP estimation.
- **Constructor:** `__init__()` takes no parameters. Initializes `self.summary = None`.
- **Precondition:** `analyze()` must be called before `summary_dict()` or `print_summary()` return meaningful output.

---

## 5. Outputs and Side Effects

- **`analyze()` return value:** A dict:
  ```python
  {
      'map': float,           # MAP estimate
      'min': float,           # Minimum sample value
      'max': float,           # Maximum sample value
      'mass_at_zero': float,  # Fraction of samples near zero
      'hdis': [               # One tuple per credible mass, ascending order
          (low_50, high_50),
          (low_95, high_95),
          (low_99, high_99),
      ]
  }
  ```
- **Side effects on `self`:** `analyze()` sets `self.samples`, `self.credible_masses`, `self.zero_mass_threshold`, `self.bins`, and `self.summary` as instance attributes. This is the mechanism by which state is stored, and why fresh instances are required.
- **`summary_dict()`** returns `self.summary` (the same dict) or `None` if `analyze()` has not been called. NOT called anywhere in the codebase.
- **`print_summary(file)`** pretty-prints the summary to a file-like object (default `sys.stdout`). NOT called anywhere in the codebase.
- **Logging:** Debug-level log messages for MAP computation path and HDI values. Warning-level if `print_summary()` is called before `analyze()`.

---

## 6. Failure Modes and Loudness

- **`ValueError("No valid samples provided.")`** -- Raised by `_validate_samples()` if all samples are NaN or Inf after filtering. Logged at ERROR level before raising.
- **`ValueError("All credible masses must be between 0 and 1.")`** -- Raised by `_validate_credible_masses()` if any mass is outside `(0, 1)`. Logged at ERROR level.
- **`ValueError("bins must be a positive integer.")`** -- Raised by `_validate_bins()` if `bins <= 0`. Logged at ERROR level.
- **`ValueError("zero_mass_threshold must be between 0 and 1.")`** -- Raised by `_validate_zero_mass_threshold()` if threshold is outside `[0, 1]`. Logged at ERROR level.
- **Degenerate HDI:** If `k < 1` for a credible mass (too few samples relative to the mass), a degenerate interval `(sorted[0], sorted[0])` is returned. This is logged at DEBUG level but does not raise.
- **Must never fail silently:** Invalid inputs must always raise. Computed HDIs that violate nesting are corrected by `_enforce_hdi_structure()`, not silently returned as-is.

---

## 7. Boundaries and Interactions

- **Layer:** Domain (`src/views_crafdapi/data/statistics.py`).
- **Callers:**
  - `_ViewsDataset._analyze_samples()` in `data/handlers.py` -- creates `PosteriorDistributionAnalyzer().analyze(...)` inline.
  - `_ViewsDataset._compute_single_map()` in `data/handlers.py` -- creates a fresh instance per call.
  - `_ViewsDataset._calculate_single_hdi()` in `data/handlers.py` -- creates a fresh instance per call.
- **Dependencies:** Only `numpy` and Python standard library (`sys`, `logging`, `typing`). No infrastructure dependencies.
- **Must not depend on:** Infrastructure layer (`managers/`), Observability layer (`wandb/`), or any I/O operations.
- **Trusts:** Callers to provide semantically meaningful samples. The class treats samples as opaque numeric arrays.

---

## 8. Examples of Correct Usage

**Standard analysis on a fresh instance:**

```python
from views_crafdapi.data.statistics import PosteriorDistributionAnalyzer
import numpy as np

samples = np.random.normal(loc=5.0, scale=2.0, size=1000)

analyzer = PosteriorDistributionAnalyzer()
result = analyzer.analyze(samples, credible_masses=(0.5, 0.95), bins=100)

print(result['map'])        # Histogram-based MAP estimate
print(result['hdis'][0])    # 50% HDI as (low, high) tuple
print(result['hdis'][1])    # 95% HDI as (low, high) tuple
```

**Zero-dominated distribution:**

```python
samples = np.concatenate([np.zeros(400), np.random.exponential(1.0, 600)])

analyzer = PosteriorDistributionAnalyzer()
result = analyzer.analyze(samples, zero_mass_threshold=0.3)
# result['map'] == 0.0 because mass_at_zero >= 0.3
```

---

## 9. Examples of Incorrect Usage

**Sharing a single instance across multiple calls (the C-01 anti-pattern):**

```python
# WRONG: Reusing the same instance across calls leaks state.
shared_analyzer = PosteriorDistributionAnalyzer()
result_a = shared_analyzer.analyze(samples_a)
result_b = shared_analyzer.analyze(samples_b)
# After this, shared_analyzer.samples == samples_b, which
# corrupts print_summary() if called expecting samples_a context.
```

The correct pattern is one instance per `analyze()` call, as all current call sites demonstrate.

**Calling summary methods before analyze:**

```python
# WRONG: summary_dict() returns None, print_summary() prints a warning.
analyzer = PosteriorDistributionAnalyzer()
result = analyzer.summary_dict()  # Returns None
```

**Passing unfiltered data expecting the class to handle all edge cases:**

```python
# WRONG: Relying solely on internal NaN filtering without caller awareness.
# If ALL values are NaN, this raises ValueError. Caller should handle this
# possibility or pre-check data quality.
analyzer = PosteriorDistributionAnalyzer()
result = analyzer.analyze(np.array([np.nan, np.nan, np.nan]))  # Raises ValueError
```

---

## 10. Test Alignment

- **Green tests (must pass):**
  - `analyze()` returns all required keys (`map`, `min`, `max`, `mass_at_zero`, `hdis`) for valid input.
  - MAP is forced to 0.0 when `mass_at_zero >= zero_mass_threshold`.
  - MAP is derived from histogram when `mass_at_zero < zero_mass_threshold`.
  - HDIs are correctly nested: for each pair `(hdis[i], hdis[i+1])`, `hdis[i+1][0] <= hdis[i][0]` and `hdis[i+1][1] >= hdis[i][1]`.
  - MAP falls within the narrowest HDI: `hdis[0][0] <= map <= hdis[0][1]`.
  - `ValueError` raised for all-NaN samples, out-of-range credible masses, non-positive bins, and out-of-range threshold.

- **Beige tests (behavioral expectations):**
  - Fresh instances produce identical results for identical inputs (deterministic given same samples).
  - `credible_masses` are returned in sorted ascending order regardless of input order.

- **Red tests (regression guards):**
  - Shared-instance concurrency: verify that two sequential `analyze()` calls on the same instance do not corrupt each other's return values (guarding against C-01 regression at the class level, even though the fix is at the call-site level).

- **Test file:** `tests/test_statistical_pipeline.py` — covers all green, beige, and red test categories above. Golden-value regression tests, NaN handling, float32 precision, and fresh-instance verification.

---

## 11. Evolution Notes

- **Stable:** The `analyze()` return dict structure (`map`, `min`, `max`, `mass_at_zero`, `hdis`) is a public contract used by multiple callers. Changing keys or semantics requires updating all consumers.
- **Candidate for change:** `summary_dict()` and `print_summary()` are unused and may be removed in a future cleanup.
- **Candidate for change:** The commented-out constructor parameters (`samples`, `auto_analyze`) suggest a prior design where samples were passed at construction time. This pattern was abandoned in favor of the current fresh-instance-per-call pattern.
- **Would require contract revision:** Adding parametric density estimation (KDE) as an alternative MAP method, changing the HDI algorithm, or adding caching/memoization across calls.

---

## End of Contract

This document defines the **intended meaning** of `PosteriorDistributionAnalyzer`.

Changes to behavior that violate this intent are bugs.  
Changes to intent must update this contract.
