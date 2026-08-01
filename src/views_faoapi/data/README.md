# Data Module

This module contains data handling and statistical analysis classes for the FAO API. It provides:

- Dataset abstractions for loading and manipulating prediction data
- Geographic aggregation across multiple administrative levels
- Posterior distribution analysis (HDI and MAP calculations)
- Statistically correct element-wise distribution summation

## Module Structure

```
data/
├── __init__.py
├── handlers.py      # Dataset classes for FAO predictions
└── README.md
```

---

## handlers.py

Contains the dataset hierarchy for handling FAO prediction data with geographic metadata.

### Class Hierarchy

```
_GridDataset  (generic (time, entity)-grid sample dataset; the frame-native compute core)
    │
    └── ForecastDataset  (FAO facade: geo-metadata + aggregation + served HDI/MAP)
                          alias: FAO_PGMDataset
```

> **S8 (#162, epic #154):** the chain was rationalized from three classes to two. `_PGDataset`
> (a 2-method refused-bequest that only added a `priogrid_id` index check) was **retired** — its
> check folded into `ForecastDataset.validate_indices`. `_ViewsDataset` was **renamed
> `_GridDataset`** and kept deliberately as the generic, geo-less base (the unit the
> parity/validation suites instantiate directly, and the vehicle `check_integrity` uses to
> round-trip a metadata-free subset). A full single-class merge was rejected (register D-21): it
> would complect the generic and FAO responsibilities and relocate a UN-facing fail-loud. The
> object-dtype spine the epic set out to drain is gone (S4); this two-level structure is the
> end-state.

---

### `_GridDataset` (generic base)

Base class for all faoapi datasets providing common functionality (no FAO geography).

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `dataframe` | pd.DataFrame | The underlying data with MultiIndex |
| `target_columns` | List[str] | Columns containing prediction distributions |
| `category` | str | Either "historical" or "forecast" |

#### Methods

##### `get_subset_dataframe`

```python
def get_subset_dataframe(
    self,
    time_ids: Optional[List[int]] = None,
    features: Optional[List[str]] = None,
    sample_idx: Optional[List[int]] = None,
    entity_ids: Optional[List[Union[str, int]]] = None,
    with_metadata: bool = True,
    aggregate: bool = False,
    level: str = "pg"
) -> pd.DataFrame
```

Returns a filtered subset of the dataframe.

**Parameters:**
- `time_ids`: Filter by month IDs (e.g., [410, 411, 412])
- `features`: Filter by feature names (e.g., ["pred_ln_sb_best"])
- `sample_idx`: Filter specific sample indices from distributions
- `entity_ids`: Filter by geographic entity (ISO3 or GAUL codes)
- `with_metadata`: Include geographic metadata columns
- `aggregate`: Aggregate distributions to specified level
- `level`: Geographic level ("pg", "country", "gaul0", "gaul1", "gaul2")

##### `calculate_hdi_map`

```python
def calculate_hdi_map(
    self,
    alpha: float = 0.9,
    time_ids: Optional[List[int]] = None,
    features: Optional[List[str]] = None,
    sample_idx: Optional[List[int]] = None,
    entity_ids: Optional[List[Union[str, int]]] = None,
    enforce_non_negative: bool = False,
    with_metadata: bool = True,
    aggregate: bool = False,
    level: str = "pg"
) -> pd.DataFrame
```

Computes Highest Density Intervals and Maximum A Posteriori estimates.

**Returns DataFrame with columns:**
- `{var}_lower`: Lower bound of HDI
- `{var}_upper`: Upper bound of HDI
- `{var}_map`: Maximum A Posteriori estimate
- `{var}_min`: Minimum sample value
- `{var}_max`: Maximum sample value

---

### `_PGDataset` — RETIRED (S8, #162)

This intermediate class (which only added a `priogrid_id` index-level check) was removed; the
check is now in `ForecastDataset.validate_indices`. The required FAO metadata columns are owned
by `ForecastDataset._METADATA_COLS`:

```python
[
    "country_iso_a3",           # ISO Alpha-3 country code
    "admin1_gaul0_code",        # GAUL Level 0 code
    "admin1_gaul0_name",        # GAUL Level 0 name
    "admin1_gaul1_code",        # GAUL Level 1 code
    "admin1_gaul1_name",        # GAUL Level 1 name
    "admin2_gaul2_code",        # GAUL Level 2 code
    "admin2_gaul2_name",        # GAUL Level 2 name
    "pg_xcoord",                # PRIO-GRID X coordinate
    "pg_ycoord"                 # PRIO-GRID Y coordinate
]
```

---

### `FAO_PGMDataset`

The primary dataset class for FAO predictions with full geographic metadata support.

#### Constructor

```python
def __init__(
    self,
    dataframe: pd.DataFrame,
    target_columns: List[str],
    metadata_columns: List[str],
    category: str = "forecast",
    validate: bool = True,
    expected_columns: Optional[List[str]] = None,
    index_names: Optional[List[str]] = None
)
```

**Parameters:**
- `dataframe`: DataFrame with MultiIndex (month_id, priogrid_id)
- `target_columns`: Columns containing prediction distributions (arrays)
- `metadata_columns`: Geographic metadata columns
- `category`: "historical" or "forecast"
- `validate`: Whether to validate indices on initialization

#### Methods

##### `_elementwise_sum`

```python
def _elementwise_sum(self, arrays: List[np.ndarray]) -> np.ndarray
```

Performs statistically correct element-wise summation of distributions.

**Example:**
```python
arrays = [np.array([1, 2, 3]), np.array([4, 5, 6])]
result = dataset._elementwise_sum(arrays)
# Returns: np.array([5, 7, 9])
```

**Why this matters:**
- Preserves sample correspondence across cells
- Maintains proper uncertainty quantification
- HDI([sum of samples]) ≠ sum of HDI bounds

##### `_aggregate_distributions`

```python
def _aggregate_distributions(
    self,
    df: pd.DataFrame,
    level: str,
    entity_ids: Optional[List[str]] = None
) -> pd.DataFrame
```

Aggregates raw distributions by geographic level using element-wise summation.

**Aggregation Process:**
1. Group PRIO-GRID cells by geographic entity
2. Sum distributions element-wise within each group
3. Preserve first occurrence of metadata columns
4. Return DataFrame with aggregated distributions

**Supported Levels:**

| Level | Group Column | Entity ID Type |
|-------|--------------|----------------|
| `country` | `country_iso_a3` | ISO Alpha-3 (e.g., "SOM") |
| `gaul0` | `admin1_gaul0_code` | Integer |
| `gaul1` | `admin1_gaul1_code` | Integer |
| `gaul2` | `admin2_gaul2_code` | Integer |

##### `validate_indices`

```python
def validate_indices(self) -> None
```

Validates that the DataFrame has the required MultiIndex structure.

**Raises:** `ValueError` if indices are invalid

##### `_get_pg_cells`

```python
def _get_pg_cells(
    self,
    level: str,
    entity_ids: List[Union[str, int]]
) -> List[int]
```

Returns PRIO-GRID cell IDs belonging to specified geographic entities.

---

## Aggregation Deep Dive

### The Problem with Naive Aggregation

When aggregating uncertainty from multiple cells, a common mistake is:

```python
# WRONG: Sum HDI bounds separately
country_lower = sum([cell.hdi_lower for cell in cells])
country_upper = sum([cell.hdi_upper for cell in cells])
```

This is **statistically incorrect** because:
- HDI bounds are not additive
- Sample correspondence is lost
- Uncertainty is underestimated

### Correct Aggregation Approach

The `FAO_PGMDataset` implements proper element-wise aggregation:

```python
# CORRECT: Sum samples element-wise, then compute HDI
cell_a = [1.0, 1.2, 1.1, 1.3, ...]  # 1000 samples
cell_b = [0.5, 0.6, 0.4, 0.7, ...]  # 1000 samples
aggregated = [1.5, 1.8, 1.5, 2.0, ...]  # element-wise sum
country_hdi = compute_hdi(aggregated)  # HDI on aggregated samples
```

### Mathematical Justification

For random variables $X_1, X_2, ..., X_n$ with posterior samples:

$$\text{HDI}\left(\sum_{i=1}^{n} X_i\right) \neq \sum_{i=1}^{n} \text{HDI}(X_i)$$

The correct approach computes:

$$Y = \sum_{i=1}^{n} X_i^{(j)} \text{ for each sample } j$$

Then:

$$\text{HDI}_\alpha(Y) = [\text{lower}, \text{upper}]$$

---

## Usage Examples

### Basic Usage

```python
from views_faoapi.data.handlers import FAO_PGMDataset

# Create dataset
dataset = FAO_PGMDataset(
    dataframe=df,
    target_columns=["pred_ln_sb_best", "pred_ln_ns_best"],
    metadata_columns=["country_iso_a3", "admin1_gaul1_code", ...],
    category="forecast"
)

# Get subset for specific countries
subset = dataset.get_subset_dataframe(
    time_ids=[410, 411, 412],
    entity_ids=["SOM", "ETH"],
    level="country",
    with_metadata=True
)
```

### Aggregated HDI Calculation

```python
# Compute aggregated HDI for Somalia
hdi_df = dataset.calculate_hdi_map(
    alpha=0.95,
    entity_ids=["SOM"],
    level="country",
    aggregate=True,
    enforce_non_negative=True
)

# Result columns:
# pred_ln_sb_best_lower, pred_ln_sb_best_upper, pred_ln_sb_best_map,
# pred_ln_sb_best_min, pred_ln_sb_best_max, ...
```

## Data Requirements

### DataFrame Structure

The input DataFrame must have:

1. **MultiIndex**: `(month_id, priogrid_id)`
2. **Target columns**: Arrays of posterior samples
3. **Metadata columns**: Geographic information

```python
# Example DataFrame structure
df.index.names  # ['month_id', 'priogrid_id']
df.columns      # ['pred_ln_sb_best', 'country_iso_a3', ...]
df['pred_ln_sb_best'].iloc[0]  # np.array([0.1, 0.2, ...]) - 1000 samples
```

### Validation

The `FAO_PGMDataset` validates:
- Index names match expected values
- Required columns exist
- Data types are correct

---

## Error Handling

| Error | Cause | Solution |
|-------|-------|----------|
| `ValueError: Invalid indices` | Missing MultiIndex | Ensure df has (month_id, priogrid_id) index |
| `KeyError: column not found` | Missing metadata | Include all required metadata columns |
| `ValueError: min_samples` | Too few samples | Ensure distributions have enough samples |

---

## See Also

- [Main README](../../README.md) - API overview and endpoints
- [Managers README](../managers/README.md) - API and storage managers
