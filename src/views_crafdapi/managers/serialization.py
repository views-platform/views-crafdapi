"""HTTP request/response (de)serialization helpers for the CRAF'd API.

The size estimate the slice route uses to refuse an answer that would be too large (413),
before computing it.
"""

# Measured cost of a JSON-records answer, per value, on real production artifacts.
#
# Two constants because a payload has two kinds of value and they differ threefold. A KEYED
# value is one column of one row -- it costs a dict entry, with the interning and hashing that
# implies. A SAMPLE ELEMENT is one posterior draw inside a list cell, which costs only the list
# slot and the float.
#
# Calibrated against two direct `tracemalloc` measurements, each linear across its range:
#
#   historical  100,000 rows x 14 keyed, 0 elements  -> 155.0 MiB  =>  1,625 B/row => ~116 B/keyed
#   forecast     13,110 rows x 17 keyed, 192 elements ->  96.7 MiB  =>  7,731 B/row => ~30 B/element
#
# Rounded UP, because this feeds a guard: over-estimating refuses slightly early, under-estimating
# lets through the payload the guard exists to stop. (C-284.)
_BYTES_PER_KEYED_VALUE = 120
_BYTES_PER_SAMPLE_ELEMENT = 32


def estimate_records_bytes(
    n_rows: int, n_keyed_columns: int, n_sample_elements_per_row: int
) -> int:
    """Estimate the peak memory a JSON-records answer will need for a frame of this shape.

    Exists so a caller can decide **before** materialising. The whole point of the bound it
    feeds is not to pay the cost it is trying to refuse, so it takes a shape rather than a
    frame.

    A row bound cannot do this job: a historical row costs ~1,625 bytes and a forecast row
    ~7,731, because the second carries a posterior sample per draw per target. One number
    calibrated on the first is wrong by nearly 5x for the second, and the sample width varies
    per delivery -- a 32-draw run and a 1000-draw run differ by another factor of thirty.

    The mechanism is shared with the sibling API this box co-hosts, which runs the same
    serializer and was calibrated against it. What does NOT transfer is the call site: there the
    guard sits on one unfiltered route, and here every bounded route takes filters.
    """
    per_row = (
        n_keyed_columns * _BYTES_PER_KEYED_VALUE
        + n_sample_elements_per_row * _BYTES_PER_SAMPLE_ELEMENT
    )
    return n_rows * per_row
