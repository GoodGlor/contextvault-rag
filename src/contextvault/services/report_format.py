"""Number and label formatting for generated reports.

The pipeline's rule is that the LLM decides *language* and this module decides
*numbers* — asking a model to format figures is how ``3.6498000E+11`` reaches a
table cell. Everything here is pure and dependency-light (no matplotlib, fpdf or
SQLAlchemy) so the PDF renderer, the chart renderer and any later exporter share
one implementation, and so it can be tested directly.
"""

import datetime
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

ColumnKind = Literal["year", "percent", "currency", "number", "text"]

SUPPORTED_LANGUAGES = ("en", "uk")
DEFAULT_LANGUAGE = "en"

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_YEAR_NAME = re.compile(r"(^|_)(year|yr|рік|роки|року)($|_)", re.IGNORECASE)
# ``fy`` and ``period`` name a fiscal year as often as ``fiscal_year`` does, but
# only as the WHOLE column name. Spelled-out ``year`` is unambiguous as a
# component; these abbreviations are not — ``period_amount`` is money, and a
# $2,024.00 total is integral and inside the range guard below, so a component
# match would strip its grouping and call it a year. Missing them offered a pie
# across 2021/2022/2023, the exact chart the period rule exists to prevent.
_YEAR_ALIAS_NAME = re.compile(r"^(fy|period)$", re.IGNORECASE)
# ``share`` deliberately does NOT appear bare: ``shares_outstanding`` is a count of
# shares, and matching it here rendered 12 billion shares as "12,000,000,000.0%".
# ``per_share`` is money (EPS), so it is excluded from the ``share_of`` alternative.
_PERCENT_NAME = re.compile(
    r"(percent|percentage|_pct|^pct|ratio|margin|(?<!per_)share_of|market_share"
    r"|відсот|частк)",
    re.IGNORECASE,
)
# A percentage is also a *range*: "margin" names the dollar amount as often as the
# rate, and 1.7e11 is plainly not a percentage. Same name-AND-range discipline the
# year check uses.
_PERCENT_MAX = Decimal(1000)
_CURRENCY_NAME = re.compile(
    r"(value|amount|revenue|assets|liabilit|income|profit|loss|cost|expense|cash|limit"
    r"|price|close|open|high|low|equity|capital|expenditure|capex|dividend|debt|payable"
    r"|receivable|inventor|goodwill|earnings|ebit|flow|per_share|eps|payment|salary|wage|margin"
    r"|сум|варт|дохід|актив|прибут|збит|витрат|потік|зобов|капітал|ціна|виплат)",
    re.IGNORECASE,
)
# Intensive quantities: a price, a rate or a per-unit figure has no total, ever.
# Summing 5,350 daily closes produced "Σ Ціна закриття: 414,87 тис." — a number
# that means nothing.
_INTENSIVE_NAME = re.compile(
    r"(price|close|open|high|low|rate|ratio|margin|yield|per_share|eps|percent|pct"
    r"|average|avg|median"
    # Returns and efficiency measures. A campaign run printed "Σ ROA" and
    # "Σ ROE" across five fiscal years — five percentages added together.
    r"|\broa\b|\broe\b|\broi\b|\broic\b|return_on|turnover|coefficient"
    r"|ціна|курс|середн|рентабельн|коефіцієнт|оборотн)",
    re.IGNORECASE,
)
# Stock (level) quantities: meaningful *within* one period, double-counted when
# added across periods. The user's own words: assets and liabilities cannot be
# added together for quarters.
_LEVEL_NAME = re.compile(
    r"(assets|liabilit|equity|debt|balance|inventor|goodwill|cash_and|outstanding"
    r"|headcount|employees|shareholder|stockholder"
    # "капітальні витрати" is capital *expenditure* — a flow that sums perfectly
    # well. Only bare "капітал" (equity) is a level.
    r"|актив|зобов|капітал(?!ьн)|власний|залишок|запас|борг)",
    re.IGNORECASE,
)
# '2024', '2024-03', '2024-03-31', '2024Q1', '2024-Q1' — periods a query produced
# as text (``to_char``) rather than as a date.
_PERIOD_TEXT = re.compile(r"^\s*\d{4}([-/](0?[1-9]|1[0-2])([-/]\d{1,2})?|[-\s]?Q[1-4])?\s*$")

