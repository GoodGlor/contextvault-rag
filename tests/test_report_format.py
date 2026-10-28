"""Number/label formatting for generated reports.

These are the rules that keep `3.6498000E+11` and `Σ fiscal_year` out of a PDF,
so they are asserted directly rather than through the renderer.
"""

import datetime
from decimal import Decimal

import pytest

from contextvault.services.report_format import (
    ColumnKind,
    dimensions_identify_rows,
    format_number,
    format_tick,
    format_value,
    infer_column_kind,
    is_measure,
    is_period_column,
    label,
    resolve_language,
    row_noun,
    rows_note,
    spans_multiple_periods,
    sums_across_rows,
)


class TestInferColumnKind:
    def test_year_column_is_not_a_measure(self) -> None:
        # The bug that produced "Σ fiscal_year: 68,544.00".
        assert infer_column_kind("fiscal_year", [2024, 2023, 2022]) == "year"
        assert infer_column_kind("period_fiscal_year", [2024, 2021]) == "year"

    def test_currency_and_number(self) -> None:
        assert infer_column_kind("value", [Decimal("3.6498E+11")]) == "currency"
        assert infer_column_kind("cards_issued", [8276, 8141]) == "number"

    def test_percent_by_name(self) -> None:
        assert infer_column_kind("percentage_of_assets", [Decimal("84.39")]) == "percent"
        assert infer_column_kind("share_pct", [12.5]) == "percent"

    def test_text_and_empty(self) -> None:
        assert infer_column_kind("line_item", ["Assets", "Liabilities"]) == "text"
        assert infer_column_kind("anything", []) == "text"

    def test_a_non_finite_value_is_not_a_number(self) -> None:
        # A NaN reaches here from a float8 division or a Postgres numeric 'NaN'.
        # Ordering a NaN against the percent range raises InvalidOperation, and
        # int('NaN') raises ValueError — either one crashed the whole PDF, though
        # ``format_value`` promises never to raise. It is text, like any value
        # that is not a number.
        assert infer_column_kind("ratio", [float("nan")]) == "text"
        assert format_value(float("nan"), "number", "en") == "nan"
        assert format_value(float("inf"), "currency", "uk") == "inf"
        assert format_value(Decimal("NaN"), "percent", "en") == "NaN"

    def test_measure_in_year_range_without_year_name_is_a_measure(self) -> None:
        # 4-digit ints are only a year when the NAME says so — otherwise a real
        # measure that happens to land in 1900-2100 would be silently dropped.
        assert infer_column_kind("cards_issued", [2024, 1955]) == "number"

    @pytest.mark.parametrize(
        "kind,expected",
        [
            ("currency", True),
            ("number", True),
            ("percent", True),
            ("year", False),
            ("text", False),
        ],
    )
    def test_is_measure(self, kind: ColumnKind, expected: bool) -> None:
        assert is_measure(kind) is expected


class TestFormatNumber:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (Decimal("364980000000"), "364,98 млрд"),
            (Decimal("2500000"), "2,5 млн"),
            (Decimal("1000"), "1 тис."),
            (Decimal("999"), "999"),
            (Decimal("-364980000000"), "-364,98 млрд"),
            (Decimal("0"), "0"),
        ],
    )
    def test_uk_currency_scaling(self, value: Decimal, expected: str) -> None:
        assert format_number(value, "currency", "uk").replace(" ", " ") == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            (Decimal("364980000000"), "364.98 bn"),
            (Decimal("2500000"), "2.5 M"),
            (Decimal("999"), "999"),
        ],
    )
    def test_en_currency_scaling(self, value: Decimal, expected: str) -> None:
        assert format_number(value, "currency", "en").replace(" ", " ") == expected

    def test_percent_one_decimal(self) -> None:
        assert format_number(Decimal("84.39640528248123"), "percent", "uk") == "84,4%"
        assert format_number(Decimal("100.0000000000"), "percent", "en") == "100.0%"

    def test_counts_are_grouped_not_scaled(self) -> None:
        # A count keeps every digit: "8,28 тис." would throw away precision that
        # a count of cards still needs.
        assert format_number(8276, "number", "uk").replace("\u00a0", " ") == "8 276"
        assert format_number(62025218, "number", "en") == "62,025,218"

    def test_year_has_no_grouping_separator(self) -> None:
        assert format_number(2024, "year", "uk") == "2024"
        assert format_number(2024, "year", "en") == "2024"

    def test_scientific_notation_never_leaks(self) -> None:
        rendered = format_number(Decimal("3.6498000E+11"), "currency", "uk")
        assert "E+" not in rendered and "e+" not in rendered


