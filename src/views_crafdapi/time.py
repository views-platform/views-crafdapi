"""
VIEWS month ID conversion utilities.

The VIEWS system represents time as integer month IDs where month_id 1 = January 1980.
These functions convert between month IDs and calendar dates.
"""

_BASE_YEAR = 1980


def date_to_month_id(year: int, month: int) -> int:
    return (year - _BASE_YEAR) * 12 + month


def month_id_to_date(month_id: int) -> str:
    year = _BASE_YEAR + (month_id - 1) // 12
    month = (month_id - 1) % 12 + 1
    return f"{year}-{month:02d}"


def month_id_range(start_year: int, start_month: int, end_year: int, end_month: int) -> list[int]:
    start = date_to_month_id(start_year, start_month)
    end = date_to_month_id(end_year, end_month)
    return list(range(start, end + 1))
