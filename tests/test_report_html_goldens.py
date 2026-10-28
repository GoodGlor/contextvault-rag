"""The identity battery: the renderer's output, frozen.

Step 5a's whole claim is that inverting the renderer changes nothing, so the
instrument is a captured document rather than an assertion about one. Assertions
are on the HTML string — both renderers compress their content streams, which
made a byte-level PDF assertion meaningless once already.

Regenerate deliberately, never automatically, and read the diff:

    UPDATE_GOLDENS=1 DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib \\
        uv run pytest tests/test_report_html_goldens.py

A golden that rewrites itself when it fails asserts nothing at all.
"""

import datetime
import os
import pathlib
import uuid
from decimal import Decimal

import pytest

from contextvault.services.report_chart import render_chart_images
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_html import build_report_html
from contextvault.services.report_llm import ChartSpec
from contextvault.services.report_style import ReportStyle, resolve_style
from contextvault.services.report_types import SectionResult, SectionSourceRef

DIRECTORY = pathlib.Path(__file__).parent / "goldens" / "report_html"
REPORT_DATE = datetime.date(2026, 8, 5)  # fixed: "today" would restamp every cover

_TIME_SERIES = QueryResult(
    columns=["period", "revenue"],
    rows=[
        ("2021-01-01", Decimal("365.8")),
        ("2022-01-01", Decimal("394.3")),
        ("2023-01-01", Decimal("383.3")),
    ],
)
_WIDE = QueryResult(
    columns=[f"col_{index}" for index in range(9)],
    rows=[tuple(Decimal(index * 10 + step) for index in range(9)) for step in range(3)],
)


def _charted(style: ReportStyle) -> SectionResult:
    """A data section carrying a real drawn chart, as the pipeline produces it."""
    spec = ChartSpec(chart_type="line", x_column="period", y_column="revenue")
    images = render_chart_images(
        _TIME_SERIES,
        spec,
        language="uk",
        section_title="Динаміка виторгу",
        report_title="Звіт",
        theme=style.chart,
    )
    return SectionResult(
        title="Динаміка виторгу",
        source="database",
        sub_question="Як змінювався виторг?",
        kind="data",
        result=_TIME_SERIES,
        chart_png=None if images is None else images.png,
        chart_svg=None if images is None else images.svg,
        chart_spec=spec.model_dump(),
    )


def _uncharted() -> SectionResult:
    return SectionResult(
        title="Таблиця без графіка",
        source="database",
        sub_question="Скільки рядків?",
        kind="data",
        result=_TIME_SERIES,
    )


def _empty() -> SectionResult:
    return SectionResult(
        title="Порожній результат",
        source="database",
        sub_question="Нічого не знайдено?",
        kind="data",
        result=QueryResult(columns=["period", "revenue"], rows=[]),
    )


def _wide() -> SectionResult:
    return SectionResult(
        title="Широка таблиця",
        source="database",
        sub_question="Дев'ять колонок",
        kind="data",
        result=_WIDE,
    )


def _text_with_sources() -> SectionResult:
    return SectionResult(
        title="Ризики з документів",
        source="documents",
        sub_question="Які ризики?",
        kind="text",
        prose="Концентрація виторгу названа чинником ризику[1].",
        sources=[
            SectionSourceRef(
                number=1,
                title="Apple Inc. · Form 10-K",
                source_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )
        ],
    )


def _text_without_sources() -> SectionResult:
    return SectionResult(
        title="Коментар",
        source="documents",
        sub_question="Загальний висновок?",
        kind="text",
        prose="Жодне джерело не процитоване в цьому розділі.",
    )


def _failed() -> SectionResult:
    return SectionResult(
        title="Розділ, що не виконався",
        source="database",
        sub_question="Що пішло не так?",
        kind="error",
        error='QueryExecutionError: relation "missing" does not exist',
    )


def fixtures() -> dict[str, tuple[str, list[SectionResult], str, ReportStyle]]:
    """name → (title, sections, language, style).

    Built as a function rather than a module constant because the charted
    sections draw a real figure, and a module-level draw would run on import in
    every test session that touches this module.
    """
    kvarta = resolve_style("kvarta")  # numbered sections, zebra rows
    minimum = resolve_style("minimum")  # no numbering
    chasopys = resolve_style("chasopys")  # two-column prose
    return {
        "data_with_chart": ("Звіт", [_charted(kvarta)], "uk", kvarta),
        "data_without_chart": ("Звіт", [_uncharted()], "uk", kvarta),
        "data_no_rows": ("Звіт", [_empty()], "uk", kvarta),
        "data_wide_landscape": ("Звіт", [_wide()], "uk", kvarta),
        "text_with_sources": ("Звіт", [_text_with_sources()], "uk", kvarta),
        "text_without_sources": ("Звіт", [_text_without_sources()], "uk", kvarta),
        "error_section": ("Звіт", [_failed()], "uk", kvarta),
        "mixed_multi_section": (
            "Зведений звіт",
            [_charted(kvarta), _text_with_sources(), _failed(), _wide()],
            "uk",
            kvarta,
        ),
        "unnumbered_template": ("Звіт", [_uncharted(), _text_without_sources()], "uk", minimum),
        "two_column_prose": ("Звіт", [_text_with_sources()], "uk", chasopys),
        "english_language": ("Report", [_uncharted()], "en", kvarta),
    }


def render(name: str) -> str:
    title, sections, language, style = fixtures()[name]
    return build_report_html(
        title=title,
        sections=sections,
        language=language,
        style=style,
        report_date=REPORT_DATE,
    )


NAMES = sorted(fixtures())
UPDATING = os.environ.get("UPDATE_GOLDENS") == "1"


@pytest.mark.parametrize("name", NAMES)
def test_the_document_matches_its_golden(name: str) -> None:
    golden = DIRECTORY / f"{name}.html"
    actual = render(name)
    if UPDATING:
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="utf-8")
        pytest.skip(f"regenerated {golden.name}")
    assert golden.exists(), (
        f"missing golden for {name!r} — regenerate with UPDATE_GOLDENS=1 and review the diff"
    )
    assert actual == golden.read_text(encoding="utf-8")


def test_the_document_is_reproducible_within_one_process() -> None:
    """The battery above is worthless if the renderer is not deterministic.

    Guards Task 1's two fixes from the document's side: a chart's SVG is
    base64'd into the markup, so an unpinned hash salt or a restored <dc:date>
    surfaces here as well as in the chart suite.
    """
    assert render("data_with_chart") == render("data_with_chart")


def test_every_golden_on_disk_has_a_fixture() -> None:
    """A golden left behind by a renamed fixture would never be asserted again."""
    on_disk = {path.stem for path in DIRECTORY.glob("*.html")}
    assert on_disk == set(NAMES)