class TestFormatValue:
    def test_none_renders_empty(self) -> None:
        assert format_value(None, "currency", "uk") == ""

    def test_text_passes_through(self) -> None:
        assert format_value("Assets", "text", "uk") == "Assets"

    def test_unexpected_type_degrades_to_str(self) -> None:
        assert format_value(object, "currency", "uk").startswith("<")


class TestLabels:
    def test_known_keys_localised(self) -> None:
        assert label("rows", "uk") == "Рядків"
        assert label("rows", "en") == "Rows"
        assert "документ" in label("no_documents", "uk").lower()

    def test_unknown_language_falls_back_to_english(self) -> None:
        assert label("rows", "de") == "Rows"


class TestResolveLanguage:
    def test_supported_value_kept(self) -> None:
        assert resolve_language("uk", prompt="anything") == "uk"

    def test_cyrillic_prompt_falls_back_to_uk(self) -> None:
        assert resolve_language(None, prompt="Зроби звіт по активах") == "uk"
        assert resolve_language("de", prompt="Зроби звіт") == "uk"

    def test_latin_prompt_falls_back_to_en(self) -> None:
        assert resolve_language(None, prompt="Build an assets report") == "en"


class TestSummability:
    """Which columns may carry a Σ — three separate reasons a total is wrong."""

    def test_a_price_is_never_totalled(self) -> None:
        """ "Σ Ціна закриття: 414,87 тис." — the total of 5,350 daily closes."""
        assert infer_column_kind("close_price", [Decimal("120.5")]) == "currency"
        assert not sums_across_rows("close_price", "currency", over_periods=True)
        assert not sums_across_rows("close_price", "currency", over_periods=False)
        assert not sums_across_rows("pe_ratio", "number", over_periods=False)
        assert not sums_across_rows("earnings_per_share", "currency", over_periods=False)

    def test_a_level_is_not_totalled_across_periods(self) -> None:
        """The reader's own words: assets and liabilities cannot be added for quarters."""
        assert not sums_across_rows("total_assets", "currency", over_periods=True)
        assert not sums_across_rows("total_liabilities", "currency", over_periods=True)
        assert not sums_across_rows("stockholders_equity", "currency", over_periods=True)

    def test_the_same_level_may_be_totalled_within_one_period(self) -> None:
        """Assets broken down by category at one date do add up to total assets."""
        assert sums_across_rows("total_assets", "currency", over_periods=False)

    def test_a_flow_is_totalled_either_way(self) -> None:
        assert sums_across_rows("total_revenue", "currency", over_periods=True)
        assert sums_across_rows("net_income", "currency", over_periods=True)

    def test_a_percentage_never_gets_a_total(self) -> None:
        assert not sums_across_rows("share_of_assets", "percent", over_periods=False)