# Magnitude suffixes, largest first. The unit word is part of the localisation:
# a Ukrainian report must not read "364.98 bn".
_SCALES: dict[str, tuple[tuple[Decimal, str], ...]] = {
    "en": ((Decimal("1e9"), "bn"), (Decimal("1e6"), "M"), (Decimal("1e3"), "k")),
    "uk": ((Decimal("1e9"), "млрд"), (Decimal("1e6"), "млн"), (Decimal("1e3"), "тис.")),
}

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "rows": "Rows",
        "page": "Page",
        "more_rows": "… {count} more {noun} omitted",
        "no_documents": "No relevant information was found in the documents for this section.",
        "no_rows": "The database holds no data matching this question.",
        "section_failed": "This section could not be generated.",
        "other": "Other",
        "more_categories": "{count} smaller categories not shown",
        "continued": "(continued)",
        "middle_rows": "… {count} more {noun} between",
        "contents": "Contents",
        "prepared_by": "Prepared by",
        "approved_by": "Approved by",
        "report_eyebrow": "Report",
        "prepared": "Prepared",
        # Σ and x̄ are notation, not words: they sit side by side on one line and
        # a reader who has not met them cannot tell which figure is which.
        "stat_total": "Total",
        "stat_mean": "Average",
        "overlapping_periods": (
            "These rows may count the same figure more than once — the periods overlap. "
            "No total is shown."
        ),
        # #204: the merged table's provenance column, and the note naming the
        # databases that answered with nothing.
        "source_database": "Database",
        "no_rows_from": "No rows came back from {names}.",
        "database_removed": "a database that is no longer connected",
        # #205: under a figure drawn from more than one database.
        "drawn_from": "Drawn from {names}",
    },
    "uk": {
        "rows": "Рядків",
        "page": "Стор.",
        "more_rows": "… ще {count} {noun} не показано",
        "no_documents": "У документах не знайдено релевантної інформації для цього розділу.",
        "no_rows": "У базі даних немає даних, що відповідають цьому запитанню.",
        "section_failed": "Цей розділ не вдалося сформувати.",
        "other": "Інше",
        "more_categories": "{count} менших категорій не показано",
        "continued": "(продовження)",
        "middle_rows": "… ще {count} {noun} посередині",
        "contents": "Зміст",
        "prepared_by": "Підготував",
        "approved_by": "Затвердив",
        "report_eyebrow": "Звіт",
        "prepared": "Сформовано",
        "stat_total": "Сума",
        "stat_mean": "Середнє",
        "overlapping_periods": (
            "Ці рядки можуть враховувати той самий показник кілька разів — періоди "
            "перекриваються. Підсумок не наведено."
        ),
        "source_database": "База даних",
        "no_rows_from": "Немає даних із: {names}.",
        "database_removed": "база даних, яку від'єднано",
        "drawn_from": "Джерела даних: {names}",
    },
}

# Ukrainian counts three ways: 1 рядок · 2-4 рядки · 5-20 рядків, repeating per
# hundred. "31 рядків" is simply wrong, and a report that formats numbers
# carefully must not fumble the word next to them.
_ROW_NOUN = {
    "en": ("row", "rows", "rows"),
    "uk": ("рядок", "рядки", "рядків"),
}


def _plural_index(count: int, language: str) -> int:
    number = abs(count)
    if language != "uk":
        return 0 if number == 1 else 1
    if number % 100 in range(11, 15):
        return 2
    last = number % 10
    if last == 1:
        return 0
    if last in (2, 3, 4):
        return 1
    return 2


