from views_faoapi.time import date_to_month_id, month_id_to_date, month_id_range


class TestDateToMonthId:
    def test_base_year(self):
        assert date_to_month_id(1980, 1) == 1

    def test_base_year_december(self):
        assert date_to_month_id(1980, 12) == 12

    def test_second_year(self):
        assert date_to_month_id(1981, 1) == 13

    def test_known_date(self):
        assert date_to_month_id(2024, 12) == 540

    def test_roundtrip(self):
        mid = date_to_month_id(2023, 6)
        result = month_id_to_date(mid)
        assert result == "2023-06"


class TestMonthIdToDate:
    def test_id_1(self):
        assert month_id_to_date(1) == "1980-01"

    def test_id_12(self):
        assert month_id_to_date(12) == "1980-12"

    def test_id_13(self):
        assert month_id_to_date(13) == "1981-01"

    def test_known_id(self):
        assert month_id_to_date(540) == "2024-12"

    def test_leading_zero_month(self):
        assert month_id_to_date(3) == "1980-03"


class TestMonthIdRange:
    def test_single_month(self):
        assert month_id_range(2024, 1, 2024, 1) == [date_to_month_id(2024, 1)]

    def test_quarter(self):
        result = month_id_range(2024, 10, 2024, 12)
        assert len(result) == 3
        assert result[0] == date_to_month_id(2024, 10)
        assert result[-1] == date_to_month_id(2024, 12)

    def test_cross_year(self):
        result = month_id_range(2023, 11, 2024, 2)
        assert len(result) == 4

    def test_full_year(self):
        result = month_id_range(2020, 1, 2020, 12)
        assert len(result) == 12

    def test_consecutive_ids(self):
        result = month_id_range(2024, 1, 2024, 6)
        for i in range(1, len(result)):
            assert result[i] == result[i - 1] + 1