class TestPeriodDetection:
    def test_dates_years_and_text_quarters_all_read_as_periods(self) -> None:
        assert is_period_column("quarter_end", [datetime.date(2024, 3, 31)])
        assert is_period_column("fiscal_year", [2023, 2024])
        assert is_period_column("period", ["2024-Q1", "2024-Q2"])
        assert not is_period_column("company", ["Apple", "Microsoft"])

    def test_abbreviated_period_names_read_as_periods(self) -> None:
        """``fy`` and ``period`` are how a query names a fiscal year in practice.

        ``_YEAR_NAME`` needs a ``_`` or a string boundary before its alternative,
        so ``fy`` cannot match it however the values look — and the values are
        bare integers, so neither the date branch nor ``_PERIOD_TEXT`` catches
        them either. The column was read as an ordinary measure, which is what
        offered a pie across 2021/2022/2023.
        """
        assert is_period_column("fy", [2021, 2022, 2023])
        assert is_period_column("period", [2021, 2022, 2023])

    def test_an_abbreviated_year_column_also_formats_as_a_year(self) -> None:
        """The same gap has a visible half: ``2,021`` in a table cell."""
        assert infer_column_kind("fy", [2021, 2022, 2023]) == "year"
        assert infer_column_kind("period", [2021, 2022, 2023]) == "year"

    def test_an_abbreviation_inside_a_longer_name_is_not_a_year(self) -> None:
        """The whole-name discipline, and it is load-bearing rather than cosmetic.

        Spelled-out ``year`` is unambiguous as a *component* (``fiscal_year``),
        but these abbreviations are not: ``period_amount`` is money. A $2,024.00
        total is integral and sits inside the 1900-2100 guard, so the range check
        alone would happily call it a year and strip its grouping.
        """
        assert infer_column_kind("period_amount", [Decimal("2000.00"), Decimal("2024.00")]) == (
            "currency"
        )
        assert not is_period_column("period_amount", [Decimal("2000.00"), Decimal("2024.00")])

    def test_one_repeated_period_does_not_count_as_spanning(self) -> None:
        columns = ["quarter_end", "category", "amount"]
        one_date = [
            (datetime.date(2024, 3, 31), "Cash", 1),
            (datetime.date(2024, 3, 31), "Inventory", 2),
        ]
        assert not spans_multiple_periods(columns, one_date)
        many = [
            (datetime.date(2024, 3, 31), "Cash", 1),
            (datetime.date(2024, 6, 30), "Cash", 2),
        ]
        assert spans_multiple_periods(columns, many)


class TestNamingTraps:
    def test_a_share_count_is_not_a_percentage(self) -> None:
        """`shares_outstanding` matched a bare `share` and rendered as 12,000,000,000.0%."""
        assert infer_column_kind("shares_outstanding", [12_000_000_000]) != "percent"

    def test_a_margin_in_dollars_is_money_and_in_points_is_a_percentage(self) -> None:
        assert infer_column_kind("gross_margin", [Decimal("1.7E+11")]) == "currency"
        assert infer_column_kind("operating_margin", [Decimal("34.2")]) == "percent"

    def test_equity_and_capex_scale_like_the_money_they_are(self) -> None:
        """They fell through to `number` and printed as 4 402 442 793 619."""
        assert infer_column_kind("stockholders_equity", [Decimal("4402442793619")]) == "currency"
        assert infer_column_kind("capital_expenditures", [Decimal("-146748093096")]) == "currency"
        assert "млрд" in format_number(Decimal("4402442793619"), "currency", "uk")


class TestPlurals:
    def test_ukrainian_row_counts_agree_with_their_noun(self) -> None:
        assert row_noun(1, "uk") == "рядок"
        assert row_noun(31, "uk") == "рядок"
        assert row_noun(3, "uk") == "рядки"
        assert row_noun(5, "uk") == "рядків"
        assert row_noun(11, "uk") == "рядків"  # the teens are the trap
        assert row_noun(12, "uk") == "рядків"

    def test_english_keeps_two_forms(self) -> None:
        assert row_noun(1, "en") == "row"
        assert row_noun(2, "en") == "rows"

    def test_the_note_interpolates_both_count_and_noun(self) -> None:
        assert rows_note("more_rows", 31, "uk") == "… ще 31 рядок не показано"
        assert rows_note("more_rows", 5, "en") == "… 5 more rows omitted"


class TestAxisTicks:
    def test_a_tick_may_scale_a_count_that_a_cell_may_not(self) -> None:
        """A locator has already rounded the tick, so scaling it loses nothing."""
        assert format_number(20_000_000, "number", "uk") == "20\u00a0000\u00a0000"
        assert format_tick(20_000_000, "number", "uk") == "20 млн"

    def test_a_year_tick_never_scales(self) -> None:
        assert format_tick(2024, "year", "uk") == "2024"

    def test_a_percentage_tick_keeps_its_sign(self) -> None:
        assert format_tick(Decimal("34.2"), "percent", "en") == "34.2%"