def row_noun(count: int, language: str) -> str:
    """The correct plural form of "row" for ``count`` in ``language``."""
    forms = _ROW_NOUN.get(language, _ROW_NOUN["en"])
    return forms[_plural_index(count, language)]


def rows_note(key: str, count: int, language: str) -> str:
    """A localised "… N more rows omitted" line with the plural agreed."""
    return label(key, language).format(count=count, noun=row_noun(count, language))


# No-break space so a grouped figure never wraps mid-number inside a table cell.
_GROUP_SEP = {"en": ",", "uk": "\u00a0"}
_DECIMAL_SEP = {"en": ".", "uk": ","}


def resolve_language(value: str | None, *, prompt: str) -> str:
    """Validate the planner's language, falling back to a script check on the prompt.

    Never raises: an unsupported or missing value degrades to a guess rather than
    failing a report that would otherwise have rendered.
    """
    if value is not None:
        primary = value.strip().lower().replace("_", "-").split("-")[0]
        if primary in SUPPORTED_LANGUAGES:
            return primary
    return "uk" if _CYRILLIC.search(prompt or "") else DEFAULT_LANGUAGE


def _as_decimal(value: Any) -> Decimal | None:
    """The value as a Decimal, or ``None`` when it is not a formattable number.

    NaN and ±Infinity are deliberately *not* numbers here: both a float8 division
    and a Postgres ``numeric`` can produce them, and every arithmetic path below
    breaks on them — ordering a NaN raises ``InvalidOperation`` and ``int('NaN')``
    raises ``ValueError``, which took down the whole PDF even though
    ``format_value`` promises never to raise. Treated as unformattable, they fall
    through to the text rendering every unknown type gets.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def infer_column_kind(name: str, values: list[Any]) -> ColumnKind:
    """Classify a result column so the renderer knows how to format and total it.

    A year is detected by name *and* value range together: a genuine measure that
    happens to sit in 1900-2100 (a count, say) keeps its measure kind, and a
    column named ``*_year`` holding real amounts is not mistaken for a year.
    """
    numbers = [d for d in (_as_decimal(v) for v in values) if d is not None]
    if not numbers:
        return "text"
    if (_YEAR_NAME.search(name) or _YEAR_ALIAS_NAME.match(name)) and all(
        d == d.to_integral_value() and Decimal(1900) <= d <= Decimal(2100) for d in numbers
    ):
        return "year"
    if _PERCENT_NAME.search(name) and all(abs(d) <= _PERCENT_MAX for d in numbers):
        return "percent"
    if _CURRENCY_NAME.search(name):
        return "currency"
    return "number"


def is_period_column(name: str, values: list[Any]) -> bool:
    """True when a column identifies the reporting period a row belongs to.

    Dates and datetimes count, as do years and the text periods a ``to_char``
    query produces (``2024-Q1``). Used to decide whether a column of levels is a
    time series, which is what makes its total meaningless.
    """
    non_null = [v for v in values if v is not None]
    if not non_null:
        return False
    if all(isinstance(v, (datetime.date, datetime.datetime)) for v in non_null):
        return True
    if infer_column_kind(name, values) == "year":
        return True
    return all(isinstance(v, str) and _PERIOD_TEXT.match(v) for v in non_null)


def spans_multiple_periods(columns: list[str], rows: list[tuple[Any, ...]]) -> bool:
    """True when the result's rows are different reporting periods.

    One period broken down by category may be totalled; the same balance across
    twelve quarters may not.
    """
    for index, name in enumerate(columns):
        values = [row[index] for row in rows]
        if is_period_column(name, values) and len({str(v) for v in values}) > 1:
            return True
    return False


def dimension_indexes(columns: list[str], rows: list[tuple[Any, ...]]) -> list[int]:
    """Which columns are the result's key rather than something it measures.

    Shared by :func:`dimensions_identify_rows` and the feedback that names the
    offending value, so the two can never disagree about what the key is.
    """
    return [
        index
        for index, name in enumerate(columns)
        if not is_measure(infer_column_kind(name, [row[index] for row in rows]))
    ]


def dimensions_identify_rows(columns: list[str], rows: list[tuple[Any, ...]]) -> bool:
    """Whether the non-measure columns pick out one row each.

    When they do not, two rows are alternative measurements of the same cell
    rather than two observations, and both totalling and charting them are
    unsound. The SEC long format produces exactly this: a 10-K carries the
    quarterly, year-to-date *and* annual figure for one tag, all under
    ``filing_fiscal_period = 'FY'``, so a result keyed on the fiscal year alone
    holds five rows for 2017 whose sum is five times the revenue.

    Deliberately not "the first column repeats". In ``(date, company, revenue)``
    the date repeats across companies while ``(date, company)`` is unique, and
    Σ revenue is a figure a reader can trust.

    A result with no dimension columns at all is *allowed*: there is no key to
    repeat, and answering False would suppress Σ on every single-column
    aggregate, which is the common and correct case.
    """
    dimensions = dimension_indexes(columns, rows)
    if not dimensions:
        return True
    keys = {tuple(str(row[index]) for index in dimensions) for row in rows}
    return len(keys) == len(rows)


def sums_across_rows(
    name: str,
    kind: ColumnKind,
    *,
    over_periods: bool,
    label: str = "",
    keys_repeat: bool = False,
) -> bool:
    """Whether Σ over the result's rows is a figure a reader can trust.

    Four separate reasons a total is wrong, in order of how badly: the rows are
    not distinct observations at all (``keys_repeat`` — see
    :func:`dimensions_identify_rows`), the kind has no total at all (a
    percentage), the quantity is intensive (a price), or the quantity is a level
    and the rows are periods (assets per quarter).

    ``label`` matters because the SEC tables are **long** format: the measure
    column is called ``value`` and the concept lives elsewhere, so a query for
    total assets per quarter yields a column whose *name* says nothing at all.
    The reader-facing heading the model wrote — "Загальні активи" — is the only
    place the meaning survives, so it is classified too.
    """
    # The fourth reason, and the only one about the rows rather than the column:
    # when the dimensions do not identify a row, two rows measure overlapping
    # spans of the same thing. Adding them is not addition.
    if keys_repeat:
        return False
    if not can_sum(kind):
        return False
    described = f"{name} {label}".strip()
    if _INTENSIVE_NAME.search(described):
        return False
    return not (over_periods and _LEVEL_NAME.search(described))


def _group(integer_part: str, language: str) -> str:
    sep = _GROUP_SEP.get(language, _GROUP_SEP["en"])
    sign, digits = ("-", integer_part[1:]) if integer_part.startswith("-") else ("", integer_part)
    grouped = f"{int(digits):,}".replace(",", sep)
    return sign + grouped


def _trim(value: Decimal, places: int, language: str, *, group: bool) -> str:
    quantized = round(value, places)
    text = f"{quantized:f}"
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    whole = _group(whole, language) if group else whole
    if not frac:
        return whole
    return whole + _DECIMAL_SEP.get(language, ".") + frac


def format_number(value: Any, kind: ColumnKind, language: str) -> str:
    """A number as a reader sees it: scaled, localised, never in scientific notation."""
    number = _as_decimal(value)
    if number is None:
        return "" if value is None else str(value)
    if kind == "year":
        return f"{int(number)}"
    if kind == "percent":
        return f"{_force_places(number, 1, language)}%"
    if kind == "currency":
        # Only money scales. A count of 8,276 cards must not become "8,28 тис." —
        # scaling trades away precision that a count still needs.
        magnitude = abs(number)
        for threshold, suffix in _SCALES.get(language, _SCALES["en"]):
            if magnitude >= threshold:
                return f"{_trim(number / threshold, 2, language, group=False)} {suffix}"
    return _trim(number, 2, language, group=True)


def format_tick(value: Any, kind: ColumnKind, language: str) -> str:
    """A number as an *axis tick*, where a large count may scale.

    ``format_number`` deliberately refuses to scale counts, because ``8,28 тис.``
    throws away precision a table cell still needs. A tick is not a cell: the
    locator has already chosen round numbers, so ``20 млн`` says exactly what
    ``20 000 000`` said, in a third of the width.
    """
    number = _as_decimal(value)
    if number is None or kind in ("year", "percent"):
        return format_number(value, kind, language)
    magnitude = abs(number)
    for threshold, suffix in _SCALES.get(language, _SCALES["en"]):
        if magnitude >= threshold:
            return f"{_trim(number / threshold, 2, language, group=False)} {suffix}"
    return format_number(value, kind, language)


def _force_places(number: Decimal, places: int, language: str) -> str:
    """Fixed decimal places — percentages read better as 100.0% than 100%."""
    text = f"{round(number, places):f}"
    whole, _, frac = text.partition(".")
    frac = (frac + "0" * places)[:places]
    return _group(whole, language) + _DECIMAL_SEP.get(language, ".") + frac


def format_value(value: Any, kind: ColumnKind, language: str) -> str:
    """Any cell value as display text. Never raises — unknown types degrade to ``str``."""
    if value is None:
        return ""
    if kind == "text":
        return clean_text(str(value))
    if _as_decimal(value) is None:
        return clean_text(str(value))
    return format_number(value, kind, language)


def aggregates_meaningfully(row_count: int) -> bool:
    """Whether Σ/x̄ over ``row_count`` rows tells a reader anything.

    Over a single row they do not: the "total" is the value itself and the "mean"
    is the same number a third time, but both are labelled as if the figure had
    been aggregated over something. A third of the totals a hundred-report run
    printed were this — "Σ Total Assets" under a request for one company's most
    recent balance.
    """
    return row_count > 1


def clean_text(value: str) -> str:
    """Strip control characters a font cannot draw.

    A model reply carried a bare ESC (0x1b) into a table cell, and fpdf warned
    that DejaVu has no glyph for it — the cell rendered as a gap. Tabs and
    newlines are kept; everything else below 0x20, plus the C1 range, goes.
    """
    return "".join(
        character
        for character in value
        if character in "\t\n"
        or not ("\x00" <= character <= "\x1f" or "\x7f" <= character <= "\x9f")
    )


# Everything from here on is a driver's own diagnostics: the echoed statement,
# the exception class, the docs link. None of it belongs on a reader's page.
_ERROR_NOISE = re.compile(r"\[SQL:.*|\(Background on this error.*|<class '[^']+'>:?", re.DOTALL)


def short_error(text: str | None, limit: int = 180) -> str:
    """A failure a reader can take in at a glance.

    A rejected query put the whole statement, the asyncpg exception class and a
    documentation URL into the report — half a page of internals under a heading
    that had promised an analysis. The full text stays on the report row for an
    admin; this is the part that gets printed.
    """
    if not text:
        return ""
    cleaned = " ".join(_ERROR_NOISE.sub("", clean_text(text)).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip(" ,;:") + "…"


def is_measure(kind: ColumnKind) -> bool:
    """Whether a column may be summed or averaged in the statistics block."""
    return kind in ("currency", "number", "percent")


def can_sum(kind: ColumnKind) -> bool:
    """Totals make sense for amounts and counts; a total of percentages does not."""
    return kind in ("currency", "number")


def label(key: str, language: str) -> str:
    """A localised UI string for the renderers."""
    table = _LABELS.get(language if language in _LABELS else DEFAULT_LANGUAGE, _LABELS["en"])
    return table.get(key, _LABELS["en"].get(key, key))


def measure_summary(
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    language: str,
    column_labels: dict[str, str],
    max_columns: int,
    overlapping: bool = False,
) -> list[tuple[str, str]]:
    """(label, value) pairs summarising every column that is genuinely a measure.

    **The single implementation of the total/mean decision** (printed as `Сума`/
    `Середнє`, `Total`/`Average` — the glyphs `Σ` and `x̄` are what this used to
    say, and #184 replaced them because they are notation, not words). The
    document and the deck
    each used to derive it themselves, and the copies drifted twice in the same
    direction: #176 taught ``keys_repeat`` to the HTML path alone and #178 taught
    it ``overlap_warning`` alone, so v42 shipped a deck printing Σ for exactly the
    rows the PDF withheld. The deck is the worse of the two to get wrong, because
    a frozen schedule emits it nightly with nobody reading over its shoulder.
    What both drifts had in common is that mutating one path left the other
    green — so the duplication is gone only while a change here can break both
    renderers' tests at once.

    ``columns``/``rows`` rather than a ``QueryResult``, because this module is
    dependency-light by contract (see the module docstring) and ``QueryResult``
    lives beside SQLAlchemy. It is also the shape ``dimensions_identify_rows``
    already takes.

    ``max_columns`` is a parameter rather than a constant because the two media
    disagree for recorded reasons: an A4 page allows 4 (nineteen Σ/x̄ lines pushed
    the content off it) and a slide allows 3 (it has no room for more). Neither
    number is the "real" one.

    ``overlapping`` is ``SectionResult.overlap_warning`` reduced to a flag: the
    query aggregated a table whose rows cover overlapping spans, so the *rows
    themselves* double-count before any column is classified. It cannot be
    derived here — ``keys_repeat`` below reads the returned keys, and this shape
    returns one row per year with the keys genuinely distinct. That is the whole
    premise of the defect, so the flag has to arrive from the section. The count
    is still a fact; neither Σ nor x̄ is, and the caveat each renderer prints
    beside them says in words that no total is shown.

    Three separate things make a total wrong, and all three were once printed.
    Totalling every numeric column produced ``Σ fiscal_year: 68,544`` (fixed by
    ``infer_column_kind``); totalling an intensive quantity produced ``Σ Ціна
    закриття: 414,87 тис.`` from 5,350 daily closes; and totalling a level across
    periods produced ``Σ Загальні активи`` over sixty quarters — the very thing a
    reader asking for quarterly balances tells you not to do. ``sums_across_rows``
    draws all three lines; the mean stays available in every case.

    Returns pairs, not rendered text: HTML lays them out as spans and PPTX joins
    them into one line. Only the decisions live here; the formatting does not.
    """
    stats: list[tuple[str, str]] = [(label("rows", language), str(len(rows)))]
    if overlapping:
        return stats
    if not aggregates_meaningfully(len(rows)):
        # One row: the "total" is the value, the "mean" is the value again.
        return stats
    over_periods = spans_multiple_periods(columns, rows)
    # A repeated key means the rows overlap; neither Σ nor x̄ survives that.
    keys_repeat = not dimensions_identify_rows(columns, rows)
    shown = 0
    for index, name in enumerate(columns):
        if shown >= max_columns:
            break
        column = [row[index] for row in rows]
        kind = infer_column_kind(name, column)
        if not is_measure(kind):
            continue
        # Decimal is neither int nor float — money arrives from asyncpg as Decimal,
        # so testing only int/float silently drops every real amount.
        values = [
            Decimal(str(v))
            for v in column
            if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
        ]
        if not values or len(values) != len(rows):
            continue
        heading = str(column_labels.get(name, name))
        if sums_across_rows(
            name, kind, over_periods=over_periods, label=heading, keys_repeat=keys_repeat
        ):
            stats.append(
                (
                    f"{label('stat_total', language)} {heading}",
                    format_number(sum(values), kind, language),
                )
            )
        if not keys_repeat:
            mean = sum(values) / Decimal(len(values))
            stats.append(
                (
                    f"{label('stat_mean', language)} {heading}",
                    format_number(mean, kind, language),
                )
            )
        shown += 1
    return stats
