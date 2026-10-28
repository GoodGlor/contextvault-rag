"""Byte-identity battery for the deck (step 5b).

Captured from the renderer **before** the block inversion and held identical
through it. That ordering is the whole point: a golden captured afterwards
asserts only that the new code equals itself. ``git log -- tests/goldens/report_pptx``
showing exactly one commit is the check that it was done right.

A .pptx is byte-reproducible only because Task 1 made it so: python-pptx stamps
every zip entry's date_time from the wall clock, so two builds seconds apart
differed while every part's content matched. ``build_report_pptx`` now pins those
timestamps. The embedded chart PNG needed nothing — matplotlib's PNG writer emits
only a ``Software`` tEXt chunk, no date.

The charted fixture sets ``chart_svg`` alongside ``chart_png`` (both captured
from one ``render_chart_images`` call) because that pairing is what production
always produces and ``default_blocks`` keys the chart block's existence on
``chart_svg`` — a PNG-only section cannot occur outside a test, so this fixture
must not manufacture one.

The chart images are read from ``tests/fixtures/`` rather than rendered per run.
Pinning the zip timestamps made a deck reproducible across *time*; it did not
make one reproducible across *platforms*. A matplotlib PNG is a raster, and
raster bytes depend on the freetype build doing the glyph hinting, so goldens
captured on macOS cannot byte-match a build on Linux. CI proved it: all seven
charted decks failed there and all five chartless ones passed.

Regenerate DELIBERATELY and read the diff:
    UPDATE_PPTX_GOLDENS=1 uv run pytest tests/test_report_pptx_goldens.py
"""

import hashlib
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from contextvault.services.report_execution import QueryResult
from contextvault.services.report_render_pptx import build_report_pptx
from contextvault.services.report_style import TEMPLATES, resolve_style
from contextvault.services.report_types import SectionResult, SectionSourceRef

GOLDEN_DIR = Path(__file__).parent / "goldens" / "report_pptx"
FIXTURE_DIR = Path(__file__).parent / "fixtures"

_RESULT = QueryResult(
    columns=["fiscal_year", "value"],
    rows=[(2024, Decimal("3.6498000E+11")), (2023, Decimal("3.5258300E+11"))],
)
_WIDE = QueryResult(
    columns=["fiscal_year", "value"],
    rows=[(2000 + n, Decimal(n) * Decimal("1000")) for n in range(20)],
)
_EMPTY = QueryResult(columns=["fiscal_year", "value"], rows=[])
_LABELS = {"fiscal_year": "Рік", "value": "Сума"}


def _chart_images(result: QueryResult) -> tuple[bytes, bytes]:
    """The charted section's images, as bytes captured from ``render_chart_images``.

    Read from disk instead of rendered so the deck is byte-identical on every
    platform (see the module docstring). Stubbing costs this battery nothing:
    it exists to prove *deck assembly* is unchanged, and chart rendering has its
    own coverage in ``test_report_chart.py``. The bytes are a real render, so the
    fixture stays a state production can actually produce.
    """
    assert result is _RESULT, (
        "the stub is a capture of _RESULT's chart; a charted case with different "
        "data needs its own capture, or it would silently get the wrong chart"
    )
    return (
        (FIXTURE_DIR / "chart_stub.png").read_bytes(),
        (FIXTURE_DIR / "chart_stub.svg").read_bytes(),
    )


def _data(result: QueryResult, *, chart: bool) -> SectionResult:
    png, svg = _chart_images(result) if chart else (None, None)
    return SectionResult(
        title="Активи Apple",
        source="database",
        sub_question="активи",
        kind="data",
        result=result,
        chart_png=png,
        # Both come from one render, mirroring production (reports.py sets them
        # together from one ChartImages) — a PNG without an SVG is not a state
        # production can produce, and default_blocks keys the chart block's
        # existence on chart_svg, so a PNG-only fixture would silently drop it.
        chart_svg=svg,
        generated_sql="SELECT 1",
        column_labels=_LABELS,
    )


def _text() -> SectionResult:
    return SectionResult(
        title="Політика відпусток",
        source="documents",
        sub_question="відпустки",
        kind="text",
        prose="Співробітники накопичують відпустку щомісяця.",
        # ``source_id`` is required and must be FIXED, never ``uuid4()``: a golden
        # is a byte comparison, and a fresh id per run would make it unassertable.
        # It is not rendered — the deck's sources line prints only number and
        # title — but a stable fixture is cheaper than reasoning about that twice.
        sources=[
            SectionSourceRef(
                number=1,
                title="policy.pdf",
                source_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )
        ],
    )


def _error() -> SectionResult:
    return SectionResult(
        title="Невдала секція",
        source="database",
        sub_question="?",
        kind="error",
        error='relation "missing" does not exist',
    )


SECTION_CASES: dict[str, list[SectionResult]] = {
    "data_chart_and_table": [_data(_RESULT, chart=True)],
    "data_table_only": [_data(_RESULT, chart=False)],
    "data_empty_result": [_data(_EMPTY, chart=False)],
    "data_overflowing_table": [_data(_WIDE, chart=False)],
    "text_with_sources": [_text()],
    "error_section": [_error()],
    "mixed_three_sections": [_data(_RESULT, chart=True), _text(), _error()],
}


def _golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.pptx"


def _assert_matches_golden(blob: bytes, path: Path) -> None:
    """Compare by digest, never by raw bytes.

    ``assert blob == path.read_bytes()`` makes pytest render a full ndiff of two
    ~50 KB binaries when it fails. Seven such failures produced a 6.5 MB CI log
    and stretched a 3.5-minute job to 25 minutes, which read as a hang rather
    than as seven ordinary assertion failures. A digest fails in one line, and
    the offset below says where to start looking.
    """
    golden = path.read_bytes()
    actual, expected = hashlib.sha256(blob).hexdigest(), hashlib.sha256(golden).hexdigest()
    if actual == expected:
        return
    offset = next(
        # strict=False on purpose: a length mismatch is itself a difference, and
        # the fallback below reports it rather than raising here.
        (i for i, (x, y) in enumerate(zip(blob, golden, strict=False)) if x != y),
        min(len(blob), len(golden)),
    )
    pytest.fail(
        f"{path.name} changed\n"
        f"  built:  sha256={actual} ({len(blob)} bytes)\n"
        f"  golden: sha256={expected} ({len(golden)} bytes)\n"
        f"  first difference at byte {offset}\n"
        f"Regenerate DELIBERATELY with UPDATE_PPTX_GOLDENS=1 and read the diff."
    )


def _build(name: str, template: str = "kvarta") -> bytes:
    return build_report_pptx(
        title="Квартальний звіт",
        sections=SECTION_CASES[name],
        language="uk",
        style=resolve_style(template),
    )


@pytest.mark.parametrize("name", sorted(SECTION_CASES))
def test_deck_matches_golden(name: str) -> None:
    blob = _build(name)
    path = _golden_path(name)
    if os.environ.get("UPDATE_PPTX_GOLDENS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        pytest.skip(f"regenerated {path.name}")
    assert path.exists(), f"missing golden {path.name}; regenerate deliberately"
    _assert_matches_golden(blob, path)


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_matches_golden(template: str) -> None:
    blob = _build("data_chart_and_table", template)
    path = _golden_path(f"template_{template}")
    if os.environ.get("UPDATE_PPTX_GOLDENS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        pytest.skip(f"regenerated {path.name}")
    assert path.exists(), f"missing golden {path.name}; regenerate deliberately"
    _assert_matches_golden(blob, path)