class TestLongFormatLabels:
    """The SEC tables are long format: the measure column is called `value`.

    A campaign run printed "Σ Загальні активи" across seventy quarters because
    the column *name* carried no meaning — only the heading the model wrote did.
    """

    def test_a_level_named_only_by_its_label_is_not_totalled(self) -> None:
        assert not sums_across_rows("value", "currency", over_periods=True, label="Загальні активи")
        assert not sums_across_rows(
            "value", "currency", over_periods=True, label="Акціонерний капітал"
        )
        assert not sums_across_rows("value", "currency", over_periods=True, label="Total Assets")

    def test_capital_expenditure_is_still_a_flow(self) -> None:
        """ "Капітальні витрати" contains "капітал" and must not be caught by it."""
        assert sums_across_rows(
            "capital_expenditures", "currency", over_periods=True, label="Капітальні витрати"
        )

    def test_a_flow_named_only_by_its_label_still_totals(self) -> None:
        assert sums_across_rows("value", "currency", over_periods=True, label="Чистий прибуток")

    def test_a_return_measure_in_the_label_is_not_totalled(self) -> None:
        assert not sums_across_rows(
            "value", "number", over_periods=True, label="Рентабельність активів"
        )


class TestDimensionsIdentifyRows:
    def test_a_repeated_fiscal_year_does_not_identify_its_rows(self) -> None:
        """sec-01's exact shape: rows for one year, one per period length.

        Nothing in the result distinguishes them, so they are alternative
        measurements of one cell rather than several observations.
        """
        columns = ["fiscal_year", "revenue"]
        rows: list[tuple[object, ...]] = [
            (2016, Decimal("215639000000")),
            (2017, Decimal("78351000000")),
            (2017, Decimal("52896000000")),
            (2017, Decimal("45408000000")),
            (2017, Decimal("229234000000")),
        ]
        assert dimensions_identify_rows(columns, rows) is False

    def test_a_date_repeated_across_companies_still_identifies_its_rows(self) -> None:
        """The case a naive "first column repeats" rule gets wrong.

        ``date`` repeats, but ``(date, company)`` is unique, so Σ revenue is a
        figure a reader can trust.
        """
        columns = ["date", "company", "revenue"]
        rows: list[tuple[object, ...]] = [
            ("2024-Q1", "Apple", Decimal("90753000000")),
            ("2024-Q1", "Microsoft", Decimal("61858000000")),
            ("2024-Q2", "Apple", Decimal("85777000000")),
            ("2024-Q2", "Microsoft", Decimal("64727000000")),
        ]
        assert dimensions_identify_rows(columns, rows) is True

    def test_a_single_row_identifies_itself(self) -> None:
        columns = ["fiscal_year", "revenue"]
        rows: list[tuple[object, ...]] = [(2017, Decimal("229234000000"))]
        assert dimensions_identify_rows(columns, rows) is True

    def test_a_result_of_only_measures_cannot_be_judged_and_is_allowed(self) -> None:
        """With no dimension columns there is no key to repeat.

        Returning False here would suppress Σ on every single-column aggregate —
        the common, correct case.
        """
        columns = ["revenue"]
        rows: list[tuple[object, ...]] = [(Decimal("1"),), (Decimal("2"),)]
        assert dimensions_identify_rows(columns, rows) is True


class TestOverlappingRows:
    def test_a_repeated_key_blocks_a_total_the_other_rules_would_allow(self) -> None:
        """Revenue is a flow, so summing it across periods is legitimate — every
        existing rule says yes. The rows overlapping is the only thing wrong, and
        it is the fifth cause of a wrong Σ."""
        assert sums_across_rows("revenue", "currency", over_periods=True, label="Дохід") is True
        assert (
            sums_across_rows(
                "revenue", "currency", over_periods=True, label="Дохід", keys_repeat=True
            )
            is False
        )


def test_every_label_exists_in_both_languages() -> None:
    from contextvault.services.report_format import _LABELS

    assert set(_LABELS["en"]) == set(_LABELS["uk"])
